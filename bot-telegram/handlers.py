"""Сценарий воронки (FSM): /start → согласие → имя → телефон → гейт подписки → выдача гайда."""
import hashlib
import logging

from aiogram import Router, F, Bot
from aiogram.enums import ChatMemberStatus
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
)

import config
import db
import texts

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


def _guide_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=texts.GUIDE_BTN, url=config.GUIDE_URL)],
    ])


@router.message(Command("start", ignore_case=True))
async def cmd_start(message: Message, command: CommandObject, state: FSMContext):
    source = (command.args or "other").lower()
    if source not in VALID_SOURCES:
        source = "other"
    await state.clear()
    await db.upsert_start(tg_user_id=message.from_user.id, source=source)
    # Имя берём из Telegram (как пользователь сам себя назвал) — без ручного ввода.
    name = (message.from_user.full_name or "").strip()[:100] or "друг"
    await db.set_name(message.from_user.id, name)
    await state.set_state(Funnel.consent)
    await message.answer(texts.greeting(name), reply_markup=_consent_kb())


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
    await cb.message.answer(texts.ask_phone(name), reply_markup=_phone_kb())


@router.message(Funnel.phone, F.contact)
async def on_phone(message: Message, state: FSMContext, bot: Bot):
    phone = message.contact.phone_number
    await db.set_phone(message.from_user.id, phone, _phone_hash(phone))
    await message.answer(texts.PHONE_OK, reply_markup=ReplyKeyboardRemove())
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


async def _go_to_gate(user_id: int, message: Message, state: FSMContext, bot: Bot):
    if await _is_subscribed(bot, user_id):
        await db.set_subscribed(user_id, True)
        await _deliver(user_id, message, state, bot)
    else:
        await state.set_state(Funnel.gate)
        await message.answer(texts.ASK_SUBSCRIBE, reply_markup=_gate_kb())


async def _is_subscribed(bot: Bot, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(config.CHANNEL_ID, user_id)
        return member.status in _SUBSCRIBED
    except Exception as e:
        # Чаще всего — бот не админ канала или пользователь ещё не взаимодействовал.
        logger.warning("Не удалось проверить подписку user=%s: %s", user_id, e)
        return False


async def _deliver(user_id: int, message: Message, state: FSMContext, bot: Bot):
    await db.mark_guide_sent(user_id)
    await state.clear()
    if config.VIDEO_NOTE_FILE_ID:
        try:
            await bot.send_video_note(message.chat.id, config.VIDEO_NOTE_FILE_ID)
        except Exception as e:
            logger.warning("Не удалось отправить видео-кружок: %s", e)
    await message.answer(texts.deliver(config.GUIDE_URL), reply_markup=_guide_kb())
