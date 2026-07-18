"""Смоук S2 (DoD 1-4, 8): транспорт против РЕАЛЬНЫХ ingest.handle_event/ingest.tick.

Сценарии: E3-идемпотентность (дубль → 1 строка), E3-рестарт (at-least-once,
reclaim через XPENDING/XCLAIM — здесь же сверяются живые сигнатуры redis-py 5.2.1,
риск §11 спеки), ядовитое → DLQ без блокировки потока, потолок доставок → DLQ,
backpressure MAXLEN~. Гард DSN: только эфемерный risuy_dev. Redis — БД №1
(SMOKE_REDIS_URL), живому консьюмеру на БД №0 не мешаем.
Env-фикстура — строго ДО импорта engine-модулей (константы читаются на импорте).
Самоочистка: DEL smoke:raw smoke:raw:dlq + delete тест-строк (префикс 9995…).
"""
import asyncio
import logging
import os

# Фикстура транспорта — строго ДО импорта engine.common/engine.ingest_consumer.
os.environ["INGEST_STREAM"] = "smoke:raw"
os.environ["INGEST_DLQ_STREAM"] = "smoke:raw:dlq"
os.environ["INGEST_GROUP"] = "smoke-ingest"
os.environ["INGEST_MAX_DELIVERIES"] = "3"
os.environ["INGEST_RECLAIM_IDLE_MS"] = "100"
os.environ["INGEST_BLOCK_MS"] = "100"
os.environ["STREAM_MAXLEN"] = "50"

import asyncpg  # noqa: E402 — импорт после env-фикстуры (осознанно)
import redis.asyncio as redis  # noqa: E402 — то же

from engine.common import config, envelope, streams  # noqa: E402 — то же
from engine.ingest_consumer import main as ingest  # noqa: E402 — то же

DSN = os.environ.get("ENGINE_TRANSPORT_SMOKE_DSN")
if not DSN or "/risuy_dev" not in DSN.split("?")[0]:
    raise SystemExit("Задайте ENGINE_TRANSPORT_SMOKE_DSN на эфемерном risuy_dev (роль engine_rw).")
REDIS_URL = os.environ.get("SMOKE_REDIS_URL")
if not REDIS_URL:
    raise SystemExit("Задайте SMOKE_REDIS_URL (redis compose-стека, БД №1).")


class BrokenPool:
    """Инъекция транзиента: PG «недоступен» — acquire() кидает asyncpg.InterfaceError.

    Исключение классифицируется handle_event как transient (_TRANSIENT) и летит
    наружу БЕЗ ack — событие остаётся в PEL, delivery-счётчик растёт на каждом
    XCLAIM-реклейме: так набирается потолок доставок (сценарий 4).
    """

    def acquire(self):
        raise asyncpg.InterfaceError("смоук: инъекция недоступности PG")


async def pel_size(r: redis.Redis) -> int:
    """Размер PEL группы (XPENDING summary)."""
    summary = await r.xpending(config.INGEST_STREAM, config.INGEST_GROUP)
    return summary.get("pending", 0)


async def row_count(pool: asyncpg.Pool, external_id: str) -> int:
    async with pool.acquire() as c:
        return await c.fetchval(
            "select count(*) from engine.raw_messages "
            "where source_kind = 'telegram' and external_id = $1",
            external_id,
        )


async def dlq_by_external_id(r: redis.Redis, external_id: str) -> dict:
    """Запись DLQ с данным external_id (поля — bytes: клиент без decode_responses)."""
    for _mid, fields in await r.xrange(config.INGEST_DLQ_STREAM):
        if fields.get(b"external_id") == external_id.encode():
            return fields
    raise AssertionError(f"в DLQ нет записи с external_id={external_id}")


async def cleanup(pool: asyncpg.Pool, r: redis.Redis) -> None:
    """Самоочистка стримов и тест-строк (все тест-события — префикс 9995… в chat_id)."""
    await r.delete(config.INGEST_STREAM, config.INGEST_DLQ_STREAM)
    async with pool.acquire() as c:
        await c.execute(
            "delete from engine.raw_messages "
            "where source_kind = 'telegram' and external_id like '9995%'"
        )


async def scenario_idempotency(pool: asyncpg.Pool, r: redis.Redis) -> None:
    """DoD 1: одно событие дважды в стриме → ровно 1 строка, PEL пуст."""
    ext = envelope.make_external_id("telegram", 999510001, 1)
    event = envelope.build("telegram", ext, "идемпотентность", lang="ru")
    await streams.emit(r, event)
    await streams.emit(r, event)
    await ingest.tick(pool, r)
    await ingest.tick(pool, r)  # добор: пустой тик подтверждает отсутствие хвоста
    assert await row_count(pool, ext) == 1, "дубль породил вторую строку (E3 сломан)"
    assert await pel_size(r) == 0, "PEL не пуст после ack обоих событий"
    print("  сценарий 1 (идемпотентность): OK")


async def scenario_restart(pool: asyncpg.Pool, r: redis.Redis) -> None:
    """DoD 2: чтение без ack («крах до ack») → повторная доставка из PEL → строка есть."""
    ext = envelope.make_external_id("telegram", 999520001, 1)
    await streams.emit(r, envelope.build("telegram", ext, "рестарт до ack"))
    read = await streams.read_batch(r)  # доставка №1 БЕЗ ack — симуляция краха
    assert len(read) == 1, f"read_batch вернул {len(read)} событий, ожидалось 1"
    await asyncio.sleep(0.2)  # idle > INGEST_RECLAIM_IDLE_MS=100
    # Живые сигнатуры redis-py 5.2.1 (риск §11 спеки): ключи ответа xpending_range.
    pending = await streams.pending_over_idle(r)
    assert len(pending) == 1, f"в PEL {len(pending)} записей, ожидалась 1"
    assert "message_id" in pending[0] and "times_delivered" in pending[0], (
        f"сигнатура xpending_range изменилась: {sorted(pending[0])}"
    )
    assert pending[0]["times_delivered"] == 1, "после первого чтения ожидалась 1 доставка"
    await ingest.tick(pool, r)  # reclaim (XCLAIM) → upsert → ack
    assert await row_count(pool, ext) == 1, "повторная доставка из PEL не записана"
    assert await pel_size(r) == 0, "PEL не пуст после reclaim+ack"
    print("  сценарий 2 (рестарт/at-least-once): OK")


async def scenario_poison(pool: asyncpg.Pool, r: redis.Redis) -> None:
    """DoD 3: ядовитое (без external_id) → DLQ+ack, валидное следом записано, цикл жив."""
    ext = envelope.make_external_id("telegram", 999530001, 1)
    # Ядовитое — сознательно сырым XADD мимо envelope (симуляция сломанного продьюсера).
    await r.xadd(config.INGEST_STREAM, {"v": "1", "source_kind": "telegram", "body": "яд"})
    await streams.emit(r, envelope.build("telegram", ext, "валидное после ядовитого"))
    await ingest.tick(pool, r)
    entries = await r.xrange(config.INGEST_DLQ_STREAM)
    assert len(entries) == 1, f"в DLQ {len(entries)} записей, ожидалась 1"
    _mid, fields = entries[0]
    assert fields.get(b"dlq_reason") == b"invalid_envelope", (
        f"dlq_reason={fields.get(b'dlq_reason')!r}, ожидался invalid_envelope"
    )
    assert b"dlq_source_id" in fields and b"dlq_at" in fields, "нет dlq_source_id/dlq_at"
    assert await row_count(pool, ext) == 1, "валидное после ядовитого не записано (блокировка!)"
    assert await pel_size(r) == 0, "PEL не пуст (ядовитое не ack'нуто после DLQ?)"
    print("  сценарий 3 (ядовитое не блокирует): OK")


async def scenario_max_deliveries(pool: asyncpg.Pool, r: redis.Redis) -> None:
    """DoD 4: транзиент INGEST_MAX_DELIVERIES раз подряд → DLQ max_deliveries, строки нет."""
    ext = envelope.make_external_id("telegram", 999540001, 1)
    await streams.emit(r, envelope.build("telegram", ext, "потолок доставок"))
    broken = BrokenPool()
    # Доставки 1..MAX: каждый тик падает транзиентом (событие в PEL, счётчик растёт).
    for попытка in range(config.INGEST_MAX_DELIVERIES):
        try:
            await ingest.tick(broken, r)
            raise AssertionError(f"tick №{попытка + 1} с BrokenPool обязан кинуть транзиент")
        except asyncpg.InterfaceError:
            pass
        await asyncio.sleep(0.15)  # idle > порога реклейма перед следующим тиком
    # Счётчик исчерпан → тик с живым пулом отправляет в DLQ, НЕ вставляет.
    await ingest.tick(pool, r)
    fields = await dlq_by_external_id(r, ext)
    assert fields.get(b"dlq_reason") == b"max_deliveries", (
        f"dlq_reason={fields.get(b'dlq_reason')!r}, ожидался max_deliveries"
    )
    assert await row_count(pool, ext) == 0, "событие записано вопреки потолку доставок"
    assert await pel_size(r) == 0, "PEL не пуст после DLQ+ack"
    print("  сценарий 4 (потолок доставок): OK")


async def scenario_maxlen(r: redis.Redis) -> None:
    """DoD 8: 500 emit при STREAM_MAXLEN=50 → approximate-трим удержал длину стрима."""
    for i in range(500):
        await streams.emit(
            r,
            envelope.build(
                "telegram", envelope.make_external_id("telegram", 999550000 + i, 1), "maxlen"
            ),
        )
    xlen = await r.xlen(config.INGEST_STREAM)
    assert 0 < xlen < 500, f"MAXLEN~ не сработал: XLEN={xlen} (ожидалось < 500)"
    print(f"  сценарий 5 (MAXLEN~): OK, XLEN={xlen}")


async def main() -> None:
    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
    r = redis.from_url(REDIS_URL)
    try:
        await cleanup(pool, r)  # чистый старт: робастность к оборванному прошлому прогону
        await streams.ensure_group(r)
        await scenario_idempotency(pool, r)
        await scenario_restart(pool, r)
        await scenario_poison(pool, r)
        await scenario_max_deliveries(pool, r)
        await scenario_maxlen(r)
    finally:
        await cleanup(pool, r)
        await r.aclose()
        await pool.close()
    print("engine_transport_smoke: OK (E3+DLQ+потолок+MAXLEN)")


logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
asyncio.run(main())
