"""Минимальный клиент ЮKassa для онлайн-оплаты ПОДПИСКИ на сервис (школа→агентство).

Без сторонних зависимостей: stdlib urllib в треде (asyncio.to_thread), чтобы не тянуть
httpx/aiohttp в requirements панели. Только два вызова:
  • create_payment — создать платёж, получить confirmation_url (редирект школы на оплату);
  • get_payment    — перепроверить статус платежа в вебхуке (НЕ доверяем телу вебхука).

Секреты (shopId/secretKey) — из config (env). Если YOOKASSA_ENABLED ложно — вызовы
бросают YooKassaError ДО сетевого запроса (онлайн-оплата выключена).

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


def _auth_header() -> str:
    raw = f"{config.YOOKASSA_SHOP_ID}:{config.YOOKASSA_SECRET_KEY}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def _request(method: str, path: str, *, body: dict | None = None,
             idempotence_key: str | None = None, timeout: float = 20.0) -> dict:
    """Синхронный вызов ЮKassa (исполняется в треде). Возвращает распарсенный JSON."""
    if not config.YOOKASSA_ENABLED:
        raise YooKassaError("ЮKassa не настроена (нет YOOKASSA_SHOP_ID/SECRET_KEY)")
    url = f"{config.YOOKASSA_API_BASE.rstrip('/')}/{path.lstrip('/')}"
    data = json.dumps(body).encode() if body is not None else None
    headers = {
        "Authorization": _auth_header(),
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if idempotence_key:
        headers["Idempotence-Key"] = idempotence_key
    # --- ВРЕМЕННЫЙ DEBUG (диагностика 401): какие креды РЕАЛЬНО загружены в процесс.
    #     Печатаем shopId + SHA-хэш ключа (НЕ сам ключ) + длину — чтобы сравнить с
    #     хэшем ключа из настроек Timeweb (env-PATCH мог не доехать до контейнера). ---
    import hashlib as _hl, logging as _lg
    _sk = config.YOOKASSA_SECRET_KEY or ""
    _lg.getLogger("admin-panel").warning(
        "YK-DEBUG shop=%r keysha=%s keylen=%d",
        config.YOOKASSA_SHOP_ID, _hl.sha256(_sk.encode()).hexdigest()[:12], len(_sk))
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
    idempotence_key: str, metadata: dict | None = None,
) -> dict:
    """Создать платёж. Возвращает dict ЮKassa (id, status, confirmation.confirmation_url).
    capture=true — одностадийный платёж (списание сразу при оплате)."""
    body = {
        "amount": {"value": amount_str(amount), "currency": currency},
        "capture": True,
        "confirmation": {"type": "redirect", "return_url": return_url},
        "description": description[:128],
        "metadata": metadata or {},
    }
    result = await asyncio.to_thread(
        _request, "POST", "/payments", body=body, idempotence_key=idempotence_key
    )
    # --- ВРЕМЕННЫЙ DEBUG: что вернула ЮKassa (есть ли confirmation_url для редиректа) ---
    import logging as _lg2
    _conf = (result.get("confirmation") or {})
    _lg2.getLogger("admin-panel").warning(
        "YK-DEBUG2 resp id=%s status=%s paid=%s conf_keys=%s url=%r return_url=%r",
        result.get("id"), result.get("status"), result.get("paid"),
        list(_conf.keys()), _conf.get("confirmation_url"), return_url)
    return result


async def get_payment(payment_id: str) -> dict:
    """Перепроверить платёж по id (вебхук): статус succeeded/canceled, amount, metadata."""
    return await asyncio.to_thread(_request, "GET", f"/payments/{payment_id}")
