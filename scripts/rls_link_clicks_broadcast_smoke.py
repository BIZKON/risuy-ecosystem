#!/usr/bin/env python3
"""Смоук follow-up харденинга №2 — RLS на link_clicks / broadcast_recipients на risuy_dev.

Проверяет, что центральный tenant-хук панели (admin-panel/db.py: setup пула ставит
app.tenant_id из contextvar активного тенанта) реально ИЗОЛИРУЕТ строки трекинга под RLS.

Гонится как gen_user (owner). Owner обычным ENABLE RLS обходит, поэтому на время теста
включаем FORCE ROW LEVEL SECURITY ТОЛЬКО на link_clicks/broadcast_recipients (owner тоже
становится субъектом политики — имитируем panel_rw), а в finally — снимаем FORCE
(возвращаем dev к прод-состоянию ENABLE-не-FORCE). Остальные таблицы цепочки (tenants/
leads/broadcasts/link_tokens) НЕ форсим — owner свободно их сидирует/чистит.

  1. ctx тенанта A → видны ТОЛЬКО клики/адресаты тенанта A; ctx B → только B;
  2. ctx None (фоновый путь без сессии) → 0 строк (RLS deny, без ошибки касту '');
  3. UPDATE под ctx A не может тронуть адресата тенанта B (RLS using прячет чужую строку).

Тестовые smoke-rls2-* удаляются в конце. На прод не запускать.

Запуск: RLS_SMOKE_DSN="postgresql://<owner>@<host>:5432/risuy_dev?sslmode=require" \
        PYTHONPATH=. python3 scripts/rls_link_clicks_broadcast_smoke.py
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

TG_A, TG_B = 990444011, 990444012
TOK_A, TOK_B = "smoke-rls2-a", "smoke-rls2-b"
FORCE_TABLES = ("link_clicks", "broadcast_recipients")
FAILS: list[str] = []


def check(name: str, cond: bool, detail="") -> None:
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail != "" else ""))
    if not cond:
        FAILS.append(name)


async def _cleanup(c):
    await c.execute("delete from link_clicks          where token like 'smoke-rls2-%'")
    await c.execute("delete from broadcast_recipients where tg_user_id = any($1::bigint[])", [TG_A, TG_B])
    await c.execute("delete from link_tokens          where token like 'smoke-rls2-%'")
    await c.execute("delete from broadcasts           where created_by = 'smoke-rls2'")
    await c.execute("delete from leads                where tg_user_id = any($1::bigint[])", [TG_A, TG_B])
    await c.execute("delete from tenants              where slug like 'smoke-rls2-%'")


async def _count(table: str, col: str, val) -> int:
    """Считает строки table по col=val через ПУЛ (с setup-хуком) — под текущим app.tenant_id."""
    async with db.pool.acquire() as c:
        return int(await c.fetchval(f"select count(*) from {table} where {col} = $1", val) or 0)


async def main() -> None:
    db.pool = await asyncpg.create_pool(DSN, min_size=1, max_size=4, setup=db._apply_tenant_guc)
    forced = []
    try:
        # ── сидирование (contextvar None → setup no-op; RLS ещё ENABLE-не-FORCE → owner пишет) ──
        db.set_active_tenant(None)
        async with db.pool.acquire() as c:
            await _cleanup(c)
            ta = await c.fetchval("insert into tenants(slug,name,status) values('smoke-rls2-a','A','active') returning id")
            tb = await c.fetchval("insert into tenants(slug,name,status) values('smoke-rls2-b','B','active') returning id")
            for tg, tok, t in ((TG_A, TOK_A, ta), (TG_B, TOK_B, tb)):
                lead = await c.fetchval(
                    "insert into leads(tg_user_id,messenger,source,status,name,tenant_id) "
                    "values($1,'tg','x','new','Лид',$2) returning id", tg, t)
                bc = await c.fetchval(
                    "insert into broadcasts(body_template,created_by,tenant_id) "
                    "values('текст {link}','smoke-rls2',$1) returning id", t)
                await c.execute(
                    "insert into broadcast_recipients(broadcast_id,lead_id,tg_user_id,tenant_id) "
                    "values($1,$2,$3,$4)", bc, lead, tg, t)
                await c.execute(
                    "insert into link_tokens(token,target_url,broadcast_id,lead_id,tenant_id) "
                    "values($1,'https://example.org',$2,$3,$4)", tok, bc, lead, t)
                await c.execute(
                    "insert into link_clicks(token,broadcast_id,lead_id,tenant_id) "
                    "values($1,$2,$3,$4)", tok, bc, lead, t)
            # FORCE — owner тоже под политикой (имитация panel_rw) — ТОЛЬКО на двух целевых
            for tbl in FORCE_TABLES:
                await c.execute(f"alter table {tbl} force row level security")
                forced.append(tbl)

        # ── 1. Изоляция чтения через центральный хук ───────────────────────────
        print("1. Изоляция link_clicks/broadcast_recipients по тенанту (через setup-хук):")
        db.set_active_tenant(ta)
        a_click = await _count("link_clicks", "token", TOK_A)
        a_recip = await _count("broadcast_recipients", "tg_user_id", TG_A)
        a_sees_b_click = await _count("link_clicks", "token", TOK_B)
        a_sees_b_recip = await _count("broadcast_recipients", "tg_user_id", TG_B)
        db.set_active_tenant(tb)
        b_click = await _count("link_clicks", "token", TOK_B)
        b_recip = await _count("broadcast_recipients", "tg_user_id", TG_B)
        b_sees_a_click = await _count("link_clicks", "token", TOK_A)
        check("ctx A видит свой клик и адресата", a_click == 1 and a_recip == 1, f"click={a_click} recip={a_recip}")
        check("ctx A НЕ видит клик/адресата B (RLS)", a_sees_b_click == 0 and a_sees_b_recip == 0,
              f"click_b={a_sees_b_click} recip_b={a_sees_b_recip}")
        check("ctx B видит свой клик и адресата", b_click == 1 and b_recip == 1, f"click={b_click} recip={b_recip}")
        check("ctx B НЕ видит клик A (RLS)", b_sees_a_click == 0, f"факт {b_sees_a_click}")

        # ── 2. Фоновый путь без тенанта → 0 строк (без ошибки касту) ───────────
        print("2. ctx None (фон/без сессии) → 0 строк, без ошибки касту '':")
        db.set_active_tenant(None)
        none_click = await _count("link_clicks", "token", TOK_A)
        none_recip = await _count("broadcast_recipients", "tg_user_id", TG_A)
        check("ctx None → 0 кликов/адресатов (RLS deny, NULL-каст безопасен)",
              none_click == 0 and none_recip == 0, f"click={none_click} recip={none_recip}")

        # ── 3. UPDATE не пересекает тенанта ────────────────────────────────────
        print("3. UPDATE под ctx A не трогает адресата B:")
        db.set_active_tenant(ta)
        async with db.pool.acquire() as c:
            res = await c.execute("update broadcast_recipients set status = 'failed' where tg_user_id = $1", TG_B)
        db.set_active_tenant(tb)
        async with db.pool.acquire() as c:
            st_b = await c.fetchval("select status from broadcast_recipients where tg_user_id = $1", TG_B)
        check("UPDATE ctx A по адресату B затронул 0 строк", res.endswith(" 0"), res)
        check("статус адресата B не изменился", st_b == "pending", repr(st_b))

    finally:
        # снять FORCE, затем почистить под owner (ENABLE-не-FORCE → owner снова обходит RLS).
        async with db.pool.acquire() as c:
            for tbl in forced:
                await c.execute(f"alter table {tbl} no force row level security")
            db.set_active_tenant(None)
        async with db.pool.acquire() as c:
            await _cleanup(c)
        await db.pool.close()

    print()
    if FAILS:
        print(f"❌ ПРОВАЛЫ ({len(FAILS)}): " + ", ".join(FAILS))
        sys.exit(1)
    print("✅ RLS link_clicks/broadcast_recipients smoke — все проверки зелёные")


if __name__ == "__main__":
    asyncio.run(main())
