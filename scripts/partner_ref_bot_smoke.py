#!/usr/bin/env python3
"""DB-смоук реф-потока — БОТ-сторона (контроллер, risuy_dev):
  PARTNERS_SMOKE_DSN="...risuy_dev..." PYTHONPATH=bot-telegram:. \
    ./.venv-smoke/bin/python scripts/partner_ref_bot_smoke.py
"""
import asyncio, os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, "bot-telegram"))
os.environ.setdefault("BOT_TOKEN", "smoke-token")
os.environ.setdefault("DATABASE_URL", "postgresql://x/y")
os.environ.setdefault("CHANNEL_ID", "-1001234567890")
os.environ.setdefault("CHANNEL_URL", "https://t.me/smoke")
os.environ.setdefault("GUIDE_URL", "https://example.com/guide")
import asyncpg  # noqa: E402
import db  # noqa: E402
DSN = os.environ.get("PARTNERS_SMOKE_DSN")
if not DSN or "/risuy_dev" not in DSN.split("?")[0]:
    raise SystemExit("Задайте PARTNERS_SMOKE_DSN на risuy_dev")
FAILS = []
PNAME = "СМОУК РефБот Партнёр"
UID = 91_000_222


def check(name, cond, detail=""):
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


async def _cleanup(c, pid):
    if pid:
        await c.execute("delete from tenant_brief where tenant_id in (select id from tenants where partner_id=$1)", pid)
        await c.execute("delete from tenants where partner_id=$1", pid)
    await c.execute("delete from partners where name=$1", PNAME)


async def main():
    db.pool = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    pid = None
    try:
        async with db.pool.acquire() as c:
            await _cleanup(c, None)
            ref = "smokeref01"
            pid = await c.fetchval("insert into partners(name,ref_code,tg_chat_id,status) "
                                   "values($1,$2,'700700','active') returning id", PNAME, ref)
        print("1. get_partner_by_ref_code (active):")
        p = await db.get_partner_by_ref_code(ref)
        check("резолв активного партнёра", p is not None and str(p["id"]) == str(pid))
        print("2. create_ref_tenant → тенант+бриф+атрибуция:")
        tid, tok = await db.create_ref_tenant(str(pid), "СМОУК РефКомпания", UID)
        check("вернул (tid, token)", bool(tid) and len(tok) >= 16)
        async with db.pool.acquire() as c:
            r = await c.fetchrow("select status, partner_id, ref_tg_user_id from tenants where id=$1", tid)
            bst = await c.fetchval("select status from tenant_brief where tenant_id=$1", tid)
        check("тенант active", r["status"] == "active")
        check("partner_id проставлен", str(r["partner_id"]) == str(pid))
        check("ref_tg_user_id проставлен", r["ref_tg_user_id"] == UID)
        check("бриф pending", bst == "pending", f"bst={bst}")
        print("3. дедуп: find_pending_ref_brief находит незавершённый:")
        dtok = await db.find_pending_ref_brief(UID, str(pid))
        check("вернул тот же token", dtok == tok, f"dtok={dtok}")
        print("4. rate-limit: count_recent_ref_tenants:")
        n = await db.count_recent_ref_tenants(UID, 24)
        check("посчитал >=1 за 24ч", n >= 1, f"n={n}")
        print("5. get_partner_chat_id:")
        check("chat_id партнёра", await db.get_partner_chat_id(str(pid)) == "700700")
        print("6. disabled партнёр не резолвится:")
        async with db.pool.acquire() as c:
            await c.execute("update partners set status='disabled' where id=$1", pid)
        check("get_partner_by_ref_code(disabled) → None", await db.get_partner_by_ref_code(ref) is None)
    finally:
        async with db.pool.acquire() as c:
            await _cleanup(c, pid)
        await db.pool.close()
    print()
    if FAILS:
        print("❌ ПРОВАЛЫ: " + ", ".join(FAILS)); sys.exit(1)
    print("✅ partner ref bot smoke — OK")


if __name__ == "__main__":
    asyncio.run(main())
