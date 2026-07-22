#!/usr/bin/env python3
"""T-1F-4: реконсиляция ОСИРОТЕВШИХ платежей магазина подписки ЮKassa.

Вебхук ЮKassa может не доехать (перманентный сбой доставки/краш ровно между
webhook_event_new и завершением → 'received' навсегда). Тогда succeeded-платёж есть в
ЮKassa, а провижининг у нас НЕ выполнен: клиент оплатил, подписки/кошелька/аккаунта нет.
Этот ops-скрипт листает succeeded-платежи магазина за окно и ДО-проводит осиротевшие,
идемпотентно (по yookassa_payment_id / статусу pending), образцом веток вебхука:
  • platform_topup                 → db.mark_topup_succeeded
  • platform_subscription          → mark_service_invoice_paid + activate_subscription_from_payment (снимок T-1F-3a)
  • platform_subscription_renewal  → db.renew_subscription
  • service_landing                → db.provision_service_landing (+ claim-письмо, как вебхук)
Плюс ОТЧЁТЫ (read-only): платежи с needs_reconciliation=true (дрейф T-1F-3a) и pending со
статусом 'failed' (план оплачен по config, но нет в БД) — требуют ручного разбора.

Магазин ШКОЛЫ (orders) — вне scope T-1F-4 (отдельная ветка заказов; Этап 1 = платформенный биллинг).

БЕЗОПАСНОСТЬ: dry-run ПО УМОЛЧАНИЮ (только отчёт, без мутаций). Мутации — ТОЛЬКО с --apply.
Прод (risuy) — ТОЛЬКО при RECONCILE_ALLOW_PROD=1 (и явном «да»); risuy_dev — свободно.
Идемпотентно: повторный прогон уже проведённого платежа = 'already' (не задваивает).

Запуск (dry-run прод): RECONCILE_ALLOW_PROD=1 DATABASE_URL="postgresql://<panel_rw|owner>@host:5432/risuy?sslmode=require" \
  YOOKASSA_SHOP_ID=… YOOKASSA_SECRET_KEY=… YOOKASSA_API_BASE=https://api.yookassa.ru/v3 \
  SESSION_SECRET=… ADMIN_USERNAME=… ADMIN_PASSWORD_HASH='$argon2…' PYTHONPATH=. python3 scripts/reconcile_yookassa.py
  (добавить --apply чтобы реально до-провести; --since-hours N окно, дефолт 168=7д).
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import secrets
import sys
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "admin-panel"))
os.environ.setdefault("DATABASE_URL", "postgresql://x/y")
os.environ.setdefault("SESSION_SECRET", "reconcile-secret-aaaaaaaaaaaaaaaa")
os.environ.setdefault("ADMIN_USERNAME", "reconcile")
os.environ.setdefault("ADMIN_PASSWORD_HASH", "$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHQ$aGFzaHN0dWI")

import asyncpg  # noqa: E402
import config   # noqa: E402
import db        # noqa: E402
import auth      # noqa: E402
import mailer    # noqa: E402
import yookassa  # noqa: E402
from shared import money  # noqa: E402


async def _has_succeeded_payment(pid: str) -> bool:
    """Проведён ли платёж: есть SUCCEEDED-строка payments. Для subscription/renewal activate/renew
    ВСТАВЛЯЮТ succeeded-строку (нет строки = не проведён); для topup строка есть с интента (pending),
    succeeded ставит mark_topup — поэтому проверяем именно status='succeeded', а не факт строки."""
    async with db.pool.acquire() as c:
        return await c.fetchval(
            "select exists(select 1 from payments where yookassa_payment_id = $1 and status = 'succeeded')", pid)


async def _pending_status(pref: str) -> str | None:
    async with db.pool.acquire() as c:
        return await c.fetchval(
            "select status from pending_service_purchase where id = $1::uuid", pref)


async def _platform_recoverable(kind: str, meta: dict) -> bool:
    """Достаточно ли данных для re-drive платформенного платежа (ревью LOW: dry-run не должен
    завышать 'would_recover' там, где восстановление невозможно → 'manual')."""
    if not meta.get("tenant_id"):
        return False
    if kind == "platform_subscription_renewal":
        return bool(meta.get("subscription_id"))
    if kind == "platform_subscription":
        plan = meta.get("plan")
        if not plan:
            return False
        async with db.pool.acquire() as c:
            return await c.fetchval("select exists(select 1 from plans where code = $1)", plan)
    return True                                       # platform_topup: достаточно tenant_id


def _older_than_hours(iso: str, hours: int) -> bool:
    """Платёж ЮKassa created_at (ISO, '…Z') старше hours? (ревью: отличить purged pending от гонки)."""
    if not iso:
        return False
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return False
    return dt < datetime.now(timezone.utc) - timedelta(hours=hours)


async def _redrive_platform(kind, meta, pid, amount_micro, payment) -> bool:
    tid = meta.get("tenant_id")
    if not tid:
        return False
    if kind == "platform_topup":
        return await db.mark_topup_succeeded(tid, pid, payment)
    if kind == "platform_subscription":
        pm = payment.get("payment_method") or {}
        card = (pm.get("card") or {}).get("last4")
        row = await db.mark_service_invoice_paid_by_payment(pid, tenant_id=tid, card_last4=card)
        # паритет с вебхуком: оплата снимает per-tenant флаг отмены (ревью LOW)
        if row is not None and await db.is_subscription_canceled(tid):
            await db.set_subscription_canceled(tid, False, actor="reconcile", ip=None, user_agent=None)
        # паритет с вебхуком: сохранённый способ оплаты → иначе восстановленная подписка НЕ
        # автопродлевается (list_due_renewals фильтрует yookassa_payment_method_id is not null; ревью MED)
        pm_id = pm.get("id") if pm.get("saved") else None
        snap = {k: meta[k] for k in
                ("pc_dir", "pc_pool_mode", "pc_pool_amt", "pc_period", "pc_old", "pc_ps") if k in meta}
        return await db.activate_subscription_from_payment(
            tid, meta.get("plan") or "", pid, amount_micro, config.SERVICE_PLAN_PERIOD_DAYS,
            payment_method_id=pm_id, receipt_email=meta.get("email") or None,
            plan_change_snapshot=snap or None)
    if kind == "platform_subscription_renewal":
        sub_id = meta.get("subscription_id")
        if not sub_id:
            return False
        return await db.renew_subscription(tid, sub_id, pid, amount_micro, config.SERVICE_PLAN_PERIOD_DAYS)
    return False


async def _send_claim(username: str, email: str) -> None:
    raw = secrets.token_urlsafe(32)
    await db.create_reset_token(
        username, hashlib.sha256(raw.encode()).hexdigest(),
        ttl_min=config.ACCOUNT_CLAIM_TTL_MIN, request_ip=None)
    base = (config.PANEL_PUBLIC_BASE_URL or "").rstrip("/")
    await mailer.send_account_claim(email, f"{base}/reset-password?token={raw}",
                                    ttl_min=config.ACCOUNT_CLAIM_TTL_MIN)


async def reconcile_one(payment: dict, *, apply: bool) -> tuple[str, str, str]:
    """Один платёж → (статус, kind, id). Статусы: already (уже проведён), would_recover (dry-run:
    осиротевший), recovered (до-проведён), noop (re-drive не сработал), transient (pending нет —
    провижининг ещё возможен ретраем), skipped_* (не наш kind / не оплачен). Идемпотентно."""
    pid = payment.get("id") or "?"
    if not (payment.get("status") == "succeeded" and payment.get("paid")):
        return ("skipped_not_paid", "?", pid)
    meta = payment.get("metadata") or {}
    kind = meta.get("kind")
    amount_micro = money.rub_to_micro((payment.get("amount") or {}).get("value") or "0")

    if kind in ("platform_topup", "platform_subscription", "platform_subscription_renewal"):
        if await _has_succeeded_payment(pid):
            return ("already", kind, pid)
        if not await _platform_recoverable(kind, meta):
            return ("manual", kind, pid)             # осиротевший, но нет tenant_id/plan/sub_id → разбор
        if not apply:
            return ("would_recover", kind, pid)
        ok = await _redrive_platform(kind, meta, pid, amount_micro, payment)
        return ("recovered" if ok else "noop", kind, pid)

    if kind == "service_landing" and meta.get("purchase_ref"):
        pref = meta["purchase_ref"]
        st = await _pending_status(pref)
        if st == "claimed":
            return ("already", kind, pid)
        if st is None:
            # pending нет: гонка (ещё не докоммитился) ИЛИ purge удалил (ПДн утеряны → ручной разбор).
            if _older_than_hours(payment.get("created_at") or "", config.PENDING_PURCHASE_TTL_HOURS):
                return ("lost_pending", kind, pid)   # невосстановимо скриптом (email/ИНН в purged pending)
            return ("transient", kind, pid)          # свежий — ретрай позже
        if st != "pending":                          # failed/expired → не ре-провижинить (без claim-спама)
            return ("manual", kind, pid)
        if not apply:
            return ("would_recover", kind, pid)
        res = await db.provision_service_landing(
            pref, pid, amount_micro, config.SERVICE_PLAN_PERIOD_DAYS,
            random_password_hash=await auth.hash_password(secrets.token_urlsafe(32)))
        if res is None:
            return ("transient", kind, pid)
        if res.get("claimed"):
            return ("already", kind, pid)
        if res.get("needs_claim") and res.get("username"):
            try:
                await _send_claim(res["username"], res["email"])
            except Exception:  # noqa: BLE001
                pass
        return ("recovered", kind, pid)

    return ("skipped_kind", kind or "?", pid)


async def _report_needs_reconciliation(since: datetime) -> int:
    async with db.pool.acquire() as c:
        rows = await c.fetch(
            "select detail->>'payment_id' pid, detail->>'plan' plan, detail->>'f' f "
            "from admin_audit where action = 'subscription_activated' "
            "  and detail->>'needs_reconciliation' = 'true' and at > $1 order by id desc", since)
    for r in rows:
        print(f"  ⚠️ needs_reconciliation: payment={r['pid']} plan={r['plan']} f={r['f']}")
    return len(rows)


async def _report_failed_pending() -> int:
    async with db.pool.acquire() as c:
        rows = await c.fetch(
            "select id, plan_code, yookassa_payment_id from pending_service_purchase "
            "where status = 'failed' order by created_at desc")
    for r in rows:
        print(f"  ⚠️ failed pending: id={r['id']} plan={r['plan_code']} yk={r['yookassa_payment_id']}")
    return len(rows)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Реконсиляция осиротевших ЮKassa-платежей")
    parser.add_argument("--apply", action="store_true", help="реально до-провести (иначе dry-run)")
    parser.add_argument("--since-hours", type=int,
                        default=int(os.environ.get("RECONCILE_SINCE_HOURS", "168")))
    args = parser.parse_args()

    dsn = os.environ.get("RECONCILE_DSN") or os.environ["DATABASE_URL"]
    dbname = dsn.split("?")[0].rsplit("/", 1)[-1]
    if dbname == "risuy" and os.environ.get("RECONCILE_ALLOW_PROD") != "1":
        raise SystemExit("Прод (risuy): задайте RECONCILE_ALLOW_PROD=1 (и явное «да»).")
    mode = "APPLY (мутирует)" if args.apply else "DRY-RUN (только отчёт)"
    print(f"reconcile_yookassa: db={dbname} режим={mode} окно={args.since_hours}ч")

    db.pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4, setup=db._apply_tenant_guc)
    try:
        since = datetime.now(timezone.utc) - timedelta(hours=args.since_hours)
        gte = since.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        payments = await yookassa.list_payments(created_gte=gte, status="succeeded")
        print(f"succeeded-платежей за окно: {len(payments)}")
        tally: dict[str, int] = {}
        for p in payments:
            status, kind, pid = await reconcile_one(p, apply=args.apply)
            tally[status] = tally.get(status, 0) + 1
            if status in ("would_recover", "recovered", "transient", "noop", "manual", "lost_pending"):
                print(f"  [{status}] kind={kind} id={pid}")
        print("итог:", tally)
        print("── отчёт (ручной разбор) ──")
        nr = await _report_needs_reconciliation(since)
        fp = await _report_failed_pending()
        if not nr and not fp:
            print("  (чисто: needs_reconciliation=0, failed pending=0)")
    finally:
        await db.pool.close()


if __name__ == "__main__":
    asyncio.run(main())
