"""Единый сервис-бот-уведомитель (Слой B): ОДИН бот на платформу постит карточки эскалаций и
триггеров в группы менеджеров клиентов. Клиент добавляет ИМЕННО его в свою группу и вставляет
её id — так не нужно пускать разговорный бот тенанта в чужие менеджерские группы.

Токен — config.NOTIFIER_BOT_TOKEN (env). Не задан → get_notifier_bot() == None, и вызывающий
(escalation/triggers) фолбэчит на разговорный бот тенанта — чтобы текущая эскалация Школы не
сломалась, пока владелец не создал нотификатора у BotFather и не прописал токен.

Бот ленивый-singleton (один aiohttp-session на процесс). Прокси — общий config.TELEGRAM_PROXY
(api.telegram.org из РФ-ЦОД только через него — как у разговорных ботов). aiogram импортируется
ЛЕНИВО внутри функции: модуль должен оставаться импортируемым в смоук-venv без aiogram (как
escalation.py)."""
import logging

import config

logger = logging.getLogger(__name__)

_bot = None        # aiogram.Bot | None
_inited = False


def get_notifier_bot():
    """Bot нотификатора (singleton) или None, если NOTIFIER_BOT_TOKEN не задан."""
    global _bot, _inited
    if _inited:
        return _bot
    _inited = True
    token = (config.NOTIFIER_BOT_TOKEN or "").strip()
    if not token:
        _bot = None
        return None
    from aiogram import Bot
    from aiogram.client.session.aiohttp import AiohttpSession
    session = AiohttpSession(proxy=config.TELEGRAM_PROXY) if config.TELEGRAM_PROXY else None
    _bot = Bot(token=token, session=session) if session else Bot(token=token)
    logger.info("Нотификатор инициализирован (прокси: %s)", bool(config.TELEGRAM_PROXY))
    return _bot
