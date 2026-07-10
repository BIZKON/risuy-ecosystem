#!/usr/bin/env python3
"""DB-смоук platform_notify (контроллер, risuy_dev): enqueue при заданном/пустом
owner_chat_id, claim→sending (SKIP LOCKED), mark sent/failed.
  PLATFORM_NOTIFY_SMOKE_DSN="...risuy_dev..." PYTHONPATH=admin-panel:. \
    ./.venv-smoke/bin/python scripts/platform_notify_smoke.py
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

DSN = os.environ.get("PLATFORM_NOTIFY_SMOKE_DSN")
if not DSN or "/risuy_dev" not in DSN.split("?")[0]:
    raise SystemExit("Задайте PLATFORM_NOTIFY_SMOKE_DSN на risuy_dev")

FAILS: list[str] = []
CHAT = 77_000_555


def check(name, cond, detail=""):
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


async def _cleanup(c):
    await c.execute("delete from platform_notify where chat_id=$1", CHAT)
    await c.execute("delete from app_settings where key='owner_chat_id' and value=''")


async def main():
    db.pool = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    try:
        async with db.pool.acquire() as c:
            await _cleanup(c)
        print("1. enqueue_platform_notify:")
        nid = await db.enqueue_platform_notify(CHAT, "тест-уведомление")
        check("enqueue вернул id", nid > 0)
        async with db.pool.acquire() as c:
            st = await c.fetchval("select status from platform_notify where id=$1", nid)
        check("статус queued", st == "queued", f"st={st}")

        print("2. claim (queued→sending, SKIP LOCKED):")
        items = await db.claim_platform_notify(10)
        check("claim вернул нашу строку", any(i["id"] == nid for i in items))
        async with db.pool.acquire() as c:
            st2 = await c.fetchval("select status from platform_notify where id=$1", nid)
        check("статус sending", st2 == "sending", f"st={st2}")
        check("повторный claim пуст (уже sending)", not any(i["id"] == nid for i in await db.claim_platform_notify(10)))

        print("3. mark sent/failed:")
        await db.mark_platform_notify_sent(nid)
        async with db.pool.acquire() as c:
            st3 = await c.fetchval("select status from platform_notify where id=$1", nid)
        check("статус sent", st3 == "sent", f"st={st3}")
        nid2 = await db.enqueue_platform_notify(CHAT, "второе")
        await db.claim_platform_notify(10)
        await db.mark_platform_notify_failed(nid2, "boom")
        async with db.pool.acquire() as c:
            st4, err = await c.fetchrow("select status, last_error from platform_notify where id=$1", nid2)
        check("статус failed + last_error", st4 == "failed" and err == "boom", f"st={st4}")

        print("4. owner_chat_id set/get:")
        await db.set_owner_chat_id_with_audit(str(CHAT), actor="smoke", ip=None, user_agent=None)
        check("get_owner_chat_id вернул заданное", await db.get_owner_chat_id() == str(CHAT))
        await db.set_owner_chat_id_with_audit(None, actor="smoke", ip=None, user_agent=None)
        check("пустой owner_chat_id → '' или None", (await db.get_owner_chat_id() or "") == "")
    finally:
        async with db.pool.acquire() as c:
            await _cleanup(c)
        await db.pool.close()
    print()
    if FAILS:
        print("❌ ПРОВАЛЫ: " + ", ".join(FAILS)); sys.exit(1)
    print("✅ platform_notify smoke — OK")


if __name__ == "__main__":
    asyncio.run(main())
