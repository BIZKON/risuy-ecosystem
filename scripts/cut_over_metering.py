#!/usr/bin/env python3
"""Cutover-разрез счётчиков: перевод per_message-тенантов на токен-пул (T-1C-3).

Единица биллинга с 1C — токен-пул (usage_ledger/credit_wallets), а не сообщения Лии.
per_message-машинерия заморожена в T-1C-1 и удаляется в T-1C-3. Этот ops-скрипт
разрезает счётчики per_message-тенантов на новую модель, идемпотентно и по одному
тенанту в своей транзакции:

  1) финальный overage СТАРОЙ единицей (count_ai_messages сверх квоты прошлого
     оплаченного периода × цена сообщения) → отдельный pending-счёт (settle-only);
  2) теневой минус (per_message шли allow_negative) сохраняется в авансе (topup);
  3) пул тарифа (included_credits) выставляется как при activate — с period_end;
  4) маркер billing_cutover_done + аудит фиксируют разрез (повтор → noop).

⚠️ 0 живых per_message-подписок на проде и dev (T-1C-1 мигрировал econom/start на
cost_multiplier) → на проде цикл = no-op. Доказательство корректности — на
СИНТЕТИЧЕСКИХ тенантах (scripts/cutover_shadow_diff_smoke.py, shadow-diff==0).

Три гвоздя списания (charge_usage) НЕ трогаются: cutover пишет ТОЛЬКО credit_wallets
(UPDATE) + service_invoices/tenant_settings/admin_audit (INSERT); в usage_ledger
(append-only) НЕ пишет.

RLS: count_ai_messages/get_latest_paid_invoice/create_period_invoice скоупятся через
db.set_active_tenant(tenant) на пуле с setup=db._apply_tenant_guc. Вызывающий ОБЯЗАН
выставить активного тенанта ДО acquire соединения, иначе панель-роль (panel_rw) под
RLS не увидит строк тенанта (owner-DSN обходит RLS — тогда скоуп держится на явном
tenant_id-фильтре кошелька).

Гард окружения: только risuy_dev; прод — лишь при CUTOVER_ALLOW_PROD=1 (и явном «да»).

Запуск (dev): CUTOVER_DSN="postgresql://<owner>@<host>:5432/risuy_dev?sslmode=require" \
              DATABASE_URL="$CUTOVER_DSN" SESSION_SECRET=... ADMIN_USERNAME=... \
              ADMIN_PASSWORD_HASH='$argon2...' PYTHONPATH=. python3 scripts/cut_over_metering.py
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import timedelta
from decimal import Decimal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "admin-panel"))

import asyncpg   # noqa: E402
import config    # noqa: E402  (admin-panel/config.py — SERVICE_PLANS, DEFAULT_TENANT_SLUG)
import db        # noqa: E402  (admin-panel/db.py — count_ai_messages, create_period_invoice, ...)

logger = logging.getLogger("cut_over_metering")

CUTOVER_PERIOD_DAYS = 30   # период пула при разрезе (econom/start — месячные)


async def _resolve_default_tenant(conn) -> object | None:
    """id тенанта по умолчанию (Школа, §8.7) — исключается из разреза. tenants — не-RLS
    реестр, резолвится независимо от активного тенанта."""
    return await conn.fetchval(
        "select id from tenants where slug = $1", config.DEFAULT_TENANT_SLUG)


async def select_cutover_tenants(conn, explicit: list | None = None) -> list:
    """Тенанты под разрез. Явный список (для точечного/смоук-прогона) имеет приоритет.

    Историческая выборка per_message-тенантов (subscriptions⨝plans billing_mode='per_message')
    после T-1C-1 пуста, поэтому дефолт (Q1): «есть tenant_settings.metering_msg_hwm И нет
    маркера billing_cutover_done» — тенанты, которых per_message-скан успел засеять, но ещё
    не разрезали."""
    if explicit:
        return list(explicit)
    rows = await conn.fetch(
        """
        select distinct s.tenant_id
        from tenant_settings s
        where s.key = 'metering_msg_hwm'
          and not exists (
              select 1 from tenant_settings d
              where d.tenant_id = s.tenant_id and d.key = 'billing_cutover_done')
        """
    )
    return [r["tenant_id"] for r in rows]


async def cut_over_tenant(conn, tenant_id, *, now=None) -> dict:
    """Идемпотентный разрез ОДНОГО тенанта. Возвращает сводку:
      {"status": "done"|"noop"|"skipped", "reason": ..., "tenant_id": ..., "frozen_hwm": ...,
       "overage_count": ..., "overage_amount": ..., "carried_microrub": ..., "included_microrub": ...}

    Порядок (весь тенант в ОДНОЙ транзакции с FOR UPDATE кошелька):
      guard Школа/нет hwm → FOR UPDATE кошелька → маркер-гейт(noop) → запрет пересечения →
      финальный overage → перенос минуса → выставить пул → маркер+аудит.
    Вызывающий выставляет db.set_active_tenant(tenant_id) ДО acquire conn (RLS-скоуп
    вспомогательных запросов, которые берут своё соединение из db.pool)."""
    db.set_active_tenant(tenant_id)

    # ── Шаг 1: guard-и (не падаем — skip) ──
    default_tid = await _resolve_default_tenant(conn)
    if default_tid is not None and str(tenant_id) == str(default_tid):
        return {"status": "skipped", "reason": "school", "tenant_id": str(tenant_id)}
    hwm_raw = await conn.fetchval(
        "select value from tenant_settings where tenant_id = $1 and key = 'metering_msg_hwm'",
        tenant_id)
    if hwm_raw is None:
        return {"status": "skipped", "reason": "no_hwm", "tenant_id": str(tenant_id)}
    frozen_hwm = int(hwm_raw)

    db_now = now or await conn.fetchval("select now()")

    async with conn.transaction():
        # ── Шаг 6 (сериализация): FOR UPDATE кошелька ДО маркер-гейта ──
        wallet = await conn.fetchrow(
            "select included_microrub, included_period_end, topup_microrub, balance_microrub "
            "from credit_wallets where tenant_id = $1 for update", tenant_id)
        if wallet is None:
            return {"status": "skipped", "reason": "no_wallet", "tenant_id": str(tenant_id)}

        # ── Шаг 2: маркер-гейт (идемпотентность) — ПОД локом кошелька ──
        done = await conn.fetchval(
            "select 1 from tenant_settings where tenant_id = $1 and key = 'billing_cutover_done'",
            tenant_id)
        if done is not None:
            return {"status": "noop", "reason": "already_cut", "tenant_id": str(tenant_id),
                    "frozen_hwm": frozen_hwm}

        # ── Шаг 3: запрет пересечения окон (пул уже выдан, но разреза не было) ──
        period_end = wallet["included_period_end"]
        if period_end is not None and period_end > db_now:
            logger.warning("cutover: тенант %s — пул уже выдан (period_end %s > now) без маркера; "
                           "пропуск во избежание двойного пула", tenant_id, period_end)
            return {"status": "skipped", "reason": "overlap", "tenant_id": str(tenant_id)}

        # ── Шаг 5: финальный overage СТАРОЙ единицей (per_message) ──
        latest = await db.get_latest_paid_invoice()   # RLS-скоуп по активному тенанту
        plan_key = latest["plan_key"] if latest is not None else None
        quota = latest["quota"] if latest is not None else None
        overage_count = 0
        overage_amount = Decimal("0")
        if latest is not None and quota is not None:
            used = await db.count_ai_messages(latest["period_start"], latest["period_end"])
            overage_count = max(0, int(used) - int(quota))
            over_price = 0
            if plan_key is not None:
                over_price = (config.SERVICE_PLANS.get(plan_key) or {}).get("overage") or 0
            overage_amount = (Decimal(str(over_price)) * overage_count).quantize(Decimal("0.01"))
        if overage_amount > 0:
            # Идемпотентность на КРАХ-РЕТРАЕ: create_period_invoice берёт СВОЁ соединение из
            # пула и коммитит НЕЗАВИСИМО от транзакции cutover. Если прошлый прогон крашнулся
            # ПОСЛЕ коммита счёта, но ДО маркера (шаг 8) — маркер/кошелёк откатились, а счёт
            # осиротел. Дедуп-пречек по натуральному ключу (tenant_id + период + признак
            # cutover created_by='billing-cutover') на conn транзакции cutover (RLS-скоуп +
            # явный tenant_id → работает и под owner-DSN): второй pending-счёт НЕ создаём,
            # осиротевший переиспользуем и продолжаем шаги 6–8.
            existing_inv = await conn.fetchval(
                "select id from service_invoices "
                "where tenant_id = $1 and period_start = $2 and period_end = $3 "
                "and created_by = 'billing-cutover' limit 1",
                tenant_id, latest["period_start"], latest["period_end"])
            if existing_inv is not None:
                logger.info("cutover: тенант %s — осиротевший cutover overage-счёт %s "
                            "(краш-ретрай); реюз, второй не создаём", tenant_id, existing_inv)
            else:
                # settle-only: plan_amount=0 (подписку не продаём), amount=overage_amount.
                # Отдельная транзакция create_period_invoice (реюз db-слоя), статус pending;
                # FOR UPDATE сериализует параллельные прогоны, дедуп закрывает крах-ретрай-щель.
                await db.create_period_invoice(
                    tenant_id=tenant_id, period_start=latest["period_start"],
                    period_end=latest["period_end"], plan_key=plan_key,
                    plan_name=latest["plan_name"], quota=quota, plan_amount=Decimal("0"),
                    overage_count=overage_count, overage_amount=overage_amount,
                    amount=overage_amount, currency=(latest["currency"] or "RUB"),
                    actor="billing-cutover", ip=None, user_agent=None)

        # ── Шаг 6: перенос теневого минуса (сохранить в авансе, НЕ в пуле) ──
        included = int(wallet["included_microrub"])
        topup = int(wallet["topup_microrub"])
        balance = int(wallet["balance_microrub"])
        available_pool = included if (period_end is not None and period_end > db_now) else 0
        if topup == 0:
            # до-1B: минус мог лежать в balance-зеркале, а не в авансе → перенести в topup.
            carried = min(0, balance - available_pool)
        else:
            # пост-1B: минус уже в авансе (topup) → перенос no-op.
            carried = 0
        new_topup = topup + carried

        # ── Шаг 7: выставить пул тарифа (как activate — перезапись, сгорание остатка) ──
        plan_included = included
        if plan_key is not None:
            row = await conn.fetchval(
                "select included_credits_microrub from plans where code = $1", plan_key)
            if row is not None:
                plan_included = int(row)
        new_period_end = db_now + timedelta(days=CUTOVER_PERIOD_DAYS)
        new_balance = plan_included + new_topup
        await conn.execute(
            "update credit_wallets set included_microrub = $2, included_period_end = $3, "
            "topup_microrub = $4, balance_microrub = $5, updated_at = now() where tenant_id = $1",
            tenant_id, plan_included, new_period_end, new_topup, new_balance)

        # ── Шаг 8: маркер + аудит ──
        marker_detail = (
            f'{{"frozen_hwm": {frozen_hwm}, "overage_count": {overage_count}, '
            f'"overage_amount": "{overage_amount}", "carried_microrub": {carried}}}')
        await conn.execute(
            "insert into tenant_settings (tenant_id, key, value) "
            "values ($1, 'billing_cutover_done', $2) on conflict (tenant_id, key) do nothing",
            tenant_id, marker_detail)
        await db._insert_audit(
            conn, actor="billing-cutover", action="billing_cutover",
            detail={"tenant_id": str(tenant_id), "frozen_hwm": frozen_hwm,
                    "overage_count": overage_count, "overage_amount": str(overage_amount),
                    "carried_microrub": carried, "included_microrub": plan_included})

    return {"status": "done", "reason": None, "tenant_id": str(tenant_id),
            "frozen_hwm": frozen_hwm, "overage_count": overage_count,
            "overage_amount": str(overage_amount), "carried_microrub": carried,
            "included_microrub": plan_included}


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    dsn = os.environ.get("CUTOVER_DSN") or config.DATABASE_URL
    if "/risuy_dev" not in dsn.split("?")[0] and os.environ.get("CUTOVER_ALLOW_PROD") != "1":
        raise SystemExit(
            "cut_over_metering: разрешён только risuy_dev. Для прода — CUTOVER_ALLOW_PROD=1 "
            "и явное подтверждение владельца.")

    db.pool = await asyncpg.create_pool(dsn, min_size=1, max_size=5, setup=db._apply_tenant_guc)
    summary = {"done": 0, "noop": 0, "skipped": 0}
    try:
        db.set_active_tenant(None)
        async with db.pool.acquire() as c:
            tenants = await select_cutover_tenants(c)
        logger.info("cutover: кандидатов на разрез — %s", len(tenants))
        for tenant_id in tenants:
            db.set_active_tenant(tenant_id)
            try:
                async with db.pool.acquire() as conn:
                    res = await cut_over_tenant(conn, tenant_id)
            except Exception:   # noqa: BLE001 — сбой одного тенанта не валит батч
                logger.exception("cutover: сбой разреза тенанта %s", tenant_id)
                continue
            summary[res["status"]] = summary.get(res["status"], 0) + 1
            logger.info("cutover: тенант %s → %s", tenant_id, res)
    finally:
        db.set_active_tenant(None)
        await db.pool.close()
    logger.info("cutover: готово — %s", summary)


if __name__ == "__main__":
    asyncio.run(main())
