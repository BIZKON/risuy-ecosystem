#!/usr/bin/env python3
"""БД-смоук СП-1 на risuy_dev (роль panel_rw, RLS ENFORCED): CRUD team_agents под RLS видит только
свой тенант; unique(tenant_id,slug); один is_default; soft-delete; agent_memory RLS.
Запуск:
  TEAM_DSN="postgresql://panel_rw:<pw>@81.31.246.136:5432/risuy_dev?sslmode=require" \
  PYTHONPATH=admin-panel:bot-telegram:. ./.venv-smoke/bin/python scripts/team_agents_db_smoke.py
"""
import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "admin-panel"))
os.environ.setdefault("DATABASE_URL", os.environ.get("TEAM_DSN", "postgresql://x/y"))
os.environ.setdefault("SESSION_SECRET", "smoke-session-secret-padding-0123456789-abcdef")
os.environ.setdefault("ADMIN_USERNAME", "smoke")
os.environ.setdefault("ADMIN_PASSWORD_HASH",
                      "$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHQ$aGFzaHN0dWI")
import asyncpg  # noqa: E402
import db as adb  # noqa: E402  (admin-panel/db.py)

TEAM_DSN = os.environ["TEAM_DSN"]
assert "/risuy_dev" in TEAM_DSN.split("?")[0], "только risuy_dev"
FAILS: list[str] = []
SLUG_T = "smoke-team-a"

# Поля, которые форма «ИИ-команда» отправляет в db.upsert_team_agent. Каждое обязано доезжать
# обратно через _TEAM_AGENT_SELECT и _present_team_agent — иначе сохранение затирает значение.
UPSERT_FIELDS = ("name", "role_preset", "system_prompt", "escalation_chat_id",
                 "escalation_topic_id", "is_orchestrator", "memory_enabled", "kb_enabled")


def check(name, cond, detail=""):
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


async def _cleanup():
    adb.set_active_tenant(None)
    async with adb.pool.acquire() as c:
        ids = await c.fetch("select id from tenants where slug = $1", SLUG_T)
    for r in ids:
        adb.set_active_tenant(str(r["id"]))
        async with adb.pool.acquire() as c:
            await c.execute("delete from agent_memory where tenant_id = $1", r["id"])
            await c.execute("delete from team_agents where tenant_id = $1", r["id"])
            await c.execute("delete from tenant_settings where tenant_id = $1", r["id"])
    adb.set_active_tenant(None)
    async with adb.pool.acquire() as c:
        await c.execute("delete from tenants where slug = $1", SLUG_T)


async def main():
    adb.pool = await asyncpg.create_pool(TEAM_DSN, min_size=1, max_size=4, setup=adb._apply_tenant_guc)
    forced = False
    try:
        adb.set_active_tenant(None)
        async with adb.pool.acquire() as c:
            ta = await c.fetchval("insert into tenants(slug,name,status) values($1,'A','active') returning id", SLUG_T)
            try:  # owner (gen_user) → FORCE, чтобы подчинить RLS; panel_rw (non-bypass) → RLS уже включён
                await c.execute("alter table team_agents force row level security")
                await c.execute("alter table agent_memory force row level security")
                forced = True
            except asyncpg.InsufficientPrivilegeError:
                print("  (роль не владелец — FORCE пропущен, RLS enforced нативно)")

        adb.set_active_tenant(str(ta))
        await adb.upsert_team_agent(ta, slug="sales", name="Продажи", role_preset="mark",
                                    system_prompt="p-sales", escalation_chat_id="", escalation_topic_id=None,
                                    is_orchestrator=False, memory_enabled=False, kb_enabled=False,
                                    actor="smoke", ip=None, user_agent=None)
        await adb.upsert_team_agent(ta, slug="support", name="Поддержка", role_preset=None,
                                    system_prompt="p-support", escalation_chat_id="", escalation_topic_id=None,
                                    is_orchestrator=False, memory_enabled=False, kb_enabled=False,
                                    actor="smoke", ip=None, user_agent=None)
        rows = await adb.list_team_agents(ta)
        check("CRUD: 2 агента видны под своим тенантом", len(rows) == 2, str(len(rows)))
        ok = await adb.set_default_team_agent(ta, "sales", actor="smoke", ip=None, user_agent=None)
        check("set_default sales", ok)
        defs = [r for r in await adb.list_team_agents(ta) if r["is_default"]]
        check("ровно один is_default", len(defs) == 1 and defs[0]["slug"] == "sales")
        await adb.upsert_team_agent(ta, slug="sales", name="Продажи2", role_preset="mark",
                                    system_prompt="p2", escalation_chat_id="", escalation_topic_id=None,
                                    is_orchestrator=False, memory_enabled=False, kb_enabled=False,
                                    actor="smoke", ip=None, user_agent=None)
        check("upsert не плодит дубль", len(await adb.list_team_agents(ta)) == 2)
        await adb.disable_team_agent(ta, "support", actor="smoke", ip=None, user_agent=None)
        en = [r for r in await adb.list_team_agents(ta) if r["enabled"]]
        check("soft-delete: 1 enabled остался", len(en) == 1 and en[0]["slug"] == "sales")
        # set_channel_agent пишет ключ tenant_settings
        await adb.set_channel_agent(ta, "tg", "sales", actor="smoke", ip=None, user_agent=None)
        async with adb.pool.acquire() as c:
            v = await c.fetchval("select value from tenant_settings where tenant_id=$1 and key=$2",
                                 ta, "agent_for_channel__tg")
        check("set_channel_agent: ключ записан", v == "sales", str(v))

        # ── Э3, регресс round-trip: «открыть агента и сохранить без изменений» не должно менять
        # НИ ОДНОГО поля. Пинит правило «поле в upsert_team_agent ⇒ поле в _TEAM_AGENT_SELECT и в
        # _present_team_agent». Пока kb_enabled выпадал из SELECT, форма отдавала его пустым и
        # каждое сохранение агента молча выключало базу знаний тенанту.
        await adb.upsert_team_agent(ta, slug="rt", name="Кругорейс", role_preset="mark",
                                    system_prompt="инструкции", escalation_chat_id="-1005001",
                                    escalation_topic_id=7, is_orchestrator=True, memory_enabled=True,
                                    kb_enabled=True, actor="smoke", ip=None, user_agent=None)
        before = next(r for r in await adb.list_team_agents(ta) if r["slug"] == "rt")
        missing = [f for f in UPSERT_FIELDS if f not in before.keys()]
        check("round-trip: SELECT отдаёт все поля формы", not missing, ", ".join(missing) or "—")
        if not missing:
            # Сохраняем ровно тем, что пришло из SELECT — так делает форма, открытая и не тронутая.
            await adb.upsert_team_agent(ta, slug="rt", actor="smoke", ip=None, user_agent=None,
                                        **{f: before[f] for f in UPSERT_FIELDS})
            after = next(r for r in await adb.list_team_agents(ta) if r["slug"] == "rt")
            diff = [f"{f}: {before[f]!r}→{after[f]!r}" for f in UPSERT_FIELDS if before[f] != after[f]]
            check("round-trip: сохранение без изменений ничего не меняет", not diff, "; ".join(diff) or "—")
        # RLS: чужой тенант не видит (ctx None → 0)
        adb.set_active_tenant(None)
        async with adb.pool.acquire() as c:
            seen = await c.fetchval("select count(*) from team_agents")
        check("RLS: ctx None → 0 строк team_agents", seen == 0, str(seen))
    finally:
        try:
            adb.set_active_tenant(None)
            async with adb.pool.acquire() as c:
                if forced:
                    await c.execute("alter table team_agents no force row level security")
                    await c.execute("alter table agent_memory no force row level security")
            await _cleanup()
        finally:
            await adb.pool.close()

    if FAILS:
        print("\n".join("❌ " + f for f in FAILS))
        raise SystemExit(1)
    print("🟢 team_agents_db_smoke зелёный")


if __name__ == "__main__":
    asyncio.run(main())
