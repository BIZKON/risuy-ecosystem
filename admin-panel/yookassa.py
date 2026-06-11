"""Минимальный клиент ЮKassa панели — ДВА магазина, одна транспортная обвязка.

  • Магазин ПОДПИСКИ (config.YOOKASSA_*) — биллинг сервиса школа→агентство:
    create_payment (счёт за тариф) + get_payment (перепроверка в вебхуке).
  • Магазин ШКОЛЫ (config.SHOP_YOOKASSA_*, Phase 1B) — продажи лидам:
    create_shop_payment («счёт из диалога», с чеком 54-ФЗ при включённой фискализации)
    + get_shop_payment (перепроверка ЗАКАЗА в вебхуке — платёж создан ботом или панелью
    в магазине школы, кредами подписки его не прочитать).

Без сторонних зависимостей: stdlib urllib в треде (asyncio.to_thread), чтобы не тянуть
httpx/aiohttp в requirements панели. Секреты — из config (env). Ключи магазина не
заданы → вызовы этого магазина бросают YooKassaError ДО сетевого запроса.

Аутентификация ЮKassa — HTTP Basic (shopId:secretKey). Все суммы — строки «9900.00».
api.yookassa.ru — РФ-сервис, из ru-1 доступен напрямую (в отличие от Telegram, прокси не нужен).
"""
from __future__ import annotations

import asyncio
import base64
import json
import urllib.error
import urllib.request
from decimal import Decimal

import config


class YooKassaError(Exception):
    """Сбой обращения к ЮKassa (выключено / сеть / не-2xx / битый ответ)."""


def amount_str(value) -> str:
    """Decimal → строка ЮKassa «9900.00» (ровно 2 знака)."""
    return f"{Decimal(value).quantize(Decimal('0.01'))}"


def _auth_header(shop_id: str, secret_key: str) -> str:
    raw = f"{shop_id}:{secret_key}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def _request(method: str, path: str, *, body: dict | None = None,
             idempotence_key: str | None = None, timeout: float = 20.0,
             creds: tuple[str, str] | None = None) -> dict:
    """Синхронный вызов ЮKassa (исполняется в треде). Возвращает распарсенный JSON.
    creds=(shop_id, secret_key); None → магазин подписки (поведение до 1B)."""
    shop_id, secret_key = creds or (config.YOOKASSA_SHOP_ID, config.YOOKASSA_SECRET_KEY)
    if not (shop_id and secret_key):
        raise YooKassaError("ЮKassa не настроена (нет shopId/secretKey этого магазина)")
    url = f"{config.YOOKASSA_API_BASE.rstrip('/')}/{path.lstrip('/')}"
    data = json.dumps(body).encode() if body is not None else None
    headers = {
        "Authorization": _auth_header(shop_id, secret_key),
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if idempotence_key:
        headers["Idempotence-Key"] = idempotence_key
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        # Тело ошибки ЮKassa полезно для лога, но НЕ светим оператору (может нести id).
        detail = ""
        try:
            detail = e.read().decode()[:500]
        except Exception:
            pass
        raise YooKassaError(f"ЮKassa HTTP {e.code}: {detail}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise YooKassaError(f"ЮKassa недоступна: {e}") from e
    except (ValueError, json.JSONDecodeError) as e:
        raise YooKassaError("ЮKassa вернула невалидный ответ") from e


async def create_payment(
    *, amount, currency: str, description: str, return_url: str,
    idempotence_key: str, metadata: dict | None = None, receipt: dict | None = None,
) -> dict:
    """Создать платёж. Возвращает dict ЮKassa (id, status, confirmation.confirmation_url).
    capture=true — одностадийный платёж (списание сразу при оплате).
    receipt — чек 54-ФЗ (опц.): добавляется только если задан (магазин с фискализацией)."""
    body = {
        "amount": {"value": amount_str(amount), "currency": currency},
        "capture": True,
        "confirmation": {"type": "redirect", "return_url": return_url},
        "description": description[:128],
        "metadata": metadata or {},
    }
    if receipt is not None:
        body["receipt"] = receipt
    return await asyncio.to_thread(
        _request, "POST", "/payments", body=body, idempotence_key=idempotence_key
    )


async def get_payment(payment_id: str) -> dict:
    """Перепроверить платёж ПОДПИСКИ по id (вебхук): статус succeeded/canceled, amount."""
    return await asyncio.to_thread(_request, "GET", f"/payments/{payment_id}")


# ── Магазин ШКОЛЫ (Phase 1B: продажи лидам) ──────────────────────────────────
def _shop_creds() -> tuple[str, str]:
    return (config.SHOP_YOOKASSA_SHOP_ID, config.SHOP_YOOKASSA_SECRET_KEY)


def _shop_receipt(phone: str | None, description: str, amount) -> dict | None:
    """Чек 54-ФЗ для платежа школы (опц.) — зеркало bot-telegram/yookassa.py::_receipt:
    включён флагом + есть телефон → одна позиция-услуга, иначе None (без чека)."""
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


async def create_shop_payment(
    *, amount, currency: str, description: str, return_url: str,
    idempotence_key: str, metadata: dict | None = None, lead_phone: str | None = None,
) -> dict:
    """Создать платёж в МАГАЗИНЕ ШКОЛЫ («счёт из диалога»). Сигнатура и receipt-логика
    зеркалят бот-сторону (bot-telegram/yookassa.py::create_payment)."""
    body: dict = {
        "amount": {"value": amount_str(amount), "currency": currency},
        "capture": True,
        "confirmation": {"type": "redirect", "return_url": return_url},
        "description": description[:128],
        "metadata": metadata or {},
    }
    receipt = _shop_receipt(lead_phone, description, amount)
    if receipt is not None:
        body["receipt"] = receipt
    return await asyncio.to_thread(
        _request, "POST", "/payments", body=body,
        idempotence_key=idempotence_key, creds=_shop_creds(),
    )


async def get_shop_payment(payment_id: str) -> dict:
    """Перепроверить платёж ЗАКАЗА школы по id (вебхук-ветка заказов)."""
    return await asyncio.to_thread(
        _request, "GET", f"/payments/{payment_id}", creds=_shop_creds()
    )
