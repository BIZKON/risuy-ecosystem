"""Тонкий aiohttp-клиент VK API (v5.199) для коллектора — канон bot-telegram/vk_driver._api.

Своя реализация (НЕ импорт bot-telegram — запрет контракта engine, [critic-fix I3]);
повторяет ЕДИНСТВЕННО важную идиому канона: «VK кладёт ошибку в ТЕЛО при HTTP 200 →
проверяем 'error'». `api.vk.com` — российский сервис, доступен из РФ-ЦОД НАПРЯМУЮ, без
прокси (в отличие от api.telegram.org — там per-account socks5 у Telethon-коллектора).

Транспорт инжектируем: конструктор принимает `api`-callable (паттерн FakeVKBot из
scripts/gate_member_smoke.py) — фейк-смоук подсовывает фикстуру без сети; дефолт (None) —
боевой aiohttp. Токен НИКОГДА не логируется (спека §2, §13).
"""
from __future__ import annotations

import logging
from typing import Awaitable, Callable

from engine.common import config

logger = logging.getLogger("engine.collector.vk")


class VKError(Exception):
    """Ошибка VK API (в теле при HTTP 200). code — числовой error_code (6/9/29 — лимиты)."""

    def __init__(self, code: int | None, message: str) -> None:
        super().__init__(f"VK[{code}]: {message}")
        self.code = code
        self.message = message


class VKClient:
    """Один сервисный VK-ключ: вызовы method → response.

    call(method, params) → data['response']; data['error'] → VKError(code, msg).
    Токен и v подмешиваются в параметры на боевом пути; в фейке — инжектированный `api`.
    """

    def __init__(
        self,
        token: str,
        *,
        api: Callable[[str, dict], Awaitable[object]] | None = None,
    ) -> None:
        self._token = token          # сервисный access_token; НЕ логировать
        self._api = api              # инжектируемый транспорт (фейк) или None → боевой aiohttp
        self._session = None         # aiohttp.ClientSession (ленивый, только боевой путь)

    async def call(self, method: str, params: dict) -> object:
        """Вызов VK API. Ошибка в теле (HTTP 200) → VKError. `response` — наружу.

        Фейк-путь: инжектированный `api(method, params)` (может сам бросить VKError).
        Боевой путь: GET {VK_API_BASE}/{method} с access_token+v; ошибка → VKError(code).
        """
        if self._api is not None:
            return await self._api(method, params)

        import aiohttp  # ленивый импорт: модуль тестируем/импортируем без aiohttp/сети

        if self._session is None:
            self._session = aiohttp.ClientSession()
        query = dict(params)
        query["access_token"] = self._token
        query["v"] = config.VK_API_VERSION
        async with self._session.get(f"{config.VK_API_BASE}/{method}", params=query) as resp:
            data = await resp.json()
        if isinstance(data, dict) and "error" in data:
            err = data["error"] if isinstance(data["error"], dict) else {}
            # error_msg НЕ содержит наш токен (VK его не эхоит) — логировать сообщение можно.
            raise VKError(err.get("error_code"), err.get("error_msg", "unknown"))
        return data["response"] if isinstance(data, dict) else data

    async def aclose(self) -> None:
        """Закрыть aiohttp-сессию (graceful). Фейк/незапущенный — no-op."""
        if self._session is not None:
            await self._session.close()
            self._session = None
