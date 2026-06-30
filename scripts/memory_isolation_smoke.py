#!/usr/bin/env python3
"""DB-смоук СП-2-память: memory_search видит ТОЛЬКО память своего тенанта/агента/лида (A≠B,
per-lead) на risuy_dev. Dummy-вектор (TEI не нужен). Throwaway-тенанты, чистка каскадом.
  TEAM_DSN="postgresql://gen_user:<pw>@<host>:5432/risuy_dev?sslmode=require" \
  PYTHONPATH=bot-telegram:. ./.venv-smoke/bin/python scripts/memory_isolation_smoke.py
"""
import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bot-telegram"))
os.environ.setdefault("DATABASE_URL", os.environ.get("TEAM_DSN", "postgresql://x/y"))
# stub-env: import db → import config; config._req падает без обязательных переменных.
os.environ.setdefault("BOT_TOKEN", "smoke")
os.environ.setdefault("CHANNEL_ID", "-100123")
os.environ.setdefault("CHANNEL_URL", "https://t.me/smoke")
os.environ.setdefault("GUIDE_URL", "https://example.com/guide")

import asyncpg  # noqa: E402
import db as bdb  # noqa: E402 (bot-telegram/db.py)

DSN = os.environ["DATABASE_URL"]
assert "/risuy_dev" in DSN.split("?")[0], "только risuy_dev"

FAILS = []


def check(n, c):
    print(f"  {'OK ' if c else 'FAIL'} {n}")
    if not c:
        FAILS.append(n)


VEC = [0.01] * 768


async def main():
    bdb.pool = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    async with bdb.pool.acquire() as c:
        ta = await c.fetchval("insert into tenants(slug,name,status) values('mem-a','A','active') returning id")
        tb = await c.fetchval("insert into tenants(slug,name,status) values('mem-b','B','active') returning id")
        aa = await c.fetchval("insert into team_agents(tenant_id,slug,name) values($1,'sales','S') returning id", ta)
        ab = await c.fetchval("insert into team_agents(tenant_id,slug,name) values($1,'sales','S') returning id", tb)
    try:
        await bdb.memory_insert(ta, aa, "СВОДКА-A лид1", VEC, metadata={"lead": "L1"})
        await bdb.memory_insert(tb, ab, "СВОДКА-B лид2", VEC, metadata={"lead": "L2"})
        a_hits = await bdb.memory_search(VEC, ta, aa, "L1", top_k=10, max_distance=2.0)
        b_hits = await bdb.memory_search(VEC, tb, ab, "L2", top_k=10, max_distance=2.0)
        a_other_lead = await bdb.memory_search(VEC, ta, aa, "L2", top_k=10, max_distance=2.0)
        check("A/лид1 видит свою сводку", "СВОДКА-A лид1" in a_hits)
        check("A НЕ видит сводку B (изоляция тенанта)", "СВОДКА-B лид2" not in a_hits)
        check("B видит свою, не A", ("СВОДКА-B лид2" in b_hits) and ("СВОДКА-A лид1" not in b_hits))
        check("per-lead: лид2 НЕ видит память лида1 (нет кросс-клиент утечки)", "СВОДКА-A лид1" not in a_other_lead)
    finally:
        async with bdb.pool.acquire() as c:
            await c.execute("delete from tenants where slug in ('mem-a','mem-b')")  # cascade чистит team_agents+agent_memory
    print("\n" + ("ВСЕ ОК" if not FAILS else "ПРОВАЛЫ: " + ", ".join(FAILS)))
    sys.exit(1 if FAILS else 0)


asyncio.run(main())
