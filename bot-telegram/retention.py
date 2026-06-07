"""Retention-cron: обезличивание ПДн по отзыву согласия + TTL переписки (§6.4 плана).

Раньше этого НЕ было в коде: ERASE_AFTER_DAYS была только константой-комментарием, лиды
никогда не обезличивались, а messages (диалоговый ПДн) копился бы вечно. Блок «Переписка»
нельзя выкатывать без этого.

Запускается рядом с nurture.run / worker.run одной asyncio-таской (см. bot.py). Под owner-
ролью (панель прав на это не имеет). Образец цикла — nurture.run.

Что делает каждый тик:
  1. Лиды с erase_requested_at + ERASE_AFTER_DAYS <= now() → обезличить (name/phone/
     phone_hash/notes → null), удалить переписку (messages), порвать связь кликов/
     получателей с субъектом, почистить PII-детали в admin_audit, записать аудит
     action='lead_erased' (доказательство срока для РКН).
  2. Абсолютный TTL переписки: обнулить text/file_id у messages старше MESSAGES_TTL_DAYS
     (самый объёмный ПДн-поток), даже без запроса на удаление.
"""
import asyncio
import logging

import config
import db

logger = logging.getLogger(__name__)


async def run(interval: int | None = None) -> None:
    """Главный цикл retention. interval по умолчанию из config.RETENTION_INTERVAL (час)."""
    interval = interval or config.RETENTION_INTERVAL
    logger.info("Retention-cron запущен (интервал %s c)", interval)
    while True:
        try:
            await _tick()
        except Exception as e:  # noqa: BLE001 — цикл не должен падать
            logger.exception("Ошибка в цикле retention: %s", e)
        await asyncio.sleep(interval)


async def _tick() -> None:
    # 1) Обезличивание по отзыву согласия (срок истёк).
    lead_ids = await db.due_for_erase(config.ERASE_AFTER_DAYS)
    for lead_id in lead_ids:
        try:
            await db.erase_lead(lead_id)
            logger.info("Лид %s обезличен (erase_requested_at + %sд)", lead_id, config.ERASE_AFTER_DAYS)
        except Exception as e:  # noqa: BLE001 — один лид не должен валить остальных
            logger.exception("Не удалось обезличить лид %s: %s", lead_id, e)

    # 2) Абсолютный TTL содержимого переписки.
    purged = await db.purge_old_message_text(config.MESSAGES_TTL_DAYS)
    if purged:
        logger.info("TTL переписки: обнулено содержимое у %s сообщений", purged)
