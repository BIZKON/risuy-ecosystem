"""collector-common (спека S3 §6) — общий poll-каркас коллекторов поверх BaseWorker.

`SourcePollingWorker` реализует ЕДИНЫЙ `collect_once()`: перечитывает `engine.sources`
под ролью engine_rw (owner обходит RLS → платформенный сбор ВСЕХ тенантов), опрашивает
каждый уникальный источник через абстрактный `poll_source()`, мапит собранное в envelope
v1 через абстрактный `to_envelope()` и отдаёт события каркасу — эмит и добивание доставки
(`_pending`, at-least-once) делает BaseWorker. Наследники (TG/VK, Task 5/6) реализуют
ТОЛЬКО две абстракции + фейк-режим; общий цикл, курсор, heartbeat и floodwait-хук — здесь.

Ключевые контракты (см. base_worker.BaseWorker):
  • Курсор источника двигаем ТОЛЬКО ПОСЛЕ подтверждённой доставки предыдущей пачки в Redis.
    Реализация: намеченные курсоры копятся в self._deferred и пишутся в PG в НАЧАЛЕ
    следующего collect_once — а run() вызывает collect_once лишь когда `_flush_pending()`
    вернул True (буфер пуст = прошлая пачка в Redis). Итог: при краше теряется максимум
    один дубль (пере-соберём с непродвинутого курсора), НИКОГДА не теряем событие.
    Это строже, чем «двигать курсор до возврата» (там окно потери при краше до XADD) —
    осознанное расхождение с упрощением плана в пользу живого контракта base_worker.
  • App-дедуп опроса: один (source_kind, external_ref) может принадлежать N тенантам —
    опрашиваем уникальный external_ref РОВНО один раз (не жжём лимиты); повторные события
    всё равно задедупит raw_messages downstream. Курсор/last_polled_at — по external_ref
    (платформенный, общий на источник), пишем во все тенант-строки этого external_ref.

ЗАПРЕТЫ (контракт engine): не импортировать bot-telegram/admin-panel код; секреты
(session/токен/ключ/proxy-пароль) НИКОГДА в логи; тенант-фолбэков нет.
"""
from __future__ import annotations

import abc
import dataclasses
import datetime as dt
import logging
import time

import asyncpg
import redis.asyncio as redis

from engine.common import accounts, base_worker, config, db

logger = logging.getLogger("engine.collector")


@dataclasses.dataclass
class PolledSource:
    """Уникальный (по external_ref) источник для опроса — результат app-дедупа N тенантов.

    `cursor` — рабочий, mutable: poll_source ВПРАВЕ его подвинуть (напр. VK next_from);
    каркас персистит его в PG после подтверждённой доставки. `tenant_ids` — все тенанты,
    следящие за этим external_ref (нужны наследнику VK для сборки поискового запроса из
    их профилей; каркасом не используются).
    """

    source_kind: str
    external_ref: str
    kind: str | None
    cursor: str | None
    tenant_ids: list[str]


class SourcePollingWorker(base_worker.BaseWorker, abc.ABC):
    """Каркас коллектора-поллера: наследники реализуют poll_source() + to_envelope().

    Наследник обязан задать классовый атрибут SOURCE_KIND (envelope.SOURCE_KINDS:
    'telegram'|'vk'|…) — это и source_kind сборки, и канал пула accounts.
    """

    #: source_kind сборки envelope И канал engine.accounts; задаёт наследник.
    SOURCE_KIND: str = ""

    def __init__(self, redis_url: str, dsn: str) -> None:
        super().__init__(redis_url)
        if not self.SOURCE_KIND:
            raise ValueError("SourcePollingWorker: наследник обязан задать SOURCE_KIND")
        self._dsn = dsn
        # PG-пул создаётся лениво в collect_once (как _pending/redis ленивы в base_worker)
        # — не переопределяем финальный run(); пул закрывается вместе с процессом.
        self._pg: asyncpg.Pool | None = None
        self._sources: list[PolledSource] | None = None
        self._sources_loaded_at: float = 0.0
        # Намеченные, но ещё не персистнутые курсоры: external_ref → cursor.
        self._deferred: dict[str, str | None] = {}
        self._collected_total: int = 0

    # ── Абстракции наследника ────────────────────────────────────────────────
    @abc.abstractmethod
    async def poll_source(self, source: PolledSource) -> list:
        """Собрать сырьё одного источника (канал-специфика TG/VK).

        Возвращает список НЕПРОЗРАЧНЫХ для каркаса item'ов — каждый item ДОЛЖЕН нести
        всё, что нужно to_envelope (включая external_ref источника для fallback chat_ref).
        МОЖЕТ подвинуть source.cursor (напр. VK next_from) — каркас его персистит.
        Канал-исключения (floodwait/ban) наследник обрабатывает сам (см. _on_floodwait);
        неожиданное исключение каркас поймает и пропустит источник (цикл не падает).
        """

    @abc.abstractmethod
    def to_envelope(self, item) -> dict:
        """Смапить один item в событие envelope v1 (ЧИСТАЯ функция — envelope.build()).

        Синхронная, без сети: тестируется на фикстурах без MTProto/HTTP.
        """

    # ── Единый цикл сбора ─────────────────────────────────────────────────────
    async def collect_once(self) -> list[dict]:
        if self._pg is None:
            self._pg = await db.make_pool(self._dsn)
        # (1) Персистим курсоры прошлой итерации. Безопасно: run() зовёт collect_once
        #     только когда _flush_pending() вернул True (прошлая пачка уже в Redis) →
        #     это и есть «двигать курсор ПОСЛЕ подтверждённой доставки».
        await self._persist_deferred()
        # (2) Перечитываем sources из PG раз в SOURCES_POLL_INTERVAL_S (app-дедуп external_ref).
        if (
            self._sources is None
            or (time.monotonic() - self._sources_loaded_at) >= config.SOURCES_POLL_INTERVAL_S
        ):
            await self._reload_sources()
        # (3) Опрашиваем каждый уникальный источник; курсор намечаем в _deferred (применим в (1)).
        events: list[dict] = []
        for source in self._sources or []:
            try:
                items = await self.poll_source(source)
            except Exception:  # noqa: BLE001 — один битый источник не валит всю итерацию
                logger.exception(
                    "poll_source упал (%s:%s) — источник пропущен",
                    source.source_kind, source.external_ref,
                )
                continue
            # Наметить курсор+пульс к персисту (даже при 0 событий — last_polled_at heartbeat).
            self._deferred[source.external_ref] = source.cursor
            for item in items:
                try:
                    events.append(self.to_envelope(item))
                except Exception:  # noqa: BLE001 — битый item не валит пачку
                    logger.exception("to_envelope упал — событие пропущено")
        if events:
            self._collected_total += len(events)
            logger.info(
                "Собрано %d событий с %d источников (%s), всего за жизнь процесса %d",
                len(events), len(self._sources or []), self.SOURCE_KIND, self._collected_total,
            )
        return events

    async def _reload_sources(self) -> None:
        """Перечитать enabled-источники канала из PG, схлопнув N тенантов в уникальный external_ref.

        БЕЗ set_tenant — сознательно: owner engine_rw обходит RLS, коллектор собирает
        ПЛАТФОРМЕННО для всех тенантов (спека §2). Курсор/last_polled_at трактуем как
        свойство external_ref (общий на источник), а не тенант-строки.
        """
        rows = await self._pg.fetch(
            "select tenant_id, kind, external_ref, cursor from engine.sources "
            "where enabled and source_kind = $1 order by external_ref",
            self.SOURCE_KIND,
        )
        grouped: dict[str, PolledSource] = {}
        for row in rows:
            ext = row["external_ref"]
            ps = grouped.get(ext)
            if ps is None:
                grouped[ext] = PolledSource(
                    source_kind=self.SOURCE_KIND,
                    external_ref=ext,
                    kind=row["kind"],
                    cursor=row["cursor"],
                    tenant_ids=[str(row["tenant_id"])],
                )
            else:
                ps.tenant_ids.append(str(row["tenant_id"]))
                if ps.cursor is None and row["cursor"] is not None:
                    ps.cursor = row["cursor"]
        self._sources = list(grouped.values())
        self._sources_loaded_at = time.monotonic()
        logger.info("Перечитаны источники %s: %d уникальных external_ref",
                    self.SOURCE_KIND, len(self._sources))

    async def _persist_deferred(self) -> None:
        """Записать намеченные курсоры/пульс во ВСЕ тенант-строки соответствующих external_ref."""
        if not self._deferred:
            return
        for ext, cursor in self._deferred.items():
            await self._pg.execute(
                "update engine.sources set last_polled_at = now(), cursor = $3 "
                "where source_kind = $1 and external_ref = $2",
                self.SOURCE_KIND, ext, cursor,
            )
        self._deferred = {}

    # ── Floodwait-хук наследника ──────────────────────────────────────────────
    async def _on_floodwait(self, account_id: str, seconds: float) -> None:
        """Наследник поймал канал-floodwait → пауза каркаса + пометка аккаунта в БД.

        floodwait_backoff держит паузу ≥ retry_after; mark_account_floodwait выводит
        аккаунт из выборки claim_account до floodwait_until (last_error БЕЗ session).
        """
        await self.floodwait_backoff(seconds)
        if self._pg is None:
            self._pg = await db.make_pool(self._dsn)
        until = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=float(seconds))
        await accounts.mark_account_floodwait(self._pg, account_id, until)

    # ── Heartbeat / readiness ─────────────────────────────────────────────────
    async def readiness(self) -> bool:
        """/readyz: Redis-ping + PG-ping + «есть пригодный аккаунт канала».

        Выполняется на ОТДЕЛЬНОМ event-loop health-сервера (health.serve) — поэтому НЕ
        трогаем self._pg / self._r (они привязаны к loop воркера; cross-loop asyncpg/redis
        → RuntimeError → /readyz вечный 503). Каждая проба — своё короткоживущее соединение
        (паттерн db.ping).
        """
        if not await db.ping(self._dsn):
            return False
        try:
            r = redis.from_url(self._redis_url)
            try:
                await r.ping()
            finally:
                await r.aclose()
        except Exception:  # noqa: BLE001 — Redis недостижим → not-ready
            return False
        return await self._account_ready()

    async def _account_ready(self) -> bool:
        """Есть ли пригодный аккаунт канала — read-only проба (НЕ claim: без мутации/локов).

        Наследник на env-токене (VK P1) или в фейк-режиме переопределяет → True.
        """
        try:
            conn = await asyncpg.connect(self._dsn)
            try:
                val = await conn.fetchval(
                    "select 1 from engine.accounts where channel = $1 and status = 'active' "
                    "and (floodwait_until is null or floodwait_until < now()) limit 1",
                    self.SOURCE_KIND,
                )
            finally:
                await conn.close()
            return val is not None
        except Exception:  # noqa: BLE001 — PG/таблица недостижимы → not-ready
            return False
