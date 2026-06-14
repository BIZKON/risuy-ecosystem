"""Вход через ВК (VK ID, id.vk.com) — OAuth2.1 + PKCE, для парадной «ИИ-Агент Про».

Без сторонних зависимостей: stdlib urllib в треде (asyncio.to_thread), как admin-panel/
yookassa.py. Активируется только при config.OAUTH_VK_ENABLED + VK_CLIENT_ID/SECRET (выдаёт
владелец, создав ВК-приложение). PKCE-verifier и anti-CSRF state переносим между /auth/vk/start
и /auth/vk/callback в ПОДПИСАННОЙ короткоживущей cookie (не в server-state).

Поток: start → 302 на AUTHORIZE (code_challenge) → пользователь подтверждает во ВК →
callback?code&state&device_id → exchange_code (code_verifier) → user_id + access_token →
fetch_user (имя/почта). external_id = str(user_id), verified=true (ВК подтвердил личность).
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import secrets
import urllib.error
import urllib.parse
import urllib.request

from itsdangerous import BadSignature, URLSafeTimedSerializer

import config

AUTHORIZE_URL = "https://id.vk.com/authorize"
TOKEN_URL = "https://id.vk.com/oauth2/auth"
USERINFO_URL = "https://id.vk.com/oauth2/user_info"

_STATE_MAX_AGE = 600  # сек: окно на прохождение OAuth-редиректа
_state_signer = URLSafeTimedSerializer(config.SESSION_SECRET, salt="vk-oauth-state")


class VKError(Exception):
    """Сбой обращения к VK ID (выключено / сеть / не-2xx / битый ответ / неверный state)."""


def enabled() -> bool:
    return bool(config.OAUTH_VK_ENABLED and config.VK_CLIENT_ID and config.VK_CLIENT_SECRET)


def make_pkce() -> tuple[str, str]:
    """(code_verifier, code_challenge) по RFC 7636, метод S256."""
    verifier = secrets.token_urlsafe(64)[:96]
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return verifier, challenge


def seal_state(state: str, verifier: str) -> str:
    """Подписанная cookie-полезная нагрузка (state + PKCE-verifier) на время редиректа."""
    return _state_signer.dumps({"s": state, "v": verifier})


def open_state(sealed: str | None) -> tuple[str, str] | None:
    """Распаковать cookie state. None — нет/просрочено/подделано."""
    if not sealed:
        return None
    try:
        data = _state_signer.loads(sealed, max_age=_STATE_MAX_AGE)
    except (BadSignature, Exception):  # noqa: BLE001 — любой сбой подписи/срока → отказ
        return None
    if not isinstance(data, dict) or "s" not in data or "v" not in data:
        return None
    return data["s"], data["v"]


def authorize_url(redirect_uri: str, state: str, code_challenge: str) -> str:
    """URL согласия VK ID (302 сюда из /auth/vk/start)."""
    q = urllib.parse.urlencode({
        "response_type": "code",
        "client_id": config.VK_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "scope": "email",
    })
    return f"{AUTHORIZE_URL}?{q}"


def _post_form(url: str, fields: dict[str, str], *, timeout: float = 15.0) -> dict:
    """Синхронный POST x-www-form-urlencoded → JSON (исполняется в треде)."""
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode()[:300]
        except Exception:  # noqa: BLE001
            pass
        raise VKError(f"VK ID HTTP {e.code}: {detail}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise VKError(f"VK ID недоступен: {e}") from e
    except (ValueError, json.JSONDecodeError) as e:
        raise VKError("VK ID вернул невалидный ответ") from e


async def exchange_code(code: str, code_verifier: str, device_id: str, redirect_uri: str) -> dict:
    """Обмен authorization_code → токен. Возврат dict VK ID (access_token, user_id, …)."""
    if not enabled():
        raise VKError("VK ID выключен (нет OAUTH_VK_ENABLED/CLIENT_ID/SECRET)")
    fields = {
        "grant_type": "authorization_code",
        "code": code,
        "code_verifier": code_verifier,
        "client_id": config.VK_CLIENT_ID,
        "client_secret": config.VK_CLIENT_SECRET,
        "device_id": device_id,
        "redirect_uri": redirect_uri,
    }
    res = await asyncio.to_thread(_post_form, TOKEN_URL, fields)
    if not res.get("access_token") or not res.get("user_id"):
        raise VKError("VK ID не вернул access_token/user_id")
    return res


async def fetch_user(access_token: str) -> dict:
    """Профиль (first_name/last_name/email) по access_token. Сбой → пустой dict (не критично)."""
    try:
        res = await asyncio.to_thread(
            _post_form, USERINFO_URL,
            {"client_id": config.VK_CLIENT_ID, "access_token": access_token},
        )
    except VKError:
        return {}
    return res.get("user") or res
