#!/usr/bin/env python3
"""DB-смоук partners — ПАНЕЛЬ-сторона (контроллер, risuy_dev):
  PARTNERS_SMOKE_DSN="...risuy_dev..." PYTHONPATH=admin-panel:. \
    ./.venv-smoke/bin/python scripts/partners_smoke.py
"""
import asyncio, os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, "admin-panel"))
os.environ.setdefault("DATABASE_URL", "postgresql://x/y")
os.environ.setdefault("SESSION_SECRET", "smoke-secret-padding-0123456789abcdef")
os.environ.setdefault("ADMIN_USERNAME", "smoke")
os.environ.setdefault("ADMIN_PASSWORD_HASH", "$argon2id$v=19$m=65536,t=3,p=4$c21va2U$c21va2U")
import asyncpg  # noqa: E402
import db  # noqa: E402
DSN = os.environ.get("PARTNERS_SMOKE_DSN")
if not DSN or "/risuy_dev" not in DSN.split("?")[0]:
    raise SystemExit("Задайте PARTNERS_SMOKE_DSN на risuy_dev")
FAILS = []
PNAME = "СМОУК Партнёр"
TNAME = "СМОУК РефТенант ООО"


def check(name, cond, detail=""):
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


async def _cleanup(c):
    await c.execute("delete from tenant_brief where tenant_id in (select id from tenants where name=$1)", TNAME)
    await c.execute("delete from tenants where name=$1", TNAME)
    await c.execute("delete from partners where name=$1", PNAME)


async def main():
    db.pool = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    try:
        async with db.pool.acquire() as c:
            await _cleanup(c)
        print("1. create_partner:")
        pid, ref = await db.create_partner(PNAME, "555111", actor="smoke", ip=None, user_agent=None)
        check("вернул (id, ref_code)", bool(pid) and len(ref) >= 8, f"ref={ref}")
        print("2. list_partners видит партнёра со счётчиками:")
        rows = await db.list_partners()
        mine = [r for r in rows if str(r["id"]) == pid]
        check("партнёр в списке", len(mine) == 1)
        check("tenant_count=0 пока нет тенантов", mine and mine[0]["tenant_count"] == 0)
        print("3. атрибуция: тенант с partner_id учитывается:")
        async with db.pool.acquire() as c:
            tid = await c.fetchval("insert into tenants(slug,name,status,partner_id) "
                                   "values($1,$2,'active',$3) returning id",
                                   "smoke-reft", TNAME, pid)
            await c.execute("insert into tenant_brief(tenant_id,token,status) values($1,'smoke-reft-tok','submitted')", tid)
        pt = await db.list_partner_tenants(pid)
        check("list_partner_tenants вернул тенанта", any(str(r["id"]) == str(tid) for r in pt))
        check("brief_status виден", any(r["brief_status"] == "submitted" for r in pt))
        rows2 = await db.list_partners()
        m2 = [r for r in rows2 if str(r["id"]) == pid][0]
        check("tenant_count=1", m2["tenant_count"] == 1, f"c={m2['tenant_count']}")
        check("brief_done=1", m2["brief_done"] == 1, f"d={m2['brief_done']}")
        print("3b. дедуп: тенант с 2 брифами → одна строка (последний бриф):")
        async with db.pool.acquire() as c:
            await c.execute("insert into tenant_brief(tenant_id,token,status,created_at) "
                            "values($1,'smoke-reft-tok2','proposed', now() + interval '1 second')", tid)
        pt2 = await db.list_partner_tenants(pid)
        mine_rows = [r for r in pt2 if str(r["id"]) == str(tid)]
        check("тенант с 2 брифами — ровно одна строка", len(mine_rows) == 1, f"строк={len(mine_rows)}")
        check("показан последний бриф (proposed)",
              bool(mine_rows) and mine_rows[0]["brief_status"] == "proposed",
              f"st={mine_rows[0]['brief_status'] if mine_rows else None}")
        rows2b = await db.list_partners()
        m2b = [r for r in rows2b if str(r["id"]) == pid][0]
        check("tenant_count всё ещё 1 (не задвоился)", m2b["tenant_count"] == 1, f"c={m2b['tenant_count']}")
        print("4. set_partner_status disabled → get_partner видит:")
        await db.set_partner_status(pid, "disabled", actor="smoke", ip=None, user_agent=None)
        check("status disabled", (await db.get_partner(pid))["status"] == "disabled")
        print("5. set_partner_chat_id:")
        await db.set_partner_chat_id(pid, "999000", actor="smoke", ip=None, user_agent=None)
        check("chat_id обновлён", (await db.get_partner(pid))["tg_chat_id"] == "999000")
    finally:
        async with db.pool.acquire() as c:
            await _cleanup(c)
        await db.pool.close()
    print()
    if FAILS:
        print("❌ ПРОВАЛЫ: " + ", ".join(FAILS)); sys.exit(1)
    print("✅ partners smoke (panel-side) — OK")


if __name__ == "__main__":
    asyncio.run(main())
