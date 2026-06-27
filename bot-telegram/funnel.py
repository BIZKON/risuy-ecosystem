"""Пер-тенантная воронка выдачи лид-магнита для ТЕНАНТ-ботов (multiplex).

DB-state-driven (без aiogram-FSM): шаг определяется callback'ом + флагами лида в БД
(consent/subscribed), а не FSM-хранилищем — устойчиво к редеплою и не требует storage на
каждый тенант-бот. Конфиг берётся из db.get_funnel_config(tenant) (tenant_settings, конструктор
в панели). Текст согласия 152-ФЗ генерится из структурных полей (shared/leadmagnet) и приходит
в cfg["consent_text"]. Реквизиты/тексты — из конфига; кнопки пока на дефолтах (конфигурируемость
кнопок — поздняя задача).

⚠️ Школа (env-бот handlers.py) этим модулем НЕ затрагивается — он только для мультиплекса.
⚠️ db-писатели (set_consent/set_phone/set_subscribed/mark_guide_sent) tenant-scoped через
contextvar — вызывать ТОЛЬКО под установленным tenant_id (middleware мультиплекса это делает).

aiogram/messaging импортируются ЛЕНИВО внутри async-шагов → чистые хелперы тестируемы в
.venv-smoke (где aiogram нет), как драйверы VK/MAX с aiohttp.
"""
import hashlib
import logging

import db

logger = logging.getLogger(__name__)


def phone_hash(phone: str) -> str:
    """sha256 только-цифр телефона. ⚠️ ИДЕНТИЧНО handlers._phone_hash и панели
    phone_query_hash — иначе поиск лида по телефону в панели молча вернёт пусто."""
    digits = "".join(ch for ch in phone if ch.isdigit())
    return hashlib.sha256(digits.encode()).hexdigest() if digits else ""

# ── Дефолтные тексты/кнопки (welcome/consent/caption приходят из cfg; остальное — дефолт) ──
CONSENT_BTN = "✅ Даю согласие"
PRIVACY_BTN = "Политика обработки данных"
PHONE_BTN = "📱 Поделиться номером"
SUBSCRIBE_BTN = "📣 Подписаться"
CHECK_SUB_BTN = "✅ Я подписался"
GUIDE_BTN = "🎁 Забрать"

DEFAULT_WELCOME = "Здравствуйте! 🌷 Помогу забрать ваш подарок — это займёт минуту."
DEFAULT_CAPTION = "Готово! 🎉 Лови свой подарок:"
ASK_PHONE = "Остался номер — нажмите кнопку ниже, так мы сможем прислать материал и быть на связи."
PHONE_OK = "Спасибо! 🌷"
PHONE_HINT = "Нажмите кнопку «📱 Поделиться номером» ниже — так надёжнее, чем вводить вручную 🙂"
ASK_SUBSCRIBE = "Остался один шаг 🙂 Подпишитесь на канал и нажмите «Я подписался» — и я сразу пришлю материал."
NOT_SUBSCRIBED_ALERT = "Пока не вижу подписки. Подпишитесь на канал и нажмите ещё раз 🌷"
NOT_CONFIGURED = "Спасибо! Материал скоро будет — мы на связи. 🌷"
FILE_PREPARING = "Спасибо! Файл готовится — пришлю буквально через минуту, загляните чуть позже 🌷"


# ── ЧИСТЫЕ хелперы (без aiogram/messaging — тестируемы в .venv-smoke) ──────────
def start_text(cfg: dict) -> str:
    """Текст первого сообщения: приветствие + (если есть) блок согласия 152-ФЗ из cfg."""
    welcome = (cfg.get("welcome_text") or "").strip() or DEFAULT_WELCOME
    consent = (cfg.get("consent_text") or "").strip()
    return welcome + ("\n\n" + consent if consent else "")


def next_after_consent(cfg: dict) -> str:
    """Куда идём после согласия: 'phone' | 'gate' | 'deliver' (по флагам конфига)."""
    if cfg.get("phone_step"):
        return "phone"
    if (cfg.get("gate") or {}).get("enabled"):
        return "gate"
    return "deliver"


def next_after_phone(cfg: dict) -> str:
    """После телефона: 'gate' | 'deliver'."""
    return "gate" if (cfg.get("gate") or {}).get("enabled") else "deliver"


def deliver_plan(cfg: dict) -> dict:
    """Что выдаём на финале (без сети). configured=False → лид-магнит не настроен (мягкий ответ).
    Для kind=file материал — загруженный продукт (product_id) ИЛИ сырой tg file_id."""
    lm = cfg.get("leadmagnet") or {}
    kind = lm.get("kind")
    caption = (lm.get("caption") or "").strip() or DEFAULT_CAPTION
    url = (lm.get("url") or "").strip() or None
    file_id = (lm.get("file_id") or "").strip() or None
    product_id = str(lm.get("product_id") or "").strip() or None
    configured = bool((kind == "link" and url) or (kind == "file" and (file_id or product_id)))
    return {"has_video": bool((cfg.get("video_note_file_id") or "").strip()),
            "kind": kind, "caption": caption, "url": url, "file_id": file_id,
            "product_id": product_id, "configured": configured}


# ── Клавиатуры (aiogram — ленивый импорт) ─────────────────────────────────────
def _consent_kb(cfg: dict):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    rows = [[InlineKeyboardButton(text=CONSENT_BTN, callback_data="consent_yes")]]
    # Внешняя ссылка на политику ИЛИ сгенерированная страница /legal/{slug}/privacy (фолбэк).
    privacy = (cfg.get("privacy_url") or cfg.get("legal_privacy_url") or "").strip()
    if privacy:
        rows.append([InlineKeyboardButton(text=PRIVACY_BTN, url=privacy)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _phone_kb():
    from aiogram.types import KeyboardButton, ReplyKeyboardMarkup
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=PHONE_BTN, request_contact=True)]],
        resize_keyboard=True, one_time_keyboard=True)


def _gate_kb(cfg: dict):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    url = (cfg.get("gate") or {}).get("channel_url") or ""
    rows = []
    if url:  # ⚠️ пустой url → aiogram отвергает кнопку (Pydantic) и ломает выдачу; тогда — только «Я подписался»
        rows.append([InlineKeyboardButton(text=SUBSCRIBE_BTN, url=url)])
    rows.append([InlineKeyboardButton(text=CHECK_SUB_BTN, callback_data="check_sub")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _guide_kb(url: str):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=GUIDE_BTN, url=url)]])


# ── Шаги воронки (async; messaging/aiogram — ленивый импорт) ──────────────────
async def start(ch, cfg: dict) -> None:
    """Приветствие + согласие (вызывается из диспетчера канала при enabled)."""
    privacy = (cfg.get("privacy_url") or cfg.get("legal_privacy_url") or "")
    await ch.send_consent(start_text(cfg), privacy or None)


async def after_consent(ch, cfg: dict) -> None:
    """После согласия: телефон → гейт → выдача (по конфигу)."""
    step = next_after_consent(cfg)
    if step == "phone":
        await ch.ask_phone(ASK_PHONE)
    elif step == "gate":
        await go_to_gate(ch, cfg)
    else:
        await deliver(ch, cfg)


async def after_phone(ch, cfg: dict) -> None:
    """После телефона: подтверждение + гейт/выдача."""
    await ch.send_text(PHONE_OK)
    if next_after_phone(cfg) == "gate":
        await go_to_gate(ch, cfg)
    else:
        await deliver(ch, cfg)


async def go_to_gate(ch, cfg: dict) -> None:
    """Гейт подписки на канал тенанта (fail-closed). Подписан → выдача; иначе просьба подписаться."""
    gate = cfg.get("gate") or {}
    if await ch.check_subscription(gate, ch.uid):
        await db.set_subscribed(ch.uid, True, messenger=ch.messenger)
        await deliver(ch, cfg)
    else:
        await ch.ask_gate(ASK_SUBSCRIBE, gate.get("channel_url"))


async def is_subscribed(bot, channel_id, user_id: int) -> bool:
    """Проверка подписки на канал тенанта. Fail-closed: ошибка/бот-не-админ/кривой id → False (гейт держит)."""
    from aiogram.enums import ChatMemberStatus
    try:
        cid = int(str(channel_id).strip())
    except (TypeError, ValueError):
        logger.warning("funnel: некорректный gate_channel_id=%r — гейт держит", channel_id)
        return False
    try:
        member = await bot.get_chat_member(cid, user_id)
    except Exception as e:  # noqa: BLE001 — бот не админ канала / Telegram недоступен
        logger.warning("funnel: не проверил подписку user=%s ch=%s: %s", user_id, channel_id, e)
        return False
    st = member.status
    if st in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR):
        return True
    if st == ChatMemberStatus.RESTRICTED:
        return bool(getattr(member, "is_member", False))
    return False


async def deliver(ch, cfg: dict) -> None:
    """Финальная выдача лид-магнита через адаптер: видео-кружок (опц.) → файл/ссылка.

    ⚠️ mark_guide_sent — ТОЛЬКО ПОСЛЕ успешной отправки материала. Воронка тенанта DB-state-driven:
    преждевременная пометка → при сбое отправки (битый file_id/Telegram down) лид помечен выданным,
    но материал не получил. При НЕнастроенном лид-магните не помечаем (выдадим, когда настроят)."""
    plan = deliver_plan(cfg)
    if plan["has_video"]:
        try:
            await ch.deliver_video_note((cfg.get("video_note_file_id") or "").strip())
        except Exception as e:  # noqa: BLE001 — видео не критично
            logger.warning("funnel: видео-кружок не отправлен: %s", e)
    if not plan["configured"]:
        await ch.deliver_text(NOT_CONFIGURED)
        return
    if plan["kind"] == "file":
        prod = None
        if plan["product_id"]:
            try:
                prod = await db.get_funnel_product(int(plan["product_id"]))
            except (TypeError, ValueError):
                prod = None
        if prod is not None:
            if prod.get("file_tg_id") or prod.get("link"):
                if await ch.deliver_file(plan["caption"], prod):
                    await db.mark_guide_sent(ch.uid, messenger=ch.messenger)
                    return
            else:
                await ch.deliver_text(FILE_PREPARING)
                return
        if plan["file_id"]:
            try:
                if await ch.deliver_file(plan["caption"], {"file_tg_id": plan["file_id"], "file_mime": None}):
                    await db.mark_guide_sent(ch.uid, messenger=ch.messenger)
                    return
            except Exception as e:  # noqa: BLE001
                logger.warning("funnel: файл-лид-магнит (file_id) не выдан (%s) — фолбэк", e)
    if plan["url"]:
        await ch.deliver_url(plan["caption"], plan["url"])
    else:
        await ch.deliver_text(plan["caption"])
    await db.mark_guide_sent(ch.uid, messenger=ch.messenger)
