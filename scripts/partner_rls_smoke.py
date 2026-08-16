#!/usr/bin/env python3
"""Смоук изоляции партнёрских таблиц (risuy_dev):
  PARTNER_RLS_SMOKE_DSN="...risuy_dev..." PYTHONPATH=admin-panel:. \
    /Users/konstantin/Downloads/risuy-ecosystem/.venv-smoke/bin/python \
    scripts/partner_rls_smoke.py

Две фазы, и обе нужны.

ФАЗА A — временный FORCE ROW LEVEL SECURITY. Подключаемся мы ВЛАДЕЛЬЦЕМ таблиц, а
владелец при обычном ENABLE политику обходит: без FORCE проверка изоляции была бы
фикцией. Пароль panel_rw живёт только в прод-env панели, поэтому подключиться его ролью
локально нельзя — приём с FORCE в проекте уже применялся (см. цель `smoke` в Makefile).

ФАЗА B — БОЕВАЯ конфигурация (ENABLE без FORCE). Проверяем то, ради чего вообще введены
SECURITY DEFINER-функции: начисление ПЛАТФОРМЕННОМУ партнёру проходит, когда
app.tenant_id равен тенанту-плательщику. Под FORCE эта проверка дала бы ложный провал —
функции исполняются под владельцем.

Скрипт возвращает FORCE в исходное состояние в finally: боевая конфигурация — ENABLE.
"""
import asyncio
import os
import sys
from decimal import Decimal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "admin-panel"))
os.environ.setdefault("DATABASE_URL", "postgresql://x/y")
os.environ.setdefault("SESSION_SECRET", "smoke-secret-padding-0123456789abcdef")
os.environ.setdefault("ADMIN_USERNAME", "smoke")
os.environ.setdefault("ADMIN_PASSWORD_HASH", "$argon2id$v=19$m=65536,t=3,p=4$c21va2U$c21va2U")

import asyncpg  # noqa: E402

import db  # noqa: E402

DSN = os.environ.get("PARTNER_RLS_SMOKE_DSN")
if not DSN or "/risuy_dev" not in DSN.split("?")[0]:
    raise SystemExit("Задайте PARTNER_RLS_SMOKE_DSN на risuy_dev")

FAILS = []
PLATFORM = "СМОУК RLS Платформенный"
TENANTP = "СМОУК RLS Тенантский"
TNAME = "СМОУК RLS Тенант"
SRC = "33333333-3333-3333-3333-333333333333"
FORCED = ["partners", "partner_accruals", "partner_payouts"]


def check(name, cond, detail=""):
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


async def _cleanup(c):
    # Внешние ключи круговые: tenants.partner_id → partners и partners.owner_tenant_id →
    # tenants. Поэтому сперва развязываем обе стороны, потом удаляем.
    await c.execute("update tenants  set partner_id      = null where name = $1", TNAME)
    await c.execute("update partners set owner_tenant_id = null where name in ($1,$2)",
                    PLATFORM, TENANTP)
    await c.execute("""delete from partner_accruals where partner_id in
                       (select id from partners where name in ($1,$2))""", PLATFORM, TENANTP)
    await c.execute("""delete from partner_payouts where partner_id in
                       (select id from partners where name in ($1,$2))""", PLATFORM, TENANTP)
    await c.execute("delete from partners where name in ($1,$2)", PLATFORM, TENANTP)
    await c.execute("delete from tenants where name = $1", TNAME)


async def main():
    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    db.pool = pool
    try:
        async with pool.acquire() as c:
            enabled = await c.fetchval(
                "select relrowsecurity from pg_class where relname = 'partner_accruals'")
            if not enabled:
                raise SystemExit(
                    "RLS не включён — примените db/migrate_partner_money_rls.sql на risuy_dev")

            await _cleanup(c)
            plat_id = await c.fetchval(
                "insert into partners (name, ref_code) values ($1,$2) returning id",
                PLATFORM, "rlsplat1")
            tenant_id = await c.fetchval(
                "insert into tenants (name, slug, status, partner_id) "
                "values ($1,$2,'active',$3) returning id", TNAME, "rls-smoke-tenant", plat_id)
            await c.fetchval(
                "insert into partners (name, ref_code, owner_tenant_id) values ($1,$2,$3) returning id",
                TENANTP, "rlsten01", tenant_id)

            print("ФАЗА A. Политика изолирует (временный FORCE):")
            for t in FORCED:
                await c.execute(f"alter table {t} force row level security")

            await c.execute("select set_config('app.tenant_id', $1, false)", str(tenant_id))
            seen = await c.fetch("select id, name from partners where name in ($1,$2)",
                                 PLATFORM, TENANTP)
            names = {r["name"] for r in seen}
            check("тенант видит своего партнёра", TENANTP in names, str(names))
            check("тенант НЕ видит платформенного", PLATFORM not in names, str(names))

            await c.execute("select set_config('app.tenant_id', '', false)")
            seen = await c.fetch("select id, name from partners where name in ($1,$2)",
                                 PLATFORM, TENANTP)
            names = {r["name"] for r in seen}
            check("платформа видит своего партнёра", PLATFORM in names, str(names))
            check("платформа НЕ видит тенантского", TENANTP not in names, str(names))

            try:
                await c.execute("select set_config('app.tenant_id', '', false)")
                await c.fetchval("select count(*) from partner_accruals")
                check("пустой app.tenant_id не даёт 22P02", True)
            except asyncpg.PostgresError as e:
                check("пустой app.tenant_id не даёт 22P02", False, str(e))

            try:
                await c.execute("select set_config('app.tenant_id', $1, false)", str(tenant_id))
                await c.execute(
                    "insert into partner_payouts (partner_id, owner_tenant_id, amount_rub, created_by) "
                    "values ($1, null, 100, 'smoke')", plat_id)
                check("запись чужого контура отбита with check", False, "вставка прошла")
            except asyncpg.PostgresError:
                check("запись чужого контура отбита with check", True)

            print("ФАЗА B. Боевая конфигурация: начисление платформенному партнёру проходит:")
            for t in FORCED:
                await c.execute(f"alter table {t} no force row level security")
            await c.execute("select set_config('app.tenant_id', $1, false)", str(tenant_id))
            async with c.transaction():
                ids = await db.accrue_for_payment(
                    c, source_kind="service_invoice", source_id=SRC,
                    client_kind="tenant", client_id=tenant_id, amount_rub=Decimal("7500"))
            check("начисление создано при GUC = тенант-плательщик", len(ids) == 1, f"строк={len(ids)}")
            amount = await c.fetchval(
                "select amount_rub from partner_accruals where partner_id = $1", plat_id)
            check("сумма 1500.00", amount == Decimal("1500.00"), str(amount))

            await c.execute("select set_config('app.tenant_id', '', false)")
    finally:
        # Боевая конфигурация — ENABLE без FORCE. Оставить FORCE значило бы сломать
        # SECURITY DEFINER-функции на этой базе.
        async with pool.acquire() as c:
            for t in FORCED:
                await c.execute(f"alter table {t} no force row level security")
            await c.execute("select set_config('app.tenant_id', '', false)")
            await _cleanup(c)
        await pool.close()

    print()
    if FAILS:
        print(f"ПРОВАЛЕНО: {len(FAILS)} — {FAILS}")
        sys.exit(1)
    print("ВСЁ ЗЕЛЁНОЕ")


asyncio.run(main())
