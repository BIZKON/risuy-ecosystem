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
  10. T-1C-2: /subscription/select — overage-биллинг убран из платёжного пути (перерасход
      сообщений сверх квоты прошлого периода больше НЕ доначисляется; итоговая сумма нового
      счёта = только plan_amount).

Гонится как gen_user (owner). Owner ENABLE-RLS обходит → на время теста FORCE RLS на
service_invoices (owner становится субъектом политики, имитируем panel_rw); в finally — снять FORCE.
Тестовые smoke-bill-* удаляются. На прод НЕ запускать.

Запуск: BILLING_SMOKE_DSN="postgresql://<owner>@<host>:5432/risuy_dev?sslmode=require" \
        PYTHONPATH=. ./.venv-smoke/bin/python scripts/billing_tenant_smoke.py
"""
import asyncio
import os
import sys
from datetime import date, datetime, timezone
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
import auth      # noqa: E402  (admin-panel/auth.py — auth.Session для прямого вызова роута)

# app.py (FastAPI) грузится с cwd=admin-panel/ — при импорте он монтирует StaticFiles("static")
# и Jinja2Templates("templates") относительными путями. Сразу возвращаем cwd назад, чтобы
# относительные пути DSN/скрипта ниже не сбились.
_cwd_before_app_import = os.getcwd()
os.chdir(os.path.join(ROOT, "admin-panel"))
import app as admin_app  # noqa: E402  (T-1C-2: прямой вызов subscription_select)
os.chdir(_cwd_before_app_import)

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
    sub = "(select id from tenants where slug like 'smoke-bill-%')"
    for tbl in ("service_invoices", "usage_ledger", "credit_wallets",
                "payments", "subscriptions", "tenant_settings", "messages"):
        await c.execute(f"delete from {tbl} where tenant_id in {sub}")
    await c.execute("delete from app_settings where key like $1",
                    f"{db.config.SERVICE_CANCEL_SETTING_KEY}:%")
    await c.execute("delete from tenants where slug like 'smoke-bill-%'")


class _FakeHeaders:
    """Пустые заголовки запроса — subscription_select читает host/origin/ua/xff, но
    при отсутствии host _same_origin(request) доверяет CSRF-токену (см. app.py)."""

    def get(self, key, default=None):  # noqa: ARG002 — сигнатура Headers.get
        return default


class _FakeRequest:
    """Минимальная замена fastapi.Request для прямого (не через ASGI) вызова
    subscription_select — тестируем платёжную ЛОГИКУ, не HTTP-транспорт."""

    def __init__(self):
        self.headers = _FakeHeaders()
        self.client = None
        self.cookies = {}


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

        # ── 9. T-1B-3: начисление в бакеты (топап→аванс; activate/renew→пул+period_end) ──
        print("9. T-1B-3 начисление в бакеты кошелька:")
        db.set_active_tenant(None)
        async with db.pool.acquire() as c:
            tc = await c.fetchval(
                "insert into tenants(slug,name,status) values('smoke-bill-c','C','active') returning id")
            # исходно: пул 500 (period_end NULL), аванс 200, флаг блокировки ИИ
            await c.execute(
                "insert into credit_wallets(tenant_id, included_microrub, included_period_end, "
                "topup_microrub, balance_microrub) values ($1, 500, null, 200, 700)", tc)
            await c.execute(
                "insert into tenant_settings(tenant_id,key,value) values ($1,'ai_wallet_blocked','1') "
                "on conflict (tenant_id,key) do nothing", tc)
            await c.execute(
                "insert into payments(tenant_id, type, yookassa_payment_id, idempotence_key, "
                "amount_microrub, status) "
                "values ($1,'topup','pay-topup-C','topup:pay-topup-C',1000000,'pending')", tc)

        # топап → аванс += 1_000_000, пул не тронут, флаг ИИ снят
        ok_t = await db.mark_topup_succeeded(tc, "pay-topup-C", {"event": "smoke"})
        async with db.pool.acquire() as c:
            w = await c.fetchrow(
                "select included_microrub, topup_microrub from credit_wallets where tenant_id=$1", tc)
            blk = await c.fetchval(
                "select count(*) from tenant_settings where tenant_id=$1 and key='ai_wallet_blocked'", tc)
        check("топап вернул True", ok_t is True)
        check("топап → аванс 200+1_000_000 = 1_000_200", w["topup_microrub"] == 1_000_200, str(w["topup_microrub"]))
        check("топап: пул не тронут (500)", w["included_microrub"] == 500, str(w["included_microrub"]))
        check("топап: блок ИИ снят", blk == 0)

        # activate econom → пул = 3_750_000_000, period_end в будущем, аванс не тронут
        ok_a = await db.activate_subscription_from_payment(tc, "econom", "pay-sub-C", 3_750_000_000, 30)
        async with db.pool.acquire() as c:
            w = await c.fetchrow(
                "select included_microrub, topup_microrub, (included_period_end > now()) as pe_future "
                "from credit_wallets where tenant_id=$1", tc)
        check("activate вернул True", ok_a is True)
        check("activate → пул = econom.included (3_750_000_000)",
              w["included_microrub"] == 3_750_000_000, str(w["included_microrub"]))
        check("activate → included_period_end в будущем", w["pe_future"] is True)
        check("activate: аванс не тронут (1_000_200)", w["topup_microrub"] == 1_000_200)

        # renew при НЕПУСТОМ пуле (сгорание): уменьшим пул → renew ПЕРЕЗАПИШЕТ, не суммирует
        async with db.pool.acquire() as c:
            await c.execute("update credit_wallets set included_microrub=1000000 where tenant_id=$1", tc)
            sub_id = await c.fetchval(
                "select id from subscriptions where tenant_id=$1 order by created_at desc limit 1", tc)
        ok_r = await db.renew_subscription(tc, sub_id, "pay-renew-C", 3_750_000_000, 30)
        async with db.pool.acquire() as c:
            w = await c.fetchrow(
                "select included_microrub, topup_microrub from credit_wallets where tenant_id=$1", tc)
        check("renew вернул True", ok_r is True)
        check("renew → пул ПЕРЕЗАПИСАН 3_750_000_000 (сгорание, не 1M+3.75B)",
              w["included_microrub"] == 3_750_000_000, str(w["included_microrub"]))
        check("renew: аванс не тронут (1_000_200)", w["topup_microrub"] == 1_000_200)

        # ── 10. T-1C-2: overage-биллинг убран из /subscription/select ──
        print("10. T-1C-2 — смена тарифа НЕ доначисляет overage:")
        db.set_active_tenant(None)
        async with db.pool.acquire() as c:
            te = await c.fetchval(
                "insert into tenants(slug,name,status) values('smoke-bill-e','E','active') returning id")
        db.set_active_tenant(te)

        # Прошлый ОПЛАЧЕННЫЙ период со СНИМКОМ маленькой квоты=2 (снимок квоты живёт в самом
        # счёте, не в config.SERVICE_PLANS — дёшево смоделировать перерасход без 500+ строк).
        prev_start, prev_end = date(2026, 5, 1), date(2026, 6, 1)
        prev_iid = await db.create_period_invoice(
            tenant_id=te, period_start=prev_start, period_end=prev_end,
            plan_key="econom", plan_name="Эконом", quota=2, plan_amount=Decimal("3750"),
            overage_count=0, overage_amount=Decimal("0"), amount=Decimal("3750"), currency="RUB",
            actor="smoke-bill", ip=None, user_agent=None)
        await db.attach_yookassa_payment(prev_iid, "pay-bill-E-prev")
        await db.mark_service_invoice_paid_by_payment("pay-bill-E-prev", tenant_id=te, card_last4="4242")

        # 5 сообщений ИИ внутри прошлого периода > quota=2 — СТАРАЯ логика per_message
        # дала бы overage_count=3, overage_amount=3*7.5=22.50 сверх plan_amount=3750.
        async with db.pool.acquire() as c:
            for i in range(5):
                await c.execute(
                    "insert into messages (tg_user_id, direction, kind, source, tenant_id, created_at) "
                    "values ($1, 'out', 'text', 'liya', $2, $3)",
                    9000 + i, te, datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc))

        # Прямой вызов роута (bypass Depends: require_session/CSRF-DI не участвуют — сессия
        # и csrf_token собраны вручную, как и должно быть при вызове функции напрямую).
        sess = auth.Session(sid="smoke-sid-e", actor="smoke-bill", csrf_token="smoke-csrf-e",
                             active_tenant_id=te)
        orig_yk_enabled = admin_app.config.YOOKASSA_ENABLED
        orig_create_payment = admin_app.yookassa.create_payment
        admin_app.config.YOOKASSA_ENABLED = True  # смоук-окружение по умолчанию без ЮKassa-креда

        async def _fake_create_payment(**_kwargs):
            return {"id": "pay-bill-E-new",
                    "confirmation": {"confirmation_url": "https://example.invalid/pay"}}

        admin_app.yookassa.create_payment = _fake_create_payment
        try:
            resp = await admin_app.subscription_select(
                request=_FakeRequest(), session=sess, plan_key="econom",
                email="", csrf_token="smoke-csrf-e")
        finally:
            admin_app.yookassa.create_payment = orig_create_payment
            admin_app.config.YOOKASSA_ENABLED = orig_yk_enabled

        loc = resp.headers.get("location", "") if hasattr(resp, "headers") else ""
        check("subscription_select редиректит на confirmation_url ЮKassa (не err=)",
              getattr(resp, "status_code", None) == 303 and "example.invalid" in loc, f"status={getattr(resp, 'status_code', None)} location={loc!r}")

        async with db.pool.acquire() as c:
            new_inv = await c.fetchrow(
                "select overage_count, overage_amount, plan_amount, amount, status "
                "from service_invoices where tenant_id=$1 and id != $2 "
                "order by created_at desc limit 1", te, prev_iid)
        check("новый счёт создан (pending)", new_inv is not None and new_inv["status"] == "pending",
              repr(new_inv["status"] if new_inv else None))
        check("overage_count == 0 (T-1C-2: overage убран из биллинга смены тарифа)",
              new_inv is not None and new_inv["overage_count"] == 0,
              str(new_inv["overage_count"]) if new_inv else "нет счёта")
        check("overage_amount == 0",
              new_inv is not None and Decimal(str(new_inv["overage_amount"])) == Decimal("0"),
              str(new_inv["overage_amount"]) if new_inv else "нет счёта")
        check("итоговая сумма счёта == plan_amount (без overage-надбавки)",
              new_inv is not None and Decimal(str(new_inv["amount"])) == Decimal(str(new_inv["plan_amount"])),
              f"{new_inv['amount']} vs {new_inv['plan_amount']}" if new_inv else "нет счёта")

        async with db.pool.acquire() as c:
            ul_count = await c.fetchval("select count(*) from usage_ledger where tenant_id=$1", te)
        check("usage_ledger не тронут платёжным путём подписки (биллинг с T-1C — токен-пул, "
              "а не сообщения; смена тарифа его не пишет)", ul_count == 0, str(ul_count))

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
