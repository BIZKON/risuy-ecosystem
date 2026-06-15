#!/usr/bin/env python3
"""Смоук per-tenant конфига «Мой ИИ-сотрудник» (tenant_settings) на risuy_dev.

Проверяет get_tenant_ai_config / set_tenant_ai_config (admin-panel/db.py):
  1. дефолты при отсутствии строки: enabled=True, prompt='', fallback='', provisioned=False;
  2. запись и чтение конфига своего тенанта (prompt/fallback/тумблер);
  3. RLS-изоляция чтения: под ctx B строки тенанта A НЕ видны (политика tenant_isolation);
  4. RLS with_check: под ctx B нельзя вставить tenant_settings с tenant_id = A;
  5. тумблер enabled off/on сохраняется;
  6. set_tenant_ai_config НЕ затирает инфра-ключ ai_agent_id (провижининг владельца);
  7. tenant_id обязателен на запись (ValueError); get с None → дефолты без падения;
  8. гранты panel_rw на tenant_settings (select/insert/update) — owner-смоук слеп к ACL,
     поэтому проверяем has_table_privilege явно.

Гонится как gen_user (owner). Owner ENABLE-RLS обходит → на время теста FORCE RLS на
tenant_settings (owner становится субъектом политики, имитируем panel_rw); в finally — снять.
Тестовые smoke-myai-* удаляются. На прод НЕ запускать.

Запуск: TENANT_AI_SMOKE_DSN="postgresql://<owner>@<host>:5432/risuy_dev?sslmode=require" \
        PYTHONPATH=. ./.venv-smoke/bin/python scripts/tenant_ai_config_smoke.py
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

DSN = os.environ.get("TENANT_AI_SMOKE_DSN")
if not DSN or "/risuy_dev" not in DSN.split("?")[0]:
    raise SystemExit("Задайте TENANT_AI_SMOKE_DSN на risuy_dev (FORCE RLS временно + delete тестовых строк).")

FAILS: list[str] = []
PROMPT_A = "Ты — Анна, администратор студии A. Отвечай тепло и на «вы»."
FALLBACK_A = "Сейчас не отвечу, напишите менеджеру A."
AGENT_A = "agent-aaaa-1111"


def check(name: str, cond: bool, detail="") -> None:
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail != "" else ""))
    if not cond:
        FAILS.append(name)


async def _cleanup(c):
    await c.execute(
        "delete from tenant_settings where tenant_id in "
        "(select id from tenants where slug like 'smoke-myai-%')")
    await c.execute("delete from tenants where slug like 'smoke-myai-%'")


async def main() -> None:
    db.pool = await asyncpg.create_pool(DSN, min_size=1, max_size=4, setup=db._apply_tenant_guc)
    forced = False
    ta = tb = None
    try:
        # ── сидирование тенантов (ctx None; RLS ещё ENABLE-не-FORCE → owner пишет) ──
        db.set_active_tenant(None)
        async with db.pool.acquire() as c:
            await _cleanup(c)
            ta = await c.fetchval("insert into tenants(slug,name,status) values('smoke-myai-a','A','active') returning id")
            tb = await c.fetchval("insert into tenants(slug,name,status) values('smoke-myai-b','B','active') returning id")
            await c.execute("alter table tenant_settings force row level security")
            forced = True

        # ── 1. дефолты при пустом конфиге ──
        print("1. дефолты при отсутствии строки:")
        d = await db.get_tenant_ai_config(ta)
        check("enabled по умолчанию = True", d["enabled"] is True)
        check("prompt по умолчанию пуст", d["system_prompt"] == "", repr(d["system_prompt"]))
        check("fallback по умолчанию пуст", d["fallback"] == "", repr(d["fallback"]))
        check("provisioned=False без ai_agent_id", d["provisioned"] is False)

        # ── 2. запись и чтение своего конфига ──
        print("2. запись и чтение конфига тенанта A:")
        await db.set_tenant_ai_config(
            ta, enabled=True, system_prompt=PROMPT_A, fallback=FALLBACK_A,
            actor="smoke-myai", ip=None, user_agent=None)
        a = await db.get_tenant_ai_config(ta)
        check("prompt сохранён", a["system_prompt"] == PROMPT_A)
        check("fallback сохранён", a["fallback"] == FALLBACK_A)
        check("enabled=True", a["enabled"] is True)

        # ── 3. RLS-изоляция чтения (политика tenant_isolation, ctx из хука пула) ──
        print("3. RLS-изоляция: ctx B не видит строки A:")
        db.set_active_tenant(tb)
        async with db.pool.acquire() as c:
            seen = await c.fetchval(
                "select count(*) from tenant_settings where tenant_id = $1", ta)
        b_cfg = await db.get_tenant_ai_config(tb)
        check("ctx B: raw-запрос не видит ни одной строки A", int(seen) == 0, f"видно {seen}")
        check("get_tenant_ai_config(B) → дефолты (не видит A)",
              b_cfg["system_prompt"] == "" and b_cfg["enabled"] is True)
        db.set_active_tenant(None)

        # ── 4. RLS with_check: под ctx B нельзя вставить строку тенанта A ──
        print("4. RLS with_check на запись:")
        db.set_active_tenant(tb)
        denied = False
        try:
            async with db.pool.acquire() as c:
                await c.execute(
                    "insert into tenant_settings (tenant_id, key, value) values ($1, 'ai_system_prompt', 'x')",
                    ta)
        except asyncpg.PostgresError:
            denied = True
        db.set_active_tenant(None)
        check("вставка строки с чужим tenant_id отклонена (with_check)", denied)

        # ── 5. тумблер enabled off/on ──
        print("5. тумблер enabled:")
        await db.set_tenant_ai_config(
            ta, enabled=False, system_prompt=PROMPT_A, fallback=FALLBACK_A,
            actor="smoke-myai", ip=None, user_agent=None)
        check("enabled=False сохранён", (await db.get_tenant_ai_config(ta))["enabled"] is False)
        await db.set_tenant_ai_config(
            ta, enabled=True, system_prompt=PROMPT_A, fallback=FALLBACK_A,
            actor="smoke-myai", ip=None, user_agent=None)
        check("enabled снова True", (await db.get_tenant_ai_config(ta))["enabled"] is True)

        # ── 6. set НЕ затирает инфра-ключ ai_agent_id (провижининг владельца) ──
        print("6. set_tenant_ai_config не трогает ai_agent_id:")
        db.set_active_tenant(ta)
        async with db.pool.acquire() as c:
            await c.execute(
                "insert into tenant_settings (tenant_id, key, value) values ($1, 'ai_agent_id', $2) "
                "on conflict (tenant_id, key) do update set value = excluded.value", ta, AGENT_A)
        db.set_active_tenant(None)
        check("provisioned=True после привязки ai_agent_id", (await db.get_tenant_ai_config(ta))["provisioned"] is True)
        await db.set_tenant_ai_config(
            ta, enabled=True, system_prompt="изменил инструкции", fallback="",
            actor="smoke-myai", ip=None, user_agent=None)
        after = await db.get_tenant_ai_config(ta)
        db.set_active_tenant(ta)
        async with db.pool.acquire() as c:
            agent_kept = await c.fetchval(
                "select value from tenant_settings where tenant_id = $1 and key = 'ai_agent_id'", ta)
        db.set_active_tenant(None)
        check("ai_agent_id сохранился после записи клиентского конфига", agent_kept == AGENT_A, repr(agent_kept))
        check("provisioned остаётся True", after["provisioned"] is True)
        check("новый prompt применился", after["system_prompt"] == "изменил инструкции")

        # ── 7. tenant_id обязателен на запись; get(None) → дефолты ──
        print("7. обязательность tenant_id:")
        raised = False
        try:
            await db.set_tenant_ai_config(
                None, enabled=True, system_prompt="x", fallback="",
                actor="smoke-myai", ip=None, user_agent=None)
        except ValueError:
            raised = True
        check("set_tenant_ai_config(None) → ValueError", raised)
        none_cfg = await db.get_tenant_ai_config(None)
        check("get_tenant_ai_config(None) → дефолты без падения",
              none_cfg["enabled"] is True and none_cfg["provisioned"] is False)

        # ── 8. гранты panel_rw на tenant_settings ──
        print("8. гранты panel_rw на tenant_settings:")
        async with db.pool.acquire() as c:
            ins = await c.fetchval("select has_table_privilege('panel_rw', 'tenant_settings', 'INSERT')")
            upd = await c.fetchval("select has_table_privilege('panel_rw', 'tenant_settings', 'UPDATE')")
            sel = await c.fetchval("select has_table_privilege('panel_rw', 'tenant_settings', 'SELECT')")
        check("panel_rw INSERT tenant_settings", ins is True,
              "грант db/panel_role.sql не накатан на этот dev?" if not ins else "")
        check("panel_rw UPDATE tenant_settings", upd is True)
        check("panel_rw SELECT tenant_settings", sel is True)

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
    print("✅ tenant ai config smoke — все проверки зелёные")


if __name__ == "__main__":
    asyncio.run(main())
