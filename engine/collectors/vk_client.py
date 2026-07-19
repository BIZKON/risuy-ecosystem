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
        Боевой путь: POST {VK_API_BASE}/{method}, access_token+v в ТЕЛЕ (data), НЕ в query-URL:
        так токен не попадает в URL и не может утечь в логи aiohttp/прокси/трейсбек ([critic-fix
        I1], риск §2/§13). Не-JSON тело (5xx/HTML/капча) → своя ошибка БЕЗ текста тела и URL.
        """
        if self._api is not None:
            return await self._api(method, params)

        import aiohttp  # ленивый импорт: модуль тестируем/импортируем без aiohttp/сети

        if self._session is None:
            self._session = aiohttp.ClientSession()
        # Токен — в data-body POST, а не в query-URL: URL несёт лишь имя метода (без секрета).
        body = dict(params)
        body["access_token"] = self._token
        body["v"] = config.VK_API_VERSION
        async with self._session.post(f"{config.VK_API_BASE}/{method}", data=body) as resp:
            try:
                # content_type=None: VK иногда отдаёт JSON как text/plain (канон vk_driver._upload).
                data = await resp.json(content_type=None)
            except (aiohttp.ClientError, ValueError):
                # Не-JSON тело: НЕ прокидываем оригинал наружу — его текст/URL могут нести
                # фрагмент запроса. Своя ошибка лишь с HTTP-кодом (from None рвёт цепочку).
                raise VKError(-1, f"не-JSON ответ VK, HTTP {resp.status}") from None
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
