#!/usr/bin/env python3
"""DB-смоук (контроллер, risuy_dev): create_tenant_admin → create_tenant_brief
цепочкой создают active-тенанта + pending-бриф под его id.
  CHAIN_SMOKE_DSN="...risuy_dev..." PYTHONPATH=admin-panel:. \
    ./.venv-smoke/bin/python scripts/create_tenant_brief_chain_smoke.py
"""
import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "admin-panel"))
os.environ.setdefault("DATABASE_URL", "postgresql://x/y")
os.environ.setdefault("SESSION_SECRET", "smoke-secret-padding-0123456789abcdef")
os.environ.setdefault("ADMIN_USERNAME", "smoke")
os.environ.setdefault("ADMIN_PASSWORD_HASH", "$argon2id$v=19$m=65536,t=3,p=4$c21va2U$c21va2U")

import asyncpg  # noqa: E402
import db  # noqa: E402

DSN = os.environ.get("CHAIN_SMOKE_DSN")
if not DSN or "/risuy_dev" not in DSN.split("?")[0]:
    raise SystemExit("Задайте CHAIN_SMOKE_DSN на risuy_dev")

FAILS: list[str] = []


def check(name, cond, detail=""):
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


async def _cleanup(c):
    await c.execute("delete from tenant_brief where tenant_id in "
                    "(select id from tenants where name like 'СМОУК Цепочка%')")
    await c.execute("delete from tenants where name like 'СМОУК Цепочка%'")


async def main():
    db.pool = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    try:
        db.set_active_tenant(None)
        async with db.pool.acquire() as c:
            await _cleanup(c)
        print("1. create_tenant_admin + create_tenant_brief цепочкой:")
        slug, tid = await db.create_tenant_admin("СМОУК Цепочка ООО", actor="smoke", ip=None, user_agent=None)
        check("тенант создан (slug, id)", bool(slug) and bool(tid))
        brief_id, token = await db.create_tenant_brief(tid, actor="smoke", ip=None, user_agent=None)
        check("бриф создан (id, token)", bool(brief_id) and len(token) >= 16)
        async with db.pool.acquire() as c:
            st = await c.fetchval("select status from tenants where id=$1", tid)
            bst = await c.fetchval("select status from tenant_brief where id=$1", brief_id)
            bt = await c.fetchval("select tenant_id from tenant_brief where id=$1", brief_id)
        check("тенант active", st == "active", f"st={st}")
        check("бриф pending", bst == "pending", f"bst={bst}")
        check("бриф привязан к тенанту", str(bt) == str(tid))
    finally:
        async with db.pool.acquire() as c:
            await _cleanup(c)
        await db.pool.close()
    print()
    if FAILS:
        print("❌ ПРОВАЛЫ: " + ", ".join(FAILS)); sys.exit(1)
    print("✅ create_tenant_brief_chain smoke — OK")


if __name__ == "__main__":
    asyncio.run(main())
