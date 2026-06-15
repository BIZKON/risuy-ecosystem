#!/usr/bin/env python3
"""Смоук CRUD триггеров клиента (раздел панели «Триггеры», admin-panel/db.py) на risuy_dev.

Проверяет list/create/count/delete_tenant_trigger:
  1. пусто при отсутствии триггеров;
  2. создание (stopwords) и чтение;
  3. RLS-изоляция: ctx B не видит триггеры A; with_check не даёт вставить чужой tenant_id;
  4. разные типы (intent/message_count/documents) создаются и фильтруются;
  5. удаление по id (свой тенант); чужой/несуществующий id не удаляется;
  6. count_tenant_triggers; tenant_id обязателен на create.

Owner FORCE RLS на tenant_triggers на время теста. Тестовые smoke-trc-* удаляются. На прод НЕ запускать.

Запуск: TRIGCRUD_SMOKE_DSN="postgresql://<owner>@<host>:5432/risuy_dev?sslmode=require" \
        PYTHONPATH=. ./.venv-smoke/bin/python scripts/tenant_triggers_panel_smoke.py
"""
import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "admin-panel"))
os.environ.setdefault("DATABASE_URL", "postgresql://x/y")
os.environ.setdefault("SESSION_SECRET", "smoke-secret-only-for-import-aaaaaaaa")
os.environ.setdefault("ADMIN_USERNAME", "smoke")
os.environ.setdefault("ADMIN_PASSWORD_HASH", "$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHQ$aGFzaHN0dWI")

import asyncpg  # noqa: E402
import db        # noqa: E402  (admin-panel/db.py)

DSN = os.environ.get("TRIGCRUD_SMOKE_DSN")
if not DSN or "/risuy_dev" not in DSN.split("?")[0]:
    raise SystemExit("Задайте TRIGCRUD_SMOKE_DSN на risuy_dev (FORCE RLS временно + delete тестовых строк).")

FAILS: list[str] = []


def check(name, cond, detail=""):
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail != "" else ""))
    if not cond:
        FAILS.append(name)


async def _cleanup(c):
    await c.execute("delete from tenant_triggers where tenant_id in (select id from tenants where slug like 'smoke-trc-%')")
    await c.execute("delete from tenants where slug like 'smoke-trc-%'")


async def _mk(ta, **kw):
    defaults = dict(type_="stopwords", action="notify_only", stopwords=[], intent_desc="",
                    msg_count=None, notify_chat_id="-1002576119452", notify_topic_id=None,
                    reply_text="", actor="smoke-trc", ip=None, user_agent=None)
    defaults.update(kw)
    await db.create_tenant_trigger(ta, **defaults)


async def main() -> None:
    db.pool = await asyncpg.create_pool(DSN, min_size=1, max_size=4, setup=db._apply_tenant_guc)
    forced = False
    ta = tb = None
    try:
        db.set_active_tenant(None)
        async with db.pool.acquire() as c:
            await _cleanup(c)
            ta = await c.fetchval("insert into tenants(slug,name,status) values('smoke-trc-a','A','active') returning id")
            tb = await c.fetchval("insert into tenants(slug,name,status) values('smoke-trc-b','B','active') returning id")
            await c.execute("alter table tenant_triggers force row level security")
            forced = True

        # ── 1. пусто ──
        print("1. пусто при отсутствии:")
        check("list(A) пуст", (await db.list_tenant_triggers(ta)) == [])
        check("count(A) = 0", (await db.count_tenant_triggers(ta)) == 0)

        # ── 2. создание stopwords + чтение ──
        print("2. создание stopwords:")
        await _mk(ta, type_="stopwords", action="notify_reply_continue",
                  stopwords=["отмена", "перенести"], reply_text="ответ")
        rows = await db.list_tenant_triggers(ta)
        check("создан 1 триггер", len(rows) == 1)
        check("stopwords прочитаны", list(rows[0]["stopwords"]) == ["отмена", "перенести"])
        check("action сохранён", rows[0]["action"] == "notify_reply_continue")
        check("reply сохранён", rows[0]["reply_text"] == "ответ")

        # ── 3. RLS-изоляция ──
        print("3. RLS-изоляция:")
        db.set_active_tenant(tb)
        async with db.pool.acquire() as c:
            seen = await c.fetchval("select count(*) from tenant_triggers where tenant_id = $1", ta)
        check("ctx B: raw не видит триггеры A", int(seen) == 0, f"видно {seen}")
        check("list(B) пуст", (await db.list_tenant_triggers(tb)) == [])
        denied = False
        try:
            async with db.pool.acquire() as c:
                await c.execute("insert into tenant_triggers (tenant_id, type, notify_chat_id) values ($1,'stopwords','-100')", ta)
        except asyncpg.PostgresError:
            denied = True
        db.set_active_tenant(None)
        check("with_check: вставка чужого tenant_id отклонена", denied)

        # ── 4. разные типы ──
        print("4. разные типы:")
        await _mk(ta, type_="intent", action="notify_only", intent_desc="просит оплату")
        await _mk(ta, type_="message_count", action="notify_only", msg_count=5)
        await _mk(ta, type_="documents", action="notify_reply_continue", reply_text="принято")
        rows = await db.list_tenant_triggers(ta)
        check("всего 4 триггера A", len(rows) == 4, f"факт {len(rows)}")
        types = sorted(r["type"] for r in rows)
        check("типы: documents/intent/message_count/stopwords", types == ["documents", "intent", "message_count", "stopwords"], repr(types))
        ic = next(r for r in rows if r["type"] == "intent")
        check("intent_desc сохранён", ic["intent_desc"] == "просит оплату")
        mc = next(r for r in rows if r["type"] == "message_count")
        check("msg_count сохранён", mc["msg_count"] == 5)

        # ── 5. удаление ──
        print("5. удаление:")
        target = rows[0]["id"]
        ok = await db.delete_tenant_trigger(ta, target, actor="smoke-trc", ip=None, user_agent=None)
        check("удаление своего триггера → True", ok is True)
        check("осталось 3", len(await db.list_tenant_triggers(ta)) == 3)
        ok2 = await db.delete_tenant_trigger(ta, target, actor="smoke-trc", ip=None, user_agent=None)
        check("повторное удаление → False", ok2 is False)
        # триггер B (создаём под ctx B) нельзя удалить из-под A
        await _mk(tb, type_="stopwords", stopwords=["b"], notify_chat_id="-100999")
        b_id = (await db.list_tenant_triggers(tb))[0]["id"]
        check("удаление чужого (B) из-под A → False", (await db.delete_tenant_trigger(ta, b_id, actor="smoke-trc", ip=None, user_agent=None)) is False)
        check("триггер B на месте", len(await db.list_tenant_triggers(tb)) == 1)

        # ── 6. count + обязательность tenant_id ──
        print("6. count + обязательность:")
        check("count(A) = 3", (await db.count_tenant_triggers(ta)) == 3)
        raised = False
        try:
            await _mk(None, type_="stopwords", stopwords=["x"])
        except ValueError:
            raised = True
        check("create(None) → ValueError", raised)

    finally:
        async with db.pool.acquire() as c:
            if forced:
                await c.execute("alter table tenant_triggers no force row level security")
            db.set_active_tenant(None)
            await _cleanup(c)
        await db.pool.close()

    print()
    if FAILS:
        print(f"❌ ПРОВАЛЫ ({len(FAILS)}): " + ", ".join(FAILS)); sys.exit(1)
    print("✅ tenant triggers CRUD smoke — все проверки зелёные")


if __name__ == "__main__":
    asyncio.run(main())
