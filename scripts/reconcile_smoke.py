#!/usr/bin/env python3
"""Смоук T-1F-4: реконсиляция осиротевших ЮKassa-платежей на risuy_dev.

Тестирует ядро reconcile_yookassa.reconcile_one синтетическими платежами (БЕЗ реального ЮKassa)
+ сидинг БД. Проверяет:
  1. service_landing осиротевший: dry-run='would_recover' (pending НЕ тронут) → apply='recovered'
     (тенант+подписка+pending claimed) → повтор='already' (идемпотентно).
  2. platform_subscription осиротевший: dry-run='would_recover' → apply='recovered' (подписка+пул,
     succeeded-payment) → повтор='already'.
  3. service_landing без pending (purchase_ref-сирота) → 'transient' (ретрай позже).
  4. не-succeeded платёж → 'skipped_not_paid'; чужой kind → 'skipped_kind'.
  5. отчёты: needs_reconciliation (аудит) + failed pending считаются.

Гонится как gen_user (owner). Тестовые smoke-tf14-* удаляются. На ПРОД не запускать (гард /risuy_dev).
Запуск: BILLING_SMOKE_DSN=…/risuy_dev PGPASSWORD=… PYTHONPATH=. <venv>/bin/python scripts/reconcile_smoke.py
"""
import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "admin-panel"))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
os.environ.setdefault("DATABASE_URL", "postgresql://x/y")
os.environ.setdefault("SESSION_SECRET", "smoke-secret-only-for-import-aaaaaaaa")
os.environ.setdefault("ADMIN_USERNAME", "smoke")
os.environ.setdefault("ADMIN_PASSWORD_HASH", "$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHQ$aGFzaHN0dWI")

import asyncpg          # noqa: E402
import db                # noqa: E402
import reconcile_yookassa as rec  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402

DSN = os.environ.get("BILLING_SMOKE_DSN")
if not DSN or "/risuy_dev" not in DSN.split("?")[0]:
    raise SystemExit("Задайте BILLING_SMOKE_DSN на risuy_dev.")

FAILS: list[str] = []


def check(name, cond, detail=""):
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail != "" else ""))
    if not cond:
        FAILS.append(name)


def _pay(pid, kind, *, status="succeeded", paid=True, value="3750.00", **meta):
    return {"id": pid, "status": status, "paid": paid,
            "amount": {"value": value, "currency": "RUB"}, "metadata": {"kind": kind, **meta}}


async def _cleanup(c):
    rows = await c.fetch(
        "select m.tenant_id, ai.username from account_identities ai "
        "join memberships m on m.username = ai.username "
        "where ai.provider='email' and ai.external_id like 'smoke-tf14-%'")
    tids = [str(r["tenant_id"]) for r in rows]
    unames = [r["username"] for r in rows]
    tids += [str(t) for t in await c.fetchval("select coalesce(array_agg(id), array[]::uuid[]) from tenants where slug like 'smoke-tf14-%'")]
    if tids:
        for tbl in ("consent_events", "tenant_billing_identity", "credit_wallets", "subscriptions",
                    "payments", "usage_ledger", "tenant_settings", "service_invoices", "memberships"):
            await c.execute(f"delete from {tbl} where tenant_id = any($1::uuid[])", tids)
    if unames:
        await c.execute("delete from password_reset_tokens where username = any($1)", unames)
        await c.execute("delete from account_identities where username = any($1)", unames)
        await c.execute("delete from admin_users where username = any($1)", unames)
    await c.execute("delete from tenants where slug like 'smoke-tf14-%'")
    await c.execute("delete from pending_service_purchase where lower(email) like 'smoke-tf14-%'")
    await c.execute("delete from admin_audit where detail->>'payment_id' like 'pay-tf14-%'")


async def main() -> None:
    db.pool = await asyncpg.create_pool(DSN, min_size=1, max_size=6, setup=db._apply_tenant_guc)
    try:
        db.set_active_tenant(None)
        async with db.pool.acquire() as c:
            await _cleanup(c)
            econ_inc = int(await c.fetchval("select included_credits_microrub from plans where code='econom'"))

        # ── 1. service_landing осиротевший ──
        print("1. service_landing осиротевший (dry-run → apply → повтор):")
        e = "smoke-tf14-land@example.test"
        pref, _ = await db.reuse_or_create_pending(
            email=e, buyer_inn="7707083893", buyer_ogrnip=None, buyer_subject_type="legal",
            is_entrepreneur=True, plan_code="econom", offer_version="2026-07-22",
            offer_text_hash="hash", agree_pdn=True)
        p1 = _pay("pay-tf14-land", "service_landing", purchase_ref=pref)
        st_dry = (await rec.reconcile_one(p1, apply=False))[0]
        async with db.pool.acquire() as c:
            pend_after_dry = await c.fetchval("select status from pending_service_purchase where id=$1::uuid", pref)
        check("dry-run → would_recover", st_dry == "would_recover", st_dry)
        check("dry-run НЕ мутировал (pending всё ещё pending)", pend_after_dry == "pending", pend_after_dry)
        st_apply = (await rec.reconcile_one(p1, apply=True))[0]
        async with db.pool.acquire() as c:
            pend_after = await c.fetchval("select status from pending_service_purchase where id=$1::uuid", pref)
            tid = await c.fetchval(
                "select m.tenant_id from account_identities ai join memberships m on m.username=ai.username "
                "where ai.external_id=$1", e)
            nsub = await c.fetchval("select count(*) from subscriptions where tenant_id=$1 and status in ('trialing','active','past_due')", tid) if tid else 0
        check("apply → recovered", st_apply == "recovered", st_apply)
        check("pending → claimed + тенант+подписка созданы", pend_after == "claimed" and tid is not None and nsub == 1)
        st_again = (await rec.reconcile_one(p1, apply=True))[0]
        check("повтор → already (идемпотентно)", st_again == "already", st_again)

        # ── 2. platform_subscription осиротевший ──
        print("2. platform_subscription осиротевший:")
        async with db.pool.acquire() as c:
            tsub = await c.fetchval("insert into tenants(slug,name,status) values('smoke-tf14-sub','TF14sub','active') returning id")
        p2 = _pay("pay-tf14-sub", "platform_subscription", tenant_id=str(tsub), plan="econom",
                  pc_dir="primary", pc_pool_mode="overwrite", pc_pool_amt=str(econ_inc), pc_period="fresh")
        st2_dry = (await rec.reconcile_one(p2, apply=False))[0]
        st2 = (await rec.reconcile_one(p2, apply=True))[0]
        async with db.pool.acquire() as c:
            nsub2 = await c.fetchval("select count(*) from subscriptions where tenant_id=$1 and status='active'", tsub)
            winc = await c.fetchval("select included_microrub from credit_wallets where tenant_id=$1", tsub)
            paid_ok = await c.fetchval("select exists(select 1 from payments where yookassa_payment_id='pay-tf14-sub' and status='succeeded')")
        st2_again = (await rec.reconcile_one(p2, apply=True))[0]
        check("dry-run → would_recover", st2_dry == "would_recover", st2_dry)
        check("apply → recovered (подписка+пул)", st2 == "recovered" and nsub2 == 1 and int(winc or 0) == econ_inc, f"st={st2} nsub={nsub2} winc={winc}")
        check("succeeded-payment записан", paid_ok is True)
        check("повтор → already", st2_again == "already", st2_again)

        # ── 3. service_landing без pending → transient ──
        print("3. service_landing без pending (сирота purchase_ref) → transient:")
        p3 = _pay("pay-tf14-nopref", "service_landing", purchase_ref="00000000-0000-0000-0000-000000000000")
        st3 = (await rec.reconcile_one(p3, apply=True))[0]
        check("→ transient", st3 == "transient", st3)

        # ── 4. пропуски ──
        print("4. пропуски (не оплачен / чужой kind):")
        st4a = (await rec.reconcile_one(_pay("pay-tf14-pend", "platform_subscription", status="pending", paid=False), apply=True))[0]
        st4b = (await rec.reconcile_one(_pay("pay-tf14-order", "order"), apply=True))[0]
        st4c = (await rec.reconcile_one(_pay("pay-tf14-nomid", "platform_subscription"), apply=True))[0]  # нет tenant_id
        check("не succeeded → skipped_not_paid", st4a == "skipped_not_paid", st4a)
        check("чужой kind → skipped_kind", st4b == "skipped_kind", st4b)
        check("platform_subscription без tenant_id → manual", st4c == "manual", st4c)

        # ── 5. отчёты ──
        print("5. отчёты needs_reconciliation + failed pending:")
        async with db.pool.acquire() as c:
            await c.execute(
                "insert into admin_audit(actor, action, detail) values('smoke','subscription_activated',"
                "'{\"payment_id\":\"pay-tf14-nr\",\"needs_reconciliation\":true,\"plan\":\"start\",\"f\":\"0\"}'::jsonb)")
            await c.execute(
                "insert into pending_service_purchase(email,buyer_inn,buyer_subject_type,is_entrepreneur,"
                "plan_code,agree_pdn,consent_at,idempotence_key,status) values("
                "'smoke-tf14-failed@example.test','7707083893','legal',true,'gone',true,now(),'svc:tf14failed','failed')")
        since = datetime.now(timezone.utc) - timedelta(hours=168)
        nr = await rec._report_needs_reconciliation(since)
        fp = await rec._report_failed_pending()
        check("needs_reconciliation найден (≥1)", nr >= 1, f"факт {nr}")
        check("failed pending найден (≥1)", fp >= 1, f"факт {fp}")

        async with db.pool.acquire() as c:
            await _cleanup(c)
    finally:
        await db.pool.close()

    print()
    if FAILS:
        print(f"❌ ПРОВАЛЫ ({len(FAILS)}): " + "; ".join(FAILS)); sys.exit(1)
    print("✅ reconcile_smoke: все проверки зелёные")


if __name__ == "__main__":
    asyncio.run(main())
