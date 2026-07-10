#!/usr/bin/env python3
"""DB-смоук platform_notify — БОТ-сторона (контроллер, risuy_dev). Дренаж/claim/mark/release/reclaim
живут в bot-telegram/db.py, поэтому импорт бот-модуля:
  PLATFORM_NOTIFY_SMOKE_DSN="...risuy_dev..." PYTHONPATH=bot-telegram:. \
    ./.venv-smoke/bin/python scripts/platform_notify_smoke.py
Панель-сторона (enqueue + set_owner_chat_id_with_audit) — в scripts/platform_notify_panel_smoke.py.
"""
import asyncio, os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, "bot-telegram"))
# bot config._req() требует эти env при импорте db→config.
os.environ.setdefault("BOT_TOKEN", "smoke-token")
os.environ.setdefault("DATABASE_URL", "postgresql://x/y")
os.environ.setdefault("CHANNEL_ID", "-1001234567890")
os.environ.setdefault("CHANNEL_URL", "https://t.me/smoke")
os.environ.setdefault("GUIDE_URL", "https://example.com/guide")

import asyncpg  # noqa: E402
import db  # noqa: E402  (bot-telegram/db.py под PYTHONPATH)

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


async def main():
    db.pool = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    orig = None
    try:
        async with db.pool.acquire() as c:
            orig = await c.fetchval("select value from app_settings where key='owner_chat_id'")
            await _cleanup(c)

        print("1. enqueue → queued:")
        nid = await db.enqueue_platform_notify(CHAT, "тест-уведомление")
        check("enqueue вернул id", nid > 0)
        async with db.pool.acquire() as c:
            st = await c.fetchval("select status from platform_notify where id=$1", nid)
        check("статус queued", st == "queued", f"st={st}")

        print("2. claim (queued→sending, attempts+1, claimed_at, SKIP LOCKED):")
        items = await db.claim_platform_notify(10)
        check("claim вернул нашу строку", any(i["id"] == nid for i in items))
        async with db.pool.acquire() as c:
            row = await c.fetchrow("select status, attempts, claimed_at from platform_notify where id=$1", nid)
        check("статус sending", row["status"] == "sending", f"st={row['status']}")
        check("attempts=1", row["attempts"] == 1, f"attempts={row['attempts']}")
        check("claimed_at заполнен", row["claimed_at"] is not None)
        check("повторный claim пуст (уже sending)",
              not any(i["id"] == nid for i in await db.claim_platform_notify(10)))

        print("3. mark sent:")
        await db.mark_platform_notify_sent(nid)
        async with db.pool.acquire() as c:
            r3 = await c.fetchrow("select status, sent_at from platform_notify where id=$1", nid)
        check("статус sent + sent_at", r3["status"] == "sent" and r3["sent_at"] is not None, f"st={r3['status']}")

        print("4. mark failed (перманент): терминальный failed + обрезка last_error:")
        nid2 = await db.enqueue_platform_notify(CHAT, "второе")
        await db.claim_platform_notify(10)
        await db.mark_platform_notify_failed(nid2, "x" * 800)
        async with db.pool.acquire() as c:
            r4 = await c.fetchrow("select status, last_error from platform_notify where id=$1", nid2)
        check("статус failed", r4["status"] == "failed", f"st={r4['status']}")
        check("last_error обрезан до 500", len(r4["last_error"]) == 500, f"len={len(r4['last_error'])}")
        check("failed НЕ переклеймится", not any(i["id"] == nid2 for i in await db.claim_platform_notify(10)))

        print("5. release (транзиент, ниже потолка) → возврат в queued для ретрая:")
        nid3 = await db.enqueue_platform_notify(CHAT, "третье")
        await db.claim_platform_notify(10)  # attempts=1
        await db.release_platform_notify(nid3, "timeout", max_attempts=5, max_age_hours=24)
        async with db.pool.acquire() as c:
            st5 = await c.fetchval("select status from platform_notify where id=$1", nid3)
        check("attempts(1)<5 → queued (ретрай)", st5 == "queued", f"st={st5}")
        check("released строка снова клеймится", any(i["id"] == nid3 for i in await db.claim_platform_notify(10)))

        print("6. release (attempts>=потолок) → терминальный failed:")
        async with db.pool.acquire() as c:
            await c.execute("update platform_notify set attempts=5 where id=$1", nid3)
        await db.release_platform_notify(nid3, "timeout", max_attempts=5, max_age_hours=24)
        async with db.pool.acquire() as c:
            st6 = await c.fetchval("select status from platform_notify where id=$1", nid3)
        check("attempts(5)>=5 → failed", st6 == "failed", f"st={st6}")

        print("7. reclaim застрявших 'sending' (claimed_at в прошлом) → queued:")
        nid4 = await db.enqueue_platform_notify(CHAT, "четвёртое")
        await db.claim_platform_notify(10)  # sending, claimed_at=now
        async with db.pool.acquire() as c:
            await c.execute("update platform_notify set claimed_at=now()-interval '1 hour' where id=$1", nid4)
        n = await db.reclaim_stuck_platform_notify(60)
        check("reclaim вернул >=1", n >= 1, f"n={n}")
        async with db.pool.acquire() as c:
            st7 = await c.fetchval("select status from platform_notify where id=$1", nid4)
        check("застрявший sending → queued", st7 == "queued", f"st={st7}")

        print("8. get_owner_chat_id (бот читает app_settings['owner_chat_id']):")
        async with db.pool.acquire() as c:
            await c.execute("insert into app_settings(key,value) values('owner_chat_id','888777') "
                            "on conflict(key) do update set value=excluded.value")
        check("get_owner_chat_id вернул заданное", await db.get_owner_chat_id() == "888777")
    finally:
        async with db.pool.acquire() as c:
            await _cleanup(c)
            if orig is None:
                await c.execute("delete from app_settings where key='owner_chat_id'")
            else:
                await c.execute("insert into app_settings(key,value) values('owner_chat_id',$1) "
                                "on conflict(key) do update set value=excluded.value", orig)
        await db.pool.close()
    print()
    if FAILS:
        print("❌ ПРОВАЛЫ: " + ", ".join(FAILS)); sys.exit(1)
    print("✅ platform_notify smoke (bot-side) — OK")


if __name__ == "__main__":
    asyncio.run(main())
