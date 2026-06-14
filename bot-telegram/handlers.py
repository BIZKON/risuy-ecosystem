"""Сценарий воронки (FSM): /start → согласие → имя → телефон → гейт подписки → выдача гайда."""
import hashlib
import logging

from aiogram import Router, F, Bot
from aiogram.enums import ChatMemberStatus
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
)

import ai
import config
import db
import kb
import messaging
import texts
import yookassa

logger = logging.getLogger(__name__)
router = Router()

VALID_SOURCES = {"reels", "dzen", "youtube", "vk", "max", "other"}
_SUBSCRIBED = {
    ChatMemberStatus.MEMBER,
    ChatMemberStatus.ADMINISTRATOR,
    ChatMemberStatus.CREATOR,
}


class Funnel(StatesGroup):
    consent = State()
    name = State()
    phone = State()
    gate = State()


def _phone_hash(phone: str) -> str:
    digits = "".join(ch for ch in phone if ch.isdigit())
    return hashlib.sha256(digits.encode()).hexdigest() if digits else ""


def _consent_kb() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=texts.CONSENT_BTN, callback_data="consent_yes")]]
    if config.PRIVACY_URL:
        rows.append([InlineKeyboardButton(text=texts.PRIVACY_BTN, url=config.PRIVACY_URL)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _phone_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=texts.PHONE_BTN, request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def _gate_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=texts.SUBSCRIBE_BTN, url=config.CHANNEL_URL)],
        [InlineKeyboardButton(text=texts.CHECK_SUB_BTN, callback_data="check_sub")],
    ])


def _guide_kb(guide_url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=texts.GUIDE_BTN, url=guide_url)],
    ])


@router.message(Command("start", ignore_case=True))
async def cmd_start(message: Message, command: CommandObject, state: FSMContext):
    # Перехват (§4 плана): оператор держит ручное управление → бот молчит. Закрывает
    # единственный обход паузы — повторный /start на тёплом лиде. Логику воронки НЕ трогаем,
    # только не даём ей стартовать на паузе (входящее уже залогировано middleware).
    if await db.is_bot_paused(message.from_user.id):
        return
    source = (command.args or "other").lower()
    if source not in VALID_SOURCES:
        source = "other"
    await state.clear()
    await db.upsert_start(tg_user_id=message.from_user.id, source=source)
    # Имя берём из Telegram (как пользователь сам себя назвал) — без ручного ввода.
    name = (message.from_user.full_name or "").strip()[:100] or "друг"
    await db.set_name(message.from_user.id, name)
    await state.set_state(Funnel.consent)
    await messaging.reply_text(
        message, texts.greeting(name), source="funnel", reply_markup=_consent_kb()
    )


@router.message(Command("stop", ignore_case=True))
async def cmd_stop(message: Message):
    """Отписка от рассылок и авто-касаний (152-ФЗ). Команда — обязательный fallback к
    inline-кнопке «Отписаться». Не конфликтует с Лией (on_free_text отсекает '/').

    Подавляет И массовые рассылки, И nurture-касания (фильтры в db). НЕ равно
    erase_requested_at (отзыв согласия на ПДн — отдельная сущность из панели). Выданный
    гайд и ответы Лии на прямой вопрос остаются. Состояние воронки НЕ трогаем.
    """
    await db.set_unsubscribed(message.from_user.id)
    await messaging.reply_text(message, texts.UNSUBSCRIBED_OK, source="system")


@router.callback_query(F.data == "unsub")
async def on_unsub(cb: CallbackQuery):
    """Inline-кнопка «Отписаться» (в футере рассылок). Идемпотентно, БЕЗ state-фильтра."""
    await cb.answer()
    await db.set_unsubscribed(cb.from_user.id)
    await messaging.send_text(cb.bot, cb.from_user.id, texts.UNSUBSCRIBED_OK, source="system")


@router.callback_query(Funnel.consent, F.data == "consent_yes")
async def on_consent(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await db.set_consent(cb.from_user.id, True)
    await state.set_state(Funnel.phone)
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    name = (cb.from_user.full_name or "друг").strip()[:100]
    await messaging.send_text(
        cb.bot, cb.from_user.id, texts.ask_phone(name),
        source="funnel", reply_markup=_phone_kb(),
    )


@router.message(Funnel.phone, F.contact)
async def on_phone(message: Message, state: FSMContext, bot: Bot):
    phone = message.contact.phone_number
    await db.set_phone(message.from_user.id, phone, _phone_hash(phone))
    await messaging.reply_text(
        message, texts.PHONE_OK, source="funnel", reply_markup=ReplyKeyboardRemove()
    )
    await _go_to_gate(message.from_user.id, message, state, bot)


@router.message(Funnel.phone)
async def on_phone_wrong(message: Message):
    await message.answer(texts.PHONE_BUTTON_HINT, reply_markup=_phone_kb())


@router.callback_query(Funnel.gate, F.data == "check_sub")
async def on_check_sub(cb: CallbackQuery, state: FSMContext, bot: Bot):
    if await _is_subscribed(bot, cb.from_user.id):
        await cb.answer("Спасибо! 🌷")
        await db.set_subscribed(cb.from_user.id, True)
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await _deliver(cb.from_user.id, cb.message, state, bot)
    else:
        await cb.answer(texts.NOT_SUBSCRIBED_ALERT, show_alert=True)


@router.callback_query(F.data == "check_sub")
async def on_check_sub_fallback(cb: CallbackQuery, state: FSMContext, bot: Bot):
    """Fallback БЕЗ state-фильтра (§8 плана). Зарегистрирован ПОСЛЕ on_check_sub: при
    state=Funnel.gate сработает основной хендлер (роутер отдаёт первому подходящему), сюда
    падает только потеря FSM на редеплое (state=None) — лид в гейте жмёт «Я подписался».

    Идемпотентно повторяет проверку подписки и выдачу (mark_guide_sent уже coalesce). На
    паузе молчим (оператор ведёт вручную). Логику воронки/гейта не меняем — повторяем её.
    """
    if await db.is_bot_paused(cb.from_user.id):
        await cb.answer()
        return
    if await _is_subscribed(bot, cb.from_user.id):
        await cb.answer("Спасибо! 🌷")
        await db.set_subscribed(cb.from_user.id, True)
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await _deliver(cb.from_user.id, cb.message, state, bot)
    else:
        await cb.answer(texts.NOT_SUBSCRIBED_ALERT, show_alert=True)


@router.callback_query(F.data.startswith("buy:"))
async def on_buy(cb: CallbackQuery, bot: Bot):
    """Клик «Купить» под рассылкой-офером (Phase 1B): pending-заказ + платёж ЮKassa
    (магазин школы) → лиду сообщение с URL-кнопкой «Перейти к оплате».

    Подтверждение оплаты ловит ВЕБХУК ПАНЕЛИ (paid + converted + «спасибо» через outbox) —
    бот здесь только выставляет счёт. Повторный клик в пределах ORDER_REUSE_MINUTES отдаёт
    ТУ ЖЕ ссылку (анти-двойное списание, db.create_or_reuse_pending_order). Паузу диалога
    НЕ гейтим: клик — явное действие лида, ссылка на оплату не «авто-болтовня» Лии.
    Любой сбой → мягкий PAY_UNAVAILABLE, заказ помечается failed (виден в «Платежах»)."""
    # Тумблер панели + ключи магазина в env: что-то выключено → кнопка из старой
    # рассылки могла пережить выключение — отвечаем мягким алертом, не молчим.
    if not (config.SHOP_PAYMENTS_CONFIGURED and await db.is_online_payments_enabled()):
        await cb.answer("Оплата временно недоступна 🥲 Напишите нам — поможем.", show_alert=True)
        return
    try:
        product_id = int((cb.data or "").split(":", 1)[1])
    except (ValueError, IndexError):
        await cb.answer()
        return
    lead = await db.get_lead_for_purchase(cb.from_user.id)
    product = await db.get_product(product_id)
    # Продаём только живой офер с ценой в рублях (ЮKassa-магазин рублёвый; цена могла
    # обнулиться/офер уйти в архив после отправки рассылки — кнопка переживает рассылку).
    if (lead is None or product is None or product.get("status") != "active"
            or not product.get("price") or product["price"] <= 0
            or (product.get("currency") or "RUB") != "RUB"):
        await cb.answer("Этот товар сейчас недоступен 🥲", show_alert=True)
        return

    order = await db.create_or_reuse_pending_order(
        lead["id"], product_id, product["price"], "RUB",
        reuse_minutes=config.ORDER_REUSE_MINUTES,
    )
    if order["reused"]:
        pay_url = order["payment_url"]
    else:
        # return_url — вернуть человека в чат бота после оплаты; имя бота берём из
        # runtime-снимка (бот сам публикует его на старте), фолбэк — просто t.me.
        username = await db.get_app_setting("bot_username")
        return_url = f"https://t.me/{username}" if username else "https://t.me"
        try:
            payment = await yookassa.create_payment(
                amount=product["price"], currency="RUB",
                description=f"{product.get('name') or 'Заказ'} — Школа Лесова",
                return_url=return_url,
                idempotence_key=str(order["id"]),
                metadata={"kind": "order", "order_id": str(order["id"])},
                lead_phone=lead.get("phone"),
            )
            pay_url = (payment.get("confirmation") or {}).get("confirmation_url")
            payment_id = payment.get("id")
            if not pay_url or not payment_id:
                raise yookassa.YooKassaError("нет confirmation_url/id в ответе")
            await db.set_order_payment(order["id"], payment_id, pay_url)
        except yookassa.YooKassaError as e:
            logger.warning("Платёж по заказу %s не создался: %s", order["id"], e)
            await db.mark_order_failed(order["id"], "платёж не создан (сбой ЮKassa)")
            await cb.answer()
            await messaging.send_text(
                bot, cb.from_user.id, texts.PAY_UNAVAILABLE, source="system"
            )
            return

    await cb.answer()
    pay_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=texts.PAY_BTN, url=pay_url)],
    ])
    await messaging.send_text(
        bot, cb.from_user.id, texts.pay_message(product), source="system", reply_markup=pay_kb
    )


@router.message(StateFilter(None), F.text)
async def on_free_text(message: Message, state: FSMContext, bot: Bot):
    """Свободное сообщение ВНЕ воронки → отвечает AI-ассистент Лия.

    Срабатывает только при пустом состоянии (StateFilter(None)). Шаги воронки
    (у них своё состояние) и команда /start (свой хендлер выше) сюда не попадают.
    parent_message_id храним в FSM data — для контекста диалога.
    """
    if message.text.startswith("/"):
        return  # неизвестные команды в AI не отправляем
    # Перехват (§4): на паузе Лия молчит — оператор отвечает руками. Входящее уже
    # залогировано middleware; просто не запускаем авто-ответ.
    if await db.is_bot_paused(message.from_user.id):
        return
    # Глобальный тумблер Лии (раздел «ИИ-агенты» панели): выключена → молчим, как при
    # паузе (оператор ответит руками). agent_id/fallback берём поверх env из тех же настроек.
    # Выбор «ИИ-сотрудника»: персона диалога (оператор в «Диалогах») > канал (source) > глобал.
    lead_source = await db.get_lead_source(message.from_user.id)
    lead_persona = await db.get_lead_persona(message.from_user.id)
    ai_cfg = await db.get_ai_overrides(lead_source, lead_persona)
    if not ai_cfg["enabled"]:
        return
    # Wave 3 (ТЗ §5.1): кошелёк prepaid-тенанта пуст → ИИ на мягкой паузе, но лид
    # без ответа не остаётся. Школа (без плана) флага не получает никогда (§8.7).
    if await db.is_ai_wallet_blocked():
        await messaging.send_text(
            bot, message.from_user.id, texts.WALLET_PAUSED, source="system"
        )
        return
    try:
        await bot.send_chat_action(message.chat.id, "typing")
    except Exception:
        pass
    data = await state.get_data()
    # RF-RAG (опц., тумблер kb_enabled в панели): подмешиваем справку из базы знаний в
    # запрос агента. Выключено / эмбеддер недоступен / база пуста → user_text = исходный
    # текст (поведение без изменений). retrieve_context не падает и не блокирует ответ.
    user_text = message.text
    if ai_cfg.get("kb_enabled"):
        kb_context = await kb.retrieve_context(user_text, lead_persona)
        user_text = kb.augment(user_text, kb_context)
    # Wave 5: контекст диалога — историей сообщений (OpenAI-эндпоинт агента серверного
    # parent_message_id не имеет). Текущее входящее уже залогировано middleware → исключаем
    # его по message_id, финальным user-turn идёт user_text (возможно с RAG-контекстом).
    history = await db.get_ai_history(
        message.from_user.id,
        exclude_tg_message_id=message.message_id,
        limit=config.AI_HISTORY_MESSAGES,
    )
    answer, msg_id = await ai.ask_ai(
        user_text, data.get("ai_parent_id"), ai_cfg, history=history
    )
    if msg_id:
        await state.update_data(ai_parent_id=msg_id)
    # Гонка Лии (§4): ask_liya мог идти до 30с — оператор мог включить паузу за это
    # время. Повторно проверяем ПЕРЕД отправкой; на паузе ответ не шлём.
    if await db.is_bot_paused(message.from_user.id):
        return
    # rich=True: ответ Лии — markdown(LLM)→Telegram-HTML с фолбэком на plain (красивый текст, §8.7).
    await messaging.send_text(bot, message.from_user.id, answer, source="liya", rich=True)


async def _go_to_gate(user_id: int, message: Message, state: FSMContext, bot: Bot):
    if await _is_subscribed(bot, user_id):
        await db.set_subscribed(user_id, True)
        await _deliver(user_id, message, state, bot)
    else:
        await state.set_state(Funnel.gate)
        await messaging.send_text(
            bot, user_id, texts.ASK_SUBSCRIBE, source="funnel", reply_markup=_gate_kb()
        )


async def _is_subscribed(bot: Bot, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(config.CHANNEL_ID, user_id)
    except Exception as e:
        # Бот не админ канала, неверный CHANNEL_ID или Telegram недоступен.
        # Фейлимся ЗАКРЫТО: при ошибке проверки материал НЕ выдаём — гейт держит.
        logger.warning("Не удалось проверить подписку user=%s: %s", user_id, e)
        return False
    if member.status in _SUBSCRIBED:
        return True
    # «restricted» — это всё ещё участник канала, если is_member=True.
    if member.status == ChatMemberStatus.RESTRICTED:
        return bool(getattr(member, "is_member", False))
    return False  # left / kicked / прочее — не подписан


async def _deliver(user_id: int, message: Message, state: FSMContext, bot: Bot):
    await db.mark_guide_sent(user_id)
    await state.clear()
    if config.VIDEO_NOTE_FILE_ID:
        try:
            await messaging.send_video_note(
                bot, user_id, config.VIDEO_NOTE_FILE_ID, source="funnel"
            )
        except Exception as e:
            logger.warning("Не удалось отправить видео-кружок: %s", e)
    # Опц. выдача лид-магнита продуктом из каталога ВМЕСТО GUIDE_URL-заглушки (решение
    # владельца): если в app_settings задан active_lead_magnet_product_id и офер готов —
    # отдаём его (фото/документ + подпись + ссылка). Любой промах → фолбэк на GUIDE_URL
    # без изменений. Логику ГЕЙТА это не трогает: сюда уже попали после успешной проверки
    # подписки; меняется только КОНТЕНТ финальной выдачи.
    if await _deliver_lead_magnet_product(user_id, bot):
        return
    # Ссылка-гайд = app_settings['guide_url'] (панель, «Интеграции») ПОВЕРХ env GUIDE_URL;
    # любой промах → env (см. db.get_effective_guide_url). Берём один раз: текст и кнопка совпадают.
    guide_url = await db.get_effective_guide_url()
    await messaging.send_text(
        bot, user_id, texts.deliver(guide_url), source="funnel", reply_markup=_guide_kb(guide_url)
    )


async def _deliver_lead_magnet_product(user_id: int, bot: Bot) -> bool:
    """Пытается выдать продукт-лид-магнит из каталога. True — выдан (звонящий не шлёт
    GUIDE_URL), False — офер не настроен/не готов/ошибка → звонящий делает фолбэк.

    Продукт берётся из app_settings (валидируется в db.get_active_lead_magnet_product:
    kind='lead_magnet', status='active', есть file_tg_id ИЛИ link). Файл идёт через
    interactive send_by_kind (фото/документ по file_mime) с подписью texts.deliver_product;
    если файла нет, но есть ссылка — обычным текстом. Ссылку даём «сырой» (per-recipient
    трекинг /r — атрибут рассылки, не воронки). Ошибка изолируется → фолбэк на GUIDE_URL.
    """
    try:
        product = await db.get_active_lead_magnet_product()
    except Exception as e:  # noqa: BLE001 — чтение настройки не должно ломать выдачу
        logger.warning("Не удалось прочитать активный лид-магнит-офер: %s", e)
        return False
    if product is None:
        return False  # офер не настроен/не готов → фолбэк на GUIDE_URL

    file_tg_id = product.get("file_tg_id")
    link = (product.get("link") or "").strip() or None
    caption = texts.deliver_product(product, link)
    try:
        if file_tg_id:
            kind = messaging.kind_for_mime(product.get("file_mime"))
            await messaging.send_by_kind(
                bot, user_id, kind, file_id=file_tg_id, caption=caption, source="funnel"
            )
        else:
            # Файла нет — выдаём текстом (у офера точно есть link, иначе db вернул None).
            await messaging.send_text(bot, user_id, caption, source="funnel")
    except Exception as e:  # noqa: BLE001 — сбой выдачи офера → фолбэк на GUIDE_URL
        logger.warning("Не удалось выдать лид-магнит-офер #%s: %s — фолбэк на GUIDE_URL",
                       product.get("id"), e)
        return False
    return True
