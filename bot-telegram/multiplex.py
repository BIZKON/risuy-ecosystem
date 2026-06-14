"""Мультиплекс тенант-ботов (Wave 3, ТЗ §5.4; DECISIONS п.6).

Один процесс (app 201859) ведёт N тенант-ботов polling-тасками. Школа работает
ОТДЕЛЬНОЙ главной таской из env (bot.py) — мультиплекс её НЕ трогает и ИСКЛЮЧАЕТ
из реестра по db.default_tenant_id(). Реестр = active-тенанты (≠ Школа) с секретом
telegram_bot_token в vault. Hot-reload: периодическая сверка реестра — новый тенант
с токеном → polling-таска поднимается БЕЗ редеплоя; suspended/canceled/без токена →
таска гасится, сессия бота закрывается.

contextvar tenant_id выставляется per-update (outer-middleware каждой таски) → все
вставки (leads/messages) и метеринг (cost-capture gateway / снапшоты агента тенанта)
идут в правильного тенанта. Школьная главная таска контекст не ставит → db.tenant_id()
у неё = env-тенант Школы (db._default_tenant_id).

⚠️ ОБЪЁМ v1 (закрывает мастер-DoD §8.8: сообщение тенант-боту → ответ ИИ → списание
cost×3): тенант-бот отвечает ЛИШЬ Лией (AI через tenant_settings) + метеринг. Полная
воронка тенанта (гейт-канал, выдача гайда, продукты, рассылки, прогрев) — СЛЕДУЮЩАЯ
подволна: требует per-tenant config (CHANNEL_ID/GUIDE_URL/VIDEO_NOTE и т.д.), который
сейчас живёт только в env Школы. До неё тенант-бот = «голая Лия».

⚠️ При ПУСТОМ реестре (нет active-тенантов кроме Школы — состояние на момент Wave 3)
мультиплекс — строго no-op: ноль доп. ботов, Школа работает ровно как раньше (§8.7).

Изоляция: цикл сверки в try/except (как nurture.run) — сбой реестра/подъёма одного
тенанта не валит ни других тенантов, ни главную таску Школы.
"""
import asyncio
import logging

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.filters import Command
from aiogram.types import Message

import ai
import config
import db
import messaging
import texts

logger = logging.getLogger(__name__)

_RELOAD_INTERVAL = 60          # период сверки реестра тенантов, сек
_TENANT_GREETING = "Здравствуйте! 🌷 Я на связи — задайте свой вопрос."


# ── Тенант-роутер (v1: только Лия) ───────────────────────────────────────────
tenant_router = Router()


@tenant_router.message(Command("start", ignore_case=True))
async def t_start(message: Message) -> None:
    """Создаёт лид тенанта (для контекста/метеринга) и шлёт приветствие.
    Воронка (согласие/телефон/гейт) — следующая подволна; v1 сразу к диалогу."""
    try:
        await db.upsert_start(tg_user_id=message.from_user.id, source="other")
    except Exception:  # noqa: BLE001 — лид не критичен для ответа
        logger.warning("multiplex: не создал лид тенанта", exc_info=True)
    await messaging.send_text(
        message.bot, message.from_user.id, _TENANT_GREETING, source="funnel"
    )


@tenant_router.message(F.text)
async def t_text(message: Message, bot: Bot) -> None:
    """Свободный текст → ответ Лии тенанта (AI из tenant_settings) + метеринг.

    Метеринг подключается автоматически: gateway-usage списывается в ai.ask_ai
    (cost-capture), cloud-ai дельта — снапшот-воркером по агенту тенанта из
    tenant_agents. Все записи идут в tenant_id из contextvar (поставлен middleware).
    """
    if (message.text or "").startswith("/"):
        return
    cfg = await db.get_tenant_ai_overrides(db.tenant_id())
    if not cfg["enabled"]:
        return
    # cloud-ai без agent_id тенанта НЕ зовём: иначе ask_liya сфолбэчится на агента
    # Школы (config.AGENT_ID) и расход ушёл бы Школе. gateway работает без agent_id.
    if cfg["backend"] != "gateway" and not cfg["agent_id"]:
        logger.info("multiplex: у тенанта %s не задан agent_id — Лия молчит", db.tenant_id())
        return
    if await db.is_ai_wallet_blocked():
        await messaging.send_text(
            bot, message.from_user.id, texts.WALLET_PAUSED, source="system"
        )
        return
    try:
        await bot.send_chat_action(message.chat.id, "typing")
    except Exception:  # noqa: BLE001
        pass
    # Wave 5: контекст диалога тенанта — историей сообщений (tenant-scoped через contextvar
    # tenant_id, поставленный middleware). Текущее входящее исключаем по message_id.
    history = await db.get_ai_history(
        message.from_user.id,
        exclude_tg_message_id=message.message_id,
        limit=config.AI_HISTORY_MESSAGES,
    )
    answer, _msg_id = await ai.ask_ai(message.text, None, cfg, history=history)
    # rich=True: ответ Лии тенант-бота — markdown→Telegram-HTML с фолбэком на plain (§8.7).
    await messaging.send_text(bot, message.from_user.id, answer, source="liya", rich=True)


# ── contextvar tenant_id per-update ──────────────────────────────────────────
class _TenantContextMiddleware:
    """Ставит db.current_tenant_id на время обработки апдейта тенант-бота и
    сбрасывает после. Так log_message/upsert_start/метеринг пишут tenant_id
    тенанта, а fire-and-forget cost-capture (create_task в ai.py) наследует
    его копией контекста."""

    def __init__(self, tenant_id) -> None:
        self._tid = tenant_id

    async def __call__(self, handler, event, data):
        token = db.current_tenant_id.set(self._tid)
        try:
            return await handler(event, data)
        finally:
            db.current_tenant_id.reset(token)


# ── Реестр и hot-reload ──────────────────────────────────────────────────────
async def run(interval: int | None = None) -> None:
    """Главный цикл мультиплекса. Сверяет реестр каждые interval сек, поднимает/
    гасит тенант-ботов. Запускается доп. таской в bot.py рядом с воронкой Школы."""
    interval = interval or _RELOAD_INTERVAL
    logger.info("Мультиплекс тенант-ботов запущен (сверка каждые %s c)", interval)
    running: dict = {}  # tenant_id -> {"task": Task, "bot": Bot}
    try:
        while True:
            try:
                await _reconcile(running)
            except Exception as e:  # noqa: BLE001 — сбой сверки не валит Школу/других
                logger.exception("Мультиплекс: ошибка сверки реестра: %s", e)
            await asyncio.sleep(interval)
    finally:
        for tid, h in list(running.items()):
            await _stop_tenant(running, tid)


async def _reconcile(running: dict) -> None:
    """Доводит набор живых тенант-тасок до желаемого по реестру."""
    default_tid = db.default_tenant_id()
    desired: dict = {}
    for t in await db.list_active_tenants():
        if t["id"] == default_tid:
            continue  # Школа живёт из env (главная таска bot.py), не дублируем
        try:
            token = await db.get_tenant_secret(t["id"], "telegram_bot_token")
        except Exception:  # noqa: BLE001 — битый секрет одного тенанта не валит сверку
            logger.warning("Мультиплекс: не прочитал токен тенанта %s", t["id"], exc_info=True)
            continue
        if token:
            desired[t["id"]] = token

    # Погасить ушедших (suspended/canceled/без токена) + умершие таски.
    for tid in list(running.keys()):
        if tid not in desired or running[tid]["task"].done():
            await _stop_tenant(running, tid)

    # Поднять новых.
    for tid, token in desired.items():
        if tid not in running:
            await _start_tenant(running, tid, token)


async def _start_tenant(running: dict, tenant_id, token: str) -> None:
    """Поднимает polling-таску тенант-бота. Прокси — общий (config.TELEGRAM_PROXY:
    api.telegram.org недоступен из РФ-ЦОД напрямую, как и у Школы)."""
    session = AiohttpSession(proxy=config.TELEGRAM_PROXY) if config.TELEGRAM_PROXY else None
    bot = Bot(token=token, session=session) if session else Bot(token=token)
    task = asyncio.create_task(_run_tenant(tenant_id, bot))
    running[tenant_id] = {"task": task, "bot": bot}
    logger.info("Мультиплекс: тенант-бот %s поднят", tenant_id)


async def _run_tenant(tenant_id, bot: Bot) -> None:
    """Polling одного тенант-бота. Свой Dispatcher + contextvar-middleware + Лия-роутер."""
    dp = Dispatcher()
    dp.message.outer_middleware(_TenantContextMiddleware(tenant_id))
    dp.message.outer_middleware(messaging.LoggingMiddleware())  # лог входящих (tenant_id из contextvar)
    dp.include_router(tenant_router)
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001 — упавший тенант-бот не валит остальных
        logger.exception("Мультиплекс: polling тенанта %s упал: %s", tenant_id, e)
    finally:
        try:
            await bot.session.close()
        except Exception:  # noqa: BLE001
            pass


async def _stop_tenant(running: dict, tenant_id) -> None:
    h = running.pop(tenant_id, None)
    if h is None:
        return
    h["task"].cancel()
    try:
        await h["task"]
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass
    logger.info("Мультиплекс: тенант-бот %s остановлен", tenant_id)
