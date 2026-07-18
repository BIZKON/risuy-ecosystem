"""Каркас коллектора (спека S2 §5) — контракт для S3/S13.

Вечный цикл (эталон bot-telegram/worker.py: тик в try/except, цикл не падает),
throttle 1–2 с с jitter между итерациями (PRD §5), floodwait_backoff-примитив,
graceful shutdown по SIGTERM/SIGINT (дорабатывает итерацию, эмитит, выходит 0).

ЗАПРЕТЫ (контракт): не импортировать bot-telegram/admin-panel код; не логировать
значения env/секретов (в S3 сюда приедут session-strings); тенант-фолбэков нет.
"""
from __future__ import annotations

import abc
import asyncio
import logging
import random
import signal

import redis.asyncio as redis

from . import config, envelope, streams

logger = logging.getLogger("engine.worker")


class BaseWorker(abc.ABC):
    """Базовый воркер-продьюсер: наследники реализуют ТОЛЬКО collect_once()."""

    def __init__(self, redis_url: str) -> None:
        self._redis_url = redis_url
        self._r: redis.Redis | None = None
        # At-least-once до Redis в пределах жизни процесса: события, чей XADD
        # не прошёл, копятся здесь и досылаются следующими витками run().
        self._pending: list[dict] = []
        self.stop = asyncio.Event()

    @abc.abstractmethod
    async def collect_once(self) -> list[dict]:
        """Одна итерация сбора: список событий, собранных envelope.build().

        Контракт S3: курсор источника двигать ТОЛЬКО после возврата из
        collect_once — доставку в Redis добивает каркас (_pending-буфер,
        at-least-once в пределах жизни процесса). Вернул события — каркас
        отвечает за них; терять их при недоступном Redis нельзя.
        """

    async def emit(self, event: dict) -> None:
        """Отправка с fail-closed валидацией: ядовитое событие НЕ эмитится.

        Валидное событие попадает в _pending и досылается до успеха (сразу или
        следующими витками run()). Клиент Redis создаётся лениво — emit можно
        звать и вне run() (одноразовый скрипт, смоук) без AttributeError.
        """
        try:
            envelope.parse(event)
        except envelope.EnvelopeError as exc:
            logger.error("emit: ядовитое событие не отправлено: %s", exc)
            return
        self._pending.append(event)
        await self._flush_pending()

    async def _flush_pending(self) -> bool:
        """Досылает _pending по порядку; True — буфер пуст (всё в Redis).

        При ошибке XADD событие ОСТАЁТСЯ в буфере (лог warning, не exception):
        пачка не теряется молча, как при прежнем «emit под generic-except».
        """
        if self._r is None:
            self._r = redis.from_url(self._redis_url)
        while self._pending:
            try:
                await streams.emit(self._r, self._pending[0])
            except Exception as exc:  # noqa: BLE001 — Redis недоступен: копим, не теряем
                logger.warning(
                    "emit: XADD не прошёл (%s: %s) — в буфере %d событий, дошлём позже",
                    type(exc).__name__, exc, len(self._pending),
                )
                return False
            self._pending.pop(0)
        return True

    async def floodwait_backoff(self, retry_after_s: float) -> None:
        """Пауза не меньше retry_after источника + jitter (вызывают коллекторы S3)."""
        delay = max(float(retry_after_s), config.THROTTLE_MIN_S) + random.uniform(0, 1)
        logger.warning("FloodWait: пауза %.1f с", delay)
        await asyncio.sleep(delay)

    def _install_signals(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self.stop.set)

    async def run(self) -> None:
        if self._r is None:  # мог появиться раньше — ленивый emit вне run()
            self._r = redis.from_url(self._redis_url)
        self._install_signals()
        logger.info("Воркер %s запущен", type(self).__name__)
        while not self.stop.is_set():
            try:
                # Сначала дослать недоставленное; пока _pending не пуст,
                # collect_once НЕ вызывается — при Redis-аутедже буфер не
                # раздувается, а собранные события не теряются (B1).
                if await self._flush_pending():
                    for event in await self.collect_once():
                        await self.emit(event)
            except Exception:  # noqa: BLE001 — цикл не должен падать
                logger.exception("Сбой итерации сбора")
            # Throttle 1–2 с с jitter; stop будит досрочно (graceful).
            delay = random.uniform(config.THROTTLE_MIN_S, config.THROTTLE_MAX_S)
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=delay)
            except TimeoutError:
                pass
        # Последняя попытка дослать буфер перед выходом (graceful; при живом
        # Redis ничего не теряем, при мёртвом — честный warning от flush).
        await self._flush_pending()
        await self._r.aclose()
        logger.info("Воркер %s остановлен (graceful)", type(self).__name__)
