"""collector-vk (спека S3 §8) — aiohttp v5.199 newsfeed.search + wall.get → envelope v1.

VK-канал pull-модельный (в отличие от push-seam Telethon): poll_source() каркаса САМ
дёргает VK API через тонкий VKClient (vk_client.py, канон vk_driver._api). Наследует
единый collect_once/курсор/heartbeat/floodwait-хук SourcePollingWorker; реализует лишь
канал-специфику: сборку поискового запроса из профилей тенантов, вызов метода, маппинг
поста/коммента в envelope через ЧИСТЫЕ _post_to_envelope/_comment_to_envelope.

Токен (P1): сервисный VK access_token хранится как строка engine.accounts(channel='vk')
в vault-конверте (тот же CLI/claim_account, что у TG-сессий) — claim_account('vk') отдаёт
его в память воркера. Отдельного env-ключа НЕ заводим: конфиг Task 1 закрыт, а accounts-
машинерия (vault, ротация CLI, readiness _account_ready channel='vk') уже покрывает ключ.
[отклонение от плана §8 «или env P1»: выбран accounts-путь — verify живого кода, см. отчёт.]

Курсор: newsfeed.search отдаёт next_from → кладём в source.cursor (каркас персистит ПОСЛЕ
подтверждённой доставки). wall.get мониторит свежие посты (offset 0) — курсор не двигаем,
пересечения добьёт дедуп raw_messages downstream.

Лимиты: VK error 6 (rps) / 9 (flood control) / 29 (метод-лимит) → floodwait_backoff по
каналу (для сервисного ключа строки accounts может не быть — спека §8; помечаем лишь если
токен пришёл из accounts). Троттлинг каркаса 1–2 с держит ~rps-бюджет.

Фейк (FAKE_VK = путь к JSON {method: response}) — инжектируемый VKClient.api без сети:
смоук проверяет маппинг/курсор/дедуп/floodwait (DoD 6). Токен НИКОГДА не логируется.
"""
from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import json
import logging

from engine.collectors import common
from engine.collectors.vk_client import VKClient, VKError
from engine.common import accounts, config, db, envelope, health

logger = logging.getLogger("engine.collector.vk")

# newsfeed.search: максимум записей за вызов (спека §8, count=200).
_NEWSFEED_COUNT = 200
# wall.get: свежие посты стены (мониторим верхушку, offset 0).
_WALL_COUNT = 100
# Коды лимитов VK (rps / flood control / метод-лимит) → floodwait_backoff.
_VK_RATE_CODES = frozenset({6, 9, 29})
# Бэкофф по каналу при лимите VK: у метода нет retry_after → фикс-пауза (P1; тонкая
# настройка per-code/адаптив — S16e). max(seconds, THROTTLE_MIN)+jitter в floodwait_backoff.
_VK_FLOOD_BACKOFF_S = 2.0


def _as_list(value) -> list:
    """jsonb-поле (asyncpg по умолчанию отдаёт JSON текстом) → list; иначе []."""
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _as_dict(value) -> dict:
    """jsonb-поле → dict; иначе {}."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _unix_to_utc(value) -> dt.datetime | None:
    """VK date (unixtime) → tz-aware UTC datetime; None → None."""
    if value is None:
        return None
    return dt.datetime.fromtimestamp(int(value), tz=dt.timezone.utc)


def _post_to_envelope(item: dict) -> dict:
    """ЧИСТАЯ функция (без сети): сырой VK-пост → событие envelope v1.

    external_id = {owner_id}:{post_id} (знак сообщества '-' сохраняется — спека §8);
    chat_ref = https://vk.com/wall{owner_id}_{post_id}; author_ref = vk:{from_id}
    (публичный числовой id, не ПДн-контакт; опускается без from_id); posted_at из
    unixtime → UTC; lang='ru'; metadata помечает тип объекта.
    """
    owner_id = item["owner_id"]
    post_id = item["id"]
    from_id = item.get("from_id")
    return envelope.build(
        "vk",
        envelope.make_external_id("vk", owner_id, post_id),
        item.get("text") or "",
        chat_ref=f"https://vk.com/wall{owner_id}_{post_id}",
        author_ref=(f"vk:{from_id}" if from_id is not None else None),
        posted_at=_unix_to_utc(item.get("date")),
        lang="ru",
        metadata={"vk_object": "post"},
    )


def _comment_to_envelope(item: dict, owner_id, post_id) -> dict:
    """ЧИСТАЯ функция: сырой VK-коммент (+контекст owner/post) → envelope v1.

    external_id = {owner_id}:{post_id}:{comment_id} — три части (спека §8). owner_id/post_id
    приходят из контекста поста (wall.getComments не дублирует их в каждом item).
    P1-сейм: маппинг готов и покрыт смоуком, но wall.getComments в poll_source за флагом
    (P1 — только посты; комменты — S13/флаг источника).
    """
    comment_id = item["id"]
    from_id = item.get("from_id")
    return envelope.build(
        "vk",
        envelope.make_external_id("vk", owner_id, post_id, comment_id),
        item.get("text") or "",
        chat_ref=f"https://vk.com/wall{owner_id}_{post_id}?reply={comment_id}",
        author_ref=(f"vk:{from_id}" if from_id is not None else None),
        posted_at=_unix_to_utc(item.get("date")),
        lang="ru",
        metadata={"vk_object": "comment", "post": f"{owner_id}_{post_id}"},
    )


def _wall_ref(external_ref: str) -> dict | None:
    """external_ref источника-стены → аргумент wall.get: {owner_id:int} или {domain:str}.

    Числовой ref (в т.ч. отрицательный для сообществ) → owner_id; короткое имя / хвост
    vk.com-ссылки → domain. Непарсибельный → None (источник пропускается).
    """
    ref = external_ref.strip()
    if ref.startswith("http"):
        ref = ref.rstrip("/").rsplit("/", 1)[-1]
    if not ref:
        return None
    try:
        return {"owner_id": int(ref)}
    except ValueError:
        return {"domain": ref}


@dataclasses.dataclass
class VKItem:
    """Непрозрачный для каркаса item: тип объекта + сырой VK-dict (+ owner/post для коммента)."""

    kind: str          # 'post' | 'comment'
    raw: dict
    owner_id: object
    post_id: object


class VKCollector(common.SourcePollingWorker):
    """Коллектор VK: pull через VKClient поверх SourcePollingWorker.

    poll_source() дёргает newsfeed.search (kind channel/group) или wall.get (kind wall),
    мапит посты в VKItem; to_envelope() каркаса делает чистый _post_to_envelope. Лимиты VK
    → floodwait_backoff. Токен — сервисный ключ из engine.accounts(channel='vk') (vault).
    """

    SOURCE_KIND = "vk"

    def __init__(self, redis_url: str, dsn: str) -> None:
        super().__init__(redis_url, dsn)
        self._fake = bool(config.FAKE_VK)
        self._client: VKClient | None = None
        self._account_id: str | None = None  # строка accounts, если токен оттуда (иначе None)
        # Фейк: счётчик опросов по external_ref (serve-once + graceful — ниже).
        self._fake_polls: dict[str, int] = {}
        # Кэш поисковых параметров (q, lat, lon) по external_ref; сбрасывается на reload sources.
        self._search_cache: dict[str, tuple[str, float | None, float | None]] = {}
        self._search_cache_at: float = -1.0

    # ── Абстракции каркаса ────────────────────────────────────────────────────
    def to_envelope(self, item: VKItem) -> dict:
        if item.kind == "comment":
            return _comment_to_envelope(item.raw, item.owner_id, item.post_id)
        return _post_to_envelope(item.raw)

    async def poll_source(self, source: common.PolledSource) -> list[VKItem]:
        """Опросить источник VK по kind; лимиты VK → backoff; курсор двигает newsfeed."""
        await self._ensure_client()
        if self._client is None:
            return []  # нет токена → ждём (heartbeat degraded, коллектор не падает)
        if self._fake and self._fake_gate(source):
            return []  # фейк: фикстуру этому источнику уже отдавали (один проход)
        try:
            if source.kind in ("channel", "group"):
                return await self._poll_newsfeed(source)
            if source.kind == "wall":
                return await self._poll_wall(source)
            logger.warning(
                "VK: неизвестный kind %r источника %s — пропущен", source.kind, source.external_ref
            )
            return []
        except VKError as exc:
            if exc.code in _VK_RATE_CODES:
                logger.warning("VK лимит (code=%s) на %s → backoff", exc.code, source.external_ref)
                await self._on_vk_flood(_VK_FLOOD_BACKOFF_S)
                return []
            raise  # прочие VK-ошибки → каркас поймает, источник пропущен на этой итерации

    def _fake_gate(self, source: common.PolledSource) -> bool:
        """Фейк serve-once + graceful: True если фикстуру источнику уже отдавали (skip).

        pass1 источника → наметит курсор в _deferred; pass2 (через _persist_deferred в начале
        следующего collect_once) его ЗАПИШЕТ → курсор персистится ПОСЛЕ доставки (контракт).
        Когда КАЖДЫЙ источник опрошен ≥2 раза — выставляем stop (graceful-выход, без сети).
        """
        n = self._fake_polls.get(source.external_ref, 0) + 1
        self._fake_polls[source.external_ref] = n
        refs = {s.external_ref for s in (self._sources or [])}
        if refs and all(self._fake_polls.get(r, 0) >= 2 for r in refs):
            self.stop.set()
        return n >= 2

    # ── VK-методы ──────────────────────────────────────────────────────────────
    async def _poll_newsfeed(self, source: common.PolledSource) -> list[VKItem]:
        """newsfeed.search по ключевым словам профилей тенантов источника; курсор = next_from."""
        query, lat, lon = await self._search_params_for(source)
        if not query:
            logger.debug("VK newsfeed: нет ключевых слов профилей для %s — пропуск", source.external_ref)
            return []
        params: dict = {"q": query, "count": _NEWSFEED_COUNT}
        if source.cursor:
            params["start_from"] = source.cursor
        if lat is not None and lon is not None:
            params["latitude"] = lat
            params["longitude"] = lon
        resp = await self._client.call("newsfeed.search", params)
        resp = resp if isinstance(resp, dict) else {}
        items = resp.get("items") or []
        next_from = resp.get("next_from")
        if next_from:
            source.cursor = str(next_from)  # каркас персистит ПОСЛЕ подтверждённой доставки
        else:
            # Пагинация исчерпана (нет next_from) → сбрасываем курсор, чтобы следующий цикл
            # начал со свежей верхушки, а не залипал на глубине ([critic-fix M5]). Полный фикс
            # «монотонно вглубь» (периодический/по времени сброс даже В процессе пагинации) —
            # S16e/S17, спека §8/§12: свежие посты на верхушке до исчерпания страниц пропускаются.
            source.cursor = None
        return [VKItem("post", it, it.get("owner_id"), it.get("id")) for it in items]

    async def _poll_wall(self, source: common.PolledSource) -> list[VKItem]:
        """wall.get: свежая верхушка стены (курсор не двигаем — дедуп downstream)."""
        ref = _wall_ref(source.external_ref)
        if ref is None:
            logger.warning("VK wall: external_ref %s не разобран — пропуск", source.external_ref)
            return []
        params: dict = {"count": _WALL_COUNT}
        params.update(ref)
        resp = await self._client.call("wall.get", params)
        resp = resp if isinstance(resp, dict) else {}
        items = resp.get("items") or []
        return [VKItem("post", it, it.get("owner_id"), it.get("id")) for it in items]

    async def _search_params_for(
        self, source: common.PolledSource
    ) -> tuple[str, float | None, float | None]:
        """Собрать (q, lat, lon) из enabled-профилей тенантов источника (кэш до reload sources).

        q — объединение intent_keywords всех профилей (без дублей, регистр-нечувствительно);
        lat/lon — из geo первого профиля с координатами (latitude/longitude или lat/lon).
        """
        if self._search_cache_at != self._sources_loaded_at:
            self._search_cache = {}
            self._search_cache_at = self._sources_loaded_at
        cached = self._search_cache.get(source.external_ref)
        if cached is not None:
            return cached
        rows = await self._pg.fetch(
            "select intent_keywords, geo from engine.search_profiles "
            "where tenant_id = any($1::uuid[]) and enabled",
            source.tenant_ids,
        )
        keywords: list[str] = []
        seen: set[str] = set()
        lat: float | None = None
        lon: float | None = None
        for row in rows:
            for keyword in _as_list(row["intent_keywords"]):
                text = str(keyword).strip()
                if text and text.lower() not in seen:
                    seen.add(text.lower())
                    keywords.append(text)
            if lat is None:
                geo = _as_dict(row["geo"])
                glat = geo.get("latitude", geo.get("lat"))
                glon = geo.get("longitude", geo.get("lon"))
                if glat is not None and glon is not None:
                    lat, lon = float(glat), float(glon)
        result = (" ".join(keywords), lat, lon)
        self._search_cache[source.external_ref] = result
        return result

    # ── Клиент / токен ──────────────────────────────────────────────────────────
    async def _ensure_client(self) -> None:
        """Лениво поднять VKClient. Фейк → фикстура FAKE_VK; боевой → сервисный токен из accounts."""
        if self._client is not None:
            return
        if self._pg is None:
            self._pg = await db.make_pool(self._dsn)
        if self._fake:
            self._client = self._build_fake_client()
            return
        acc = await accounts.claim_account(self._pg, self.SOURCE_KIND)
        if acc is None:
            logger.warning(
                "Нет VK-сервисного токена (engine.accounts channel=vk) — VK-сбор ждёт (heartbeat degraded)"
            )
            return
        self._account_id = acc.id
        self._client = VKClient(acc.session_string)  # session_string = сервисный access_token

    def _build_fake_client(self) -> VKClient:
        """FAKE_VK: JSON {method: response} → инжектированный api-реплей (без сети).

        Значение response с ключом '__error__' (code/msg) → VKClient.call бросит VKError —
        так фикстура моделирует лимиты VK (6/9/29) для смоука.
        """
        with open(config.FAKE_VK, encoding="utf-8") as fh:
            fixture = json.load(fh)

        async def _replay(method: str, params: dict) -> object:
            entry = fixture.get(method)
            if isinstance(entry, dict) and "__error__" in entry:
                err = entry["__error__"]
                raise VKError(err.get("code"), err.get("msg", "fake"))
            return entry if entry is not None else {}

        logger.info("VK фейк-режим: фикстура на %d методов", len(fixture))
        return VKClient("fake", api=_replay)

    async def _on_vk_flood(self, seconds: float) -> None:
        """Лимит VK → пауза каркаса; пометка accounts — лишь если токен из accounts (спека §8)."""
        await self.floodwait_backoff(seconds)
        if self._account_id and self._pg is not None:
            until = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=float(seconds))
            await accounts.mark_account_floodwait(self._pg, self._account_id, until)

    async def _account_ready(self) -> bool:
        """Фейк не требует токена (нет сети) → readiness не блокируем на пуле."""
        if self._fake:
            return True
        return await super()._account_ready()

    # ── Жизненный цикл ──────────────────────────────────────────────────────────
    async def run(self) -> None:
        """Обёртка BaseWorker.run(): пул PG заранее (readiness/claim), aiohttp-сессия закрывается graceful."""
        self._pg = await db.make_pool(self._dsn)
        try:
            await super().run()
        finally:
            if self._client is not None:
                await self._client.aclose()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    dsn = config.req("ENGINE_DSN")
    redis_url = config.req("REDIS_URL")
    if not config.FAKE_VK:
        # Боевой режим: сервисный токен лежит в vault (engine.accounts) → мастер-ключ обязателен.
        from shared import vault
        if not vault.enabled():
            raise SystemExit("VAULT_MASTER_KEY не задан/невалиден — боевой VK-сбор невозможен (токен в vault)")
    collector = VKCollector(redis_url, dsn)
    health.serve(config.COLLECTOR_HEALTH_PORT, collector.readiness)
    asyncio.run(collector.run())


if __name__ == "__main__":
    main()
