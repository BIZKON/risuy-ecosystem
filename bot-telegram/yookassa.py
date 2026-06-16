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
    """Decimal/число → строка ЮKassa «2900.00» (ровно 2 знака).
    ⚠️ Идентичен admin-panel/yookassa.py::amount_str и родственен shared/money.py::micro_to_amount_str
    (та же 2-знаковая сериализация, но из micro-int). Меняешь формат — синхронно во всех трёх."""
    return f"{Decimal(value).quantize(Decimal('0.01'))}"


def _receipt(phone: str | None, description: str, amount) -> dict | None:
    """Чек 54-ФЗ (опционально). None — чек не прикладываем (выключен/нет телефона).
    ⚠️ Структура ЗЕРКАЛИТСЯ в admin-panel/yookassa.py::_shop_receipt (+ _service_receipt/_recurrent_receipt).
    Дрейф vat_code/полей → фискальный магазин отвергнет платёж. Меняешь — синхронно во всех билдерах чека.

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
    creds: tuple[str, str] | None = None,
) -> dict:
    """Создать платёж. Возвращает dict ЮKassa (id, status, confirmation.confirmation_url).
    capture=true — списание сразу. Idempotence-Key = id заказа: ретрай не плодит платежи.

    creds=(shop_id, secret_key) — магазин конкретного ТЕНАНТА (Слой C: бот тенанта берёт их
    из своего vault → приём оплаты на СВОЙ счёт). None → магазин Школы из env (config.SHOP_YOOKASSA_*,
    поведение до Слоя C). Без кред вовсе (None и Школа не настроена) → YooKassaError ДО сети."""
    shop_id, secret_key = creds or (config.SHOP_YOOKASSA_SHOP_ID, config.SHOP_YOOKASSA_SECRET_KEY)
    if not (shop_id and secret_key):
        raise YooKassaError("ЮKassa не настроена (нет shopId/secretKey магазина)")
    body: dict = {
        "amount": {"value": amount_str(amount), "currency": currency},
        "capture": True,
        "confirmation": {"type": "redirect", "return_url": return_url},
        "description": description[:128],
        "metadata": metadata or {},
    }
    # Чек 54-ФЗ — ТОЛЬКО для магазина Школы (env-конфиг SHOP_RECEIPT_ENABLED/SHOP_VAT_CODE). Для
    # кассы ТЕНАНТА (creds задан) школьный чек НЕ прикладываем (кросс-тенант фиск-утечка: чужой
    # ОФД/НДС-режим); фискализация тенанта — на стороне его магазина ЮKassa (per-tenant receipt —
    # отдельная волна). Без чека строгий магазин может отвергнуть платёж — лид получит мягкий фолбэк.
    if creds is None:
        receipt = _receipt(lead_phone, description, amount)
        if receipt is not None:
            body["receipt"] = receipt
    url = f"{config.YOOKASSA_API_BASE.rstrip('/')}/payments"
    auth = aiohttp.BasicAuth(shop_id, secret_key)
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
