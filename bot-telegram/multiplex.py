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
import escalation
import messaging
import texts
import triggers

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
    # Слой B: детерминированные триггеры тенанта (стоп-слова / кол-во сообщений) — ДО проверки
    # agent_id/кошелька: работают без ИИ-агента (клиент может настроить триггеры до провижининга).
    if await triggers.handle_text(bot, message):
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
    # Слой B: intent-триггеры тенанта → их описания в системный промпт (Лия эмитит [[TRIGGER:N]]).
    intent_trigs = await db.get_active_triggers(db.tenant_id(), types=("intent",))
    if intent_trigs:
        cfg = {**cfg, "system_prompt": (cfg.get("system_prompt") or "")
               + "\n\n" + triggers.build_intent_addendum(intent_trigs)}
    # A3 Слой A: ask_ai вырезает служебные маркеры (клиент тенант-бота их НЕ видит); esc != None
    # → горячий лид → карточка в адрес ТЕНАНТА; trig_idxs → сработавшие intent-триггеры.
    answer, _msg_id, esc, trig_idxs = await ai.ask_ai(message.text, None, cfg, history=history)
    # rich=True: ответ Лии тенант-бота — markdown→Telegram-HTML с фолбэком на plain (§8.7).
    await messaging.send_text(bot, message.from_user.id, answer, source="liya", rich=True)
    if esc is not None:
        await escalation.escalate(bot, message.from_user.id, esc)
    if trig_idxs:
        await triggers.fire_intent(bot, message, intent_trigs, trig_idxs)


@tenant_router.message(F.document)
async def t_document(message: Message, bot: Bot) -> None:
    """Слой B: документ от лида тенанта → триггер типа documents (если настроен у тенанта)."""
    cfg = await db.get_tenant_ai_overrides(db.tenant_id())
    if not cfg["enabled"]:
        return
    await triggers.handle_document(bot, message)


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
    running: dict = {}     # tenant_id -> {"task": Task, "bot": Bot}  (Telegram)
    running_vk: dict = {}  # tenant_id -> {"task": Task, "bot": VKBot} (Слой C: ВКонтакте)
    try:
        while True:
            try:
                await _reconcile(running)
            except Exception as e:  # noqa: BLE001 — сбой сверки не валит Школу/других
                logger.exception("Мультиплекс: ошибка сверки реестра (tg): %s", e)
            try:
                await _reconcile_vk(running_vk)   # аддитивный VK-проход (TG не трогает)
            except Exception as e:  # noqa: BLE001
                logger.exception("Мультиплекс: ошибка сверки реестра (vk): %s", e)
            await asyncio.sleep(interval)
    finally:
        for tid in list(running.keys()):
            await _stop_tenant(running, tid)
        for tid in list(running_vk.keys()):
            await _stop_channel(running_vk, tid, "VK")


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


# ── Слой C: канал ВКонтакте (аддитивно к Telegram-проходу) ────────────────────────────
async def _vk_respond(vkbot, tenant_id, from_id: int, peer_id: int, text: str) -> None:
    """VK-лид → ответ Лии тенанта (ядро: разговор + эскалация). tenant-контекст ставим ЯВНО
    (VK-поллер не идёт через aiogram-middleware). Зеркалит t_text, канал messenger='vk'.
    Триггеры на VK — отдельный шаг; здесь ядро (диалог + горячий лид)."""
    token = db.current_tenant_id.set(tenant_id)
    try:
        cfg = await db.get_tenant_ai_overrides(tenant_id)
        if not cfg["enabled"]:
            return
        await db.upsert_start(from_id, source="vk", messenger="vk")
        await db.log_message(tg_user_id=from_id, messenger="vk", direction="in", text=text)
        # cloud-ai без agent_id тенанта не зовём (иначе расход ушёл бы Школе); gateway — без agent_id.
        if cfg["backend"] != "gateway" and not cfg["agent_id"]:
            return
        if await db.is_ai_wallet_blocked():
            await vkbot.send(peer_id, texts.WALLET_PAUSED)
            return
        history = await db.get_ai_history(from_id, messenger="vk", limit=config.AI_HISTORY_MESSAGES)
        answer, _msg_id, esc, _trig = await ai.ask_ai(text, None, cfg, history=history)
        await vkbot.send(peer_id, answer)
        await db.log_message(tg_user_id=from_id, messenger="vk", direction="out", source="liya", text=answer)
        if esc is not None:
            # bot=None → карточку шлёт ЕДИНЫЙ нотификатор в TG-группу менеджеров; ссылка на клиента — vk.com.
            await escalation.escalate(None, from_id, esc, messenger="vk")
    finally:
        db.current_tenant_id.reset(token)


async def _reconcile_vk(running_vk: dict) -> None:
    """Доводит набор VK-поллеров до желаемого: тенанты с заданными vk_token + vk_group_id (vault).
    Школа (дефолт-тенант) — не здесь (живёт из env). Аддитивно к Telegram-проходу."""
    default_tid = db.default_tenant_id()
    desired: dict = {}
    for t in await db.list_active_tenants():
        if t["id"] == default_tid:
            continue
        try:
            token = await db.get_tenant_secret(t["id"], "vk_token")
            group_id = await db.get_tenant_secret(t["id"], "vk_group_id")
        except Exception:  # noqa: BLE001 — битый секрет одного тенанта не валит сверку
            logger.warning("Мультиплекс: не прочитал VK-секреты тенанта %s", t["id"], exc_info=True)
            continue
        gid = (group_id or "").strip()
        if token and gid.isdigit():
            desired[t["id"]] = (token, int(gid))

    for tid in list(running_vk.keys()):
        if tid not in desired or running_vk[tid]["task"].done():
            await _stop_channel(running_vk, tid, "VK")
    for tid, (token, gid) in desired.items():
        if tid not in running_vk:
            await _start_vk(running_vk, tid, token, gid)


async def _start_vk(running_vk: dict, tenant_id, token: str, group_id: int) -> None:
    """Поднимает VK-long-poll-таску тенанта. api.vk.com из РФ-ЦОД — напрямую, без прокси."""
    import vk_driver
    vkbot = vk_driver.VKBot(token, group_id, on_message=None)

    async def _on_message(from_id, peer_id, text):
        await _vk_respond(vkbot, tenant_id, from_id, peer_id, text)

    vkbot.on_message = _on_message
    task = asyncio.create_task(_run_vk(tenant_id, vkbot))
    running_vk[tenant_id] = {"task": task, "bot": vkbot}
    logger.info("Мультиплекс: VK-бот тенанта %s поднят (group=%s)", tenant_id, group_id)


async def _run_vk(tenant_id, vkbot) -> None:
    """Long-poll одного VK-бота. Упавший не валит остальных."""
    try:
        await vkbot.run()
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        logger.exception("Мультиплекс: VK-поллер тенанта %s упал: %s", tenant_id, e)


async def _stop_channel(running: dict, tenant_id, label: str) -> None:
    """Гасит канальную таску (VK/будущие). Сессию канал закрывает в своём finally."""
    h = running.pop(tenant_id, None)
    if h is None:
        return
    h["task"].cancel()
    try:
        await h["task"]
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass
    logger.info("Мультиплекс: %s-бот тенанта %s остановлен", label, tenant_id)
