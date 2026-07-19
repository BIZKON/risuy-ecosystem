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
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
)

import ai
import config
import db
import escalation
import funnel
import funnel_channels
import kb
import memory
import messaging
import selling
import texts
import triggers
import yookassa

logger = logging.getLogger(__name__)

_RELOAD_INTERVAL = 60          # период сверки реестра тенантов, сек
_TENANT_GREETING = "Здравствуйте! 🌷 Я на связи — задайте свой вопрос."

# C3: отписка от рассылок в VK/MAX по ключевому слову (у каналов нет inline-кнопки «Отписаться»
# как в TG; футер рассылки просит ответить «СТОП»). Точное совпадение, без ведущего «/».
# ⚠️ 152-ФЗ-ИНВАРИАНТ: слово из футера рассылки (worker._CHANNEL_UNSUB_FOOTER, сейчас «СТОП») ОБЯЗАНО
# быть в этом наборе — иначе лид не сможет отписаться. Меняешь набор/футер — синхронно с worker.
_UNSUB_WORDS = {"стоп", "stop", "отписаться", "отписка", "отписать"}


def _is_unsub(text: str | None) -> bool:
    return (text or "").strip().lower().lstrip("/") in _UNSUB_WORDS


async def _lead_provenance(uid: int, *, messenger: str) -> str:
    """152-ФЗ (SL §6, путь 6): провенанс лида для fail-closed гейта авто-диалога Лии в тенант-
    каналах (TG/VK/MAX). Нет строки → 'inbound_optin' (инбаунд без изменений: аутбаунд-строку
    B-FWD физически создаёт с provenance='outbound_signal'). Сбой БД пробрасывается → хендлер
    прерывается ДО ask_ai (fail-closed на ошибке). tenant-scoped через contextvar tenant_id."""
    col = db._user_col(messenger)
    async with db.pool.acquire() as c:
        prov = await c.fetchval(
            f"select provenance from leads where {col} = $1 and tenant_id = $2",
            uid, db.tenant_id())
    return prov or "inbound_optin"

# ── Реестр живых канальных ботов (Слой C3) ────────────────────────────────────
# VK/MAX-боты живут ТОЛЬКО в этом процессе (long-poll-таски). worker.py (тот же процесс,
# см. bot.py) берёт их отсюда для ИСХОДЯЩЕЙ доставки (ответ оператора / рассылка), чтобы НЕ
# открывать второе подключение к API канала. Заполняются в run() (алиасятся как локали реконсайла).
_running_tg: dict = {}    # tenant_id -> {"task", "bot": Bot}    (Telegram — для исходящей/дожима)
_running_vk: dict = {}    # tenant_id -> {"task", "bot": VKBot}
_running_max: dict = {}   # tenant_id -> {"task", "bot": MAXBot}


def get_channel_bot(tenant_id, messenger: str):
    """Живой канальный бот тенанта для исходящей доставки (Bot/VKBot/MAXBot) или None, если канал
    не поднят (не настроен / только что рестартнули / дефолт-тенант Школы — она вне мультиплекса).
    Воркер/дожиг при None оставляет строку/касание на следующий тик."""
    reg = (_running_tg if messenger == "tg" else _running_vk if messenger == "vk"
           else _running_max if messenger == "max" else None)
    if reg is None:
        return None
    h = reg.get(tenant_id)
    return h["bot"] if h else None


# ── Тенант-роутер (v1: только Лия) ───────────────────────────────────────────
tenant_router = Router()


@tenant_router.message(Command("start", ignore_case=True))
async def t_start(message: Message) -> None:
    """Тенант-бот /start. Если у тенанта включена воронка выдачи лид-магнита (конструктор в
    панели, funnel_enabled) — ведём её: приветствие+согласие → телефон/гейт → выдача. Иначе v1:
    приветствие + свободный диалог Лии. Перехват оператора (пауза) — бот молчит."""
    if await db.is_bot_paused(message.from_user.id):
        return
    try:
        await db.upsert_start(tg_user_id=message.from_user.id, source="other")
    except Exception:  # noqa: BLE001 — лид не критичен для ответа
        logger.warning("multiplex: не создал лид тенанта", exc_info=True)
    cfg = await db.get_funnel_config(db.tenant_id())
    if cfg["enabled"]:
        name = (message.from_user.full_name or "").strip()[:100]
        if name:
            try:
                await db.set_name(message.from_user.id, name)
            except Exception:  # noqa: BLE001 — имя не критично
                logger.warning("multiplex: не записал имя лида", exc_info=True)
        await funnel.start(funnel_channels.TgFunnelChannel(message.bot, message.from_user.id), cfg)
        return
    await messaging.send_text(
        message.bot, message.from_user.id, _TENANT_GREETING, source="funnel"
    )


@tenant_router.callback_query(F.data == "consent_yes")
async def t_consent(cb: CallbackQuery) -> None:
    """Согласие 152-ФЗ в тенант-воронке → set_consent → следующий шаг (телефон/гейт/выдача).
    DB-state-driven (без FSM). На паузе оператора — молчим; воронка выключена — no-op; лид уже
    получил материал (guide_sent) — не гоняем по шагам заново (повтор старой кнопки из истории)."""
    if await db.is_bot_paused(cb.from_user.id):
        await cb.answer()
        return
    cfg = await db.get_funnel_config(db.tenant_id())
    if not cfg["enabled"]:
        await cb.answer()
        return
    if await db.get_lead_status(cb.from_user.id) == "guide_sent":
        await cb.answer("Вы уже получили материал 🎉", show_alert=True)
        return
    await cb.answer()
    await db.set_consent(cb.from_user.id, True, consent_text=cfg.get("consent_text") or None, channel="tg")
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:  # noqa: BLE001
        pass
    await funnel.after_consent(funnel_channels.TgFunnelChannel(cb.bot, cb.from_user.id), cfg)


@tenant_router.message(F.contact)
async def t_contact(message: Message) -> None:
    """Телефон (кнопка «Поделиться номером») в тенант-воронке → set_phone → гейт/выдача."""
    if await db.is_bot_paused(message.from_user.id):
        return
    cfg = await db.get_funnel_config(db.tenant_id())
    if not cfg["enabled"]:
        return
    # Только собственный контакт (как on_phone главного бота): пересланный чужой контакт
    # записал бы в лид чужой номер (ПДн третьего лица, 152-ФЗ).
    if message.contact.user_id != message.from_user.id:
        return
    phone = message.contact.phone_number
    if not phone:  # контакт без номера (редкий, но валидный апдейт) → не падаем
        return
    await db.set_phone(message.from_user.id, phone, funnel.phone_hash(phone))
    await funnel.after_phone(funnel_channels.TgFunnelChannel(message.bot, message.from_user.id), cfg)


@tenant_router.callback_query(F.data == "check_sub")
async def t_check_sub(cb: CallbackQuery) -> None:
    """Проверка подписки на канал тенанта (гейт). Подписан → выдача; иначе alert. Fail-closed."""
    if await db.is_bot_paused(cb.from_user.id):
        await cb.answer()
        return
    cfg = await db.get_funnel_config(db.tenant_id())
    if not cfg["enabled"] or not (cfg.get("gate") or {}).get("enabled"):
        await cb.answer()
        return
    if await funnel.is_subscribed(cb.bot, cfg["gate"]["channel_id"], cb.from_user.id):
        await cb.answer(funnel.PHONE_OK)
        await db.set_subscribed(cb.from_user.id, True, messenger="tg")
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except Exception:  # noqa: BLE001
            pass
        await funnel.deliver(funnel_channels.TgFunnelChannel(cb.bot, cb.from_user.id), cfg)
    else:
        await cb.answer(funnel.NOT_SUBSCRIBED_ALERT, show_alert=True)


@tenant_router.message(Command("revoke", ignore_case=True))
async def t_revoke(message: Message) -> None:
    """Отзыв согласия на обработку ПДн субъектом (152-ФЗ ст.9 ч.2 — «в любой момент»): ставит
    erase_requested_at + unsubscribed_at + пишет consent_events('revoked'). Обезличивание — retention-cron
    (ERASE_AFTER_DAYS). После отзыва бот молчит на свободный текст (см. t_text)."""
    await db.request_erase(message.from_user.id, channel="tg")
    await messaging.send_text(message.bot, message.from_user.id, texts.REVOKE_OK, source="system")


@tenant_router.message(F.text)
async def t_text(message: Message, bot: Bot) -> None:
    """Свободный текст → ответ Лии тенанта (AI из tenant_settings) + метеринг.

    Метеринг подключается автоматически: gateway-usage списывается в ai.ask_ai
    (cost-capture), cloud-ai дельта — снапшот-воркером по агенту тенанта из
    tenant_agents. Все записи идут в tenant_id из contextvar (поставлен middleware).
    """
    if (message.text or "").startswith("/"):
        return
    # Перехват: оператор взял лид руками (триггер notify_reply_pause / панель) → Лия и триггеры
    # молчат (как Школа handlers.on_free_text и каналы VK/MAX). Входящее уже залогировал middleware.
    if await db.is_bot_paused(message.from_user.id):
        return
    # Субъект отозвал согласие → бот молчит (стоп-обработка ПДн, 152-ФЗ). Возврат — через /start + согласие.
    if await db.is_erase_requested(message.from_user.id):
        return
    # 152-ФЗ (SL §6, путь 6, fail-closed): авто-диалог Лии — ТОЛЬКО для инбаунд-лида (opt-in).
    # Аутбаунд-сигнал / раздача (provenance != 'inbound_optin') = субъект без согласия → авто-контакт
    # запрещён (152-ФЗ / ФЗ-38); не отвечаем и не шлём триггер-канед. Легальный выход (§7) — consent-funnel:
    # при захвате согласия provenance повышается до 'inbound_optin' и диалог разблокируется штатно. Инбаунд — no-op.
    if await _lead_provenance(message.from_user.id, messenger="tg") != "inbound_optin":
        return
    # СП-1: выбор агента команды по слоям диалог>канал>дефолт (фолбэк на легаси внутри резолвера).
    _persona = await db.get_lead_persona(message.from_user.id)
    _source = await db.get_lead_source(message.from_user.id)
    cfg = await db.resolve_team_agent_cfg(db.tenant_id(), source=_source, lead_agent_slug=_persona)
    if not cfg["enabled"]:
        return
    # Слой B: детерминированные триггеры тенанта (стоп-слова / кол-во сообщений) — ДО проверки
    # agent_id/кошелька: работают без ИИ-агента (клиент может настроить триггеры до провижининга).
    # Слой C: движок канал-агностичен → строим TriggerCtx (TG); reply шлёт ответ клиенту в TG.
    ctx = triggers.TriggerCtx(
        messenger="tg", external_id=message.from_user.id, text=message.text or "",
        reply=lambda body: messaging.send_text(
            bot, message.from_user.id, body, source="trigger", rich=True),
        notifier_fallback_bot=bot)
    if await triggers.handle_text(ctx):
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
    # СП-2a RAG (база знаний тенанта, фильтр по отделу=slug) + СП-2-память (сводки прошлых
    # диалогов С ЭТИМ клиентом, per-lead). Собираем оба блока контекста и подмешиваем ОДНИМ
    # augment (порядок: знание → память → вопрос). Тумблеры на агента; пусто/сбой → без изменений.
    user_text = message.text
    contexts: list[str] = []
    # Один эмбеддинг запроса на ОБА ретрива (KB + память) — не гоняем TEI дважды на пути ответа.
    _need_rag = cfg.get("kb_enabled") or (cfg.get("memory_enabled") and cfg.get("team_agent_id"))
    qvec = await kb.embed_query(message.text) if _need_rag else None
    if qvec:
        if cfg.get("kb_enabled"):
            kb_context = await kb.retrieve_context(
                message.text, db.tenant_id(), cfg.get("agent_slug"), vec=qvec)
            if kb_context:
                contexts.append(kb_context)
        if cfg.get("memory_enabled") and cfg.get("team_agent_id"):
            mem_context = await memory.retrieve(
                message.text, db.tenant_id(), cfg["team_agent_id"], str(message.from_user.id), vec=qvec)
            if mem_context:
                contexts.append(mem_context)
    if contexts:
        user_text = kb.augment(message.text, "\n\n".join(contexts))
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
    answer, _msg_id, esc, trig_idxs = await ai.ask_ai(user_text, None, cfg, history=history)
    # rich=True: ответ Лии тенант-бота — markdown→Telegram-HTML с фолбэком на plain (§8.7).
    await messaging.send_text(bot, message.from_user.id, answer, source="liya", rich=True)
    if esc is not None:
        # СП-1: адрес отдела выбранного агента (если задан) перекрывает общий адрес тенанта.
        _ec = (cfg.get("escalation_chat_id") or "").strip()
        _ov = (int(_ec), cfg.get("escalation_topic_id")) if _ec.lstrip("-").isdigit() else None
        await escalation.escalate(bot, message.from_user.id, esc, target_override=_ov)
    if trig_idxs:
        await triggers.fire_intent(ctx, intent_trigs, trig_idxs)
    # СП-2-память: каждые N ходов — суммаризировать диалог в долгую память (best-effort, ПОСЛЕ
    # ответа клиенту — не тормозит). Абсолютный счётчик ходов (не окно истории) → порог раз в N.
    if cfg.get("memory_enabled") and cfg.get("team_agent_id"):
        _mem_hist = await db.get_ai_history(message.from_user.id, limit=config.AI_HISTORY_MESSAGES)
        await memory.maybe_summarize(
            external_id=message.from_user.id, tenant_id=db.tenant_id(), cfg=cfg,
            history=_mem_hist, msg_count=await db.count_ai_messages(message.from_user.id),
            lead_key=str(message.from_user.id))


@tenant_router.message(F.document)
async def t_document(message: Message, bot: Bot) -> None:
    """Слой B: документ от лида тенанта → триггер типа documents (если настроен у тенанта)."""
    if await db.is_bot_paused(message.from_user.id):     # перехват оператора (как t_text/Школа)
        return
    cfg = await db.get_tenant_ai_overrides(db.tenant_id())
    if not cfg["enabled"]:
        return
    ctx = triggers.TriggerCtx(
        messenger="tg", external_id=message.from_user.id, text=message.caption or "",
        reply=lambda body: messaging.send_text(
            bot, message.from_user.id, body, source="trigger", rich=True),
        notifier_fallback_bot=bot)
    await triggers.handle_document(ctx)


# ── Слой C: продажи тенанта на ЕГО кассу ЮKassa (creds из vault) — общее ядро ──────────
# Канал-агностично (TG/VK/MAX). Чистые хелперы определения команды/кнопок — в selling.py
# (тестируемы без aiogram). Идентичность/креды/продукт — tenant-scoped через contextvar tenant_id.
async def _make_pay_url(messenger: str, external_id: int, product_id: int, return_url: str):
    """Канал-агностично: создать (или переиспользовать) платёж за продукт на кассу ТЕКУЩЕГО
    тенанта (creds из vault). Возвращает (pay_url, product) или (None, product|None). tenant-scoped
    (contextvar). get_sellable_product скоупит tenant_id → защита от крафтнутого buy:<чужой_id>;
    get_lead_for_purchase — по каналу (messenger). Idempotence-Key=order.id (нет двойного списания)."""
    creds = await db.get_tenant_shop_creds()
    if creds is None:
        return None, None
    lead = await db.get_lead_for_purchase(external_id, messenger=messenger)
    product = await db.get_sellable_product(product_id)
    if lead is None or product is None:
        return None, product
    order = await db.create_or_reuse_pending_order(
        lead["id"], product_id, product["price"], "RUB", reuse_minutes=config.ORDER_REUSE_MINUTES)
    if order["reused"]:
        return order["payment_url"], product
    try:
        payment = await yookassa.create_payment(
            amount=product["price"], currency="RUB",
            description=(product.get("name") or "Заказ")[:128], return_url=return_url,
            idempotence_key=str(order["id"]),
            metadata={"kind": "order", "order_id": str(order["id"])},
            lead_phone=lead.get("phone"), creds=creds,
        )
        pay_url = (payment.get("confirmation") or {}).get("confirmation_url")
        payment_id = payment.get("id")
        if not pay_url or not payment_id:
            raise yookassa.YooKassaError("нет confirmation_url/id в ответе")
        await db.set_order_payment(order["id"], payment_id, pay_url)
        return pay_url, product
    except yookassa.YooKassaError as e:
        logger.warning("multiplex: платёж (%s) по заказу %s не создался: %s", messenger, order["id"], e)
        await db.mark_order_failed(order["id"], "платёж не создан (сбой ЮKassa)")
        return None, product


def _shop_markup(products: list[dict]) -> InlineKeyboardMarkup:
    """TG-рендер кнопок витрины (по одной на строку) из общих selling.shop_button_rows."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=b["label"], callback_data=f"buy:{b['payload']['id']}")]
        for b in selling.shop_button_rows(products)
    ])


@tenant_router.message(Command("shop", ignore_case=True))
async def t_shop(message: Message, bot: Bot) -> None:
    """Витрина тенанта (TG): активные оферы с кнопками «Купить». Доступно, когда тенант подключил
    кассу (shop_yookassa_* в vault, раздел «Продукты»). Лид создаётся, как при /start."""
    try:
        await db.upsert_start(tg_user_id=message.from_user.id, source="other")
    except Exception:  # noqa: BLE001 — лид не критичен для показа витрины
        logger.warning("multiplex: /shop не создал лид тенанта", exc_info=True)
    if await db.get_tenant_shop_creds() is None:
        await messaging.send_text(
            bot, message.from_user.id,
            "Онлайн-оплата у этого бота пока не подключена 🥲", source="system")
        return
    products = await db.list_sellable_products()
    if not products:
        await messaging.send_text(
            bot, message.from_user.id,
            "Сейчас нет товаров к покупке. Загляните позже 🌷", source="system")
        return
    await messaging.send_text(
        bot, message.from_user.id, "Выберите, что хотите оплатить:",
        source="system", reply_markup=_shop_markup(products))


@tenant_router.callback_query(F.data.startswith("buy:"))
async def t_buy(cb: CallbackQuery, bot: Bot) -> None:
    """Клик «Купить» у тенант-бота (TG) → платёж ЮKassa на КАССУ ТЕНАНТА → ссылка «Перейти к
    оплате». Логика — общая _make_pay_url (messenger='tg'). Подтверждение ловит вебхук панели."""
    try:
        product_id = int((cb.data or "").split(":", 1)[1])
    except (ValueError, IndexError):
        await cb.answer()
        return
    try:
        me = await bot.get_me()
        return_url = f"https://t.me/{me.username}" if me.username else "https://t.me"
    except Exception:  # noqa: BLE001
        return_url = "https://t.me"
    pay_url, product = await _make_pay_url("tg", cb.from_user.id, product_id, return_url)
    await cb.answer()
    if not pay_url:
        await messaging.send_text(bot, cb.from_user.id, texts.PAY_UNAVAILABLE, source="system")
        return
    pay_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=texts.PAY_BTN, url=pay_url)],
    ])
    await messaging.send_text(
        bot, cb.from_user.id, texts.pay_message(product), source="system", reply_markup=pay_kb)


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
    # ВСЕ реестры — МОДУЛЬНЫЕ (доступны get_channel_bot для исходящей доставки C3 + дожима item B).
    running = _running_tg       # tenant_id -> {"task": Task, "bot": Bot}    (Telegram)
    running_vk = _running_vk    # tenant_id -> {"task": Task, "bot": VKBot}  (Слой C: ВКонтакте)
    running_max = _running_max  # tenant_id -> {"task": Task, "bot": MAXBot} (Слой C: MAX)
    running.clear()
    running_vk.clear()
    running_max.clear()
    try:
        while True:
            try:
                await _reconcile(running)
            except Exception as e:  # noqa: BLE001 — сбой сверки не валит Школу/других
                logger.exception("Мультиплекс: ошибка сверки реестра (tg): %s", e)
            try:
                await _reconcile_vk(running_vk)    # аддитивные канальные проходы (TG не трогают)
            except Exception as e:  # noqa: BLE001
                logger.exception("Мультиплекс: ошибка сверки реестра (vk): %s", e)
            try:
                await _reconcile_max(running_max)
            except Exception as e:  # noqa: BLE001
                logger.exception("Мультиплекс: ошибка сверки реестра (max): %s", e)
            await asyncio.sleep(interval)
    finally:
        for tid in list(running.keys()):
            await _stop_tenant(running, tid)
        for tid in list(running_vk.keys()):
            await _stop_channel(running_vk, tid, "VK")
        for tid in list(running_max.keys()):
            await _stop_channel(running_max, tid, "MAX")


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
    # Слой C: tenant-контекст и на callback_query — иначе buy-callback (оплата) обращался бы
    # к БД без tenant_id (заказ/продукт/креды кассы ушли бы не тому тенанту или упали).
    dp.callback_query.outer_middleware(_TenantContextMiddleware(tenant_id))
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
async def _vk_shop(vkbot, peer_id: int) -> None:
    """Витрина VK: активные товары тенанта кнопками (inline keyboard с payload). tenant-контекст
    уже установлен вызывающим. Касса не подключена / нет товаров → текстовый ответ."""
    if await db.get_tenant_shop_creds() is None:
        await vkbot.send(peer_id, "Онлайн-оплата у этого бота пока не подключена 🥲")
        return
    products = await db.list_sellable_products()
    if not products:
        await vkbot.send(peer_id, "Сейчас нет товаров к покупке. Загляните позже 🌷")
        return
    await vkbot.send_keyboard(peer_id, "Выберите, что хотите оплатить:", selling.shop_button_rows(products))


async def _vk_respond(vkbot, tenant_id, from_id: int, peer_id: int, text: str, payload=None) -> None:
    """VK-лид → продажи (витрина/оплата по кнопке-payload или слову) ИЛИ разговор Лии (+триггеры,
    эскалация). tenant-контекст ставим ЯВНО (VK-поллер не идёт через aiogram-middleware)."""
    token = db.current_tenant_id.set(tenant_id)
    try:
        await db.upsert_start(from_id, source="vk", messenger="vk")
        # C3: отписка от рассылок по слову «СТОП» (футер VK/MAX-рассылки). До продаж/Лии.
        if _is_unsub(text):
            await db.log_message(tg_user_id=from_id, messenger="vk", direction="in", text=text)
            await db.set_unsubscribed(from_id, messenger="vk")
            await vkbot.send(peer_id, texts.UNSUBSCRIBED_OK)
            return
        # 152-ФЗ: воронка/согласие ДО продаж и Лии. Отзыв обрабатываем в любой момент.
        if funnel.is_revoke(text):
            await db.request_erase(from_id, channel="vk", messenger="vk")
            await vkbot.send(peer_id, texts.REVOKE_OK)
            return
        if await db.is_erase_requested(from_id, messenger="vk"):
            return  # субъект отозвал согласие → молчим
        fcfg = await db.get_funnel_config(tenant_id)
        if fcfg["enabled"] and funnel.requisites_filled(fcfg):
            lead = await db.get_lead_snapshot(from_id, messenger="vk") or {}
            if (lead.get("status") or "") != "guide_sent":
                ch = funnel_channels.VkFunnelChannel(vkbot, peer_id, from_id)
                consent_pressed = bool(payload and payload.get("cmd") == "consent_yes")
                await funnel.dispatch(ch, fcfg, lead, {"text": text, "consent_pressed": consent_pressed})
                return
        # Слой C: продажи — по кнопке (payload) или слову-триггеру; независимо от тумблера Лии.
        sell = selling.selling_command(text, payload)
        if sell is not None:
            # Логируем входящее (история диалога / счётчик), как разговорная ветка логирует свои.
            await db.log_message(tg_user_id=from_id, messenger="vk", direction="in", text=text)
            if sell[0] == "buy":
                pay_url, product = await _make_pay_url("vk", from_id, sell[1], "https://vk.com")
                if pay_url:
                    await vkbot.send_link(peer_id, texts.pay_message(product), pay_url, texts.PAY_BTN)
                else:
                    await vkbot.send(peer_id, texts.PAY_UNAVAILABLE)
            else:
                await _vk_shop(vkbot, peer_id)
            return
        cfg = await db.get_tenant_ai_overrides(tenant_id)
        if not cfg["enabled"]:
            return
        # Историю диалога читаем ДО лога текущего входящего — иначе текущий вопрос попал бы в history
        # ПОСЛЕДНЕЙ user-записью И был бы добавлен ask_ai финальным turn'ом (дубль в модель). TG этого
        # избегает exclude по message_id; у канала нативного id под рукой нет → читаем history раньше.
        history = await db.get_ai_history(from_id, messenger="vk", limit=config.AI_HISTORY_MESSAGES)
        await db.log_message(tg_user_id=from_id, messenger="vk", direction="in", text=text)
        # Перехват: оператор взял лид руками (триггер notify_reply_pause / панель) → Лия и триггеры
        # молчат (зеркалит TG: is_bot_paused-гейт ДО движка). Входящее уже залогировано выше
        # (история/счётчик не теряются), Лия просто не отвечает.
        if await db.is_bot_paused(from_id, messenger="vk"):
            return
        # 152-ФЗ (SL §6, путь 6, fail-closed): гейт покрывает VK-мультиплекс (иначе дыра в VK).
        # Авто-диалог Лии — ТОЛЬКО для инбаунд-лида; аутбаунд (provenance != 'inbound_optin') = без
        # согласия → не отвечаем и не шлём триггер-канед (не контактируем). Выход — consent-funnel (§7);
        # при согласии provenance → 'inbound_optin' → разблокировка. Инбаунд — no-op (upsert_start=opt-in).
        if await _lead_provenance(from_id, messenger="vk") != "inbound_optin":
            return
        # Слой C: ответ КЛИЕНТУ в VK + лог source='trigger' (canned-ответ без LLM не тарифицируется
        # как 'liya'). Замыкание над peer_id/from_id — инкапсулирует канал для движка триггеров.
        async def _reply(body: str) -> None:
            await vkbot.send(peer_id, body)
            await db.log_message(tg_user_id=from_id, messenger="vk", direction="out",
                                 source="trigger", text=body)
        ctx = triggers.TriggerCtx(messenger="vk", external_id=from_id, text=text,
                                  reply=_reply, notifier_fallback_bot=None)
        # Слой C: текстовые триггеры тенанта на VK (стоп-слова / кол-во) — ДО agent_id/кошелька
        # (работают без ИИ-агента). Сработал → return (ИИ-ответ на этот ход пропускаем).
        if await triggers.handle_text(ctx):
            return
        # cloud-ai без agent_id тенанта не зовём (иначе расход ушёл бы Школе); gateway — без agent_id.
        if cfg["backend"] != "gateway" and not cfg["agent_id"]:
            return
        if await db.is_ai_wallet_blocked():
            await vkbot.send(peer_id, texts.WALLET_PAUSED)
            return
        # Слой C: intent-триггеры тенанта → их описания в системный промпт (Лия эмитит [[TRIGGER:N]]).
        intent_trigs = await db.get_active_triggers(tenant_id, types=("intent",))
        if intent_trigs:
            cfg = {**cfg, "system_prompt": (cfg.get("system_prompt") or "")
                   + "\n\n" + triggers.build_intent_addendum(intent_trigs)}
        answer, _msg_id, esc, trig_idxs = await ai.ask_ai(text, None, cfg, history=history)
        await vkbot.send(peer_id, answer)
        await db.log_message(tg_user_id=from_id, messenger="vk", direction="out", source="liya", text=answer)
        if esc is not None:
            # bot=None → карточку шлёт ЕДИНЫЙ нотификатор в TG-группу менеджеров; ссылка на клиента — vk.com.
            await escalation.escalate(None, from_id, esc, messenger="vk")
        if trig_idxs:
            await triggers.fire_intent(ctx, intent_trigs, trig_idxs)
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

    async def _on_message(from_id, peer_id, text, payload=None):
        await _vk_respond(vkbot, tenant_id, from_id, peer_id, text, payload)

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
    """Гасит канальную таску (VK/MAX/будущие). Сессию канал закрывает в своём finally."""
    h = running.pop(tenant_id, None)
    if h is None:
        return
    h["task"].cancel()
    try:
        await h["task"]
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass
    logger.info("Мультиплекс: %s-бот тенанта %s остановлен", label, tenant_id)


# ── Слой C: канал MAX (зеркало VK; идентичность user_id, ответ на chat_id) ─────────────
async def _max_shop(maxbot, chat_id: int) -> None:
    """Витрина MAX: товары тенанта inline-кнопками (callback). tenant-контекст уже установлен."""
    if await db.get_tenant_shop_creds() is None:
        await maxbot.send(chat_id, "Онлайн-оплата у этого бота пока не подключена 🥲")
        return
    products = await db.list_sellable_products()
    if not products:
        await maxbot.send(chat_id, "Сейчас нет товаров к покупке. Загляните позже 🌷")
        return
    await maxbot.send_keyboard(chat_id, "Выберите, что хотите оплатить:", selling.shop_button_rows(products))


async def _max_callback(maxbot, tenant_id, user_id: int, chat_id: int, payload, callback_id: str) -> None:
    """Нажата inline-кнопка MAX (покупка). messenger='max'; идентичность=user_id, ответ на chat_id.
    tenant-контекст ставим ЯВНО. Платёж — на кассу тенанта (общая _make_pay_url)."""
    token = db.current_tenant_id.set(tenant_id)
    try:
        await db.upsert_start(user_id, source="max", messenger="max")
        await db.note_max_chat_id(user_id, chat_id)   # C3: адрес ответа MAX (≠ user_id в личке)
        # 152-ФЗ: согласие через callback (кнопка «consent_yes»).
        if payload and payload.get("cmd") == "consent_yes":
            # 152-ФЗ: субъект отозвал согласие → старая кнопка не должна откатить отзыв.
            if await db.is_erase_requested(user_id, messenger="max"):
                await maxbot.answer_callback(callback_id)
                return
            fcfg = await db.get_funnel_config(tenant_id)
            if fcfg["enabled"] and funnel.requisites_filled(fcfg):
                lead = await db.get_lead_snapshot(user_id, messenger="max") or {}
                ch = funnel_channels.MaxFunnelChannel(maxbot, chat_id, user_id)
                await funnel.dispatch(ch, fcfg, lead, {"text": "", "consent_pressed": True})
            await maxbot.answer_callback(callback_id)
            return
        sell = selling.selling_command(None, payload)
        if sell is not None and sell[0] == "buy":
            pay_url, product = await _make_pay_url("max", user_id, sell[1], "https://max.ru")
            if pay_url:
                await maxbot.send_link(chat_id, texts.pay_message(product), pay_url, texts.PAY_BTN)
            else:
                await maxbot.send(chat_id, texts.PAY_UNAVAILABLE)
        await maxbot.answer_callback(callback_id)
    finally:
        db.current_tenant_id.reset(token)


async def _max_respond(maxbot, tenant_id, user_id: int, chat_id: int, text: str) -> None:
    """MAX-лид → витрина (слово-триггер) ИЛИ разговор Лии (+триггеры, эскалация). messenger='max';
    идентичность лида = user_id (→ leads.max_user_id), отвечаем на chat_id (≠ user_id в личке)."""
    token = db.current_tenant_id.set(tenant_id)
    try:
        await db.upsert_start(user_id, source="max", messenger="max")
        await db.note_max_chat_id(user_id, chat_id)   # C3: адрес ответа MAX (≠ user_id в личке)
        # C3: отписка от рассылок по слову «СТОП» (футер VK/MAX-рассылки). До продаж/Лии.
        if _is_unsub(text):
            await db.log_message(tg_user_id=user_id, messenger="max", direction="in", text=text)
            await db.set_unsubscribed(user_id, messenger="max")
            await maxbot.send(chat_id, texts.UNSUBSCRIBED_OK)
            return
        # 152-ФЗ: воронка/согласие до продаж и Лии. Отзыв — в любой момент.
        if funnel.is_revoke(text):
            await db.request_erase(user_id, channel="max", messenger="max")
            await maxbot.send(chat_id, texts.REVOKE_OK)
            return
        if await db.is_erase_requested(user_id, messenger="max"):
            return  # субъект отозвал согласие → молчим
        fcfg = await db.get_funnel_config(tenant_id)
        if fcfg["enabled"] and funnel.requisites_filled(fcfg):
            lead = await db.get_lead_snapshot(user_id, messenger="max") or {}
            if (lead.get("status") or "") != "guide_sent":
                ch = funnel_channels.MaxFunnelChannel(maxbot, chat_id, user_id)
                await funnel.dispatch(ch, fcfg, lead, {"text": text, "consent_pressed": False})
                return
        # Слой C: витрина по слову-триггеру (покупка — кнопкой → message_callback → _max_callback).
        sell = selling.selling_command(text, None)
        if sell is not None and sell[0] == "shop":
            await db.log_message(tg_user_id=user_id, messenger="max", direction="in", text=text)
            await _max_shop(maxbot, chat_id)
            return
        cfg = await db.get_tenant_ai_overrides(tenant_id)
        if not cfg["enabled"]:
            return
        # Историю читаем ДО лога входящего (как VK): иначе текущий вопрос задвоится в модель.
        history = await db.get_ai_history(user_id, messenger="max", limit=config.AI_HISTORY_MESSAGES)
        await db.log_message(tg_user_id=user_id, messenger="max", direction="in", text=text)
        # Перехват (зеркалит VK/TG): оператор на паузе → Лия и триггеры молчат, входящее залогировано.
        if await db.is_bot_paused(user_id, messenger="max"):
            return
        # 152-ФЗ (SL §6, путь 6, fail-closed): гейт покрывает MAX-мультиплекс (иначе дыра в MAX).
        # Авто-диалог Лии — ТОЛЬКО для инбаунд-лида; аутбаунд (provenance != 'inbound_optin') = без
        # согласия → не отвечаем и не шлём триггер-канед (не контактируем). Выход — consent-funnel (§7);
        # при согласии provenance → 'inbound_optin' → разблокировка. Инбаунд — no-op (upsert_start=opt-in).
        if await _lead_provenance(user_id, messenger="max") != "inbound_optin":
            return
        # Ответ КЛИЕНТУ на chat_id (≠ user_id в личке) + лог source='trigger'. Замыкание над chat_id/
        # user_id инкапсулирует канал для движка триггеров.
        async def _reply(body: str) -> None:
            await maxbot.send(chat_id, body)
            await db.log_message(tg_user_id=user_id, messenger="max", direction="out",
                                 source="trigger", text=body)
        ctx = triggers.TriggerCtx(messenger="max", external_id=user_id, text=text,
                                  reply=_reply, notifier_fallback_bot=None)
        # Текстовые триггеры на MAX (стоп-слова / кол-во) — ДО agent_id/кошелька.
        if await triggers.handle_text(ctx):
            return
        if cfg["backend"] != "gateway" and not cfg["agent_id"]:
            return
        if await db.is_ai_wallet_blocked():
            await maxbot.send(chat_id, texts.WALLET_PAUSED)
            return
        intent_trigs = await db.get_active_triggers(tenant_id, types=("intent",))
        if intent_trigs:
            cfg = {**cfg, "system_prompt": (cfg.get("system_prompt") or "")
                   + "\n\n" + triggers.build_intent_addendum(intent_trigs)}
        answer, _msg_id, esc, trig_idxs = await ai.ask_ai(text, None, cfg, history=history)
        await maxbot.send(chat_id, answer)
        await db.log_message(tg_user_id=user_id, messenger="max", direction="out", source="liya", text=answer)
        if esc is not None:
            await escalation.escalate(None, user_id, esc, messenger="max")
        if trig_idxs:
            await triggers.fire_intent(ctx, intent_trigs, trig_idxs)
    finally:
        db.current_tenant_id.reset(token)


async def _reconcile_max(running_max: dict) -> None:
    """VK-аналог для MAX: тенанты с max_bot_token (vault). Школа (дефолт) — не здесь."""
    default_tid = db.default_tenant_id()
    desired: dict = {}
    for t in await db.list_active_tenants():
        if t["id"] == default_tid:
            continue
        try:
            token = await db.get_tenant_secret(t["id"], "max_bot_token")
        except Exception:  # noqa: BLE001
            logger.warning("Мультиплекс: не прочитал MAX-токен тенанта %s", t["id"], exc_info=True)
            continue
        if token:
            desired[t["id"]] = token

    for tid in list(running_max.keys()):
        if tid not in desired or running_max[tid]["task"].done():
            await _stop_channel(running_max, tid, "MAX")
    for tid, token in desired.items():
        if tid not in running_max:
            await _start_max(running_max, tid, token)


async def _start_max(running_max: dict, tenant_id, token: str) -> None:
    """Поднимает MAX-long-poll-таску тенанта. platform-api.max.ru из РФ-ЦОД — напрямую."""
    import max_driver
    maxbot = max_driver.MAXBot(token, on_message=None)

    async def _on_message(user_id, chat_id, text):
        await _max_respond(maxbot, tenant_id, user_id, chat_id, text)

    async def _on_callback(user_id, chat_id, payload, callback_id):
        await _max_callback(maxbot, tenant_id, user_id, chat_id, payload, callback_id)

    maxbot.on_message = _on_message
    maxbot.on_callback = _on_callback
    task = asyncio.create_task(_run_max(tenant_id, maxbot))
    running_max[tenant_id] = {"task": task, "bot": maxbot}
    logger.info("Мультиплекс: MAX-бот тенанта %s поднят", tenant_id)


async def _run_max(tenant_id, maxbot) -> None:
    try:
        await maxbot.run()
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        logger.exception("Мультиплекс: MAX-поллер тенанта %s упал: %s", tenant_id, e)
