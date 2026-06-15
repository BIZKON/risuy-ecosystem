"""Слой C: драйвер канала MAX (мессенджер VK, max.ru) — long-poll GET /updates + POST /messages.

`platform-api.max.ru` — российская инфра (VK), доступна из РФ-ЦОД НАПРЯМУЮ, без прокси (в отличие
от api.telegram.org). Авторизация — HTTP-заголовок `Authorization: <token>` (без `Bearer`, без
query-токена). Приём — long-poll `GET /updates?marker=` (курсор как aiogram offset). Отправка —
`POST /messages?chat_id=<id>` тело `{text, format:"html"}`.

⚠️ Идентичность: лид = `message.sender.user_id` (→ leads.max_user_id). Отвечать на
`message.recipient.chat_id` — в личке он ≠ user_id (в отличие от tg/vk, где совпадают).

Чистые функции (parse/link) тестируемы без сети. aiohttp импортируется ЛЕНИВО."""
import asyncio
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


def max_client_link(max_user_id: int) -> tuple[str, str]:
    """(url, подпись) на клиента MAX для карточки менеджеру. Публичного профиля по id у MAX нет →
    url пустой, в карточке показываем id (менеджер отвечает через панель/диалог)."""
    return "", f"Клиент в MAX (id {max_user_id}) — ответьте через панель"


class MAXBot:
    """Один бот MAX тенанта: long-poll приём + send. token — из vault тенанта (max_bot_token).
    on_message(user_id, chat_id, text) — async-колбэк пайплайна ответа."""

    def __init__(self, token: str, on_message):
        self.token = token
        self.on_message = on_message
        self._session = None  # aiohttp.ClientSession

    async def send(self, chat_id: int, text: str) -> None:
        """POST /messages?chat_id=<id>. НЕ бросает — ответ не должен ронять loop."""
        import aiohttp
        try:
            async with self._session.post(
                f"{MAX_API}/messages", params={"chat_id": int(chat_id)},
                json={"text": text[:4000], "format": "html"},
            ) as r:
                if r.status != 200:
                    logger.error("MAX messages HTTP %s: %s", r.status, (await r.text())[:200])
        except (aiohttp.ClientError, asyncio.TimeoutError, Exception) as e:  # noqa: BLE001
            logger.warning("MAX send не удался (chat=%s): %s", chat_id, e)

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
                        await self._session.delete(f"{MAX_API}/subscriptions", json={"url": s["url"]})
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
                    parsed = parse_message_created(upd)
                    if parsed:
                        asyncio.create_task(self._safe_handle(*parsed))
                marker = data.get("marker", marker)   # продвигаем курсор
        finally:
            if self._session is not None:
                await self._session.close()

    async def _safe_handle(self, user_id, chat_id, text):
        try:
            await self.on_message(user_id, chat_id, text)
        except Exception:  # noqa: BLE001
            logger.warning("MAX on_message упал (user=%s)", user_id, exc_info=True)
