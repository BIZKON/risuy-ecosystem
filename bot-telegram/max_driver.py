"""Слой C: драйвер канала MAX (мессенджер VK, max.ru) — long-poll GET /updates + POST /messages.

`platform-api.max.ru` — российская инфра (VK), доступна из РФ-ЦОД НАПРЯМУЮ, без прокси (в отличие
от api.telegram.org). Авторизация — HTTP-заголовок `Authorization: <token>` (без `Bearer`, без
query-токена). Приём — long-poll `GET /updates?marker=` (курсор как aiogram offset). Отправка —
`POST /messages?chat_id=<id>` тело `{text, format:"html"}`.

⚠️ Идентичность: лид = `message.sender.user_id` (→ leads.max_user_id). Отвечать на
`message.recipient.chat_id` — в личке он ≠ user_id (в отличие от tg/vk, где совпадают).

Чистые функции (parse/link) тестируемы без сети. aiohttp импортируется ЛЕНИВО."""
import asyncio
import json
import logging

logger = logging.getLogger(__name__)

MAX_API = "https://platform-api.max.ru"
_LP_TIMEOUT = 30          # сек long-poll
_HTTP_PAD = 15            # запас к таймауту HTTP-клиента
_LP_LIMIT = 100


def parse_message_created(update: dict) -> tuple[int, int, str] | None:
    """(user_id, chat_id, text) из события update_type=message_created, либо None если это не
    входящее текстовое сообщение. user_id — идентичность лида; chat_id — КУДА отвечать
    (recipient.chat_id, в личке ≠ user_id). Защищён от кривого payload."""
    try:
        if (update or {}).get("update_type") != "message_created":
            return None
        msg = (update or {}).get("message") or {}
        text = ((msg.get("body") or {}).get("text") or "").strip()
        user_id = int(((msg.get("sender") or {}).get("user_id")) or 0)
        chat_id = int(((msg.get("recipient") or {}).get("chat_id")) or 0)
        if user_id <= 0 or not chat_id or not text:
            return None
        return user_id, chat_id, text
    except (TypeError, ValueError):
        return None


def parse_message_callback(update: dict) -> tuple[int, int, dict | None, str] | None:
    """(user_id, chat_id, payload, callback_id) из update_type=message_callback (нажата inline-
    кнопка). Формат: callback.{payload, callback_id, user.user_id, message.recipient.chat_id}.
    payload — dict из JSON-строки кнопки (Слой C: команды покупки), либо None. None если не
    callback / нет адресата. Защищён от кривого payload."""
    try:
        if (update or {}).get("update_type") != "message_callback":
            return None
        cb = (update or {}).get("callback") or {}
        user_id = int(((cb.get("user") or {}).get("user_id")) or 0)
        # ⚠️ message — СИБЛИНГ callback на верхнем уровне апдейта (офиц. Go SDK / OpenAPI / реальные
        # payload'ы MAX: update.message.recipient.chat_id), НЕ вложен в callback. Фолбэк на
        # callback.message — на случай иной версии формата. (Навык max-bot-miniapp здесь ошибался.)
        msg_obj = (update or {}).get("message") or cb.get("message") or {}
        chat_id = int(((msg_obj.get("recipient") or {}).get("chat_id")) or 0)
        callback_id = cb.get("callback_id") or ""
        payload = None
        raw = cb.get("payload")
        if raw:
            try:
                p = json.loads(raw)
                payload = p if isinstance(p, dict) else None
            except (ValueError, TypeError):
                payload = None
        if user_id <= 0 or not chat_id:
            return None
        return user_id, chat_id, payload, callback_id
    except (TypeError, ValueError):
        return None


def max_client_link(max_user_id: int) -> tuple[str, str]:
    """(url, подпись) на клиента MAX для карточки менеджеру. Публичного профиля по id у MAX нет →
    url пустой, в карточке показываем id (менеджер отвечает через панель/диалог)."""
    return "", f"Клиент в MAX (id {max_user_id}) — ответьте через панель"


class MAXBot:
    """Один бот MAX тенанта: long-poll приём + send. token — из vault тенанта (max_bot_token).
    on_message(user_id, chat_id, text) — async-колбэк пайплайна ответа."""

    def __init__(self, token: str, on_message, on_callback=None):
        self.token = token
        self.on_message = on_message
        self.on_callback = on_callback   # Слой C: нажатие inline-кнопки (покупка)
        self._session = None  # aiohttp.ClientSession

    async def send(self, chat_id: int, text: str, *, attachments: list | None = None) -> None:
        """POST /messages?chat_id=<id>. attachments — вложения MAX (inline_keyboard и т.п.).
        НЕ бросает — ответ не должен ронять loop."""
        import aiohttp
        body = {"text": text[:4000], "format": "html"}
        if attachments:
            body["attachments"] = attachments
        try:
            async with self._session.post(
                f"{MAX_API}/messages", params={"chat_id": int(chat_id)}, json=body,
            ) as r:
                if r.status != 200:
                    logger.error("MAX messages HTTP %s: %s", r.status, (await r.text())[:200])
        except (aiohttp.ClientError, asyncio.TimeoutError, Exception) as e:  # noqa: BLE001
            logger.warning("MAX send не удался (chat=%s): %s", chat_id, e)

    async def send_keyboard(self, chat_id: int, text: str, buttons: list[dict]) -> None:
        """inline_keyboard с callback-кнопками: buttons=[{label, payload(dict)}], по кнопке на строку.
        Клик → update message_callback с payload (Слой C: витрина покупки)."""
        rows = [[{"type": "callback", "text": (b["label"] or "")[:128],
                  "payload": json.dumps(b["payload"], ensure_ascii=False)}] for b in buttons]
        await self.send(chat_id, text,
                        attachments=[{"type": "inline_keyboard", "payload": {"buttons": rows}}])

    async def send_link(self, chat_id: int, text: str, url: str, label: str) -> None:
        """inline_keyboard с кнопкой-ссылкой (оплата ЮKassa)."""
        att = [{"type": "inline_keyboard", "payload": {"buttons": [
            [{"type": "link", "text": (label or "Оплатить")[:128], "url": url}]]}}]
        await self.send(chat_id, text, attachments=att)

    async def answer_callback(self, callback_id: str, text: str | None = None) -> None:
        """Best-effort ответ на callback (убрать «часики» на кнопке): POST /answers?callback_id=.
        НЕ бросает — оплата уже отправлена отдельным сообщением, точный формат уточняется live."""
        import aiohttp
        if not callback_id:
            return
        try:
            body = {"notification": text[:200]} if text else {}
            async with self._session.post(
                f"{MAX_API}/answers", params={"callback_id": callback_id}, json=body,
            ) as r:
                if r.status != 200:
                    logger.info("MAX answer_callback HTTP %s", r.status)
        except (aiohttp.ClientError, asyncio.TimeoutError, Exception) as e:  # noqa: BLE001
            logger.info("MAX answer_callback не удался: %s", e)

    async def run(self) -> None:
        """Long-poll GET /updates с курсором marker. Каждое message_created → on_message в таске."""
        import aiohttp
        # Authorization — на уровне сессии (применяется ко всем запросам).
        self._session = aiohttp.ClientSession(
            headers={"Authorization": self.token, "Content-Type": "application/json"})
        marker = None
        try:
            # best-effort: снять webhook, иначе long-poll и webhook взаимоисключающи (могут не прийти updates).
            try:
                async with self._session.get(f"{MAX_API}/subscriptions") as r:
                    subs = (await r.json()).get("subscriptions") or []
                for s in subs:
                    if s.get("url"):
                        # ⚠️ url — QUERY-параметр (?url=), НЕ JSON-тело: офиц. MAX API
                        # (DELETE /subscriptions?url=...), а тело DELETE многие стеки игнорируют →
                        # webhook молча не снимется и конфликтнёт с long-poll (часть updates уйдёт
                        # на webhook). Лог статуса — чтобы тихий no-op не маскировал провал отписки.
                        async with self._session.delete(
                            f"{MAX_API}/subscriptions", params={"url": s["url"]}) as dr:
                            if dr.status != 200:
                                logger.info("MAX delete subscription HTTP %s (url=%s)", dr.status, s["url"])
            except Exception:  # noqa: BLE001 — best-effort, не критично
                pass
            logger.info("MAX long-poll стартовал")
            while True:
                params = {"timeout": _LP_TIMEOUT, "limit": _LP_LIMIT}
                if marker is not None:
                    params["marker"] = marker
                try:
                    async with self._session.get(
                        f"{MAX_API}/updates", params=params,
                        timeout=aiohttp.ClientTimeout(total=_LP_TIMEOUT + _HTTP_PAD),
                    ) as r:
                        data = await r.json()
                except (aiohttp.ClientError, asyncio.TimeoutError):
                    await asyncio.sleep(2)
                    continue
                except Exception:  # noqa: BLE001
                    logger.warning("MAX long-poll: ошибка чтения", exc_info=True)
                    await asyncio.sleep(3)
                    continue
                for upd in data.get("updates", []):
                    msg = parse_message_created(upd)
                    if msg:
                        asyncio.create_task(self._safe_handle(*msg))
                        continue
                    cb = parse_message_callback(upd)   # Слой C: нажата inline-кнопка (покупка)
                    if cb:
                        if self.on_callback is not None:
                            asyncio.create_task(self._safe_callback(*cb))
                        continue
                    # Диагностика непротестированной зоны: сообщение/коллбэк пришёл, но не распарсился
                    # (вероятно recipient.chat_id отсутствует в личке — по OpenAPI оба поля nullable).
                    # Сейчас такое молча терялось бы; логируем СЫРОЙ апдейт, чтобы живой тест в ЛС сразу
                    # показал реальную форму recipient (chat_id/user_id) — и точечную правку адресации
                    # можно было сделать по факту, а не гадая (см. handoff: chat_id-null в личке MAX).
                    if (upd or {}).get("update_type") in ("message_created", "message_callback"):
                        logger.warning(
                            "MAX: %s не распарсился (recipient без chat_id? личка?): %s",
                            upd.get("update_type"), json.dumps(upd, ensure_ascii=False)[:600])
                marker = data.get("marker", marker)   # продвигаем курсор
        finally:
            if self._session is not None:
                await self._session.close()

    async def _safe_handle(self, user_id, chat_id, text):
        try:
            await self.on_message(user_id, chat_id, text)
        except Exception:  # noqa: BLE001
            logger.warning("MAX on_message упал (user=%s)", user_id, exc_info=True)

    async def _safe_callback(self, user_id, chat_id, payload, callback_id):
        try:
            await self.on_callback(user_id, chat_id, payload, callback_id)
        except Exception:  # noqa: BLE001
            logger.warning("MAX on_callback упал (user=%s)", user_id, exc_info=True)
