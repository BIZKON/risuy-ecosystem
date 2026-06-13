#!/usr/bin/env python3
"""Смоук харденинга №2 — RLS на leads/messages/outbox на risuy_dev.

Проверяет, что центральный tenant-хук панели (admin-panel/db.py: setup пула ставит
app.tenant_id из contextvar активного тенанта) реально ИЗОЛИРУЕТ строки под RLS.

Гонится как gen_user (owner). Owner обычным ENABLE RLS обходит, поэтому на время теста
включаем FORCE ROW LEVEL SECURITY (owner тоже становится субъектом политики — имитируем
panel_rw), а в finally — снимаем FORCE (возвращаем dev к прод-состоянию ENABLE-не-FORCE).

  1. ctx тенанта A → видны ТОЛЬКО лиды/сообщения/outbox тенанта A; ctx B → только B;
  2. ctx None (как у фонового пути без сессии) → 0 строк (RLS deny, без ошибки касту);
  3. UPDATE под ctx A не может тронуть лид тенанта B (RLS using прячет чужую строку).

Тестовые smoke-rls-* удаляются в конце. На прод не запускать.

Запуск: RLS_SMOKE_DSN="postgresql://<owner>@<host>:5432/risuy_dev?sslmode=require" \
        PYTHONPATH=. python3 scripts/rls_leads_messages_smoke.py
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

DSN = os.environ.get("RLS_SMOKE_DSN")
if not DSN or "/risuy_dev" not in DSN.split("?")[0]:
    raise SystemExit("Задайте RLS_SMOKE_DSN на risuy_dev (FORCE RLS временно + delete тестовых строк).")

TG_A, TG_B = 990444001, 990444002
FORCE_TABLES = ("leads", "messages", "outbox")
FAILS: list[str] = []


def check(name: str, cond: bool, detail="") -> None:
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail != "" else ""))
    if not cond:
        FAILS.append(name)


async def _cleanup(c):
    await c.execute("delete from outbox where tg_user_id = any($1::bigint[])", [TG_A, TG_B])
    await c.execute("delete from messages where tg_user_id = any($1::bigint[])", [TG_A, TG_B])
    await c.execute("delete from leads where tg_user_id = any($1::bigint[])", [TG_A, TG_B])
    await c.execute("delete from tenants where slug like 'smoke-rls-%'")


async def _count(table: str, tg: int) -> int:
    """Считает строки table по tg через ПУЛ (с setup-хуком) — т.е. под текущим app.tenant_id."""
    async with db.pool.acquire() as c:
        return int(await c.fetchval(f"select count(*) from {table} where tg_user_id = $1", tg) or 0)


async def main() -> None:
    db.pool = await asyncpg.create_pool(DSN, min_size=1, max_size=4, setup=db._apply_tenant_guc)
    forced = []
    try:
        # ── сидирование (contextvar None → setup no-op; RLS ещё ENABLE-не-FORCE → owner пишет) ──
        db.set_active_tenant(None)
        async with db.pool.acquire() as c:
            await _cleanup(c)
            ta = await c.fetchval("insert into tenants(slug,name,status) values('smoke-rls-a','A','active') returning id")
            tb = await c.fetchval("insert into tenants(slug,name,status) values('smoke-rls-b','B','active') returning id")
            la = await c.fetchval(
                "insert into leads(tg_user_id,messenger,source,status,name,tenant_id) "
                "values($1,'tg','x','new','Лид A',$2) returning id", TG_A, ta)
            lb = await c.fetchval(
                "insert into leads(tg_user_id,messenger,source,status,name,tenant_id) "
                "values($1,'tg','x','new','Лид B',$2) returning id", TG_B, tb)
            for lead, tg, t in ((la, TG_A, ta), (lb, TG_B, tb)):
                await c.execute(
                    "insert into messages(lead_id,tg_user_id,direction,kind,text,tenant_id) "
                    "values($1,$2,'in','text','привет',$3)", lead, tg, t)
                await c.execute(
                    "insert into outbox(lead_id,tg_user_id,kind,text,status,created_by,tenant_id) "
                    "values($1,$2,'text','ответ','queued','smoke',$3)", lead, tg, t)
            # FORCE — owner тоже под политикой (имитация panel_rw)
            for tbl in FORCE_TABLES:
                await c.execute(f"alter table {tbl} force row level security")
                forced.append(tbl)

        # ── 1. Изоляция чтения через центральный хук ───────────────────────────
        print("1. Изоляция leads/messages/outbox по тенанту (через setup-хук):")
        db.set_active_tenant(ta)
        a_leads, a_msg, a_out = await _count("leads", TG_A), await _count("messages", TG_A), await _count("outbox", TG_A)
        a_sees_b = await _count("leads", TG_B)
        db.set_active_tenant(tb)
        b_leads = await _count("leads", TG_B)
        b_sees_a = await _count("leads", TG_A)
        check("ctx A видит свой лид/сообщение/outbox", a_leads == 1 and a_msg == 1 and a_out == 1,
              f"leads={a_leads} msg={a_msg} out={a_out}")
        check("ctx A НЕ видит лид B (RLS)", a_sees_b == 0, f"факт {a_sees_b}")
        check("ctx B видит свой лид", b_leads == 1, f"факт {b_leads}")
        check("ctx B НЕ видит лид A (RLS)", b_sees_a == 0, f"факт {b_sees_a}")

        # ── 2. Фоновый путь без тенанта → 0 строк (без ошибки касту) ───────────
        print("2. ctx None (фон/без сессии) → 0 строк, без ошибки:")
        db.set_active_tenant(None)
        none_leads = await _count("leads", TG_A)
        check("ctx None → 0 лидов (RLS deny, NULL-каст безопасен)", none_leads == 0, f"факт {none_leads}")

        # ── 3. UPDATE не пересекает тенанта ────────────────────────────────────
        print("3. UPDATE под ctx A не трогает лид B:")
        db.set_active_tenant(ta)
        async with db.pool.acquire() as c:
            res = await c.execute("update leads set name = 'ВЗЛОМ' where tg_user_id = $1", TG_B)
        db.set_active_tenant(tb)
        async with db.pool.acquire() as c:
            nm_b = await c.fetchval("select name from leads where tg_user_id = $1", TG_B)
        check("UPDATE ctx A по лиду B затронул 0 строк", res.endswith(" 0"), res)
        check("имя лида B не изменилось", nm_b == "Лид B", repr(nm_b))

    finally:
        # снять FORCE и почистить (под owner — но FORCE ещё активен → ставим тенант для DML;
        # ALTER (DDL) RLS не трогает, чистка delete — нужен bypass: сначала снимаем FORCE).
        async with db.pool.acquire() as c:
            for tbl in forced:
                await c.execute(f"alter table {tbl} no force row level security")
            db.set_active_tenant(None)
        async with db.pool.acquire() as c:  # теперь owner снова обходит RLS (ENABLE-не-FORCE) → чистим
            await _cleanup(c)
        await db.pool.close()

    print()
    if FAILS:
        print(f"❌ ПРОВАЛЫ ({len(FAILS)}): " + ", ".join(FAILS))
        sys.exit(1)
    print("✅ RLS leads/messages/outbox smoke — все проверки зелёные")


if __name__ == "__main__":
    asyncio.run(main())
