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
