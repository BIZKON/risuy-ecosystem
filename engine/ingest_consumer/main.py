"""Walking-skeleton потребителя: читает engine:raw и делает ОДИН idempotent-ish insert
в engine.raw_messages под ролью engine_rw с явным app.tenant_id. РЕАЛЬНАЯ транспортная
семантика (consumer-group, ack/retry, DLQ, backpressure, дедуп) — S2, НЕ здесь."""
from __future__ import annotations

import asyncio
import os

import redis.asyncio as aioredis

from engine.common import db as engine_db
from engine.common import health


async def _readiness(pool) -> bool:
    try:
        async with pool.acquire() as c:
            await c.execute("select 1")
        return True
    except Exception:
        return False


async def run() -> None:
    dsn = os.environ["ENGINE_DSN"]
    redis_url = os.environ["REDIS_URL"]
    pool = await engine_db.make_pool(dsn)
    health.serve(int(os.environ.get("HEALTH_PORT", "8090")), lambda: _readiness(pool))
    r = aioredis.from_url(redis_url)
    last_id = "0"
    while True:
        resp = await r.xread({"engine:raw": last_id}, count=10, block=5000)
        for _stream, entries in resp or []:
            for msg_id, fields in entries:
                last_id = msg_id
                f = {k.decode(): v.decode() for k, v in fields.items()}
                async with pool.acquire() as c:
                    await engine_db.set_tenant(c, f["tenant_id"])
                    await c.execute(
                        "insert into engine.raw_messages (tenant_id, source_kind, external_id, text) "
                        "values ($1,$2,$3,$4) on conflict (source_kind, external_id) do nothing",
                        f["tenant_id"], f["source_kind"], f["external_id"], f["text"],
                    )
                print(f"ingest: строка записана из {msg_id.decode()}")


if __name__ == "__main__":
    asyncio.run(run())
