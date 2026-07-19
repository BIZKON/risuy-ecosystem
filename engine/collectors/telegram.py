"""collector-telegram (спека S3 §7) — Telethon push→queue→collect_once → envelope v1.

Модель «push→poll seam» (вариант (a) разведки — единственный, совместимый с контрактом
BaseWorker): Telethon-клиент живёт внутри run(), его хэндлер events.NewMessage кладёт сырое
сообщение в per-source буфер; poll_source() каркаса дренирует буфер и мапит в envelope.
Прямой emit из хэндлера ЗАПРЕЩЁН — он обошёл бы _pending/at-least-once/floodwait/graceful
BaseWorker (риск §13). Эмит и добивание доставки делает исключительно BaseWorker.

read-only (спека §2): ТОЛЬКО events.NewMessage; никаких get_participants; get_entity —
минимально (резолв каждого источника один раз на старте, дальше кэш Telethon).

Секреты (session_string / proxy-пароль) НИКОГДА не логируются (спека §2, §13); статус-переходы
аккаунта (floodwait/banned) пишет engine.common.accounts (last_error БЕЗ session).

Фейк-режим (FAKE_TELEGRAM = путь к JSON-фикстуре) гонит список событий через ТОТ ЖЕ
_to_envelope без MTProto — смоук проверяет маппинг/идемпотентность/floodwait/graceful.
"""
from __future__ import annotations

import asyncio
import collections
import dataclasses
import datetime as dt
import json
import logging
import urllib.parse

from engine.collectors import common
from engine.common import accounts, config, db, envelope, health

logger = logging.getLogger("engine.collector.telegram")


@dataclasses.dataclass
class CollectedMessage:
    """Нормализованное сырое сообщение — единый вход _to_envelope для боевого и фейк-путей.

    Поля минимальны и НЕ несут ПДн-контактов: sender_id — публичный числовой id автора
    (не телефон/username контакта). source_external_ref — ссылка/ref источника из
    engine.sources: fallback chat_ref для приватных каналов без @username.
    date — datetime (боевой Telethon message.date, tz-aware UTC) ИЛИ ISO-строка (фикстура).
    """

    chat_id: int
    message_id: int
    raw_text: str
    date: object
    sender_id: int | None
    chat_username: str | None
    source_external_ref: str


def _canon_chat_id(chat_id: int) -> int:
    """Telethon marked-id → каноничный внутренний id БЕЗ префикса -100 (риск §13).

    Telethon (get_peer_id) помечает peer'ы АРИФМЕТИКОЙ, не строковым префиксом:
      канал/супергруппа  marked = -(10**12 + internal)  → внутренний = -marked - 10**12;
      базовая группа     marked = -internal             → внутренний = -marked;
      пользователь        marked =  internal             → внутренний =  marked.
    Приводим marked к внутреннему id ЧИСЛОВОЙ инверсией (порог -10**12), чтобы форма
    external_id не зависела от библиотеки/пути и дедуп raw_messages не рассинхронился.
    ЗАФИКСИРОВАНО (regression в смоуке §13): строковый префикс '-100' был неверен — ложно
    срабатывал на базовых группах с id на «100» (−1005 → мусор) и промахивался на каналах с
    internal ≥ ~10**10 (marked '-102…'/'-110…' не начинается на '-100'). Смена формулы =
    рассинхрон дедупа.
    """
    if chat_id <= -(10**12):
        return -chat_id - 10**12  # канал/супергруппа
    if chat_id < 0:
        return -chat_id  # базовая группа
    return chat_id  # пользователь


def _as_utc(value) -> dt.datetime | None:
    """date фикстуры (ISO-строка) или Telethon message.date (datetime) → tz-aware UTC."""
    if value is None:
        return None
    moment = dt.datetime.fromisoformat(value) if isinstance(value, str) else value
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return moment.astimezone(dt.timezone.utc)


def _to_envelope(item: CollectedMessage) -> dict:
    """ЧИСТАЯ функция (без сети): CollectedMessage → событие envelope v1.

    external_id = {canon_chat_id}:{message_id} (chat_id БЕЗ -100 — спека §7);
    chat_ref = https://t.me/<username> для публичных, иначе fallback source_external_ref;
    author_ref = tg:<sender_id> (публичный id, не ПДн-контакт), опускается для анонимных;
    posted_at — tz-aware UTC; lang='ru'; metadata — канал-специфика (chat_username).
    """
    metadata: dict = {}
    if item.chat_username:
        metadata["chat_username"] = item.chat_username
    return envelope.build(
        "telegram",
        envelope.make_external_id("telegram", _canon_chat_id(item.chat_id), item.message_id),
        item.raw_text or "",
        chat_ref=(
            f"https://t.me/{item.chat_username}" if item.chat_username else item.source_external_ref
        ),
        author_ref=(f"tg:{item.sender_id}" if item.sender_id is not None else None),
        posted_at=_as_utc(item.date),
        lang="ru",
        metadata=metadata or None,
    )


def _socks5(proxy_ref: str | None):
    """accounts.proxy_ref (socks5://[user:pass@]host:port) → proxy-кортеж python-socks.

    None → без прокси. Значение proxy_ref НЕ логируем (несёт пароль — спека §13).
    Прокси — отдельный per-account слой (НЕ bot-telegram TELEGRAM_PROXY, спека §2).
    """
    if not proxy_ref:
        return None
    from python_socks import ProxyType  # ленивый импорт: только боевой путь

    parts = urllib.parse.urlparse(proxy_ref)
    return (
        ProxyType.SOCKS5, parts.hostname, parts.port, True,
        parts.username or None, parts.password or None,
    )


class TelegramCollector(common.SourcePollingWorker):
    """Коллектор Telegram: push→queue seam поверх SourcePollingWorker.

    Наследует единый collect_once/курсор/heartbeat/floodwait-хук каркаса; реализует лишь
    канал-специфику: боевой Telethon-клиент (или фейк-фикстура) наполняет per-source буферы,
    которые дренирует poll_source(); маппинг item→envelope делает чистый _to_envelope.
    """

    SOURCE_KIND = "telegram"

    def __init__(self, redis_url: str, dsn: str) -> None:
        super().__init__(redis_url, dsn)
        self._fake = bool(config.FAKE_TELEGRAM)
        # external_ref → накопленные сообщения (наполняет хэндлер/фикстура, дренирует poll_source).
        # deque(maxlen) ([critic-fix M4]): при недоступном Redis collect_once не зовётся → буфер
        # не дренируется; maxlen отсекает старейшие, чтобы память не росла неограниченно.
        # Push-модель: недренированное при shutdown ТЕРЯЕТСЯ (at-least-once base_worker покрывает
        # лишь события, УЖЕ возвращённые collect_once); переполнение = at-most-once на верхушке.
        self._buffers: dict[str, collections.deque[CollectedMessage]] = {}
        self._client = None  # боевой TelegramClient; None в фейке/до старта
        self._account_id: str | None = None  # активный аккаунт (floodwait/ban-переходы)
        self._entities: list = []  # резолвнутые сущности источников (для chats= хэндлера)
        # canon_chat_id → (external_ref, chat_username): резолв источников один раз на старте.
        self._chat_index: dict[int, tuple[str, str | None]] = {}

    # ── Абстракции каркаса ────────────────────────────────────────────────────
    def to_envelope(self, item: CollectedMessage) -> dict:
        return _to_envelope(item)

    async def poll_source(self, source: common.PolledSource) -> list[CollectedMessage]:
        """Дренировать буфер источника (наполнен push-хэндлером/фикстурой)."""
        buf = self._buffers.pop(source.external_ref, None)
        items = list(buf) if buf else []
        # Фейк: один проход по фикстуре — и graceful-выход (все буферы опустели). Боевой
        # режим крутится вечно (push), stop приходит только по SIGTERM (BaseWorker).
        if self._fake and not any(self._buffers.values()):
            self.stop.set()
        return items

    async def _account_ready(self) -> bool:
        """Фейк не требует аккаунта (нет MTProto) → readiness не блокируем на пуле."""
        if self._fake:
            return True
        return await super()._account_ready()

    # ── Жизненный цикл источника (push seam) ──────────────────────────────────
    async def run(self) -> None:
        """Обёртка BaseWorker.run(): поднять источник (Telethon-клиент/фикстуру) → цикл → закрыть.

        Пул PG заводим здесь (нужен резолву источников/claim ДО первого collect_once);
        collect_once каркаса переиспользует self._pg. Контракт run() BaseWorker не ломаем —
        лишь окружаем его стартом/остановом канал-клиента (super().run() отрабатывает цикл,
        _pending-добивание и graceful полностью).
        """
        self._pg = await db.make_pool(self._dsn)
        await self._reload_sources()
        await self._start_source()
        try:
            await super().run()
        finally:
            await self._stop_source()

    async def _start_source(self) -> None:
        if self._fake:
            self._load_fixture()
            return
        await self._start_client()

    def _load_fixture(self) -> None:
        """FAKE_TELEGRAM: список событий из JSON → per-source буферы (без сети)."""
        with open(config.FAKE_TELEGRAM, encoding="utf-8") as fh:
            raw = json.load(fh)
        for ev in raw:
            msg = CollectedMessage(
                chat_id=ev["chat_id"],
                message_id=ev["message_id"],
                raw_text=ev.get("raw_text", ""),
                date=ev.get("date"),
                sender_id=ev.get("sender_id"),
                chat_username=ev.get("chat_username"),
                source_external_ref=ev["source_external_ref"],
            )
            self._buffers.setdefault(msg.source_external_ref, []).append(msg)
        logger.info("Фейк-режим: загружено %d событий из фикстуры", len(raw))

    async def _start_client(self) -> None:
        """Боевой путь: claim аккаунт → Telethon connect → резолв источников → NewMessage-хэндлер.

        read-only. Неавторизованная/забаненная сессия → mark_account_banned + следующий аккаунт;
        FloodWait на резолве → _on_floodwait + следующий аккаунт; нет живого аккаунта → выходим
        (heartbeat degraded, коллектор не падает).
        """
        from telethon import TelegramClient, errors, events
        from telethon.sessions import StringSession
        from telethon.utils import get_peer_id

        while True:
            acc = await accounts.claim_account(self._pg, self.SOURCE_KIND)
            if acc is None:
                logger.warning("Нет живого TG-аккаунта — коллектор ждёт (heartbeat degraded)")
                return
            client = TelegramClient(
                StringSession(acc.session_string),
                int(config.req("TG_API_ID")), config.req("TG_API_HASH"),
                proxy=_socks5(acc.proxy_ref),
            )
            try:
                await client.connect()
                if not await client.is_user_authorized():
                    await accounts.mark_account_banned(self._pg, acc.id, "unauthorized")
                    await client.disconnect()
                    continue
                await self._resolve_sources(client, get_peer_id, errors)
            except errors.FloodWaitError as exc:
                await self._on_floodwait(acc.id, exc.seconds)
                await client.disconnect()
                continue
            except (errors.UserDeactivatedError, errors.AuthKeyError) as exc:
                await accounts.mark_account_banned(self._pg, acc.id, type(exc).__name__)
                await client.disconnect()
                continue
            client.add_event_handler(
                self._on_new_message, events.NewMessage(chats=self._entities or None)
            )
            self._client = client
            self._account_id = acc.id
            logger.info("Telethon-клиент поднят: %d источников под наблюдением", len(self._chat_index))
            return

    async def _resolve_sources(self, client, get_peer_id, errors) -> None:
        """Резолв каждого источника в сущность (get_entity — минимально, кэш Telethon).

        Строит self._chat_index (canon_chat_id → (external_ref, username)) для маршрутизации
        входящих событий и self._entities (для chats= хэндлера). Недоступный источник —
        пропускаем (не валим старт); FloodWait пробрасываем наверх (ротация аккаунта).
        """
        self._chat_index = {}
        self._entities = []
        for source in self._sources or []:
            try:
                entity = await client.get_entity(source.external_ref)
            except errors.FloodWaitError:
                raise
            except Exception:  # noqa: BLE001 — недоступный источник не валит старт клиента
                logger.warning("Источник не резолвится, пропущен: %s", source.external_ref)
                continue
            canon = _canon_chat_id(get_peer_id(entity))
            self._chat_index[canon] = (source.external_ref, getattr(entity, "username", None))
            self._entities.append(entity)

    async def _on_new_message(self, event) -> None:
        """events.NewMessage → per-source буфер (прямой emit ЗАПРЕЩЁН — ломает _pending, §13)."""
        indexed = self._chat_index.get(_canon_chat_id(event.chat_id))
        if indexed is None:
            return  # событие не из наблюдаемого источника
        external_ref, username = indexed
        msg = CollectedMessage(
            chat_id=event.chat_id,
            message_id=event.id,
            raw_text=event.raw_text or "",
            date=event.message.date,
            sender_id=event.sender_id,
            chat_username=username,
            source_external_ref=external_ref,
        )
        self._buffers.setdefault(external_ref, []).append(msg)

    async def _stop_source(self) -> None:
        """Graceful-останов канал-клиента (буфер дренируется caller'ом до вызова)."""
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:  # noqa: BLE001 — закрытие best-effort, не роняем выход
                logger.warning("Telethon-клиент: ошибка при отключении")
            self._client = None


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    dsn = config.req("ENGINE_DSN")
    redis_url = config.req("REDIS_URL")
    if not config.FAKE_TELEGRAM:
        # Боевой режим: vault-ключ и MTProto-креды обязательны (fail-fast на старте).
        from shared import vault
        if not vault.enabled():
            raise SystemExit("VAULT_MASTER_KEY не задан/невалиден — боевой TG-сбор невозможен")
        config.req("TG_API_ID")
        config.req("TG_API_HASH")
    collector = TelegramCollector(redis_url, dsn)
    health.serve(config.COLLECTOR_HEALTH_PORT, collector.readiness)
    asyncio.run(collector.run())


if __name__ == "__main__":
    main()
