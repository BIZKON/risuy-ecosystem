#!/usr/bin/env python3
"""Смоук per-tenant конфига эскалации (tenant_settings, блок «Эскалация» в /my-agent) на risuy_dev.

Проверяет get_tenant_escalation_config / set_tenant_escalation_config (admin-panel/db.py):
  1. дефолты при отсутствии строки: enabled=False, chat_id='', topic_id='';
  2. запись и чтение адреса эскалации своего тенанта;
  3. RLS-изоляция: под ctx B строки A НЕ видны (политика tenant_isolation);
  4. RLS with_check: под ctx B нельзя вставить escalation-строку с tenant_id = A;
  5. тумблер enabled off/on;
  6. set_tenant_escalation_config НЕ затирает ИИ-ключи (ai_system_prompt) того же тенанта;
  7. tenant_id обязателен на запись (ValueError); get(None) → дефолты без падения.

Owner FORCE RLS на tenant_settings на время теста (как billing/tenant_ai_config smoke).
Тестовые smoke-esc-* удаляются. На прод НЕ запускать.

Запуск: TENANT_ESC_SMOKE_DSN="postgresql://<owner>@<host>:5432/risuy_dev?sslmode=require" \
        PYTHONPATH=. ./.venv-smoke/bin/python scripts/tenant_escalation_smoke.py
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

DSN = os.environ.get("TENANT_ESC_SMOKE_DSN")
if not DSN or "/risuy_dev" not in DSN.split("?")[0]:
    raise SystemExit("Задайте TENANT_ESC_SMOKE_DSN на risuy_dev (FORCE RLS временно + delete тестовых строк).")

FAILS: list[str] = []
CHAT_A = "-1002576119452"


def check(name: str, cond: bool, detail="") -> None:
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail != "" else ""))
    if not cond:
        FAILS.append(name)


async def _cleanup(c):
    await c.execute(
        "delete from tenant_settings where tenant_id in "
        "(select id from tenants where slug like 'smoke-esc-%')")
    await c.execute("delete from tenants where slug like 'smoke-esc-%'")


async def main() -> None:
    db.pool = await asyncpg.create_pool(DSN, min_size=1, max_size=4, setup=db._apply_tenant_guc)
    forced = False
    ta = tb = None
    try:
        db.set_active_tenant(None)
        async with db.pool.acquire() as c:
            await _cleanup(c)
            ta = await c.fetchval("insert into tenants(slug,name,status) values('smoke-esc-a','A','active') returning id")
            tb = await c.fetchval("insert into tenants(slug,name,status) values('smoke-esc-b','B','active') returning id")
            await c.execute("alter table tenant_settings force row level security")
            forced = True

        # ── 1. дефолты ──
        print("1. дефолты при отсутствии строки:")
        d = await db.get_tenant_escalation_config(ta)
        check("enabled по умолчанию = False", d["enabled"] is False)
        check("chat_id по умолчанию пуст", d["chat_id"] == "", repr(d["chat_id"]))
        check("topic_id по умолчанию пуст", d["topic_id"] == "", repr(d["topic_id"]))

        # ── 2. запись и чтение ──
        print("2. запись и чтение адреса эскалации A:")
        await db.set_tenant_escalation_config(
            ta, enabled=True, chat_id=CHAT_A, topic_id="12",
            actor="smoke-esc", ip=None, user_agent=None)
        a = await db.get_tenant_escalation_config(ta)
        check("enabled=True", a["enabled"] is True)
        check("chat_id сохранён", a["chat_id"] == CHAT_A, repr(a["chat_id"]))
        check("topic_id сохранён", a["topic_id"] == "12", repr(a["topic_id"]))

        # ── 3. RLS-изоляция чтения ──
        print("3. RLS-изоляция: ctx B не видит адрес A:")
        db.set_active_tenant(tb)
        async with db.pool.acquire() as c:
            seen = await c.fetchval("select count(*) from tenant_settings where tenant_id = $1", ta)
        b_cfg = await db.get_tenant_escalation_config(tb)
        check("ctx B: raw-запрос не видит строк A", int(seen) == 0, f"видно {seen}")
        check("get(B) → дефолты (не видит A)", b_cfg["chat_id"] == "" and b_cfg["enabled"] is False)
        db.set_active_tenant(None)

        # ── 4. RLS with_check ──
        print("4. RLS with_check на запись:")
        db.set_active_tenant(tb)
        denied = False
        try:
            async with db.pool.acquire() as c:
                await c.execute(
                    "insert into tenant_settings (tenant_id, key, value) values ($1, 'escalation_chat_id', 'x')", ta)
        except asyncpg.PostgresError:
            denied = True
        db.set_active_tenant(None)
        check("вставка с чужим tenant_id отклонена (with_check)", denied)

        # ── 5. тумблер off/on ──
        print("5. тумблер enabled:")
        await db.set_tenant_escalation_config(
            ta, enabled=False, chat_id=CHAT_A, topic_id="12", actor="smoke-esc", ip=None, user_agent=None)
        check("enabled=False сохранён", (await db.get_tenant_escalation_config(ta))["enabled"] is False)

        # ── 6. не затирает ИИ-ключи того же тенанта ──
        print("6. set эскалации не трогает ai_system_prompt:")
        await db.set_tenant_ai_config(
            ta, enabled=True, system_prompt="инструкции A", fallback="фолбэк A",
            actor="smoke-esc", ip=None, user_agent=None)
        await db.set_tenant_escalation_config(
            ta, enabled=True, chat_id=CHAT_A, topic_id="", actor="smoke-esc", ip=None, user_agent=None)
        ai_after = await db.get_tenant_ai_config(ta)
        check("ai_system_prompt сохранён после записи эскалации", ai_after["system_prompt"] == "инструкции A")
        check("эскалация записана рядом", (await db.get_tenant_escalation_config(ta))["chat_id"] == CHAT_A)

        # ── 7. обязательность tenant_id ──
        print("7. обязательность tenant_id:")
        raised = False
        try:
            await db.set_tenant_escalation_config(
                None, enabled=True, chat_id=CHAT_A, topic_id="", actor="smoke-esc", ip=None, user_agent=None)
        except ValueError:
            raised = True
        check("set(None) → ValueError", raised)
        none_cfg = await db.get_tenant_escalation_config(None)
        check("get(None) → дефолты без падения", none_cfg["enabled"] is False and none_cfg["chat_id"] == "")

    finally:
        async with db.pool.acquire() as c:
            if forced:
                await c.execute("alter table tenant_settings no force row level security")
            db.set_active_tenant(None)
            await _cleanup(c)
        await db.pool.close()

    print()
    if FAILS:
        print(f"❌ ПРОВАЛЫ ({len(FAILS)}): " + ", ".join(FAILS))
        sys.exit(1)
    print("✅ tenant escalation smoke — все проверки зелёные")


if __name__ == "__main__":
    asyncio.run(main())
