#!/usr/bin/env python3
"""Смоук T-1F-3a: закрытие Important #1 (цена select-time ↔ пул webhook-time) на risuy_dev.

Проверяет `db.activate_subscription_from_payment(..., plan_change_snapshot=…)` — вебхук применяет
УПЛАЧЕННЫЙ пул (снимок select-time из metadata), а не пересчитывает на f_webhook:

  1. АПГРЕЙД, период НЕ истёк, снимок совпал с живой (план+якорь) → add уплаченной дельты, период
     СОХРАНЁН (keep), needs_reconciliation=false.
  2. АПГРЕЙД, период ИСТЁК к вебхуку (f_webhook≤0) → add ТОЛЬКО уплаченной дельты + СВЕЖИЙ период
     (пул usable, НЕ мёртвый) + needs_reconciliation=true. [регресс H-3: было keep→период в прошлом→пул=0]
  3. АПГРЕЙД, живая сменила план (дрейф pc_old≠) → add уплаченной дельты + свежий период + recon,
     НЕ overwrite полным Inc_new. [регресс H-4: фолбэк-пересчёт давал полный пул за частичную доплату]
  4. OVERWRITE-снимок (первичная, живой нет) → INSERT, пул = Inc_new, свежий период, recon=false.
  5. Идемпотентность: повтор того же payment_id → False, пул не удвоен.
  6. БЕЗ снимка (legacy) → первичная активация работает по-старому.

Гонится как gen_user (owner, RLS ENABLE-не-FORCE → owner читает всё). Тестовые smoke-tf13a-* удаляются.
На ПРОД не запускать (гард /risuy_dev).

Запуск: BILLING_SMOKE_DSN="postgresql://<owner>@<host>:5432/risuy_dev?sslmode=require" \
        PGPASSWORD=… PYTHONPATH=. <venv>/bin/python scripts/tf13a_snapshot_smoke.py
"""
import asyncio
import os
import sys
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
from shared.money import ceil_mul  # noqa: E402

DSN = os.environ.get("BILLING_SMOKE_DSN")
if not DSN or "/risuy_dev" not in DSN.split("?")[0]:
    raise SystemExit("Задайте BILLING_SMOKE_DSN на risuy_dev.")

FAILS: list[str] = []
PERIOD_DAYS = 30


def check(name: str, cond: bool, detail="") -> None:
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail != "" else ""))
    if not cond:
        FAILS.append(name)


async def _cleanup(c):
    sub = "(select id from tenants where slug like 'smoke-tf13a-%')"
    for tbl in ("usage_ledger", "credit_wallets", "payments", "subscriptions",
                "tenant_settings", "service_invoices"):
        await c.execute(f"delete from {tbl} where tenant_id in {sub}")
    await c.execute("delete from admin_audit where detail->>'payment_id' like 'pay-tf13a-%'")
    await c.execute("delete from tenants where slug like 'smoke-tf13a-%'")


async def _seed_live_sub(c, tid, plan_id, *, start_days_ago, end_days_from_now):
    """Живая подписка с точными timestamptz; возвращает current_period_start (для якоря pc_ps)."""
    row = await c.fetchrow(
        "insert into subscriptions (tenant_id, plan_id, status, current_period_start, current_period_end) "
        "values ($1, $2, 'active', now() - make_interval(days => $3), now() + make_interval(days => $4)) "
        "returning current_period_start",
        tid, plan_id, start_days_ago, end_days_from_now)
    return row["current_period_start"]


async def _seed_wallet(c, tid, included, period_end_days):
    await c.execute(
        "insert into credit_wallets (tenant_id, included_microrub, included_period_end, "
        "                            topup_microrub, balance_microrub, updated_at) "
        "values ($1, $2, now() + make_interval(days => $3), 0, $2, now()) "
        "on conflict (tenant_id) do update set included_microrub = excluded.included_microrub, "
        "  included_period_end = excluded.included_period_end, topup_microrub = 0, "
        "  balance_microrub = excluded.included_microrub, updated_at = now()",
        tid, int(included), period_end_days)


async def _wallet_included(c, tid) -> int:
    return int(await c.fetchval("select included_microrub from credit_wallets where tenant_id = $1", tid))


async def _sub_state(c, tid):
    return await c.fetchrow(
        "select s.current_period_end, s.current_period_end > now() as future, p.code as plan_code "
        "from subscriptions s join plans p on p.id = s.plan_id "
        "where s.tenant_id = $1 and s.status in ('trialing','active','past_due') "
        "order by s.created_at desc, s.id desc limit 1", tid)


async def _needs_recon(c, payment_id) -> bool:
    v = await c.fetchval(
        "select detail->>'needs_reconciliation' from admin_audit "
        "where action = 'subscription_activated' and detail->>'payment_id' = $1 "
        "order by id desc limit 1", payment_id)
    return v == "true"


async def _reset(c, tid):
    for tbl in ("credit_wallets", "payments", "subscriptions"):
        await c.execute(f"delete from {tbl} where tenant_id = $1", tid)
    await c.execute("delete from admin_audit where detail->>'payment_id' like 'pay-tf13a-%'")


async def main() -> None:
    db.pool = await asyncpg.create_pool(DSN, min_size=1, max_size=4, setup=db._apply_tenant_guc)
    try:
        db.set_active_tenant(None)
        async with db.pool.acquire() as c:
            await _cleanup(c)
            tid = await c.fetchval(
                "insert into tenants(slug,name,status) values('smoke-tf13a-1','TF13a','active') returning id")
            econ = await c.fetchrow("select id, included_credits_microrub inc from plans where code='econom'")
            strt = await c.fetchrow("select id, included_credits_microrub inc from plans where code='start'")
        econ_id, econ_inc = econ["id"], int(econ["inc"])
        strt_id, strt_inc = strt["id"], int(strt["inc"])
        delta = strt_inc - econ_inc
        paid_delta = ceil_mul(delta, Decimal("0.5"))   # оплаченная дельта апгрейда при f_select=0.5
        print(f"планы: econom inc={econ_inc} start inc={strt_inc} Δ={delta} paid_delta(f=.5)={paid_delta}")

        def snap_add(pc_old, pc_ps):
            return {"pc_dir": "upgrade", "pc_pool_mode": "add", "pc_pool_amt": str(paid_delta),
                    "pc_period": "keep", "pc_old": pc_old, "pc_ps": pc_ps.isoformat() if pc_ps else ""}

        # ── 1. апгрейд, период НЕ истёк, снимок совпал → add дельты, keep период, no recon ──
        print("1. апгрейд · период жив · снимок совпал → add дельты + keep:")
        async with db.pool.acquire() as c:
            await _reset(c, tid)
            ps = await _seed_live_sub(c, tid, econ_id, start_days_ago=15, end_days_from_now=15)
            await _seed_wallet(c, tid, econ_inc, 15)
        ok = await db.activate_subscription_from_payment(
            tid, "start", "pay-tf13a-1", 999, PERIOD_DAYS, plan_change_snapshot=snap_add("econom", ps))
        async with db.pool.acquire() as c:
            inc = await _wallet_included(c, tid); st = await _sub_state(c, tid); rec = await _needs_recon(c, "pay-tf13a-1")
        check("активирована", ok is True)
        check("пул = econom + оплаченная дельта", inc == econ_inc + paid_delta, f"{inc} vs {econ_inc + paid_delta}")
        check("период СОХРАНЁН (keep, в будущем)", st["future"] is True)
        check("план → start", st["plan_code"] == "start")
        check("needs_reconciliation = false", rec is False)

        # ── 2. апгрейд, период ИСТЁК → add ТОЛЬКО дельты + СВЕЖИЙ период + recon [регресс H-3] ──
        print("2. апгрейд · период ИСТЁК (f_webhook≤0) → add дельты + fresh + recon:")
        async with db.pool.acquire() as c:
            await _reset(c, tid)
            ps = await _seed_live_sub(c, tid, econ_id, start_days_ago=40, end_days_from_now=-10)  # истёк
            await _seed_wallet(c, tid, econ_inc, -10)
        await db.activate_subscription_from_payment(
            tid, "start", "pay-tf13a-2", 999, PERIOD_DAYS, plan_change_snapshot=snap_add("econom", ps))
        async with db.pool.acquire() as c:
            inc = await _wallet_included(c, tid); st = await _sub_state(c, tid); rec = await _needs_recon(c, "pay-tf13a-2")
        check("пул = econom + ТОЛЬКО дельта (не полный Inc_new)", inc == econ_inc + paid_delta,
              f"{inc} vs {econ_inc + paid_delta}")
        check("период СВЕЖИЙ (usable, в будущем) [H-3 регресс]", st["future"] is True)
        check("needs_reconciliation = true", rec is True)

        # ── 3. апгрейд, живая сменила план (дрейф) → add дельты, НЕ overwrite Inc_new [регресс H-4] ──
        print("3. апгрейд · дрейф (живая уже start, pc_old=econom) → add дельты, НЕ full Inc_new:")
        base = 1_000_000_000
        async with db.pool.acquire() as c:
            await _reset(c, tid)
            await _seed_live_sub(c, tid, strt_id, start_days_ago=5, end_days_from_now=25)  # уже START
            await _seed_wallet(c, tid, base, 25)
        # снимок сделан на select-time когда живая была econom (pc_old=econom, чужой якорь)
        import datetime as _dt
        await db.activate_subscription_from_payment(
            tid, "start", "pay-tf13a-3", 999, PERIOD_DAYS,
            plan_change_snapshot=snap_add("econom", _dt.datetime(2020, 1, 1, tzinfo=_dt.timezone.utc)))
        async with db.pool.acquire() as c:
            inc = await _wallet_included(c, tid); st = await _sub_state(c, tid); rec = await _needs_recon(c, "pay-tf13a-3")
        check("пул = base + дельта (add, НЕ overwrite full Inc_new) [H-4 регресс]",
              inc == base + paid_delta, f"{inc} vs {base + paid_delta} (buggy overwrite={strt_inc})")
        check("период свежий", st["future"] is True)
        check("needs_reconciliation = true", rec is True)

        # ── 4. overwrite-снимок, живой нет → INSERT, пул = Inc_new, свежий период, no recon ──
        print("4. overwrite-снимок · живой нет (первичная) → пул = Inc_new:")
        snap_ow = {"pc_dir": "primary", "pc_pool_mode": "overwrite", "pc_pool_amt": str(econ_inc),
                   "pc_period": "fresh", "pc_old": "", "pc_ps": ""}
        async with db.pool.acquire() as c:
            await _reset(c, tid)
        await db.activate_subscription_from_payment(
            tid, "econom", "pay-tf13a-4", 999, PERIOD_DAYS, plan_change_snapshot=snap_ow)
        async with db.pool.acquire() as c:
            inc = await _wallet_included(c, tid); st = await _sub_state(c, tid); rec = await _needs_recon(c, "pay-tf13a-4")
        check("пул = Inc_new (econom)", inc == econ_inc, f"{inc} vs {econ_inc}")
        check("период свежий, план econom", st is not None and st["future"] is True and st["plan_code"] == "econom")
        check("needs_reconciliation = false", rec is False)

        # ── 5. идемпотентность: повтор того же payment_id → False, пул не удвоен ──
        print("5. идемпотентность повтора payment_id:")
        ok2 = await db.activate_subscription_from_payment(
            tid, "econom", "pay-tf13a-4", 999, PERIOD_DAYS, plan_change_snapshot=snap_ow)
        async with db.pool.acquire() as c:
            inc = await _wallet_included(c, tid)
        check("повтор → False (уже обработан)", ok2 is False)
        check("пул НЕ удвоен", inc == econ_inc, f"{inc} vs {econ_inc}")

        # ── 6. без снимка (legacy) → первичная активация по-старому ──
        print("6. без снимка (legacy) → первичная активация:")
        async with db.pool.acquire() as c:
            await _reset(c, tid)
        await db.activate_subscription_from_payment(tid, "econom", "pay-tf13a-6", 999, PERIOD_DAYS)
        async with db.pool.acquire() as c:
            inc = await _wallet_included(c, tid); st = await _sub_state(c, tid)
        check("legacy пул = Inc_new (econom)", inc == econ_inc, f"{inc} vs {econ_inc}")
        check("legacy период свежий", st is not None and st["future"] is True)

        async with db.pool.acquire() as c:
            await _cleanup(c)
    finally:
        await db.pool.close()

    print()
    if FAILS:
        print(f"❌ ПРОВАЛЫ ({len(FAILS)}): " + "; ".join(FAILS)); sys.exit(1)
    print("✅ tf13a_snapshot_smoke: все проверки зелёные")


if __name__ == "__main__":
    asyncio.run(main())
