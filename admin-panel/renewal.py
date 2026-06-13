"""Wave 2b — cron безакцептных автосписаний рекуррента ЮKassa (ТЗ §5.3).

Фоновая задача lifespan панели (один процесс uvicorn — без дублей; RLS-скан по
тенантам). Каждые SERVICE_RENEWAL_INTERVAL сек:
  • перебирает active-тенантов, у каждого — подписки готовые к продлению
    (живые, с сохранённой картой, истёкший период, не превышен потолок попыток,
    прошёл backoff);
  • создаёт БЕЗАКЦЕПТНЫЙ платёж по сохранённому payment_method_id (чек 54-ФЗ с
    сохранённым email — фискальный магазин подписки) с metadata
    kind='platform_subscription_renewal';
  • succeeded → подписку продлит ВЕБХУК (db.renew_subscription: UPDATE период +
    included_credits, идемпотентно по payment_id) — единая точка, как у Wave 4;
  • canceled → past_due/canceled (потолок попыток) + ops-алерт; pending → ждём.

⚠️ ФИЧЕ-ФЛАГ SERVICE_RENEWAL_ENABLED (дефолт OFF): cron создаёт РЕАЛЬНЫЕ платежи.
Включать ТОЛЬКО после E2E на ТЕСТОВОМ магазине 1379463 (на боевом нельзя — реальные
списания). Idempotence-Key платежа стабилен за период → повторный тик не плодит
списаний (ЮKassa дедупит ключ 24ч).
"""
import asyncio
import logging
import time

import config
import db
import yookassa
from shared.money import micro_to_amount_str, rub_to_micro

logger = logging.getLogger("admin-panel")

_ALERT_INTERVAL = 3600.0
_alerted: dict[str, float] = {}


def _alert_due(key: str) -> bool:
    now = time.monotonic()
    if now - _alerted.get(key, 0.0) > _ALERT_INTERVAL:
        _alerted[key] = now
        return True
    return False


def _recurrent_receipt(email: str | None, description: str, amount_value: str) -> dict | None:
    """Чек 54-ФЗ для безакцептного платежа (фискальный магазин подписки требует).
    Зеркало app._service_receipt, но email — сохранённый при первой оплате."""
    if not config.SERVICE_RECEIPT_ENABLED:
        return None
    if not email:
        return None
    return {
        "customer": {"email": email},
        "items": [{
            "description": description[:128], "quantity": "1.00",
            "amount": {"value": amount_value, "currency": config.SERVICE_CURRENCY},
            "vat_code": config.SERVICE_VAT_CODE,
            "payment_subject": "service", "payment_mode": "full_payment",
        }],
    }


async def run() -> None:
    """Главный цикл. При выключенном флаге — один лог и выход (cron не работает)."""
    if not config.SERVICE_RENEWAL_ENABLED:
        logger.info("Автосписания рекуррента ВЫКЛЮЧЕНЫ (SERVICE_RENEWAL_ENABLED=0) — cron не запущен")
        return
    logger.info("Cron автосписаний запущен (интервал %s c)", config.SERVICE_RENEWAL_INTERVAL)
    while True:
        try:
            await _tick()
        except Exception:  # noqa: BLE001 — цикл не должен падать
            logger.exception("Ошибка тика автосписаний")
        await asyncio.sleep(config.SERVICE_RENEWAL_INTERVAL)


async def _tick() -> None:
    for t in await db.list_active_tenants_for_renewal():
        try:
            due = await db.list_due_renewals(
                t["id"], retry_hours=config.SERVICE_RENEWAL_RETRY_HOURS,
                max_attempts=config.SERVICE_RENEWAL_MAX_ATTEMPTS)
        except Exception:  # noqa: BLE001 — один тенант не валит остальных
            logger.exception("Автосписания: сбой скана тенанта %s", t["id"])
            continue
        for s in due:
            try:
                await _charge_one(t["id"], s)
            except Exception:  # noqa: BLE001 — одна подписка не валит остальные
                logger.exception("Автосписания: сбой подписки %s", s["id"])


async def _charge_one(tenant_id, s) -> None:
    sub_id = s["id"]
    # Фискальный магазин требует чек с email — без него платёж отвергнется. Не
    # дёргаем ЮKassa впустую: помечаем попытку (backoff) и шлём ops-алерт.
    if config.SERVICE_RECEIPT_ENABLED and not s["receipt_email"]:
        await db.bump_renewal_attempt(tenant_id, sub_id)
        if _alert_due(f"noemail:{sub_id}"):
            logger.error("Автосписания: у подписки %s нет email для чека 54-ФЗ — пропуск", sub_id)
        return

    amount_value = micro_to_amount_str(int(s["price_microrub"]))
    idem = f"renew:{sub_id}:{s['current_period_end'].isoformat()}"
    description = f"Продление подписки «{s['plan_code']}»"
    try:
        payment = await yookassa.create_recurrent_payment(
            amount=amount_value, currency=config.SERVICE_CURRENCY, description=description,
            payment_method_id=s["yookassa_payment_method_id"], idempotence_key=idem,
            metadata={"kind": "platform_subscription_renewal", "subscription_id": str(sub_id),
                      "tenant_id": str(tenant_id), "plan": s["plan_code"],
                      "email": s["receipt_email"] or ""},
            receipt=_recurrent_receipt(s["receipt_email"], description, amount_value),
        )
    except yookassa.YooKassaError:
        logger.exception("Автосписания: ЮKassa отклонила создание платежа подписки %s", sub_id)
        await db.bump_renewal_attempt(tenant_id, sub_id)
        return

    await db.bump_renewal_attempt(tenant_id, sub_id)
    status = payment.get("status")
    if status == "succeeded" and payment.get("paid"):
        # Продлеваем СРАЗУ (idempotent по payment_id) — НЕ полагаемся только на вебхук:
        # при потере вебхука деньги были бы списаны, а подписка не продлена. Вебхук
        # тоже придёт → renew_subscription увидит payment в журнале → no-op.
        amount_micro = rub_to_micro((payment.get("amount") or {}).get("value") or "0")
        await db.renew_subscription(
            tenant_id, sub_id, payment.get("id"), amount_micro, config.SERVICE_PLAN_PERIOD_DAYS)
        logger.info("Автосписание подписки %s: платёж %s succeeded → продлено", sub_id, payment.get("id"))
    elif status == "canceled":
        canceled = await db.mark_renewal_failed(
            tenant_id, sub_id, max_attempts=config.SERVICE_RENEWAL_MAX_ATTEMPTS)
        logger.warning("Автосписание подписки %s отклонено (canceled)%s",
                       sub_id, " → подписка отменена" if canceled else " → past_due, будет ретрай")
        if canceled and _alert_due(f"canceled:{sub_id}"):
            await _ops_note(f"Подписка {sub_id} отменена: автосписание не прошло "
                            f"{config.SERVICE_RENEWAL_MAX_ATTEMPTS} раз. Свяжитесь с клиентом.")
    # pending/waiting_for_capture — оставляем, обработает вебхук или следующий тик.


async def _ops_note(text: str) -> None:
    """Ops-уведомление в журнал (панель не шлёт в Telegram — это делает бот; здесь
    достаточно error-лога, который виден в Timeweb logs)."""
    logger.error("OPS: %s", text)
