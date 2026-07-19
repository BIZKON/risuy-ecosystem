"""collector-common (спека S3 §6) — общий poll-каркас коллекторов поверх BaseWorker.

`SourcePollingWorker` реализует ЕДИНЫЙ `collect_once()`: перечитывает `engine.sources`
под ролью engine_rw (owner обходит RLS → платформенный сбор ВСЕХ тенантов), опрашивает
каждый уникальный источник через абстрактный `poll_source()`, мапит собранное в envelope
v1 через абстрактный `to_envelope()` и отдаёт события каркасу — эмит и добивание доставки
(`_pending`, at-least-once) делает BaseWorker. Наследники (TG/VK, Task 5/6) реализуют
ТОЛЬКО две абстракции + фейк-режим; общий цикл, курсор, heartbeat и floodwait-хук — здесь.

Ключевые контракты (см. base_worker.BaseWorker):
  • `cursor` двигаем ТОЛЬКО ПОСЛЕ подтверждённой доставки предыдущей пачки в Redis.
    Реализация: намеченные курсоры копятся в self._deferred и пишутся в PG в НАЧАЛЕ
    следующего collect_once — а run() вызывает collect_once лишь когда `_flush_pending()`
    вернул True (буфер пуст = прошлая пачка в Redis). Итог: при краше теряется максимум
    один дубль (пере-соберём с непродвинутого курсора), НИКОГДА не теряем событие.
    Это строже, чем «двигать курсор до возврата» (там окно потери при краше до XADD) —
    осознанное расхождение с упрощением плана в пользу живого контракта base_worker.
    ГРАНИЧНЫЙ СЛУЧАЙ shutdown ([critic-fix I3]): курсор ПОСЛЕДНЕЙ итерации перед graceful/
    one-shot остаётся в _deferred и НЕ персистится (следующего collect_once нет; run()
    финальный и его не переопределяем). Последствие безопасно: на рестарте пере-соберём
    с непродвинутого курсора → максимум один дубль пачки, который добьёт dedup raw_messages
    downstream. Событий НЕ теряем (консервативное направление — дубль, не потеря).
  • `last_polled_at` (пульс «опрошено», heartbeat §10) — ОТДЕЛЬНО от курсора: пишем СРАЗУ
    после успешного poll_source в ТЕКУЩЕМ collect_once ([critic-fix I3]). Свежесть пульса
    не должна зависеть от следующего витка (иначе heartbeat degraded ложно при one-shot/паузе).
  • App-дедуп опроса: один (source_kind, external_ref, kind) может принадлежать N тенантам —
    опрашиваем уникальный ключ РОВНО один раз (не жжём лимиты); повторные события всё равно
    задедупит raw_messages downstream. Ключ включает `kind` ([critic-fix M2]): один external_ref
    у разных тенантов с РАЗНЫМ kind (напр. VK 'wall' vs 'channel') — это разные способы опроса,
    их нельзя схлопывать по первой строке. Курсор/last_polled_at — по (external_ref, kind)
    (платформенный на способ опроса), пишем во все тенант-строки с этим (external_ref, kind).

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
        # Намеченные, но ещё не персистнутые курсоры: (external_ref, kind) → cursor.
        # Ключ включает kind ([critic-fix M2]) — иначе два способа опроса одного external_ref
        # затирали бы курсор друг друга (wall cursor=None перезаписал бы newsfeed next_from).
        self._deferred: dict[tuple[str, str | None], str | None] = {}
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
            # Пульс «опрошено» пишем СРАЗУ (heartbeat §10 — свежесть не ждёт следующего витка,
            # [critic-fix I3]); курсор — в _deferred (персист ПОСЛЕ подтверждённой доставки).
            await self._touch_polled(source.external_ref, source.kind)
            self._deferred[(source.external_ref, source.kind)] = source.cursor
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
        """Перечитать enabled-источники канала из PG, схлопнув N тенантов в уникальный (external_ref, kind).

        БЕЗ set_tenant — сознательно: owner engine_rw обходит RLS, коллектор собирает
        ПЛАТФОРМЕННО для всех тенантов (спека §2). Курсор/last_polled_at трактуем как
        свойство (external_ref, kind) (общий на способ опроса, [critic-fix M2]), а не тенант-строки.
        """
        rows = await self._pg.fetch(
            "select tenant_id, kind, external_ref, cursor from engine.sources "
            "where enabled and source_kind = $1 order by external_ref",
            self.SOURCE_KIND,
        )
        # Ключ дедупа = (external_ref, kind) ([critic-fix M2]): разный kind у одного external_ref
        # (разные тенанты) = разные способы опроса → отдельные записи, не схлопывать по первой.
        grouped: dict[tuple[str, str | None], PolledSource] = {}
        for row in rows:
            key = (row["external_ref"], row["kind"])
            ps = grouped.get(key)
            if ps is None:
                grouped[key] = PolledSource(
                    source_kind=self.SOURCE_KIND,
                    external_ref=row["external_ref"],
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
        logger.info("Перечитаны источники %s: %d уникальных (external_ref, kind)",
                    self.SOURCE_KIND, len(self._sources))

    async def _persist_deferred(self) -> None:
        """Записать намеченные КУРСОРЫ во все тенант-строки соответствующих (external_ref, kind).

        Только cursor — контракт «двигать ПОСЛЕ подтверждённой доставки». last_polled_at сюда
        НЕ входит: пульс пишется сразу в collect_once (_touch_polled, [critic-fix I3]).
        """
        if not self._deferred:
            return
        for (ext, kind), cursor in self._deferred.items():
            await self._pg.execute(
                "update engine.sources set cursor = $3 "
                "where source_kind = $1 and external_ref = $2 and kind is not distinct from $4::text",
                self.SOURCE_KIND, ext, cursor, kind,
            )
        self._deferred = {}

    async def _touch_polled(self, external_ref: str, kind: str | None) -> None:
        """Пульс heartbeat: last_polled_at=now() во все тенант-строки (external_ref, kind).

        Пишется СРАЗУ после успешного poll_source ([critic-fix I3]) — свежесть §10 не зависит
        от следующего collect_once. НЕ трогает cursor (тот идёт через _deferred после доставки).
        """
        await self._pg.execute(
            "update engine.sources set last_polled_at = now() "
            "where source_kind = $1 and external_ref = $2 and kind is not distinct from $3::text",
            self.SOURCE_KIND, external_ref, kind,
        )

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
