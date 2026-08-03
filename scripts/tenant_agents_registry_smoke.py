#!/usr/bin/env python3
"""БД-смоук реестра `tenant_agents` на risuy_dev (дыра B этапа 0).

Реестр — ЕДИНСТВЕННЫЙ вход метеринга cloud-ai: снапшот-воркер бота метрирует только тех
агентов, что перечислены в `tenant_agents` (bot-telegram/metering_worker.py:94,105).
До этой правки в панели не было ни одной записи в реестр — агента создавали, а строку
клали руками SQL-ом (db/schema_metering_w3.sql:31). То есть расход платящего клиента
не попадал в счёт вовсе.

Смоук проверяет регистратор `db.register_tenant_agent` / `db.unregister_tenant_agent`:
принадлежность, идемпотентность, отказ при чужом агенте, поведение под RLS и то, что
запрос воркера действительно видит зарегистрированного агента.

Запуск:
  TENANT_AGENTS_DSN="postgresql://<owner>:<pw>@81.31.246.136:5432/risuy_dev?sslmode=require" \
  PYTHONPATH=admin-panel:. ./.venv/bin/python scripts/tenant_agents_registry_smoke.py

⚠️ Только risuy_dev: смоук создаёт и удаляет тенантов и строки реестра.
"""
import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "admin-panel"))
DSN = os.environ.get("TENANT_AGENTS_DSN") or os.environ.get("DATABASE_URL", "")
os.environ.setdefault("DATABASE_URL", DSN or "postgresql://x/y")
os.environ.setdefault("SESSION_SECRET", "smoke-session-secret-padding-0123456789-abcdef")
os.environ.setdefault("ADMIN_USERNAME", "smoke")
os.environ.setdefault("ADMIN_PASSWORD_HASH",
                      "$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHQ$aGFzaHN0dWI")
import asyncpg  # noqa: E402
import db as adb  # noqa: E402  (admin-panel/db.py)

assert DSN, "задайте TENANT_AGENTS_DSN на risuy_dev"
assert "/risuy_dev" in DSN.split("?")[0], "только risuy_dev — смоук пишет и удаляет строки"

SLUG_A = "smoke-ta-a"
SLUG_B = "smoke-ta-b"
AGENT = 999999101          # заведомо несуществующий в аккаунте Timeweb номер
ACTOR = {"actor": "tenant_agents_registry_smoke.py", "ip": None, "user_agent": None}
FAILS: list[str] = []


def check(name, cond, detail=""):
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


async def _audit_count(action: str) -> int:
    """Записей аудита с данным action. Читается вне тенант-контекста (admin_audit без RLS)."""
    adb.set_active_tenant(None)
    async with adb.pool.acquire() as c:
        return int(await c.fetchval(
            "select count(*) from admin_audit where actor = $1 and action = $2",
            ACTOR["actor"], action) or 0)


async def _rows_for(tenant_id) -> int:
    adb.set_active_tenant(str(tenant_id))
    async with adb.pool.acquire() as c:
        return int(await c.fetchval(
            "select count(*) from tenant_agents where agent_id = $1", AGENT) or 0)


async def _cleanup():
    adb.set_active_tenant(None)
    async with adb.pool.acquire() as c:
        await c.execute("delete from tenant_agents where agent_id = $1", AGENT)
        await c.execute("delete from admin_audit where actor = $1", ACTOR["actor"])
        await c.execute("delete from tenants where slug = any($1::text[])", [SLUG_A, SLUG_B])


async def main():
    adb.pool = await asyncpg.create_pool(DSN, min_size=1, max_size=4, setup=adb._apply_tenant_guc)
    forced = False
    try:
        await _cleanup()
        adb.set_active_tenant(None)
        async with adb.pool.acquire() as c:
            tid_a = await c.fetchval(
                "insert into tenants(slug,name,status) values($1,'Тенант A','active') returning id", SLUG_A)
            tid_b = await c.fetchval(
                "insert into tenants(slug,name,status) values($1,'Тенант B','active') returning id", SLUG_B)
            # owner (gen_user) обходит RLS → форсим, чтобы политика реально проверялась.
            # Под panel_rw (non-bypass) RLS и так включён, ALTER просто не пройдёт по правам.
            try:
                await c.execute("alter table tenant_agents force row level security")
                forced = True
            except asyncpg.InsufficientPrivilegeError:
                pass

        print("1. Регистрация агента за тенантом A:")
        ok = await adb.register_tenant_agent(
            tid_a, AGENT, access_id="acc-smoke-a", note="смоук", **ACTOR)
        check("register вернул True", ok is True, repr(ok))
        check("под тенантом A строка ровно одна", await _rows_for(tid_a) == 1)
        check("аудит tenant_agent_register записан", await _audit_count("tenant_agent_register") == 1)

        print("2. Идемпотентность (тот же тенант, тот же агент):")
        ok2 = await adb.register_tenant_agent(tid_a, AGENT, access_id="acc-smoke-a", **ACTOR)
        check("повтор вернул True", ok2 is True, repr(ok2))
        check("строк по-прежнему одна", await _rows_for(tid_a) == 1)
        check("аудит НЕ вырос (повтор не событие)",
              await _audit_count("tenant_agent_register") == 1)

        print("3. Чужой агент — отказ, а не тихая перепривязка:")
        ok3 = await adb.register_tenant_agent(tid_b, AGENT, access_id="acc-smoke-b", **ACTOR)
        check("register вернул False", ok3 is False, repr(ok3))
        check("строка осталась за тенантом A", await _rows_for(tid_a) == 1)
        check("у тенанта B строки нет", await _rows_for(tid_b) == 0)
        check("ровно один аудит tenant_agent_conflict",
              await _audit_count("tenant_agent_conflict") == 1)

        print("4. Битые аргументы — False, без исключения и без записей:")
        before = await _audit_count("tenant_agent_register")
        r_none = await adb.register_tenant_agent(tid_a, None, **ACTOR)
        r_empty = await adb.register_tenant_agent(tid_a, "", **ACTOR)
        r_txt = await adb.register_tenant_agent(tid_a, "не-число", **ACTOR)
        r_notid = await adb.register_tenant_agent(None, AGENT, **ACTOR)
        check("agent_id=None → False", r_none is False, repr(r_none))
        check("agent_id='' → False", r_empty is False, repr(r_empty))
        check("agent_id='не-число' → False", r_txt is False, repr(r_txt))
        check("tenant_id=None → False (не исключение)", r_notid is False, repr(r_notid))
        check("аудит не вырос от битых аргументов",
              await _audit_count("tenant_agent_register") == before)

        print("5. RLS: чужой тенант не видит строку (и не падает):")
        try:
            seen_b = await _rows_for(tid_b)
            check("под тенантом B видно 0 строк без исключения", seen_b == 0, str(seen_b))
        except Exception as e:  # noqa: BLE001
            check("под тенантом B видно 0 строк без исключения", False, f"{type(e).__name__}: {e}")

        print("6. Ловушка пустого GUC (политика без nullif падает 22P02):")
        # Политика в db/schema_metering_w3.sql кастует current_setting без nullif: пустая строка
        # после RESET ALL даёт ''::uuid → InvalidTextRepresentationError ПОСРЕДИ транзакции,
        # а не «0 строк». Соседние политики (schema_team_agents.sql) сделаны через nullif.
        empty_guc_ok = None
        async with adb.pool.acquire() as c:
            async with c.transaction():
                await c.execute("select set_config('app.tenant_id', '', true)")
                try:
                    await c.fetchval("select count(*) from tenant_agents")
                    empty_guc_ok = True
                except asyncpg.InvalidTextRepresentationError:
                    empty_guc_ok = False
        if empty_guc_ok:
            check("пустой app.tenant_id → 0 строк (миграция nullif применена)", True)
        else:
            print("  ⚠️  пустой app.tenant_id роняет запрос (22P02) — ожидаемо ДО применения "
                  "db/migrate_tenant_agents_rls_nullif.sql; это не FAIL смоука, а его повод")

        print("7. Запрос снапшот-воркера видит зарегистрированного агента:")
        # Воркер ходит СВОИМ соединением под owner-DSN и RLS обходит (bot-telegram/metering_worker.py:94).
        # ⚠️ На время проверки снимаем force: он подчиняет политике даже владельца таблицы, то есть
        # воспроизводил бы условия, которых у воркера на проде нет, и давал бы ложный FAIL.
        raw = await asyncpg.connect(DSN)
        try:
            if forced:
                await raw.execute("alter table tenant_agents no force row level security")
            rows = await raw.fetch("select agent_id, tenant_id from tenant_agents where agent_id = $1", AGENT)
            check("агент виден запросом воркера", len(rows) == 1, f"строк={len(rows)}")
            check("привязан к тенанту A", bool(rows) and rows[0]["tenant_id"] == tid_a)
        finally:
            if forced:
                await raw.execute("alter table tenant_agents force row level security")
            await raw.close()

        print("8. Отвязка (перепривязка возможна только явно):")
        un = await adb.unregister_tenant_agent(tid_a, AGENT, **ACTOR)
        check("unregister вернул True", un is True, repr(un))
        check("строки больше нет", await _rows_for(tid_a) == 0)
        check("аудит tenant_agent_unregister записан",
              await _audit_count("tenant_agent_unregister") == 1)
        un2 = await adb.unregister_tenant_agent(tid_a, AGENT, **ACTOR)
        check("повторная отвязка → False", un2 is False, repr(un2))
        check("аудит отвязки не вырос", await _audit_count("tenant_agent_unregister") == 1)

        print("9. После отвязки агента можно закрепить за другим тенантом:")
        ok9 = await adb.register_tenant_agent(tid_b, AGENT, access_id="acc-smoke-b", **ACTOR)
        check("register за тенантом B вернул True", ok9 is True, repr(ok9))
        check("строка теперь у тенанта B", await _rows_for(tid_b) == 1)
    finally:
        try:
            if forced:
                adb.set_active_tenant(None)
                async with adb.pool.acquire() as c:
                    await c.execute("alter table tenant_agents no force row level security")
        except Exception:  # noqa: BLE001 — уборка не должна маскировать результат
            pass
        await _cleanup()
        await adb.pool.close()

    print()
    if FAILS:
        print(f"❌ ПРОВАЛЫ ({len(FAILS)}): " + "; ".join(FAILS))
        sys.exit(1)
    print("🟢 tenant_agents_registry_smoke зелёный")


if __name__ == "__main__":
    asyncio.run(main())
