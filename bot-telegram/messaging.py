"""Единый слой исходящих сообщений + лог переписки.

Зачем модуль:
  • ОДИН process-wide token-bucket (~15/с) перед КАЖДОЙ отправкой бота — воронка, Лия,
    прогрев, outbox, рассылки делят общий лимит Telegram (~30/с). Берём 15 с headroom под
    интерактив; рассылка уступает приоритет (§5.3 плана).
  • ЕДИНООБРАЗНАЯ обработка TelegramRetryAfter (429) во всех путях: sleep(retry_after+1),
    запись НЕ помечать отправленной (caller решает повтор). Раньше nurture/Лия 429 не ловили.
  • Зеркалирование каждого исходящего в messages (direction='out', source=...).
  • LoggingMiddleware — лог входящих ДО роутинга, мягкий резолв lead_id, изоляция от воронки.

Несущий инвариант: ничего из этого НЕ меняет логику воронки/гейта/Лии — только оборачивает
отправку (rate-limit + лог) и добавляет лог входящих. Все ошибки лога изолированы.
"""
import asyncio
import logging
import time
from typing import Any, Awaitable, Callable

from aiogram import Bot, BaseMiddleware
from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import (
    InlineKeyboardButton, InlineKeyboardMarkup, Message, TelegramObject,
)

import config
import db
import texts

logger = logging.getLogger(__name__)

# Лимиты длины Telegram (§5.11): обычный текст ≤4096, caption с файлом ≤1024.
TEXT_LIMIT = 4096
CAPTION_LIMIT = 1024


def unsubscribe_markup() -> InlineKeyboardMarkup:
    """Inline-футер «Отписаться» для КАЖДОЙ рассылки (152-ФЗ/38-ФЗ — простой отказ в
    самом сообщении, §5.8). callback_data='unsub' → on_unsub (идемпотентно). Прикрепляется
    воркером рассылок ко всем рассылочным сообщениям (текст и медиа)."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=texts.UNSUBSCRIBE_BTN, callback_data="unsub")],
    ])


# ── Process-wide token-bucket с приоритетом интерактиву ──────────────────────
# Приоритеты acquire(): чем меньше число — тем раньше обслуживается при нехватке
# токенов. Интерактив (воронка/Лия/служебка) обгоняет фон (рассылка/outbox), даже
# если фон занял весь бакет батчем — живой consent/гейт/ответ Лии не встаёт в хвост.
PRIO_INTERACTIVE = 0
PRIO_BACKGROUND = 1


class _TokenBucket:
    """Асинхронный token-bucket с приоритетной очередью. Все send_* идут через один экземпляр.

    rate токенов/с, ёмкость = rate (бёрст в один «секундный» запас). Корректен в одном
    event-loop (без потоков). Приоритет интерактиву (план §5.3 — «приоритет интерактиву,
    рассылка уступает») реализован так:

      • КРИТИЧНО: бакет НЕ удерживает мьютекс через `await sleep`. Раньше acquire() спал с
        зажатым Lock — один фон-вызов (рассылка) сериализовал за собой ВСЕХ строго FIFO, и
        живой consent/гейт/ответ Лии вставал в хвост за батчем рассылки (+секунды задержки).
        Теперь ожидающие спят на персональных Future, а единый планировщик `_pump` будит их
        ПО ПРИОРИТЕТУ, как только накапливается целый токен. Lock держим лишь на короткие
        синхронные секции (refill + постановка в очередь / выдача токена) — без await внутри.
      • Очередь приоритизирована ключом (prio, seq): меньший prio (интерактив) будится
        первым; внутри одного prio — FIFO по seq. Свободный токен мгновенно отдаётся, только
        если впереди нет ожидающего с не-большим приоритетом (иначе бы фон обгонял интерактив).

    Дополнительно фон сам себя притормаживает (worker.py гонит рассылку батчами с паузами) —
    это второй слой уступки, но НЕ единственный: приоритет здесь гарантирует интерактив даже
    при полностью выбранном бакете.
    """

    def __init__(self, rate: float):
        self.rate = max(rate, 0.1)
        self.capacity = self.rate
        self._tokens = self.rate
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()
        # Упорядоченный набор ожидающих: (prio, seq) → Future. Меньший ключ будится первым.
        self._waiters: dict[tuple[int, int], "asyncio.Future[None]"] = {}
        self._seq = 0
        self._pump_handle: asyncio.TimerHandle | None = None

    def _refill(self) -> None:
        now = time.monotonic()
        self._tokens = min(self.capacity, self._tokens + (now - self._updated) * self.rate)
        self._updated = now

    async def acquire(self, prio: int = PRIO_BACKGROUND) -> None:
        """Берёт один токен; при нехватке ждёт, уступая очередь интерактиву.

        Lock держим только на короткую синхронную секцию — НЕ через sleep/await. Спим на
        собственном Future, который будит `_pump` в порядке приоритета по мере накопления.
        """
        async with self._lock:
            self._refill()
            # Свободный токен берём сразу, только если впереди нет приоритетного ожидающего.
            if self._tokens >= 1.0 and not self._has_earlier_waiter(prio):
                self._tokens -= 1.0
                return
            self._seq += 1
            key = (prio, self._seq)
            fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
            self._waiters[key] = fut
            self._schedule_pump_locked()
        try:
            await fut  # токен уже списан планировщиком при пробуждении (см. _pump)
        except asyncio.CancelledError:
            # Отменили ожидание — вернуть наш токен (если уже списан) и убрать из очереди.
            async with self._lock:
                if self._waiters.pop(key, None) is None:
                    # Нас успели разбудить (токен списан), но ожидание отменено — вернём токен.
                    self._tokens = min(self.capacity, self._tokens + 1.0)
                    self._schedule_pump_locked()
            raise

    def _has_earlier_waiter(self, prio: int) -> bool:
        """Есть ли в очереди ожидающий, которого нужно обслужить раньше нас (prio ≤ наш)."""
        return any(wp <= prio for (wp, _seq) in self._waiters)

    def _schedule_pump_locked(self) -> None:
        """Планирует ближайший прогон планировщика (вызывать ТОЛЬКО под self._lock)."""
        if self._pump_handle is not None or not self._waiters:
            return
        self._refill()
        delay = max(0.0, 1.0 - self._tokens) / self.rate
        self._pump_handle = asyncio.get_running_loop().call_later(delay, self._pump)

    def _pump(self) -> None:
        """Планировщик: будит ожидающих по приоритету, пока есть целые токены.

        Запускается из call_later (в event-loop, не во время чужой корутины → синхронные
        мутации безопасны). Списывает токен ЗА КАЖДОГО разбуженного (резерв), чтобы повторный
        acquire проснувшегося не пересчитал бакет в минус. Перепланирует себя, если очередь
        не пуста, на момент накопления следующего токена.
        """
        self._pump_handle = None
        self._refill()
        while self._waiters and self._tokens >= 1.0:
            key = min(self._waiters)
            fut = self._waiters.pop(key)
            self._tokens -= 1.0  # резервируем токен за разбуженного
            if not fut.done():
                fut.set_result(None)
            else:
                # Ожидание отменили до пробуждения — токен не нужен, вернуть.
                self._tokens = min(self.capacity, self._tokens + 1.0)
        if self._waiters:
            delay = max(0.0, 1.0 - self._tokens) / self.rate
            self._pump_handle = asyncio.get_running_loop().call_later(delay, self._pump)


_bucket = _TokenBucket(config.BROADCAST_RATE)


async def _rate_limited_call(
    coro_factory: Callable[[], Awaitable[Any]], *, prio: int = PRIO_BACKGROUND
) -> Any:
    """Прогоняет один Telegram-вызов через bucket + единый 429-ретрай.

    prio — приоритет в бакете: PRIO_INTERACTIVE для воронки/Лии/служебных подсказок,
    PRIO_BACKGROUND (дефолт) для рассылок/outbox. coro_factory вызывается заново на
    каждой попытке (корутину нельзя await дважды). TelegramRetryAfter обрабатывается
    локально (sleep+повтор) — единообразно для всех путей. Прочие ошибки пробрасываются
    наверх (caller решает: pending/failed).
    """
    while True:
        await _bucket.acquire(prio)
        try:
            return await coro_factory()
        except TelegramRetryAfter as e:
            wait = getattr(e, "retry_after", 1) + 1
            logger.warning("429 от Telegram: ждём %s c и повторяем", wait)
            await asyncio.sleep(wait)
            # повтор: снова берём токен и пробуем — запись остаётся неотправленной до успеха


# Источники, считающиеся интерактивом (живой человек ждёт ответ) → приоритет в бакете.
# nurture/broadcast — фоновый bulk-маркетинг, уступает (план §5.3).
_INTERACTIVE_SOURCES = {"funnel", "liya", "system", "manual"}


def _prio_for_source(source: str | None) -> int:
    return PRIO_INTERACTIVE if source in _INTERACTIVE_SOURCES else PRIO_BACKGROUND


# ── Классификаторы типов входящих/исходящих ──────────────────────────────────
def classify_incoming(message: Message) -> tuple[str, str | None, str | None]:
    """Возвращает (kind, text, file_id) для входящего сообщения.

    Покрывает реальные типы Telegram; для неизвестного — ('other', None, None).
    Подпись медиа (caption) кладём в text, чтобы оператор видел сопроводительный текст.
    """
    if message.text is not None:
        return "text", message.text, None
    cap = message.caption
    if message.photo:
        return "photo", cap, message.photo[-1].file_id
    if message.document:
        return "document", cap, message.document.file_id
    if message.video:
        return "video", cap, message.video.file_id
    if message.voice:
        return "voice", cap, message.voice.file_id
    if message.video_note:
        return "video_note", None, message.video_note.file_id
    if message.audio:
        return "audio", cap, message.audio.file_id
    if message.animation:
        return "animation", cap, message.animation.file_id
    if message.sticker:
        return "sticker", None, message.sticker.file_id
    return "other", cap, None


# ── Лог входящих (outer-middleware на dp.message) ─────────────────────────────
class LoggingMiddleware(BaseMiddleware):
    """Пишет messages(direction='in') ДО роутинга — ловит всё вне фильтров состояния.

    Вешается ТОЛЬКО на dp.message (не на callback_query — нажатия кнопок не переписка).
    Резолв lead_id мягкий (нет лида → null, tg_user_id есть всегда). Любая ошибка лога
    проглатывается (логируется warning) — переписка-лог НИКОГДА не ломает воронку/Лию.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict], Awaitable[Any]],
        event: Message,
        data: dict,
    ) -> Any:
        try:
            user = event.from_user
            if user is not None and not user.is_bot:
                kind, text, file_id = classify_incoming(event)
                await db.log_message(
                    tg_user_id=user.id,
                    direction="in",
                    kind=kind,
                    text=text,
                    file_id=file_id,
                    source=None,  # source — только у исходящих
                    tg_message_id=event.message_id,
                )
        except Exception:  # noqa: BLE001 — изоляция от воронки
            logger.warning("LoggingMiddleware: не залогировал входящее", exc_info=True)
        return await handler(event, data)


# ── Исходящие: rate-limit + лог за один вызов ─────────────────────────────────
async def send_text(
    bot: Bot,
    tg_user_id: int,
    text: str,
    *,
    source: str,
    reply_markup: Any | None = None,
    log: bool = True,
) -> Message | None:
    """Шлёт текст через общий bucket и зеркалит в messages(direction='out', source=...).

    source ∈ funnel|liya|nurture|manual|broadcast|system. log=False — для служебных
    подсказок, которые не считаем диалогом (см. план §3). Возвращает отправленное
    сообщение (или пробрасывает не-429 исключение caller'у).
    """
    text = text[:TEXT_LIMIT]
    sent: Message = await _rate_limited_call(
        lambda: bot.send_message(tg_user_id, text, reply_markup=reply_markup),
        prio=_prio_for_source(source),
    )
    if log:
        await db.log_message(
            tg_user_id=tg_user_id,
            direction="out",
            kind="text",
            text=text,
            source=source,
            tg_message_id=getattr(sent, "message_id", None),
        )
    return sent


async def reply_text(
    message: Message,
    text: str,
    *,
    source: str,
    reply_markup: Any | None = None,
    log: bool = True,
) -> Message | None:
    """Версия для хендлеров воронки/Лии: отвечает в чат события через общий bucket+лог.

    Семантически = message.answer(...), но проходит rate-limit и логируется. Использует
    message.bot. Точечная замена message.answer в логируемых точках воронки.
    """
    text = text[:TEXT_LIMIT]
    sent: Message = await _rate_limited_call(
        lambda: message.answer(text, reply_markup=reply_markup),
        prio=_prio_for_source(source),
    )
    if log and message.from_user is not None:
        await db.log_message(
            tg_user_id=message.from_user.id,
            direction="out",
            kind="text",
            text=text,
            source=source,
            tg_message_id=getattr(sent, "message_id", None),
        )
    return sent


async def send_video_note(
    bot: Bot, tg_user_id: int, file_id: str, *, source: str, log: bool = True
) -> Message | None:
    """Видео-кружок через общий bucket + лог (kind='video_note')."""
    sent: Message = await _rate_limited_call(
        lambda: bot.send_video_note(tg_user_id, file_id),
        prio=_prio_for_source(source),
    )
    if log:
        await db.log_message(
            tg_user_id=tg_user_id,
            direction="out",
            kind="video_note",
            file_id=file_id,
            source=source,
            tg_message_id=getattr(sent, "message_id", None),
        )
    return sent


async def send_by_kind(
    bot: Bot, tg_user_id: int, kind: str, *, file_id: str, caption: str | None,
    source: str, reply_markup: Any | None = None, log: bool = True,
) -> Message | None:
    """Медиа-отправка с готовым file_id через общий bucket + лог + ПРИОРИТЕТ источника.

    Интерактивный аналог raw_send_* для воронки/Лии: тот же приоритет в бакете, что у
    send_text (PRIO_INTERACTIVE для funnel/liya/system), и зеркалирование в messages
    (raw_send_by_kind лога не делает — он для фонового воркера, который логирует сам с
    lead_id). Нужен для выдачи продукта-лид-магнита в воронке (фото/документ + caption).
    Под капотом — один raw_send_by_kind с нужным prio (без двойного rate-limit). caption
    капится до 1024 внутри raw_send_by_kind. log=False — без зеркала в тред.
    """
    sent: Message = await raw_send_by_kind(
        bot, tg_user_id, kind, file_id=file_id, caption=caption,
        reply_markup=reply_markup, prio=_prio_for_source(source),
    )
    if log:
        await db.log_message(
            tg_user_id=tg_user_id,
            direction="out",
            kind=kind,
            text=(caption[:CAPTION_LIMIT] if caption else None),
            file_id=file_id,
            source=source,
            tg_message_id=getattr(sent, "message_id", None),
        )
    return sent


# ── Тип отправки по MIME (картинка → photo, иначе document) ──────────────────
# Единственное правило вывода kind из mime на стороне БОТА. Для рассылок kind считает
# и хранит панель (broadcasts.kind, admin-panel/app.py::_kind_for_mime); для ПРОДУКТОВ
# каталога файл лежит с file_mime, а kind в БД не хранится — бот выводит его этой же
# функцией (схема schema_products.sql: «Тип отправки код выводит из file_mime тем же
# правилом, что и рассылки»). ПОБАЙТОВО совпадает с панельным _kind_for_mime:
# image/* → photo, всё прочее (pdf/doc/xls/zip/mp4/…) → document. gif с mime image/gif
# уйдёт как photo (как и в рассылках) — Telegram сам отрисует анимацию.
def kind_for_mime(mime: str | None) -> str:
    return "photo" if (mime or "").lower().startswith("image/") else "document"


# ── Низкоуровневые отправки для воркера рассылок (по kind, с rate-limit) ──────
# Лог в messages воркер делает сам (нужен lead_id), поэтому здесь log не трогаем —
# эти функции только проводят вызов через bucket+429.
# reply_markup опционален: воркёр рассылок прикрепляет футер «Отписаться» (§5.8); для
# точечных ответов оператора (outbox 1:1) markup не передаётся — это не маркетинг.
async def raw_send_text(bot: Bot, chat_id: int, text: str,
                        *, reply_markup: Any | None = None,
                        prio: int = PRIO_BACKGROUND) -> Message:
    return await _rate_limited_call(
        lambda: bot.send_message(chat_id, text[:TEXT_LIMIT], reply_markup=reply_markup),
        prio=prio,
    )


async def raw_send_by_kind(bot: Bot, chat_id: int, kind: str, *, file_id: str | None,
                           caption: str | None, reply_markup: Any | None = None,
                           prio: int = PRIO_BACKGROUND) -> Message:
    """Отправка по типу для рассылки/outbox с готовым file_id. caption капится до 1024.

    parse_mode НЕ задаём — всё plain (§5.11). Для неизвестного kind при наличии file_id
    шлём документом; иначе — текстом caption. reply_markup (опц.) прикрепляется ко всем
    типам — все send_*-методы Bot API его принимают (футер «Отписаться» в рассылке, §5.8).
    prio — приоритет в бакете: дефолт PRIO_BACKGROUND (рассылка/outbox), но воронка/Лия
    зовут с PRIO_INTERACTIVE через send_by_kind (живой человек ждёт выдачу).
    """
    cap = caption[:CAPTION_LIMIT] if caption else None
    if kind == "text" or not file_id:
        return await raw_send_text(bot, chat_id, caption or "", reply_markup=reply_markup, prio=prio)
    if kind == "photo":
        return await _rate_limited_call(
            lambda: bot.send_photo(chat_id, file_id, caption=cap, reply_markup=reply_markup), prio=prio)
    if kind == "video":
        return await _rate_limited_call(
            lambda: bot.send_video(chat_id, file_id, caption=cap, reply_markup=reply_markup), prio=prio)
    if kind == "voice":
        return await _rate_limited_call(
            lambda: bot.send_voice(chat_id, file_id, caption=cap, reply_markup=reply_markup), prio=prio)
    if kind == "video_note":
        return await _rate_limited_call(
            lambda: bot.send_video_note(chat_id, file_id, reply_markup=reply_markup), prio=prio)
    if kind == "audio":
        return await _rate_limited_call(
            lambda: bot.send_audio(chat_id, file_id, caption=cap, reply_markup=reply_markup), prio=prio)
    if kind == "animation":
        return await _rate_limited_call(
            lambda: bot.send_animation(chat_id, file_id, caption=cap, reply_markup=reply_markup), prio=prio)
    if kind == "sticker":
        return await _rate_limited_call(
            lambda: bot.send_sticker(chat_id, file_id, reply_markup=reply_markup), prio=prio)
    # document / other
    return await _rate_limited_call(
        lambda: bot.send_document(chat_id, file_id, caption=cap, reply_markup=reply_markup), prio=prio)


async def upload_file_to_chat(bot: Bot, chat_id: int, kind: str, *, content: bytes,
                              filename: str | None, mime: str | None,
                              caption: str | None) -> tuple[Message, str]:
    """Первичная заливка файла рассылки в СЛУЖЕБНЫЙ чат (§5.6) → (message, tg_file_id).

    Возвращает file_id для переиспользования. Тип метода — по kind. parse_mode plain.
    """
    from aiogram.types import BufferedInputFile
    buf = BufferedInputFile(content, filename=filename or "file")
    cap = caption[:CAPTION_LIMIT] if caption else None
    if kind == "photo":
        msg = await _rate_limited_call(lambda: bot.send_photo(chat_id, buf, caption=cap))
        file_id = msg.photo[-1].file_id
    elif kind == "video":
        msg = await _rate_limited_call(lambda: bot.send_video(chat_id, buf, caption=cap))
        file_id = msg.video.file_id
    elif kind == "voice":
        msg = await _rate_limited_call(lambda: bot.send_voice(chat_id, buf, caption=cap))
        file_id = msg.voice.file_id
    elif kind == "audio":
        msg = await _rate_limited_call(lambda: bot.send_audio(chat_id, buf, caption=cap))
        file_id = msg.audio.file_id
    elif kind == "animation":
        msg = await _rate_limited_call(lambda: bot.send_animation(chat_id, buf, caption=cap))
        file_id = msg.animation.file_id
    else:  # document / other
        msg = await _rate_limited_call(lambda: bot.send_document(chat_id, buf, caption=cap))
        file_id = msg.document.file_id
    return msg, file_id
