"""Клиент ЮKassa для онлайн-оплаты ПРОДАЖ ШКОЛЫ (магазин школы, НЕ магазин подписки).

Бот только СОЗДАЁТ платёж (кнопка «Купить» / клик лида) и отдаёт confirmation_url.
Перепроверку статуса и отметку «оплачено» делает ВЕБХУК ПАНЕЛИ (единый URL в ЛК;
панель матчит заказ по orders.provider_payment_id и ходит в ЮKassa теми же
SHOP-кредами). Сюда get_payment не нужен.

Секреты — ТОЛЬКО env бота (config.SHOP_YOOKASSA_*). Ключи не заданы →
SHOP_PAYMENTS_CONFIGURED ложно, и вызов бросает YooKassaError ДО сети (кнопка
«Купить» в этом случае вообще не показывается — см. worker._product_buy_markup).

Чек 54-ФЗ (боевой магазин с фискализацией): при config.SHOP_RECEIPT_ENABLED к платежу
прикладывается receipt с телефоном лида и одной позицией-услугой. Телефона нет →
платёж уходит БЕЗ чека (магазин со строгой фискализацией его отвергнет — заказ
останется pending и лид получит мягкую ошибку; лиды без телефона в рассылки почти
не попадают — воронка собирает телефон до гайда).

api.yookassa.ru — РФ-сервис, из ru-1 доступен напрямую (прокси, как для Telegram, не нужен).
"""
from __future__ import annotations

import json
from decimal import Decimal

import aiohttp

import config


class YooKassaError(Exception):
    """Сбой обращения к ЮKassa (выключено / сеть / не-2xx / битый ответ)."""


def amount_str(value) -> str:
    """Decimal/число → строка ЮKassa «2900.00» (ровно 2 знака)."""
    return f"{Decimal(value).quantize(Decimal('0.01'))}"


def _receipt(phone: str | None, description: str, amount) -> dict | None:
    """Чек 54-ФЗ (опционально). None — чек не прикладываем (выключен/нет телефона).

    Одна позиция-услуга на полную сумму. vat_code — из env (дефолт 1 = без НДС,
    типично для УСН/самозанятых). payment_subject/mode — услуга с полной оплатой.
    """
    if not config.SHOP_RECEIPT_ENABLED:
        return None
    digits = "".join(ch for ch in (phone or "") if ch.isdigit())
    if not digits:
        return None
    return {
        "customer": {"phone": digits},
        "items": [{
            "description": description[:128],
            "quantity": "1.00",
            "amount": {"value": amount_str(amount), "currency": "RUB"},
            "vat_code": config.SHOP_VAT_CODE,
            "payment_subject": "service",
            "payment_mode": "full_payment",
        }],
    }


async def create_payment(
    *, amount, currency: str, description: str, return_url: str,
    idempotence_key: str, metadata: dict | None = None, lead_phone: str | None = None,
) -> dict:
    """Создать платёж в магазине школы. Возвращает dict ЮKassa
    (id, status, confirmation.confirmation_url). capture=true — списание сразу.

    Idempotence-Key = id заказа: ретрай того же заказа не плодит платежи.
    """
    if not config.SHOP_PAYMENTS_CONFIGURED:
        raise YooKassaError("ЮKassa магазина школы не настроена (нет SHOP_YOOKASSA_*)")
    body: dict = {
        "amount": {"value": amount_str(amount), "currency": currency},
        "capture": True,
        "confirmation": {"type": "redirect", "return_url": return_url},
        "description": description[:128],
        "metadata": metadata or {},
    }
    receipt = _receipt(lead_phone, description, amount)
    if receipt is not None:
        body["receipt"] = receipt
    url = f"{config.YOOKASSA_API_BASE.rstrip('/')}/payments"
    auth = aiohttp.BasicAuth(config.SHOP_YOOKASSA_SHOP_ID, config.SHOP_YOOKASSA_SECRET_KEY)
    headers = {"Idempotence-Key": idempotence_key, "Content-Type": "application/json"}
    timeout = aiohttp.ClientTimeout(total=20)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=body, headers=headers, auth=auth) as resp:
                raw = await resp.text()
                if resp.status // 100 != 2:
                    # Тело ошибки полезно в логе, лиду не показывается (звонящий шлёт фолбэк).
                    raise YooKassaError(f"ЮKassa HTTP {resp.status}: {raw[:500]}")
                return json.loads(raw)
    except YooKassaError:
        raise
    except (aiohttp.ClientError, TimeoutError, OSError) as e:
        raise YooKassaError(f"ЮKassa недоступна: {e}") from e
    except (ValueError, json.JSONDecodeError) as e:
        raise YooKassaError("ЮKassa вернула невалидный ответ") from e
