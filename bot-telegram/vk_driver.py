"""Слой C: драйвер канала ВКонтакте (сообщения сообщества) — Bots Long Poll + messages.send.

Транспорт для разговорного ИИ-бота в ВК: приём `message_new` через Bots Long Poll
(`groups.getLongPollServer` → цикл `act=a_check`), отправка ответа через `messages.send`
(с ОБЯЗАТЕЛЬНЫМ уникальным `random_id`). На сырых `aiohttp`-вызовах (без SDK).

`api.vk.com` — российский сервис, доступен из РФ-ЦОД НАПРЯМУЮ, без прокси (в отличие от
`api.telegram.org`). Чистые функции (parse/link/random_id/failed) тестируемы без сети.

Идентичность: `from_id` (>0 — пользователь) — это «external_id» лида в канале vk (ляжет в
leads.vk_user_id, db._user_col('vk')). Отвечать на `peer_id` (в личке = from_id).
"""
import asyncio
import json
import logging
import os

logger = logging.getLogger(__name__)

VK_API = "https://api.vk.com/method"
# Версия VK API: актуальная стабильная 5.x. Можно переопределить env VK_API_VERSION.
VK_API_VERSION = os.environ.get("VK_API_VERSION", "5.199")
_LP_WAIT = 25                       # сек: держим long-poll соединение
_HTTP_TIMEOUT_PAD = 15             # запас к wait для HTTP-таймаута клиента
_RANDOM_ID_MOD = 0x7FFFFFFF        # int32-положительный диапазон для random_id


def next_random_id(counter: int) -> int:
    """Уникальный random_id для messages.send (ОБЯЗАТЕЛЕН: повтор → VK молча НЕ отправит).
    Детерминированно-уникальный по счётчику отправок (без Math.random — переживает рестарт
    в паре с persisted-счётчиком; для нашего кейса достаточно монотонного по процессу)."""
    return (counter % _RANDOM_ID_MOD) + 1


def parse_message_new(update: dict) -> tuple[int, int, str, dict | None] | None:
    """(from_id, peer_id, text, payload) из события Long Poll, либо None если это не входящее
    сообщение пользователя. Формат Long Poll 5.x: object.message.{from_id,peer_id,text,payload}.
    Игнорим сообщения от сообщества (from_id < 0) и нет адреса. payload — dict из JSON-строки
    нажатой кнопки клавиатуры (Слой C: команды /купить), либо None. Пропускаем, если нет ни
    текста, ни payload. Защищён от кривого payload."""
    try:
        if (update or {}).get("type") != "message_new":
            return None
        msg = (((update or {}).get("object") or {}).get("message")) or {}
        from_id = int(msg.get("from_id") or 0)
        peer_id = int(msg.get("peer_id") or 0)
        text = (msg.get("text") or "").strip()
        payload = None
        raw = msg.get("payload")
        if raw:
            try:
                p = json.loads(raw)
                payload = p if isinstance(p, dict) else None
            except (ValueError, TypeError):
                payload = None
        if from_id <= 0 or not peer_id or (not text and payload is None):
            return None
        return from_id, peer_id, text, payload
    except (TypeError, ValueError):
        return None


def vk_client_link(vk_user_id: int) -> tuple[str, str]:
    """(url, подпись) на профиль клиента ВК — для карточки менеджеру (аналог tg://user?id=)."""
    return f"https://vk.com/id{vk_user_id}", "Написать клиенту в ВКонтакте"


def vk_attachment(media_type: str, owner_id: int, media_id: int) -> str:
    """VK attachment-строка для messages.send: '<type><owner_id>_<media_id>' (owner_id сообщества
    отрицательный). media_type: 'photo' | 'doc'. Несколько вложений — через запятую."""
    return f"{media_type}{owner_id}_{media_id}"


def vk_media_type_for_kind(kind: str) -> str:
    """outbox/broadcast kind (photo|document|voice|audio) → VK media_type. Фото → photo;
    всё прочее (документ/голос/аудио) → doc (VK шлёт их как документ-вложение)."""
    return "photo" if kind == "photo" else "doc"


# ── HTTP-клиент VK (aiohttp; импорт ленивый — модуль тестируем без aiohttp/сети) ──
class VKError(Exception):
    pass


class VKBot:
    """Один бот сообщества ВК: long-poll приём + messages.send. token/group_id — из vault тенанта.
    on_message(from_id, peer_id, text, payload) — async-колбэк (пайплайн ответа; payload — dict
    нажатой кнопки или None). НЕ держит состояние БД."""

    def __init__(self, token: str, group_id: int, on_message):
        self.token = token
        self.group_id = int(group_id)
        self.on_message = on_message
        self._send_counter = 0
        self._session = None  # aiohttp.ClientSession

    async def _api(self, method: str, **params):
        """Вызов VK API. VK кладёт ошибку в ТЕЛО при HTTP 200 → проверяем 'error'."""
        params |= {"access_token": self.token, "v": VK_API_VERSION}
        async with self._session.get(f"{VK_API}/{method}", params=params) as r:
            data = await r.json()
        if "error" in data:
            raise VKError(f"{method}: {data['error']}")
        return data["response"]

    async def _get_lp(self) -> tuple[str, str, str]:
        """groups.getLongPollServer → (server, key, ts)."""
        lp = await self._api("groups.getLongPollServer", group_id=self.group_id)
        return lp["server"], lp["key"], str(lp["ts"])

    async def send(self, peer_id: int, text: str, *, keyboard: dict | None = None,
                   attachment: str | None = None) -> bool:
        """messages.send (random_id ОБЯЗАТЕЛЕН и уникален). keyboard — VK-клавиатура (dict → JSON).
        attachment — строка вложений ('photo<o>_<id>,doc<o>_<id>'). НЕ бросает — не роняет loop.
        Возвращает True при успешной отправке, False при ошибке (VK error-в-теле / сеть) — воркер
        рассылки/outbox по False помечает доставку failed/release; разговорный путь возврат игнорит."""
        import aiohttp
        self._send_counter += 1
        rid = next_random_id(self._send_counter)
        try:
            params = {"peer_id": int(peer_id), "message": text[:4000], "random_id": rid,
                      "access_token": self.token, "v": VK_API_VERSION}
            if keyboard is not None:
                params["keyboard"] = json.dumps(keyboard, ensure_ascii=False)
            if attachment:
                params["attachment"] = attachment
            async with self._session.post(f"{VK_API}/messages.send", data=params) as r:
                data = await r.json()
            if "error" in data:
                logger.error("VK messages.send error: %s", data["error"])
                return False
            return True
        except (aiohttp.ClientError, asyncio.TimeoutError, Exception) as e:  # noqa: BLE001
            logger.warning("VK messages.send не удался (peer=%s): %s", peer_id, e)
            return False

    async def _upload(self, upload_url: str, field: str, content: bytes, filename: str,
                      content_type: str) -> dict:
        """Multipart-загрузка байтов на upload-сервер VK (он свежий на КАЖДЫЙ файл — берётся из
        getMessagesUploadServer). upload-сервер отдаёт JSON иногда с text/plain → content_type=None."""
        import aiohttp
        form = aiohttp.FormData()
        form.add_field(field, content, filename=filename, content_type=content_type)
        async with self._session.post(upload_url, data=form) as r:
            return await r.json(content_type=None)

    async def send_photo(self, peer_id: int, content: bytes, *, caption: str = "",
                         filename: str = "photo.jpg") -> bool:
        """Фото в ЛС: photos.getMessagesUploadServer(peer_id) → upload(поле 'photo') →
        photos.saveMessagesPhoto → attachment 'photo<owner>_<id>' → messages.send. НЕ бросает.
        Возвращает True/False (успех доставки) — для ветвления воркера."""
        import aiohttp
        try:
            up = await self._api("photos.getMessagesUploadServer", peer_id=int(peer_id))
            res = await self._upload(up["upload_url"], "photo", content, filename, "image/jpeg")
            saved = await self._api("photos.saveMessagesPhoto", photo=res["photo"],
                                    server=res["server"], hash=res["hash"])
            ph = saved[0]
            return await self.send(peer_id, caption, attachment=vk_attachment("photo", ph["owner_id"], ph["id"]))
        except (aiohttp.ClientError, asyncio.TimeoutError, Exception) as e:  # noqa: BLE001
            logger.warning("VK send_photo не удался (peer=%s): %s", peer_id, e)
            return False

    async def send_document(self, peer_id: int, content: bytes, *, filename: str,
                            caption: str = "") -> bool:
        """Документ/файл в ЛС: docs.getMessagesUploadServer(type='doc',peer_id) → upload(поле 'file')
        → docs.save → attachment 'doc<owner>_<id>' → messages.send. НЕ бросает. Возвращает True/False."""
        import aiohttp
        try:
            up = await self._api("docs.getMessagesUploadServer", type="doc", peer_id=int(peer_id))
            res = await self._upload(up["upload_url"], "file", content, filename,
                                     "application/octet-stream")
            saved = await self._api("docs.save", file=res["file"], title=filename)
            doc = saved["doc"]
            return await self.send(peer_id, caption, attachment=vk_attachment("doc", doc["owner_id"], doc["id"]))
        except (aiohttp.ClientError, asyncio.TimeoutError, Exception) as e:  # noqa: BLE001
            logger.warning("VK send_document не удался (peer=%s): %s", peer_id, e)
            return False

    async def send_keyboard(self, peer_id: int, text: str, buttons: list[dict]) -> None:
        """Inline-клавиатура: buttons=[{label, payload(dict)}], по кнопке на строку. text-кнопка с
        payload: клик → новое message_new с этим payload (Слой C: витрина покупки). Cap 6 строк
        (по 1 кнопке/строку); inline-лимит VK = 6 строк (× до 5 кнопок/строку), наша раскладка внутри."""
        rows = [[{"action": {"type": "text", "label": (b["label"] or "")[:40],
                             "payload": json.dumps(b["payload"], ensure_ascii=False)}}]
                for b in buttons[:6]]
        await self.send(peer_id, text, keyboard={"inline": True, "buttons": rows})

    async def send_link(self, peer_id: int, text: str, url: str, label: str) -> None:
        """Сообщение с inline-кнопкой open_link (ссылка на оплату ЮKassa)."""
        kb = {"inline": True, "buttons": [[{"action": {"type": "open_link", "link": url,
                                                       "label": (label or "Оплатить")[:40]}}]]}
        await self.send(peer_id, text, keyboard=kb)

    async def run(self) -> None:
        """Bots Long Poll: getLongPollServer → цикл a_check c обработкой failed 1/2/3.
        Каждое message_new → on_message в отдельной таске (не блокируем poll)."""
        import aiohttp
        self._session = aiohttp.ClientSession()
        try:
            server, key, ts = await self._get_lp()
            logger.info("VK long-poll стартовал (group=%s)", self.group_id)
            while True:
                try:
                    async with self._session.get(
                        server, params={"act": "a_check", "key": key, "ts": ts, "wait": _LP_WAIT},
                        timeout=aiohttp.ClientTimeout(total=_LP_WAIT + _HTTP_TIMEOUT_PAD),
                    ) as r:
                        upd = await r.json()
                except (aiohttp.ClientError, asyncio.TimeoutError):
                    await asyncio.sleep(2)
                    continue
                except Exception:  # noqa: BLE001
                    logger.warning("VK long-poll: ошибка чтения", exc_info=True)
                    await asyncio.sleep(3)
                    continue
                failed = upd.get("failed")
                if failed:
                    server, key, ts = await self._handle_failed(failed, upd, server, key, ts)
                    continue
                ts = str(upd.get("ts", ts))            # ВСЕГДА продвигаем курсор
                for e in upd.get("updates", []):
                    parsed = parse_message_new(e)
                    if parsed:
                        asyncio.create_task(self._safe_handle(*parsed))
        finally:
            if self._session is not None:
                await self._session.close()

    async def _handle_failed(self, failed, upd, server, key, ts):
        """failed: 1→новый ts; 2→пере-getLongPollServer (ts оставить); 3→полный пере-get."""
        if failed == 1:
            return server, key, str(upd.get("ts", ts))
        if failed == 2:
            srv, k, _ = await self._get_lp()
            return srv, k, ts
        # failed == 3 (или иное) — полностью пере-получаем
        return await self._get_lp()

    async def _safe_handle(self, from_id, peer_id, text, payload=None):
        try:
            await self.on_message(from_id, peer_id, text, payload)
        except Exception:  # noqa: BLE001 — обработка одного сообщения не должна валить poll
            logger.warning("VK on_message упал (from=%s)", from_id, exc_info=True)
