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


def parse_message_new(update: dict) -> tuple[int, int, str] | None:
    """(from_id, peer_id, text) из события Long Poll, либо None если это не входящее текстовое
    сообщение пользователя. Формат Long Poll 5.x: object.message.{from_id,peer_id,text}. Игнорим
    сообщения от сообщества (from_id < 0) и пустой текст. Защищён от кривого payload."""
    try:
        if (update or {}).get("type") != "message_new":
            return None
        msg = (((update or {}).get("object") or {}).get("message")) or {}
        from_id = int(msg.get("from_id") or 0)
        peer_id = int(msg.get("peer_id") or 0)
        text = (msg.get("text") or "").strip()
        if from_id <= 0 or not peer_id or not text:    # не юзер / нет адреса / пустой текст
            return None
        return from_id, peer_id, text
    except (TypeError, ValueError):
        return None


def vk_client_link(vk_user_id: int) -> tuple[str, str]:
    """(url, подпись) на профиль клиента ВК — для карточки менеджеру (аналог tg://user?id=)."""
    return f"https://vk.com/id{vk_user_id}", "Написать клиенту в ВКонтакте"


# ── HTTP-клиент VK (aiohttp; импорт ленивый — модуль тестируем без aiohttp/сети) ──
class VKError(Exception):
    pass


class VKBot:
    """Один бот сообщества ВК: long-poll приём + messages.send. token/group_id — из vault тенанта.
    on_message(from_id, peer_id, text) — async-колбэк (пайплайн ответа). НЕ держит состояние БД."""

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

    async def send(self, peer_id: int, text: str) -> None:
        """messages.send (random_id ОБЯЗАТЕЛЕН и уникален). НЕ бросает — ответ не должен ронять loop."""
        import aiohttp
        self._send_counter += 1
        rid = next_random_id(self._send_counter)
        try:
            params = {"peer_id": int(peer_id), "message": text[:4000], "random_id": rid,
                      "access_token": self.token, "v": VK_API_VERSION}
            async with self._session.post(f"{VK_API}/messages.send", data=params) as r:
                data = await r.json()
            if "error" in data:
                logger.error("VK messages.send error: %s", data["error"])
        except (aiohttp.ClientError, asyncio.TimeoutError, Exception) as e:  # noqa: BLE001
            logger.warning("VK messages.send не удался (peer=%s): %s", peer_id, e)

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

    async def _safe_handle(self, from_id, peer_id, text):
        try:
            await self.on_message(from_id, peer_id, text)
        except Exception:  # noqa: BLE001 — обработка одного сообщения не должна валить poll
            logger.warning("VK on_message упал (from=%s)", from_id, exc_info=True)
