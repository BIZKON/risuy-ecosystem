"""Walking-skeleton потребителя: читает engine:raw и делает ОДИН idempotent-ish insert
в engine.raw_messages под ролью engine_rw. raw_messages — SHARED сырьё (без tenant_id;
тенант появляется в matching, не здесь). РЕАЛЬНАЯ транспортная семантика (consumer-group,
ack/retry, DLQ, backpressure, дедуп) — S2, НЕ здесь."""
from __future__ import annotations

import asyncio
import os

import redis.asyncio as aioredis

from engine.common import db as engine_db
from engine.common import health


async def run() -> None:
    dsn = os.environ["ENGINE_DSN"]
    redis_url = os.environ["REDIS_URL"]
    pool = await engine_db.make_pool(dsn)
    # readiness = db.ping со СВОИМ соединением (health крутит свой event-loop; пул
    # главного loop трогать нельзя — cross-loop RuntimeError → вечный 503).
    health.serve(int(os.environ.get("HEALTH_PORT", "8090")), lambda: engine_db.ping(dsn))
    r = aioredis.from_url(redis_url)
    last_id = "0"
    while True:
        resp = await r.xread({"engine:raw": last_id}, count=10, block=5000)
        for _stream, entries in resp or []:
            for msg_id, fields in entries:
                last_id = msg_id
                f = {k.decode(): v.decode() for k, v in fields.items()}
                async with pool.acquire() as c:
                    # raw_messages — SHARED сырьё (без tenant_id): глобальный дедуп, тенант
                    # появляется в matching, не здесь → set_tenant не нужен.
                    status = await c.execute(
                        "insert into engine.raw_messages (source_kind, external_id, body) "
                        "values ($1,$2,$3) on conflict (source_kind, external_id) do nothing",
                        f["source_kind"], f["external_id"], f["text"],
                    )
                # asyncpg возвращает статус "INSERT 0 1" (вставлено) либо "INSERT 0 0" (дубль-пропуск).
                inserted = status.rsplit(" ", 1)[-1] == "1"
                verb = "строка записана" if inserted else "дубликат пропущен"
                print(f"ingest: {verb} — {msg_id.decode()}")


if __name__ == "__main__":
    asyncio.run(run())
