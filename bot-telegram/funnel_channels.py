"""Канальные адаптеры воронки лид-магнита (I/O за единым интерфейсом). Машина состояний и тексты
живут в funnel.py и от канала не зависят. TG-адаптер обёртывает текущий messaging.* + aiogram-
клавиатуры (поведение Telegram сохраняется); VK/MAX — драйверы (Задачи 4/5). aiogram/messaging —
ленивый импорт (адаптер VK/MAX тестируем без них)."""
import logging
import db
import funnel

logger = logging.getLogger(__name__)


class TgFunnelChannel:
    """Адаптер Telegram: bot + user_id. Зеркалит прежний прямой код multiplex/funnel."""
    messenger = "tg"

    def __init__(self, bot, uid: int):
        self.bot = bot
        self.uid = uid

    async def send_text(self, text: str) -> None:
        import messaging
        await messaging.send_text(self.bot, self.uid, text, source="funnel")

    async def send_consent(self, text: str, privacy_url: str | None) -> None:
        import messaging
        await messaging.send_text(self.bot, self.uid, text, source="funnel",
                                  reply_markup=funnel._consent_kb({"privacy_url": privacy_url}))

    async def ask_phone(self, text: str) -> None:
        import messaging
        await messaging.send_text(self.bot, self.uid, text, source="funnel", reply_markup=funnel._phone_kb())

    async def ask_gate(self, text: str, channel_url: str | None) -> None:
        import messaging
        await messaging.send_text(self.bot, self.uid, text, source="funnel",
                                  reply_markup=funnel._gate_kb({"gate": {"channel_url": channel_url}}))

    async def check_subscription(self, gate_cfg: dict, uid: int) -> bool:
        return await funnel.is_subscribed(self.bot, (gate_cfg or {}).get("channel_id"), uid)

    async def deliver_text(self, text: str) -> None:
        import messaging
        await messaging.send_text(self.bot, self.uid, text, source="funnel")

    async def deliver_url(self, caption: str, url: str) -> None:
        import messaging
        await messaging.send_text(self.bot, self.uid, f"{caption}\n\n{url}", source="funnel",
                                  reply_markup=funnel._guide_kb(url))

    async def deliver_file(self, caption: str, product: dict) -> bool:
        """TG-выдача файла по file_tg_id. Возвращает True при успехе (для пометки guide_sent)."""
        import messaging
        if product.get("file_tg_id"):
            kind = messaging.kind_for_mime(product.get("file_mime"))
            await messaging.send_by_kind(self.bot, self.uid, kind, file_id=product["file_tg_id"],
                                         caption=caption, source="funnel")
            return True
        if product.get("link"):
            await self.deliver_url(caption, product["link"])
            return True
        return False

    async def deliver_video_note(self, file_id: str) -> None:
        import messaging
        await messaging.send_video_note(self.bot, self.uid, file_id, source="funnel")


class VkFunnelChannel:
    """Адаптер VK: vkbot + peer_id (адрес ответа) + uid (=from_id, идентичность vk_user_id).
    aiogram/messaging не импортируем — тестируем без них."""
    messenger = "vk"

    def __init__(self, vkbot, peer_id: int, uid: int):
        self.bot = vkbot
        self.peer_id = peer_id
        self.uid = uid

    async def send_text(self, text: str) -> None:
        await self.bot.send(self.peer_id, text)

    async def send_consent(self, text: str, privacy_url: str | None) -> None:
        await self.bot.send_keyboard(
            self.peer_id, text,
            [{"label": funnel.CONSENT_BTN, "payload": {"cmd": "consent_yes"}}],
        )
        if privacy_url:
            await self.bot.send_link(self.peer_id, funnel.PRIVACY_BTN, privacy_url, funnel.PRIVACY_BTN)

    async def ask_phone(self, text: str) -> None:
        await self.bot.send(self.peer_id, text + "\n\nНапишите номер телефона сообщением 🙂")

    async def ask_gate(self, text: str, channel_url: str | None) -> None:
        msg = text + (f"\n\n{channel_url}" if channel_url else "")
        await self.bot.send(self.peer_id, msg + "\n\nПодпишитесь и напишите «я подписался».")

    async def check_subscription(self, gate_cfg: dict, uid: int) -> bool:
        """Проверка членства в VK-сообществе-гейте. Fail-closed: нет group_id → держим гейт."""
        gid = (gate_cfg or {}).get("vk_gate_group_id")
        if not gid:
            return False
        return await self.bot.is_member(int(gid), uid)

    async def deliver_text(self, text: str) -> None:
        await self.bot.send(self.peer_id, text)

    async def deliver_url(self, caption: str, url: str) -> None:
        await self.bot.send(self.peer_id, f"{caption}\n\n{url}")

    async def deliver_file(self, caption: str, product: dict) -> bool:
        """VK-выдача: байты (file_bytes) → send_document; ссылка → deliver_url. file_tg_id — не используем."""
        if product.get("file_bytes"):
            return await self.bot.send_document(
                self.peer_id,
                product["file_bytes"],
                filename=product.get("file_name") or "file",
                caption=caption,
            )
        if product.get("link"):
            await self.deliver_url(caption, product["link"])
            return True
        return False

    async def deliver_video_note(self, file_id: str) -> None:
        return  # видео-кружок — TG-only, на VK пропуск


class MaxFunnelChannel:
    """Адаптер MAX: maxbot + chat_id (адрес ответа, ≠ uid в личке) + uid (=user_id, max_user_id).
    aiogram/messaging не импортируем — тестируем без них."""
    messenger = "max"

    def __init__(self, maxbot, chat_id: int, uid: int):
        self.bot = maxbot
        self.chat_id = chat_id
        self.uid = uid

    async def send_text(self, text: str) -> None:
        await self.bot.send(self.chat_id, text)

    async def send_consent(self, text: str, privacy_url: str | None) -> None:
        await self.bot.send_keyboard(
            self.chat_id, text,
            [{"label": funnel.CONSENT_BTN, "payload": {"cmd": "consent_yes"}}],
        )
        if privacy_url:
            await self.bot.send_link(self.chat_id, funnel.PRIVACY_BTN, privacy_url, funnel.PRIVACY_BTN)

    async def ask_phone(self, text: str) -> None:
        await self.bot.send(self.chat_id, text + "\n\nНапишите номер телефона сообщением 🙂")

    async def ask_gate(self, text: str, channel_url: str | None) -> None:
        msg = text + (f"\n\n{channel_url}" if channel_url else "")
        await self.bot.send(self.chat_id, msg + "\n\nПодпишитесь и напишите «я подписался».")

    async def check_subscription(self, gate_cfg: dict, uid: int) -> bool:
        """Проверка членства в MAX-канале-гейте. Fail-closed: нет chat_id → держим гейт."""
        cid = (gate_cfg or {}).get("max_gate_chat_id")
        if not cid:
            return False
        return await self.bot.is_channel_member(int(cid), uid)

    async def deliver_text(self, text: str) -> None:
        await self.bot.send(self.chat_id, text)

    async def deliver_url(self, caption: str, url: str) -> None:
        await self.bot.send(self.chat_id, f"{caption}\n\n{url}")

    async def deliver_file(self, caption: str, product: dict) -> bool:
        """MAX-выдача: байты → send_media (image/file по mime); ссылка → deliver_url. file_tg_id — не используем."""
        if product.get("file_bytes"):
            mt = "image" if (product.get("file_mime") or "").startswith("image/") else "file"
            return await self.bot.send_media(
                self.chat_id,
                media_type=mt,
                content=product["file_bytes"],
                caption=caption,
                filename=product.get("file_name") or "file",
            )
        if product.get("link"):
            await self.deliver_url(caption, product["link"])
            return True
        return False

    async def deliver_video_note(self, file_id: str) -> None:
        return  # видео-кружок — TG-only, на MAX пропуск
