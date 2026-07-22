#!/usr/bin/env python3
"""Смоук T-1F-3b: анонимный лендинг-провижининг + B2B на risuy_dev.

Проверяет DB-ядро (без HTTP/ЮKassa): db.reuse_or_create_pending + db.provision_service_landing +
db.purge_pending_service_purchase.

  1. reuse_or_create_pending: повтор (email,plan,pending) в окне → ТОТ ЖЕ id/idempotence_key
     (дедуп двойного сабмита, ревью H-2); другой plan → другой id.
  2. provision (new email) → tenant(active) + admin_users(operator, password_set=false) +
     membership(owner) + account_identities(email, verified=false) + tenant_billing_identity +
     ОДНА subscription + wallet=Inc_new + consent_events(offer+privacy) + pending→claimed;
     результат is_new=true, needs_claim=true.
  3. повтор того же purchase_ref → {claimed:true}, идемпотентно (второго тенанта/подписки нет).
  4. РЕЮЗ тем же email (др. purchase/plan=start) → ТОТ ЖЕ tenant, wallet = start Inc_new (overwrite
     полный пул, НЕ прораченный — ревью CRIT-1); второго тенанта НЕТ.
  5. РЕЮЗ тенанта в suspended → реактивирован active (решение владельца).
  6. provision несуществующего purchase_ref → None (ТРАНЗИЕНТ, ревью M-14).
  7. purge удаляет старые pending/failed; claimed сохраняются.

Гонится как gen_user (owner, RLS bypass). Тестовые smoke-tf13b-* удаляются. На ПРОД не запускать.
Запуск: BILLING_SMOKE_DSN=…/risuy_dev PGPASSWORD=… PYTHONPATH=. <venv>/bin/python scripts/subscribe_provision_smoke.py
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
import db        # noqa: E402

DSN = os.environ.get("BILLING_SMOKE_DSN")
if not DSN or "/risuy_dev" not in DSN.split("?")[0]:
    raise SystemExit("Задайте BILLING_SMOKE_DSN на risuy_dev.")

FAILS: list[str] = []
PERIOD_DAYS = 30
DUMMY_HASH = "$argon2id$v=19$m=65536,t=3,p=4$c21va2U$c21va2VoYXNo"   # неюзабельный хеш для смоука
INN = "7707083893"


def check(name, cond, detail=""):
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail != "" else ""))
    if not cond:
        FAILS.append(name)


async def _cleanup(c):
    rows = await c.fetch(
        "select m.tenant_id, ai.username from account_identities ai "
        "join memberships m on m.username = ai.username "
        "where ai.provider = 'email' and ai.external_id like 'smoke-tf13b-%'")
    tids = list({str(r["tenant_id"]) for r in rows})
    unames = list({r["username"] for r in rows})
    if tids:
        for tbl in ("consent_events", "tenant_billing_identity", "credit_wallets", "subscriptions",
                    "payments", "usage_ledger", "tenant_settings", "service_invoices", "memberships"):
            await c.execute(f"delete from {tbl} where tenant_id = any($1::uuid[])", tids)
    if unames:
        await c.execute("delete from password_reset_tokens where username = any($1)", unames)
        await c.execute("delete from account_identities where username = any($1)", unames)
        await c.execute("delete from admin_users where username = any($1)", unames)
    if tids:
        await c.execute("delete from tenants where id = any($1::uuid[])", tids)
    await c.execute("delete from pending_service_purchase where lower(email) like 'smoke-tf13b-%'")
    await c.execute("delete from admin_audit where detail->>'payment_id' like 'pay-tf13b-%'")


async def _tenant_of_email(c, email):
    return await c.fetchval(
        "select m.tenant_id from account_identities ai join memberships m on m.username = ai.username "
        "where ai.provider = 'email' and ai.external_id = $1 limit 1", email.lower())


async def _mk_pending(email, plan):
    return await db.reuse_or_create_pending(
        email=email, buyer_inn=INN, buyer_ogrnip=None, buyer_subject_type="legal",
        is_entrepreneur=True, plan_code=plan, offer_version="2026-07-22",
        offer_text_hash="deadbeef", agree_pdn=True)


async def main() -> None:
    db.pool = await asyncpg.create_pool(DSN, min_size=1, max_size=6, setup=db._apply_tenant_guc)
    try:
        db.set_active_tenant(None)
        async with db.pool.acquire() as c:
            await _cleanup(c)
            econ_inc = int(await c.fetchval("select included_credits_microrub from plans where code='econom'"))
            strt_inc = int(await c.fetchval("select included_credits_microrub from plans where code='start'"))

        # ── 1. reuse_or_create_pending: дедуп ──
        print("1. дедуп pending (двойной сабмит):")
        e1 = "smoke-tf13b-dedup@example.test"
        pid_a, idem_a = await _mk_pending(e1, "econom")
        pid_b, idem_b = await _mk_pending(e1, "econom")
        pid_c, idem_c = await _mk_pending(e1, "start")
        check("повтор (email,plan) → тот же id", pid_a == pid_b, f"{pid_a} vs {pid_b}")
        check("повтор → тот же idempotence_key", idem_a == idem_b)
        check("другой plan → другой id", pid_c != pid_a)

        # ── 2. provision new email ──
        print("2. провижининг нового email:")
        e2 = "smoke-tf13b-new@example.test"
        pref, _ = await _mk_pending(e2, "econom")
        res = await db.provision_service_landing(
            pref, "pay-tf13b-2", 3750_000_000, PERIOD_DAYS, random_password_hash=DUMMY_HASH)
        check("is_new=true, needs_claim=true", bool(res) and res["is_new"] and res["needs_claim"], repr(res))
        tid = res["tenant_id"] if res else None
        async with db.pool.acquire() as c:
            trow = await c.fetchrow("select status from tenants where id=$1::uuid", tid)
            urow = await c.fetchrow("select role, active, password_set from admin_users where username=$1", res["username"])
            irow = await c.fetchrow("select provider, verified, external_id from account_identities where username=$1", res["username"])
            mrow = await c.fetchrow("select role from memberships where username=$1", res["username"])
            bid = await c.fetchrow("select buyer_inn, buyer_subject_type from tenant_billing_identity where tenant_id=$1::uuid", tid)
            nsub = await c.fetchval("select count(*) from subscriptions where tenant_id=$1::uuid and status in ('trialing','active','past_due')", tid)
            winc = int(await c.fetchval("select included_microrub from credit_wallets where tenant_id=$1::uuid", tid))
            nconsent = await c.fetchval("select count(*) from consent_events where tenant_id=$1::uuid", tid)
            pstat = await c.fetchval("select status from pending_service_purchase where id=$1::uuid", pref)
        check("tenant active", trow and trow["status"] == "active", repr(trow))
        check("admin_users operator+active+password_set=false", urow and urow["role"]=="operator" and urow["active"] and urow["password_set"] is False, repr(urow))
        check("account_identities email+verified=false", irow and irow["provider"]=="email" and irow["verified"] is False and irow["external_id"]==e2)
        check("membership owner", mrow and mrow["role"]=="owner")
        check("tenant_billing_identity записан", bid and bid["buyer_inn"]==INN and bid["buyer_subject_type"]=="legal")
        check("ровно ОДНА живая подписка", nsub == 1, f"факт {nsub}")
        check("wallet пул = econom Inc_new", winc == econ_inc, f"{winc} vs {econ_inc}")
        check("consent_events: offer+privacy = 2 строки", nconsent == 2, f"факт {nconsent}")
        check("pending → claimed", pstat == "claimed", repr(pstat))

        # ── 3. идемпотентность повтора purchase_ref ──
        print("3. идемпотентность повтора provision:")
        res3 = await db.provision_service_landing(
            pref, "pay-tf13b-2", 3750_000_000, PERIOD_DAYS, random_password_hash=DUMMY_HASH)
        async with db.pool.acquire() as c:
            ntenants = await c.fetchval(
                "select count(distinct m.tenant_id) from account_identities ai "
                "join memberships m on m.username=ai.username where ai.external_id=$1", e2)
        check("повтор → claimed:true (no-op)", bool(res3) and res3.get("claimed") is True, repr(res3))
        check("второго тенанта НЕТ (email=1 tenant)", ntenants == 1, f"факт {ntenants}")

        # ── 4. РЕЮЗ тем же email, plan=start → тот же tenant, полный пул start (CRIT-1) ──
        print("4. реюз email (plan=start) → тот же tenant, полный пул (не прораченный):")
        pref4, _ = await _mk_pending(e2, "start")
        res4 = await db.provision_service_landing(
            pref4, "pay-tf13b-4", 7500_000_000, PERIOD_DAYS, random_password_hash=DUMMY_HASH)
        async with db.pool.acquire() as c:
            tid4 = await _tenant_of_email(c, e2)
            winc4 = int(await c.fetchval("select included_microrub from credit_wallets where tenant_id=$1", tid4))
            nsub4 = await c.fetchval("select count(*) from subscriptions where tenant_id=$1 and status in ('trialing','active','past_due')", tid4)
            ntenants4 = await c.fetchval(
                "select count(distinct m.tenant_id) from account_identities ai "
                "join memberships m on m.username=ai.username where ai.external_id=$1", e2)
        check("is_new=false (реюз)", bool(res4) and res4["is_new"] is False, repr(res4))
        check("тот же tenant (email=1 tenant)", ntenants4 == 1, f"факт {ntenants4}")
        check("wallet = start Inc_new (полный, overwrite) [CRIT-1]", winc4 == strt_inc, f"{winc4} vs {strt_inc} (прораченный был бы меньше)")
        check("по-прежнему ОДНА живая подписка", nsub4 == 1, f"факт {nsub4}")

        # ── 5. реюз suspended → реактивация ──
        print("5. реюз тенанта suspended → active:")
        async with db.pool.acquire() as c:
            await c.execute("update tenants set status='suspended' where id=$1", tid4)
        pref5, _ = await _mk_pending(e2, "econom")
        await db.provision_service_landing(
            pref5, "pay-tf13b-5", 3750_000_000, PERIOD_DAYS, random_password_hash=DUMMY_HASH)
        async with db.pool.acquire() as c:
            st5 = await c.fetchval("select status from tenants where id=$1", tid4)
        check("suspended → active после оплаты", st5 == "active", repr(st5))

        # ── 6. несуществующий purchase_ref → транзиент None ──
        print("6. provision несуществующего purchase_ref → None (транзиент):")
        res6 = await db.provision_service_landing(
            "00000000-0000-0000-0000-000000000000", "pay-tf13b-6", 1, PERIOD_DAYS, random_password_hash=DUMMY_HASH)
        check("None (не помечать processed)", res6 is None, repr(res6))

        # ── 7. purge ──
        print("7. purge брошенных pending:")
        e7 = "smoke-tf13b-purge@example.test"
        pref7, _ = await _mk_pending(e7, "econom")
        async with db.pool.acquire() as c:
            await c.execute("update pending_service_purchase set created_at = now() - interval '100 hours' where id=$1::uuid", pref7)
        deleted = await db.purge_pending_service_purchase(72)
        async with db.pool.acquire() as c:
            gone = await c.fetchval("select count(*) from pending_service_purchase where id=$1::uuid", pref7)
            claimed_kept = await c.fetchval("select status from pending_service_purchase where id=$1::uuid", pref)
        check("старый pending удалён", gone == 0 and deleted >= 1, f"deleted={deleted}")
        check("claimed сохранён (не purge)", claimed_kept == "claimed", repr(claimed_kept))

        async with db.pool.acquire() as c:
            await _cleanup(c)
    finally:
        await db.pool.close()

    print()
    if FAILS:
        print(f"❌ ПРОВАЛЫ ({len(FAILS)}): " + "; ".join(FAILS)); sys.exit(1)
    print("✅ subscribe_provision_smoke: все проверки зелёные")


if __name__ == "__main__":
    asyncio.run(main())
