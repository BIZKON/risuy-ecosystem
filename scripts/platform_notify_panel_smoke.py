#!/usr/bin/env python3
"""DB-смоук platform_notify — ПАНЕЛЬ-сторона (контроллер, risuy_dev). Панель только ставит в
очередь (enqueue) и пишет owner_chat_id (set_owner_chat_id_with_audit) — импорт панель-модуля:
  PLATFORM_NOTIFY_SMOKE_DSN="...risuy_dev..." PYTHONPATH=admin-panel:. \
    ./.venv-smoke/bin/python scripts/platform_notify_panel_smoke.py
Бот-сторона (claim/mark/release/reclaim) — в scripts/platform_notify_smoke.py.
"""
import asyncio, os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, "admin-panel"))
os.environ.setdefault("DATABASE_URL", "postgresql://x/y")
os.environ.setdefault("SESSION_SECRET", "smoke-secret-padding-0123456789abcdef")
os.environ.setdefault("ADMIN_USERNAME", "smoke")
os.environ.setdefault("ADMIN_PASSWORD_HASH", "$argon2id$v=19$m=65536,t=3,p=4$c21va2U$c21va2U")

import asyncpg  # noqa: E402
import config  # noqa: E402
import db  # noqa: E402  (admin-panel/db.py под PYTHONPATH)

DSN = os.environ.get("PLATFORM_NOTIFY_SMOKE_DSN")
if not DSN or "/risuy_dev" not in DSN.split("?")[0]:
    raise SystemExit("Задайте PLATFORM_NOTIFY_SMOKE_DSN на risuy_dev")

FAILS: list[str] = []
CHAT = 77_000_555


def check(name, cond, detail=""):
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


async def main():
    db.pool = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    orig = None
    try:
        async with db.pool.acquire() as c:
            orig = await c.fetchval("select value from app_settings where key='owner_chat_id'")
            await c.execute("delete from platform_notify where chat_id=$1", CHAT)

        print("0. ключ app_settings зеркалит бот (литерал 'owner_chat_id'):")
        check("config.OWNER_CHAT_ID_SETTING_KEY == 'owner_chat_id'",
              config.OWNER_CHAT_ID_SETTING_KEY == "owner_chat_id",
              f"={config.OWNER_CHAT_ID_SETTING_KEY!r}")

        print("1. panel enqueue → queued:")
        pid = await db.enqueue_platform_notify(CHAT, "панель-уведомление")
        check("enqueue вернул id", pid > 0)
        async with db.pool.acquire() as c:
            st = await c.fetchval("select status from platform_notify where id=$1", pid)
        check("статус queued", st == "queued", f"st={st}")

        print("2. set_owner_chat_id_with_audit → get_owner_chat_id (+audit):")
        await db.set_owner_chat_id_with_audit("12345", actor="smoke", ip=None, user_agent=None)
        check("get вернул заданное '12345'", await db.get_owner_chat_id() == "12345")
        async with db.pool.acquire() as c:
            n_audit = await c.fetchval("select count(*) from admin_audit where action='owner_chat_id_set'")
        check("audit-строка owner_chat_id_set (>=1)", n_audit >= 1, f"count={n_audit}")

        print("3. очистка адреса (None → пусто):")
        await db.set_owner_chat_id_with_audit(None, actor="smoke", ip=None, user_agent=None)
        check("get после None → '' или None", (await db.get_owner_chat_id() or "") == "")
    finally:
        async with db.pool.acquire() as c:
            await c.execute("delete from platform_notify where chat_id=$1", CHAT)
            if orig is None:
                await c.execute("delete from app_settings where key='owner_chat_id'")
            else:
                await c.execute("insert into app_settings(key,value) values('owner_chat_id',$1) "
                                "on conflict(key) do update set value=excluded.value", orig)
        await db.pool.close()
    print()
    if FAILS:
        print("❌ ПРОВАЛЫ: " + ", ".join(FAILS)); sys.exit(1)
    print("✅ platform_notify smoke (panel-side) — OK")


if __name__ == "__main__":
    asyncio.run(main())
