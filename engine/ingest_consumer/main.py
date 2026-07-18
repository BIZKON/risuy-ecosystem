"""Ingest-consumer: engine:raw (consumer-group) → идемпотентный upsert в engine.raw_messages.

Семантика — спека S2 §4:
- at-least-once: XACK ТОЛЬКО после успешного INSERT; рестарт до ack → повторная
  доставка из PEL → дедуп упирается в on conflict do nothing;
- permanent (ядовитый envelope / не-транзиентная ошибка вставки) → DLQ + XACK,
  поток не блокируется; transient (PG/Redis недоступны) → БЕЗ ack, экспоненциальный
  backoff, возврат через XPENDING/XCLAIM-reclaim; потолок доставок → DLQ;
- raw_messages — SHARED (без tenant_id): set_tenant НЕ вызывается (S1-RAW).
"""
import asyncio
import json
import logging
import os
import random
import signal
import time

import asyncpg
import redis.asyncio as redis

from engine.common import config, db, envelope, health, streams

logger = logging.getLogger("engine.ingest")

UPSERT_SQL = """
    insert into engine.raw_messages
        (source_kind, external_id, chat_ref, author_ref, posted_at, body, lang, metadata)
    values ($1, $2, $3, $4, $5, $6, $7, $8::jsonb)
    on conflict (source_kind, external_id) do nothing
"""

# Классификация ошибок PG (спека §4, per-task ревью Task 3): permanent = ТОЛЬКО явные
# не-ретраябельные классы вставки — данные / целостность / схема-права. Все
# остальные PostgresError (классы 08/40/53/57: обрыв соединения, deadlock и
# serialization, исчерпание ресурсов, рестарт/recovery сервера — проверено на
# живом asyncpg 0.30.0: CannotConnectNowError и пр. НЕ PostgresConnectionError)
# — транзиент: ретрай без ack; упорная неизвестная ошибка упрётся в потолок
# доставок → DLQ max_deliveries, а НЕ дренируется в DLQ как insert_permanent
# в окно рутинного рестарта PG.
_PG_PERMANENT = (
    asyncpg.DataError,
    asyncpg.IntegrityConstraintViolationError,
    asyncpg.SyntaxOrAccessError,
)

# Транзиенты: недоступность/обрыв PG или Redis → ретрай без ack (спека §4).
# asyncpg.PostgresError здесь = «всё серверное, кроме _PG_PERMANENT» (порядок
# except в handle_event: permanent проверяется ПЕРВЫМ).
# redis.ConnectionError/TimeoutError — re-export redis.asyncio (НЕ импортировать
# redis.exceptions отдельно: `import redis.exceptions` перепривязал бы имя `redis`
# к синхронному top-level пакету).
_TRANSIENT = (
    asyncpg.PostgresError,
    asyncpg.InterfaceError,
    redis.ConnectionError,
    redis.TimeoutError,
    ConnectionError,
    TimeoutError,
    OSError,
)


async def handle_event(pool, r, message_id, fields) -> None:
    """Одно событие: parse → upsert → ack. Permanent → DLQ+ack. Транзиент — наружу."""
    try:
        row = envelope.parse(fields)
    except envelope.EnvelopeError as exc:
        logger.error("Ядовитое событие %s: %s → DLQ(%s)", message_id, exc, exc.reason)
        await streams.to_dlq(r, fields, exc.reason, message_id)
        await streams.ack(r, message_id)
        return
    try:
        async with pool.acquire() as conn:
            tag = await conn.execute(
                UPSERT_SQL,
                row["source_kind"],
                row["external_id"],
                row["chat_ref"],
                row["author_ref"],
                row["posted_at"],
                row["body"],
                row["lang"],
                json.dumps(row["metadata"], ensure_ascii=False),
            )
    except _PG_PERMANENT as exc:
        # Явно не-ретраябельная ошибка вставки (данные/целостность/схема) — permanent.
        logger.error("Событие %s (%s): ошибка вставки %s → DLQ", message_id, row["external_id"], exc)
        await streams.to_dlq(r, fields, "insert_permanent", message_id)
        await streams.ack(r, message_id)
        return
    except _TRANSIENT:
        raise  # общий backoff в run(); событие остаётся в PEL
    if tag == "INSERT 0 0":
        logger.info("Дубль %s:%s пропущен", row["source_kind"], row["external_id"])
    else:
        logger.info("Записано %s:%s", row["source_kind"], row["external_id"])
    await streams.ack(r, message_id)


async def tick(pool, r) -> int:
    """Один тик: reclaim зависших (с потолком доставок) → чтение новых. Возвращает число событий."""
    n = 0
    for entry in await streams.pending_over_idle(r):
        message_id = entry["message_id"]
        deliveries = entry["times_delivered"]
        claimed = await streams.claim(r, message_id)
        if not claimed:
            continue  # другой консьюмер успел
        _, fields = claimed[0]
        if deliveries >= config.INGEST_MAX_DELIVERIES:
            logger.error(
                "Событие %s: потолок доставок (%d) исчерпан → DLQ", message_id, deliveries
            )
            await streams.to_dlq(r, fields, "max_deliveries", message_id)
            await streams.ack(r, message_id)
        else:
            await handle_event(pool, r, message_id, fields)
        n += 1
    for message_id, fields in await streams.read_batch(r):
        await handle_event(pool, r, message_id, fields)
        n += 1
    return n


async def _probe_ready(dsn: str, redis_url: str) -> bool:
    """Собственно проба PG И Redis, каждый — своим короткоживущим подключением
    (cross-loop-грабля). Потолок времени навешивает _check_ready."""
    if not await db.ping(dsn):
        return False
    # Свои сокет-таймауты: без них зависшее соединение (дроп без RST) держит
    # пробу дефолтные десятки секунд — дольше потолка health-треда.
    probe = redis.from_url(redis_url, socket_connect_timeout=2, socket_timeout=2)
    try:
        await probe.ping()
        return True
    except Exception:  # noqa: BLE001 — readiness не роняет health-тред
        return False
    finally:
        await probe.aclose()


async def _check_ready(dsn: str, redis_url: str) -> bool:
    """Readiness: PG И Redis под общим потолком 4 с.

    Контракт health.serve — Awaitable[bool] (200/503 по значению): любая
    недоступность → False, исключения НЕ выпускаем в health-тред.
    Потолок 4 с < .result(timeout=5) health-треда: иначе рваное соединение
    (db.ping ждёт дефолтный timeout asyncpg 60 с) оставляет /readyz без
    HTTP-ответа вовсе — хуже честного 503.
    """
    try:
        return await asyncio.wait_for(_probe_ready(dsn, redis_url), timeout=4.0)
    except TimeoutError:
        return False


async def _sleep_or_stop(stop: asyncio.Event, delay: float) -> None:
    """Прерываемая пауза (паттерн base_worker): stop будит досрочно.

    Голый asyncio.sleep не будится SIGTERM'ом: длинный backoff при аутедже
    переживал бы stop_grace_period 15 с compose → SIGKILL вместо graceful.
    """
    try:
        await asyncio.wait_for(stop.wait(), timeout=delay)
    except TimeoutError:
        pass


async def run() -> None:
    dsn = config.req("ENGINE_DSN")
    redis_url = config.req("REDIS_URL")
    pool = await db.make_pool(dsn)
    r = redis.from_url(redis_url)
    await streams.ensure_group(r)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    backoff = 1.0
    last_lag_log = 0.0
    logger.info(
        "Ingest-consumer запущен: стрим=%s группа=%s консьюмер=%s",
        config.INGEST_STREAM, config.INGEST_GROUP, config.INGEST_CONSUMER,
    )
    while not stop.is_set():
        try:
            await tick(pool, r)
            backoff = 1.0
        except redis.ResponseError as exc:
            # Самовосстановление NOGROUP (спека §4, fail-mode «не тихий fail-open»):
            # стрим/группа исчезли (DEL/FLUSHDB/потеря AOF) → XPENDING/XREADGROUP
            # кидают NOGROUP. Без этой ветки он падал бы в generic-except —
            # вечный лог+sleep без единого чтения, т.е. тихий отказ под видом
            # живого процесса. ensure_group идемпотентен (BUSYGROUP глотается),
            # поэтому пересоздание безопасно и возвращает консьюмера в строй.
            if "NOGROUP" not in str(exc):
                logger.exception("Сбой тика ingest (ResponseError)")
                await _sleep_or_stop(stop, 1.0)
                continue
            logger.warning("NOGROUP (%s) — пересоздаю группу и продолжаю", exc)
            try:
                await streams.ensure_group(r)
            except Exception:  # noqa: BLE001 — Redis мог упасть между NOGROUP и пересозданием
                logger.exception("Пересоздание группы не удалось — повтор на следующем витке")
                await _sleep_or_stop(stop, 1.0)
            continue
        except _TRANSIENT as exc:
            pause = backoff + random.uniform(0, backoff / 2)
            logger.warning("Транзиент (%s: %s) — пауза %.1f с", type(exc).__name__, exc, pause)
            await _sleep_or_stop(stop, pause)
            backoff = min(backoff * 2, config.INGEST_BACKOFF_MAX_S)
        except Exception:  # noqa: BLE001 — цикл не должен падать (эталон worker.py)
            logger.exception("Сбой тика ingest")
            await _sleep_or_stop(stop, 1.0)
        now = time.monotonic()
        if now - last_lag_log >= config.INGEST_LAG_LOG_EVERY_S:
            try:
                length, pel = await streams.lag_snapshot(r)
                logger.info("Лаг: XLEN=%d PEL=%d", length, pel)
            except Exception:  # noqa: BLE001 — наблюдаемость best-effort
                logger.warning("Лаг-снапшот недоступен")
            last_lag_log = now
    await pool.close()
    await r.aclose()
    logger.info("Ingest-consumer остановлен (graceful)")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    port = int(os.environ.get("HEALTH_PORT", "8090"))
    health.serve(port, lambda: _check_ready(config.req("ENGINE_DSN"), config.req("REDIS_URL")))
    asyncio.run(run())


if __name__ == "__main__":
    main()
