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
    """Обходит ВСЕ тенанты (включая suspended — 152-ФЗ не зависит от статуса подписки):
    все выборки ниже tenant-scoped через contextvar, без обхода лиды тенант-ботов после
    /revoke не обезличивались бы никогда. Дефолт-тенант (Школа) идёт без контекста —
    как раньше; один сбойный тенант не валит остальных."""
    default = db.default_tenant_id()
    try:
        await _tick_tenant()  # Школа (дефолт-контекст)
    except Exception as e:  # noqa: BLE001 — сбой Школы не должен блокировать остальных
        logger.exception("Retention: тик дефолт-тенанта (Школа) упал: %s", e)
    for tid in await db.list_tenant_ids():
        if tid == default:
            continue
        token = db.current_tenant_id.set(tid)
        try:
            await _tick_tenant()
        except Exception as e:  # noqa: BLE001 — один тенант не должен ронять цикл
            logger.exception("Retention: тик тенанта %s упал: %s", tid, e)
        finally:
            db.current_tenant_id.reset(token)


async def _tick_tenant() -> None:
    # 1) Обезличивание по отзыву согласия (срок истёк).
    lead_ids = await db.due_for_erase(config.ERASE_AFTER_DAYS)
    for lead_id in lead_ids:
        try:
            await db.erase_lead(lead_id)
            logger.info("Лид %s обезличен (erase_requested_at + %sд)", lead_id, config.ERASE_AFTER_DAYS)
        except Exception as e:  # noqa: BLE001 — один лид не должен валить остальных
            logger.exception("Не удалось обезличить лид %s: %s", lead_id, e)

    # 1b) Обезличивание клуб-членов по отзыву (в т.ч. чистых, без lead — retention по leads
    # их не видит). Отдельная выборка по club_members.erase_requested_at (L4-retention-club-tables).
    member_ids = await db.club_due_for_erase(config.ERASE_AFTER_DAYS)
    for member_id in member_ids:
        try:
            await db.club_erase_member(member_id)
            logger.info("Клуб-член %s обезличен (erase_requested_at + %sд)", member_id, config.ERASE_AFTER_DAYS)
        except Exception as e:  # noqa: BLE001 — один член не должен валить остальных
            logger.exception("Не удалось обезличить клуб-члена %s: %s", member_id, e)

    # 2) Абсолютный TTL содержимого переписки.
    purged = await db.purge_old_message_text(config.MESSAGES_TTL_DAYS)
    if purged:
        logger.info("TTL переписки: обнулено содержимое у %s сообщений", purged)

    # 3) Просроченные pending-заказы онлайн-оплаты → failed (платёж в ЮKassa давно
    # истёк; лента «Платежей» не копит вечный pending). Не ПДн-чистка, но тот же
    # часовой househeeping-цикл — отдельный воркер ради одной строки не заводим.
    stale = await db.mark_stale_yookassa_orders_failed(config.ORDER_STALE_HOURS)
    if stale:
        logger.info("Онлайн-оплата: %s просроченных pending-заказов → failed", stale)
