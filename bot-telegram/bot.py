"""Точка входа Telegram-бота.

Бот работает на long-polling. Рядом поднимаем крошечный HTTP health-эндпоинт на $PORT —
этого ждёт Timeweb App Platform (проксирует 80/443 на порт контейнера). Сам бот HTTP не использует.
"""
import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.types import BotCommand
from aiohttp import web

import config
import db
import nurture
from handlers import router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("bot")


async def _health(_request: web.Request) -> web.Response:
    return web.Response(text="ok")


async def _start_health() -> web.AppRunner:
    app = web.Application()
    app.router.add_get("/", _health)
    app.router.add_get("/health", _health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", config.PORT)
    await site.start()
    logger.info("Health-сервер на порту %s", config.PORT)
    return runner


async def main() -> None:
    await db.init()
    health = await _start_health()

    if config.TELEGRAM_PROXY:
        # Прячем креды прокси в логе — печатаем только host:port.
        logger.info("Telegram через прокси: %s", config.TELEGRAM_PROXY.rsplit("@", 1)[-1])
        bot = Bot(token=config.BOT_TOKEN, session=AiohttpSession(proxy=config.TELEGRAM_PROXY))
    else:
        bot = Bot(token=config.BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)

    nurture_task = asyncio.create_task(nurture.run(bot))
    try:
        # Кнопка-меню (☰) с /start — всегда доступна, тапом запускает воронку без ручного ввода.
        await bot.set_my_commands([BotCommand(command="start", description="Начать заново 🌷")])
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("Бот запущен на long-polling")
        await dp.start_polling(bot)
    finally:
        nurture_task.cancel()
        await health.cleanup()
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
