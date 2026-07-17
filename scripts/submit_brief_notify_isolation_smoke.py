#!/usr/bin/env python3
"""РЕГРЕССИЯ Critical (финал-ревью Куска B): сбой уведомления Событие-2 НЕ должен откатывать
сам сабмит брифа. Раньше enqueue сидел ВНУТРИ транзакции submit_brief → падение INSERT
аборти́ло транзакцию → COMMIT становился ROLLBACK → бриф молча терялся при возврате 'ok'.
Фикс: enqueue вынесен ПОСЛЕ коммита, best-effort. Тест подсовывает падающий enqueue и
проверяет, что бриф всё равно 'submitted' с сохранёнными ответами.

Секция 4 (Task 5, Кусок C): реферальный (партнёрский) тенант при прохождении брифа шлёт
ДВА уведомления — владельцу (как обычно) И партнёру (best-effort, свой try/except).
Проверяет обе очереди платформенных уведомлений + что инвариант сабмита цел.
  PLATFORM_NOTIFY_SMOKE_DSN="...risuy_dev..." PYTHONPATH=bot-telegram:. \
    ./.venv-smoke/bin/python scripts/submit_brief_notify_isolation_smoke.py
"""
import asyncio, json, os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, "bot-telegram"))
os.environ.setdefault("BOT_TOKEN", "smoke-token")
os.environ.setdefault("DATABASE_URL", "postgresql://x/y")
os.environ.setdefault("CHANNEL_ID", "-1001234567890")
os.environ.setdefault("CHANNEL_URL", "https://t.me/smoke")
os.environ.setdefault("GUIDE_URL", "https://example.com/guide")

import asyncpg  # noqa: E402
import db  # noqa: E402  (bot-telegram/db.py)

DSN = os.environ.get("PLATFORM_NOTIFY_SMOKE_DSN")
if not DSN or "/risuy_dev" not in DSN.split("?")[0]:
    raise SystemExit("Задайте PLATFORM_NOTIFY_SMOKE_DSN на risuy_dev")

FAILS: list[str] = []
TOKEN = "smoke-notify-iso-token"
TNAME = "СМОУК NotifyIso ООО"

# Секция 4 — партнёрский (реферальный) тенант.
TOKEN2 = "smoke-notify-iso-token2"
TNAME2 = "СМОУК NotifyIso РефКомпания ООО"
PNAME = "СМОУК NotifyIso Партнёр"
PREF_CODE = "smokenisoref"
OWNER_CHAT = "12345"
PARTNER_CHAT = "700700"


def check(name, cond, detail=""):
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


async def _cleanup(c):
    await c.execute("delete from tenant_brief where token=$1", TOKEN)
    await c.execute("delete from tenants where name=$1", TNAME)


async def _cleanup_partner(c):
    await c.execute("delete from tenant_brief where token=$1", TOKEN2)
    await c.execute("delete from tenants where name=$1", TNAME2)
    await c.execute("delete from partners where name=$1", PNAME)
    await c.execute("delete from platform_notify where text like $1", f"%{TNAME2}%")


async def main():
    db.pool = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    orig_chat = None
    orig_enqueue = db.enqueue_platform_notify
    called = {"n": 0}

    async def _raising_enqueue(chat_id, text):
        called["n"] += 1
        raise RuntimeError("smoke: forced enqueue failure")

    try:
        async with db.pool.acquire() as c:
            orig_chat = await c.fetchval("select value from app_settings where key='owner_chat_id'")
            await _cleanup(c)
            await _cleanup_partner(c)
            tid = await c.fetchval("insert into tenants (slug, name) values ($1,$2) returning id",
                                   "smoke-notify-iso", TNAME)
            brief_id = await c.fetchval(
                "insert into tenant_brief (tenant_id, token) values ($1,$2) returning id", tid, TOKEN)
            # owner_chat_id ЗАДАН → путь уведомления Событие-2 будет пройден (и упадёт).
            await c.execute("insert into app_settings(key,value) values('owner_chat_id',$1) "
                            "on conflict(key) do update set value=excluded.value", OWNER_CHAT)

        print("1. submit_brief при ПАДАЮЩЕМ enqueue уведомления:")
        db.enqueue_platform_notify = _raising_enqueue  # монки-патч: имитируем сбой INSERT/доставки
        res = await db.submit_brief(TOKEN, {"q1": "ответ"})
        check("submit_brief вернул 'ok'", res == "ok", f"res={res}")
        check("путь уведомления реально пройден (enqueue вызван)", called["n"] == 1, f"n={called['n']}")

        print("2. КЛЮЧЕВОЕ: сабмит брифа НЕ откатился сбоем уведомления:")
        async with db.pool.acquire() as c:
            r = await c.fetchrow("select status, answers, submitted_at from tenant_brief where id=$1", brief_id)
        check("статус submitted (не откатился в pending)", r["status"] == "submitted", f"st={r['status']}")
        check("ответы сохранены", r["answers"] is not None and json.loads(r["answers"]).get("q1") == "ответ")
        check("submitted_at заполнен", r["submitted_at"] is not None)

        print("3. повторный submit того же токена → 'already' (статус-машина цела):")
        db.enqueue_platform_notify = orig_enqueue
        res2 = await db.submit_brief(TOKEN, {"q1": "второй"})
        check("повтор → already", res2 == "already", f"res={res2}")

        print("4. партнёрский тенант: submit_brief шлёт ДВА уведомления (владелец + партнёр):")
        async with db.pool.acquire() as c:
            pid = await c.fetchval(
                "insert into partners(name,ref_code,tg_chat_id,status) values($1,$2,$3,'active') "
                "returning id",
                PNAME, PREF_CODE, PARTNER_CHAT)
            tid2 = await c.fetchval(
                "insert into tenants (slug, name, partner_id) values ($1,$2,$3) returning id",
                "smoke-notify-iso-ref", TNAME2, pid)
            brief_id2 = await c.fetchval(
                "insert into tenant_brief (tenant_id, token) values ($1,$2) returning id", tid2, TOKEN2)
            # owner_chat_id по-прежнему '12345' (задан в шаге 1, не менялся).

        res3 = await db.submit_brief(TOKEN2, {"q1": "ответ"})
        check("submit_brief (партнёрский тенант) вернул 'ok'", res3 == "ok", f"res={res3}")

        async with db.pool.acquire() as c:
            n = await c.fetchval(
                "select count(*) from platform_notify where status='queued' and "
                "(chat_id=$1 or chat_id=$2) and created_at > now()-interval '1 minute'",
                12345, 700700)  # owner_chat_id, partner tg_chat_id
        check("оба уведомления (владелец+партнёр) поставлены", n >= 2, f"n={n}")

        async with db.pool.acquire() as c:
            r2 = await c.fetchrow("select status from tenant_brief where id=$1", brief_id2)
        check("бриф партнёрского тенанта submitted (инвариант цел)", r2["status"] == "submitted", f"st={r2['status']}")
    finally:
        db.enqueue_platform_notify = orig_enqueue
        async with db.pool.acquire() as c:
            await _cleanup(c)
            await _cleanup_partner(c)
            if orig_chat is None:
                await c.execute("delete from app_settings where key='owner_chat_id'")
            else:
                await c.execute("insert into app_settings(key,value) values('owner_chat_id',$1) "
                                "on conflict(key) do update set value=excluded.value", orig_chat)
        await db.pool.close()
    print()
    if FAILS:
        print("❌ ПРОВАЛЫ: " + ", ".join(FAILS)); sys.exit(1)
    print("✅ submit_brief notify-isolation regression — OK (уведомление не рушит сабмит)")


if __name__ == "__main__":
    asyncio.run(main())
