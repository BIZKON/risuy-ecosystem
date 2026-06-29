#!/usr/bin/env python3
"""DB-смоук СП-2a: kb_search видит ТОЛЬКО чанки своего тенанта (изоляция A≠B) на risuy_dev.
Тестирует ЯВНЫЙ tenant-фильтр бота (owner-роль вставляет чанки обоих тенантов, kb_search
обязан изолировать по параметру tenant_id). Запуск:
  TEAM_DSN="postgresql://gen_user:<pw>@<host>:5432/risuy_dev?sslmode=require" \
  PYTHONPATH=bot-telegram:. ./.venv-smoke/bin/python scripts/kb_tenant_isolation_smoke.py
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
import db as bdb  # noqa: E402  (bot-telegram/db.py)

DSN = os.environ["DATABASE_URL"]
assert "/risuy_dev" in DSN.split("?")[0], "только risuy_dev"
FAILS: list[str] = []
VEC = [0.01] * 768
VECLIT = "[" + ",".join("0.01" for _ in range(768)) + "]"


def check(name, cond):
    print(f"  {'OK ' if cond else 'FAIL'} {name}")
    if not cond:
        FAILS.append(name)


async def main():
    bdb.pool = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    async with bdb.pool.acquire() as c:
        ta = await c.fetchval("insert into tenants(slug,name,status) values('kb-smoke-a','A','active') returning id")
        tb = await c.fetchval("insert into tenants(slug,name,status) values('kb-smoke-b','B','active') returning id")
        da = await c.fetchval("insert into kb_documents(tenant_id,title,content) values($1,'A','a') returning id", ta)
        db2 = await c.fetchval("insert into kb_documents(tenant_id,title,content) values($1,'B','b') returning id", tb)
        await c.execute("insert into kb_chunks(tenant_id,document_id,chunk_index,content,embedding) "
                        "values($1,$2,0,'ФАКТ-A',$3::vector)", ta, da, VECLIT)
        await c.execute("insert into kb_chunks(tenant_id,document_id,chunk_index,content,embedding) "
                        "values($1,$2,0,'ФАКТ-B',$3::vector)", tb, db2, VECLIT)
    try:
        a = await bdb.kb_search(VEC, ta, top_k=10, max_distance=2.0)
        b = await bdb.kb_search(VEC, tb, top_k=10, max_distance=2.0)
        plat = await bdb.kb_search(VEC, None, top_k=10, max_distance=2.0)
        check("A видит ФАКТ-A", "ФАКТ-A" in a)
        check("A НЕ видит ФАКТ-B (изоляция)", "ФАКТ-B" not in a)
        check("B видит ФАКТ-B, не A", ("ФАКТ-B" in b) and ("ФАКТ-A" not in b))
        check("scope None не видит чанки тенантов (NULL-строк нет)", ("ФАКТ-A" not in plat) and ("ФАКТ-B" not in plat))
    finally:
        async with bdb.pool.acquire() as c:
            # Явная очистка в порядке зависимостей (не полагаемся на FK-cascade — на Wave-0 БД он мог быть no-action).
            sub = "select id from tenants where slug in ('kb-smoke-a','kb-smoke-b')"
            await c.execute(f"delete from kb_chunks    where tenant_id in ({sub})")
            await c.execute(f"delete from kb_documents where tenant_id in ({sub})")
            await c.execute("delete from tenants where slug in ('kb-smoke-a','kb-smoke-b')")
    print("\nВСЕ ОК" if not FAILS else "\nПРОВАЛЫ: " + ", ".join(FAILS))
    sys.exit(1 if FAILS else 0)


asyncio.run(main())
