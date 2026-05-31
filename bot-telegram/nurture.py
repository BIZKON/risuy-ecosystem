"""Фоновый прогрев. Раз в минуту ищет лидов, которым пора очередное касание, и шлёт.
Время касания берём от guide_sent_at + задержка — переживает рестарты (всё в БД)."""
import asyncio
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError

import config
import db
import texts

logger = logging.getLogger(__name__)

# (номер, колонка, задержка_сек, текст)
_FOLLOW_UPS = [
    (1, "follow_up_1_at", config.FOLLOW_UP_DELAYS[0], texts.FOLLOW_UP_1),
    (2, "follow_up_2_at", config.FOLLOW_UP_DELAYS[1], texts.FOLLOW_UP_2),
    (3, "follow_up_3_at", config.FOLLOW_UP_DELAYS[2], texts.FOLLOW_UP_3),
]


async def run(bot: Bot, interval: int = 60) -> None:
    logger.info("Прогрев запущен (интервал %s c)", interval)
    while True:
        try:
            await _tick(bot)
        except Exception as e:
            logger.exception("Ошибка в цикле прогрева: %s", e)
        await asyncio.sleep(interval)


async def _tick(bot: Bot) -> None:
    for n, col, delay, text in _FOLLOW_UPS:
        for tg_user_id in await db.get_due_followups(col, delay):
            try:
                await bot.send_message(tg_user_id, text)
            except TelegramForbiddenError:
                logger.info("Пользователь %s заблокировал бота — пропускаем касание %s", tg_user_id, n)
            except Exception as e:
                logger.warning("Касание %s для %s не доставлено: %s", n, tg_user_id, e)
            finally:
                # Помечаем отправленным в любом случае, чтобы не зацикливаться на одном лиде.
                await db.mark_followup_sent(col, tg_user_id)
