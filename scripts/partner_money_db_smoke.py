#!/usr/bin/env python3
"""DB-смоук начислений (risuy_dev):
  PARTNER_MONEY_SMOKE_DSN="...risuy_dev..." PYTHONPATH=admin-panel:. \
    /Users/konstantin/Downloads/risuy-ecosystem/.venv-smoke/bin/python \
    scripts/partner_money_db_smoke.py
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

DSN = os.environ.get("PARTNER_MONEY_SMOKE_DSN")
if not DSN or "/risuy_dev" not in DSN.split("?")[0]:
    raise SystemExit("Задайте PARTNER_MONEY_SMOKE_DSN на risuy_dev")

FAILS = []
PSELLER = "СМОУК Продавец"
PMENTOR = "СМОУК Наставник"
TNAME = "СМОУК Тенант-плательщик"
SRC1 = "11111111-1111-1111-1111-111111111111"
SRC2 = "22222222-2222-2222-2222-222222222222"


def check(name, cond, detail=""):
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


async def _cleanup(c):
    await c.execute("""delete from partner_accruals where partner_id in
                       (select id from partners where name in ($1,$2))""", PSELLER, PMENTOR)
    await c.execute("""delete from partner_payouts where partner_id in
                       (select id from partners where name in ($1,$2))""", PSELLER, PMENTOR)
    await c.execute("delete from tenants where name = $1", TNAME)
    await c.execute("update partners set parent_id = null where name in ($1,$2)", PSELLER, PMENTOR)
    await c.execute("delete from partners where name in ($1,$2)", PSELLER, PMENTOR)


async def main():
    db.pool = await asyncpg.create_pool(DSN, min_size=1, max_size=8)
    try:
        async with db.pool.acquire() as c:
            await _cleanup(c)
            mentor_id = await c.fetchval(
                "insert into partners (name, ref_code) values ($1, $2) returning id",
                PMENTOR, "smokem01")
            seller_id = await c.fetchval(
                "insert into partners (name, ref_code, parent_id) values ($1,$2,$3) returning id",
                PSELLER, "smokes01", mentor_id)
            tenant_id = await c.fetchval(
                "insert into tenants (name, slug, status, partner_id) "
                "values ($1,$2,'active',$3) returning id", TNAME, "smoke-payer", seller_id)

            print("1. Первый платёж начисляет продавцу и наставнику:")
            async with c.transaction():
                ids = await db.accrue_for_payment(
                    c, source_kind="service_invoice", source_id=SRC1,
                    client_kind="tenant", client_id=tenant_id, amount_rub=Decimal("7500"))
            check("создано две строки", len(ids) == 2, f"строк={len(ids)}")
            rows = await c.fetch(
                "select level, amount_rub, reason, rate_percent from partner_accruals "
                "where client_id = $1 order by level", tenant_id)
            check("продавцу 1500.00", rows[0]["amount_rub"] == Decimal("1500.00"),
                  str(rows[0]["amount_rub"]))
            check("наставнику 375.00", rows[1]["amount_rub"] == Decimal("375.00"),
                  str(rows[1]["amount_rub"]))
            check("ставка скопирована в строку", rows[0]["rate_percent"] == Decimal("20.00"),
                  str(rows[0]["rate_percent"]))

            print("2. Повторный вебхук того же платежа не начисляет дважды:")
            async with c.transaction():
                again = await db.accrue_for_payment(
                    c, source_kind="service_invoice", source_id=SRC1,
                    client_kind="tenant", client_id=tenant_id, amount_rub=Decimal("7500"))
            total = await c.fetchval(
                "select count(*) from partner_accruals where client_id = $1", tenant_id)
            check("новых строк нет", not again, f"вернул={again}")
            check("в базе по-прежнему 2", total == 2, f"строк={total}")

            print("3. ВТОРОЙ платёж того же клиента не начисляет ничего (первый платёж):")
            async with c.transaction():
                second = await db.accrue_for_payment(
                    c, source_kind="service_invoice", source_id=SRC2,
                    client_kind="tenant", client_id=tenant_id, amount_rub=Decimal("7500"))
            total = await c.fetchval(
                "select count(*) from partner_accruals where client_id = $1", tenant_id)
            check("вернул пусто", not second, f"вернул={second}")
            check("в базе по-прежнему 2", total == 2, f"строк={total}")

            print("4. Гонка: 20 параллельных начислений дают ровно одну пару строк:")
            await c.execute("delete from partner_accruals where client_id = $1", tenant_id)

            async def one():
                async with db.pool.acquire() as cc:
                    async with cc.transaction():
                        return await db.accrue_for_payment(
                            cc, source_kind="service_invoice", source_id=SRC1,
                            client_kind="tenant", client_id=tenant_id, amount_rub=Decimal("7500"))

            got = await asyncio.gather(*[one() for _ in range(20)], return_exceptions=True)
            errs = [g for g in got if isinstance(g, Exception)]
            total = await c.fetchval(
                "select count(*) from partner_accruals where client_id = $1", tenant_id)
            check("исключений нет", not errs, str(errs[:1]))
            check("ровно 2 строки после 20 попыток", total == 2, f"строк={total}")

            print("5. Отключённый партнёр не получает ничего:")
            await c.execute("delete from partner_accruals where client_id = $1", tenant_id)
            await c.execute("update partners set status='disabled' where id=$1", seller_id)
            async with c.transaction():
                off = await db.accrue_for_payment(
                    c, source_kind="service_invoice", source_id=SRC1,
                    client_kind="tenant", client_id=tenant_id, amount_rub=Decimal("7500"))
            check("вернул пусто", not off, f"вернул={off}")
            await c.execute("update partners set status='active' where id=$1", seller_id)

            print("6. Клиент без партнёра не порождает начислений:")
            await c.execute("update tenants set partner_id = null where id = $1", tenant_id)
            async with c.transaction():
                none_rows = await db.accrue_for_payment(
                    c, source_kind="service_invoice", source_id=SRC1,
                    client_kind="tenant", client_id=tenant_id, amount_rub=Decimal("7500"))
            check("вернул пусто", not none_rows, f"вернул={none_rows}")

            await _cleanup(c)
    finally:
        await db.pool.close()

    print()
    if FAILS:
        print(f"ПРОВАЛЕНО: {len(FAILS)} — {FAILS}")
        sys.exit(1)
    print("ВСЁ ЗЕЛЁНОЕ")


asyncio.run(main())
