"""Обвязка Redis Streams транспорта (спека S2 §4).

Единственный модуль, который говорит с Redis командами стримов. XADD событий —
только через emit() (валидацию делает вызывающий: base_worker/консьюмер);
DLQ-записи — единственный легальный XADD мимо envelope-контракта.
"""
from __future__ import annotations

import datetime as dt

import redis.asyncio as redis

from . import config


async def ensure_group(r: redis.Redis) -> None:
    """Создаёт consumer-group идемпотентно; id='0' — дочитать уже лежащее в стриме."""
    try:
        await r.xgroup_create(config.INGEST_STREAM, config.INGEST_GROUP, id="0", mkstream=True)
    except redis.ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise


async def read_batch(r: redis.Redis) -> list[tuple[bytes, dict]]:
    """Новые события группы: [(id, fields)], пусто по BLOCK-таймауту."""
    resp = await r.xreadgroup(
        config.INGEST_GROUP,
        config.INGEST_CONSUMER,
        {config.INGEST_STREAM: ">"},
        count=config.INGEST_BATCH,
        block=config.INGEST_BLOCK_MS,
    )
    if not resp:
        return []
    return resp[0][1]


async def pending_over_idle(r: redis.Redis) -> list[dict]:
    """Записи PEL с idle >= порога реклейма: [{message_id, times_delivered, ...}]."""
    return await r.xpending_range(
        config.INGEST_STREAM,
        config.INGEST_GROUP,
        min="-",
        max="+",
        count=config.INGEST_BATCH,
        idle=config.INGEST_RECLAIM_IDLE_MS,
    )


async def claim(r: redis.Redis, message_id) -> list[tuple[bytes, dict]]:
    """XCLAIM записи на себя (инкрементит delivery count — аналог attempts на claim).

    Пусто в двух случаях: (а) другой консьюмер успел забрать/ack'нуть первым;
    (б) сообщение вытримлено MAXLEN~ — Redis 7 при триме сам удаляет запись из
    PEL, XCLAIM возвращает пусто (проверено на живом Redis 7.4).
    """
    return await r.xclaim(
        config.INGEST_STREAM,
        config.INGEST_GROUP,
        config.INGEST_CONSUMER,
        min_idle_time=config.INGEST_RECLAIM_IDLE_MS,
        message_ids=[message_id],
    )


async def ack(r: redis.Redis, message_id) -> None:
    await r.xack(config.INGEST_STREAM, config.INGEST_GROUP, message_id)


async def to_dlq(r: redis.Redis, fields: dict, reason: str, source_id) -> None:
    """Ядовитое/исчерпавшее ретраи — в DLQ-стрим (тоже с потолком MAXLEN~)."""
    entry = dict(fields)
    entry["dlq_reason"] = reason
    entry["dlq_source_id"] = source_id if isinstance(source_id, (str, bytes)) else str(source_id)
    entry["dlq_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    await r.xadd(
        config.INGEST_DLQ_STREAM, entry, maxlen=config.STREAM_MAXLEN, approximate=True
    )


async def emit(r: redis.Redis, event: dict) -> None:
    """XADD события в основной стрим с backpressure-потолком MAXLEN~ (спека §4)."""
    await r.xadd(config.INGEST_STREAM, event, maxlen=config.STREAM_MAXLEN, approximate=True)


async def lag_snapshot(r: redis.Redis) -> tuple[int, int]:
    """(XLEN, размер PEL) — минимальная наблюдаемость до S16e."""
    length = await r.xlen(config.INGEST_STREAM)
    summary = await r.xpending(config.INGEST_STREAM, config.INGEST_GROUP)
    return length, summary.get("pending", 0)
