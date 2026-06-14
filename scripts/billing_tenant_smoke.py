#!/usr/bin/env python3
"""Смоук tenant-изоляции биллинга подписки (service_invoices) на risuy_dev.

Проверяет, что после tenant_id + RLS на service_invoices:
  1. create_period_invoice пишет tenant_id; счёт виден ТОЛЬКО своему тенанту;
  2. mark_service_invoice_paid_by_payment (путь ВЕБХУКА, без сессии) ставит app.tenant_id
     сам и отмечает счёт оплаченным под RLS;
  3. RLS-изоляция чтения: ctx A видит свой счёт, ctx B — НЕ видит счёт A (get_latest_paid_invoice
     и list_service_invoices); ctx None → пусто;
  4. RLS with_check: под ctx A нельзя вставить счёт с tenant_id = B;
  5. service_revenue_total СКАНИТ по всем тенантам (платформенная выручка) — НЕ зависит от ctx;
  6. tenant_id обязателен (create_period_invoice/mark_..._paid → ValueError при None);
  7. per-tenant флаг отмены: отмена тенанта A не трогает B.

Гонится как gen_user (owner). Owner ENABLE-RLS обходит → на время теста FORCE RLS на
service_invoices (owner становится субъектом политики, имитируем panel_rw); в finally — снять FORCE.
Тестовые smoke-bill-* удаляются. На прод НЕ запускать.

Запуск: BILLING_SMOKE_DSN="postgresql://<owner>@<host>:5432/risuy_dev?sslmode=require" \
        PYTHONPATH=. ./.venv-smoke/bin/python scripts/billing_tenant_smoke.py
"""
import asyncio
import os
import sys
from datetime import date
from decimal import Decimal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "admin-panel"))
os.environ.setdefault("DATABASE_URL", "postgresql://x/y")
os.environ.setdefault("SESSION_SECRET", "smoke-secret-only-for-import-aaaaaaaa")
os.environ.setdefault("ADMIN_USERNAME", "smoke")
os.environ.setdefault("ADMIN_PASSWORD_HASH", "$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHQ$aGFzaHN0dWI")

import asyncpg  # noqa: E402
import db        # noqa: E402  (admin-panel/db.py)

DSN = os.environ.get("BILLING_SMOKE_DSN")
if not DSN or "/risuy_dev" not in DSN.split("?")[0]:
    raise SystemExit("Задайте BILLING_SMOKE_DSN на risuy_dev (FORCE RLS временно + delete тестовых строк).")

AMOUNT_A = Decimal("3750.00")
FAILS: list[str] = []


def check(name: str, cond: bool, detail="") -> None:
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail != "" else ""))
    if not cond:
        FAILS.append(name)


async def _cleanup(c):
    await c.execute(
        "delete from service_invoices where tenant_id in "
        "(select id from tenants where slug like 'smoke-bill-%')")
    await c.execute("delete from app_settings where key like $1",
                    f"{db.config.SERVICE_CANCEL_SETTING_KEY}:%")
    await c.execute("delete from tenants where slug like 'smoke-bill-%'")


async def main() -> None:
    db.pool = await asyncpg.create_pool(DSN, min_size=1, max_size=4, setup=db._apply_tenant_guc)
    forced = False
    ta = tb = None
    try:
        # ── сидирование тенантов (ctx None; RLS ещё ENABLE-не-FORCE → owner пишет) ──
        db.set_active_tenant(None)
        async with db.pool.acquire() as c:
            await _cleanup(c)
            ta = await c.fetchval("insert into tenants(slug,name,status) values('smoke-bill-a','A','active') returning id")
            tb = await c.fetchval("insert into tenants(slug,name,status) values('smoke-bill-b','B','active') returning id")
            await c.execute("alter table service_invoices force row level security")
            forced = True

        # ── 1. create_period_invoice пишет tenant_id (self-set app.tenant_id) ──
        print("1. создание счёта с tenant_id:")
        iid = await db.create_period_invoice(
            tenant_id=ta, period_start=date(2026, 6, 1), period_end=date(2026, 7, 1),
            plan_key="econom", plan_name="Эконом", quota=500, plan_amount=AMOUNT_A,
            overage_count=0, overage_amount=Decimal("0"), amount=AMOUNT_A, currency="RUB",
            actor="smoke-bill", ip=None, user_agent=None)
        db.set_active_tenant(ta)
        async with db.pool.acquire() as c:
            tid_of = await c.fetchval("select tenant_id from service_invoices where id = $1", iid)
        check("счёт создан с tenant_id = A", str(tid_of) == str(ta), f"{tid_of} vs {ta}")

        # ── 2. вебхук-путь: attach + mark paid под RLS (self-set tenant) ──
        print("2. отметка оплаты (путь вебхука, без сессии):")
        db.set_active_tenant(ta)
        await db.attach_yookassa_payment(iid, "pay-bill-A")
        row = await db.mark_service_invoice_paid_by_payment("pay-bill-A", tenant_id=ta, card_last4="4242")
        check("mark_..._paid вернул строку (оплачено)", row is not None and row["status"] == "paid",
              repr(row["status"] if row else None))

        # ── 3. RLS-изоляция чтения ──
        print("3. RLS-изоляция чтения счёта:")
        db.set_active_tenant(ta)
        a_latest = await db.get_latest_paid_invoice()
        a_hist = await db.list_service_invoices()
        db.set_active_tenant(tb)
        b_latest = await db.get_latest_paid_invoice()
        b_hist = await db.list_service_invoices()
        db.set_active_tenant(None)
        none_latest = await db.get_latest_paid_invoice()
        check("ctx A видит свой оплаченный счёт", a_latest is not None and a_latest["plan_key"] == "econom")
        check("ctx A: история = 1", len(a_hist) == 1, f"факт {len(a_hist)}")
        check("ctx B НЕ видит счёт A (get_latest_paid_invoice)", b_latest is None, repr(b_latest))
        check("ctx B: история пуста", len(b_hist) == 0, f"факт {len(b_hist)}")
        check("ctx None → счёт не виден (RLS deny)", none_latest is None)

        # ── 4. RLS with_check: под ctx A нельзя вставить счёт тенанта B ──
        print("4. RLS with_check на запись:")
        db.set_active_tenant(ta)
        denied = False
        try:
            async with db.pool.acquire() as c:
                await c.execute(
                    "insert into service_invoices (tenant_id, period_start, period_end, plan_key, "
                    "plan_name, plan_amount, amount, status) "
                    "values ($1,$2,$3,'econom','Эконом',1,1,'pending')", tb, date(2026, 6, 1), date(2026, 7, 1))
        except asyncpg.PostgresError:
            denied = True
        check("вставка счёта с чужим tenant_id отклонена (with_check)", denied)

        # ── 5. service_revenue_total — кросс-тенантный скан (не зависит от ctx) ──
        print("5. платформенная выручка (скан по тенантам):")
        db.set_active_tenant(tb)   # ctx B — но скан всё равно видит счёт A
        rev = Decimal(str(await db.service_revenue_total() or 0))
        check("выручка платформы включает счёт A независимо от ctx", rev == AMOUNT_A, f"{rev} vs {AMOUNT_A}")

        # ── 6. tenant_id обязателен ──
        print("6. обязательность tenant_id:")
        cpi_raised = mpi_raised = False
        try:
            await db.create_period_invoice(
                tenant_id=None, period_start="2026-06-01", period_end="2026-07-01",
                plan_key="econom", plan_name="Эконом", quota=500, plan_amount=AMOUNT_A,
                overage_count=0, overage_amount=Decimal("0"), amount=AMOUNT_A, currency="RUB",
                actor="smoke-bill", ip=None, user_agent=None)
        except ValueError:
            cpi_raised = True
        try:
            await db.mark_service_invoice_paid_by_payment("x", tenant_id=None)
        except ValueError:
            mpi_raised = True
        check("create_period_invoice(tenant_id=None) → ValueError", cpi_raised)
        check("mark_..._paid(tenant_id=None) → ValueError", mpi_raised)

        # ── 7. per-tenant флаг отмены ──
        print("7. per-tenant флаг отмены подписки:")
        await db.set_subscription_canceled(ta, True, actor="smoke-bill", ip=None, user_agent=None)
        a_can = await db.is_subscription_canceled(ta)
        b_can = await db.is_subscription_canceled(tb)
        check("отмена A → A canceled", a_can is True)
        check("отмена A НЕ затронула B", b_can is False)
        await db.set_subscription_canceled(ta, False, actor="smoke-bill", ip=None, user_agent=None)
        check("снятие отмены A → A не canceled", (await db.is_subscription_canceled(ta)) is False)

        # ── 8. column-гранты panel_rw (owner-смоук слеп к ним → проверяем явно) ──
        # has_column_privilege ловит рассинхрон код↔грант (INSERT именует tenant_id, а грант — нет):
        # owner обходит column-ACL, поэтому без этой проверки прод-«permission denied» не виден.
        print("8. column-INSERT-гранты panel_rw на tenant_id:")
        async with db.pool.acquire() as c:
            si_tid = await c.fetchval(
                "select has_column_privilege('panel_rw', 'service_invoices', 'tenant_id', 'INSERT')")
            ord_tid = await c.fetchval(
                "select has_column_privilege('panel_rw', 'orders', 'tenant_id', 'INSERT')")
        check("panel_rw может INSERT service_invoices.tenant_id", si_tid is True,
              "грант db/panel_role.sql не накатан на этот dev?" if not si_tid else "")
        check("panel_rw может INSERT orders.tenant_id (смежный фикс)", ord_tid is True)

    finally:
        async with db.pool.acquire() as c:
            if forced:
                await c.execute("alter table service_invoices no force row level security")
            db.set_active_tenant(None)
            await _cleanup(c)
        await db.pool.close()

    print()
    if FAILS:
        print(f"❌ ПРОВАЛЫ ({len(FAILS)}): " + ", ".join(FAILS))
        sys.exit(1)
    print("✅ billing tenant smoke — все проверки зелёные")


if __name__ == "__main__":
    asyncio.run(main())
