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
import escalation
import kb
import messaging
import texts
import triggers
import yookassa
from shared.club import (
    CLUB_CHAIN_POSITIONS,
    build_club_consent_text,
    validate_club_registration,
)

logger = logging.getLogger(__name__)
router = Router()

# ⚠️ Держать в синхроне с admin-panel/config.py::SOURCES + SOURCE_LABELS и комментом-списком в
# db/schema.sql (колонка source). Бот и панель — РАЗНЫЕ процессы (импорт невозможен): новая площадка
# = править ВСЕ три места. Бот валидирует входящий deep-link source против этого набора.
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


class ClubSignup(StatesGroup):
    """FSM-воронка регистрации в «Клуб предпринимателей» (Task 3b). Отдельная от Funnel:
    свой лид-магнит (клуб), свой набор шагов. Вход — deep-link ?start=club или /club.
    Шаги собирают карточку бизнеса; на завершении вызываются club-хелперы (Task 3a)."""
    consent = State()
    display_name = State()
    city = State()
    okved = State()
    offering = State()
    seeking = State()
    chain = State()


class PartnerRef(StatesGroup):
    """Реф-поток: клиент по партнёрской ссылке называет компанию → бот создаёт тенанта+бриф."""
    company = State()


# ⚠️ ИДЕНТИЧНО панели admin-panel/db.py::phone_query_hash (sha256 только-цифры). Любое расхождение
# → поиск лида по телефону в панели молча вернёт пусто. Меняешь алгоритм — синхронно в обоих местах.
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


def _club_consent_kb(privacy_url: str = "") -> InlineKeyboardMarkup:
    """Кнопка согласия для входа в клуб + (опц.) политика ПДн. Согласие — callback
    club_consent_yes; политика — ПЕР-ТЕНАНТНЫЙ privacy_url (как в основной воронке:
    privacy_url тенанта / сгенерированная /legal/{slug}/privacy), с фолбэком на глобальный
    config.PRIVACY_URL. Мульти-тенант: показываем Политику именно оператора-тенанта."""
    rows = [[InlineKeyboardButton(text=texts.CLUB_JOIN_BTN, callback_data="club_consent_yes")]]
    url = privacy_url or config.PRIVACY_URL
    if url:
        rows.append([InlineKeyboardButton(text=texts.PRIVACY_BTN, url=url)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _club_chain_kb() -> InlineKeyboardMarkup:
    """Inline-кнопки выбора позиции в цепочке поставки из CLUB_CHAIN_POSITIONS (Task 3a).
    callback_data='club_chain:<code>' — code валидируется через validate_club_registration."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=f"club_chain:{code}")]
        for code, label in CLUB_CHAIN_POSITIONS
    ])


async def _club_start(message: Message, state: FSMContext) -> None:
    """Общий вход в воронку клуба (из deep-link ?start=club и из /club). Операторское имя
    берём из funnel-config активного тенанта (панель, «Интеграции») — ключ company_name
    (= operator_name с фолбэком на company_name, см. db.get_funnel_config) — поверх env-
    фолбэка config.CLUB_OPERATOR_NAME. Если реквизиты оператора у тенанта не настроены —
    вход в клуб отклоняется (см. ниже): показывать/хешировать согласие с пустым/заглушечным
    оператором нельзя, иначе consent_events зафиксирует юридически ничтожный текст (152-ФЗ).
    Текст согласия строим один раз и кладём в FSM data — на финале хешируем ИМЕННО то, что
    показали человеку (club_consent_record.text_hash)."""
    cfg = await db.get_funnel_config(db.tenant_id())
    operator = (cfg.get("company_name") or config.CLUB_OPERATOR_NAME or "").strip()
    if not operator:
        logger.warning(
            "club: у тенанта %s не настроены реквизиты оператора — клуб-вход отклонён",
            db.tenant_id(),
        )
        await messaging.reply_text(
            message,
            "Клуб сейчас недоступен: у оператора не настроены реквизиты. Загляните чуть позже 🙂",
            source="system",
        )
        return
    operator_inn = (cfg.get("operator_inn") or "").strip()
    operator_email = (cfg.get("operator_email") or "").strip()
    privacy_url = (cfg.get("privacy_url") or cfg.get("legal_privacy_url") or "").strip()
    # 152-ФЗ ст.9: полный состав согласия невозможен без идентификации оператора (ИНН) и
    # порядка отзыва (email) — паритет с эталоном воронки. Нет реквизитов → клуб-вход отклонён.
    if not (operator_inn and operator_email):
        logger.warning(
            "club: у тенанта %s не заполнены ИНН/email оператора — клуб-вход отклонён (152-ФЗ состав)",
            db.tenant_id(),
        )
        await messaging.reply_text(
            message,
            "Клуб сейчас недоступен: у оператора не настроены реквизиты. Загляните чуть позже 🙂",
            source="system",
        )
        return
    consent_text = build_club_consent_text("club_join", operator, operator_inn,
                                           operator_email, privacy_hint=bool(privacy_url))
    await state.set_state(ClubSignup.consent)
    await state.update_data(club_consent_text=consent_text)
    await messaging.reply_text(
        message, texts.CLUB_INTRO + consent_text, source="funnel",
        reply_markup=_club_consent_kb(privacy_url),
    )


async def _club_finish(user_id: int, bot: Bot, state: FSMContext) -> None:
    """Финал воронки клуба: валидация → создание карточки участника (club_members +
    club_profiles) → фиксация согласия (consent_events). Если tg-юзер — известный лид
    тенанта (пришёл по приглашению «Пригласить в клуб», вход B), участник и запись
    согласия привязываются к его lead_id; иначе (чистый лид-магнит) lead_id = None.
    club_member_create сам скоупит lead по тенанту (подзапрос) — чужой не привяжется."""
    data = await state.get_data()
    reg = {k: data.get(k) for k in ("display_name", "city", "okved", "offering", "seeking", "chain_position")}
    errs = validate_club_registration(reg)
    if errs:
        await messaging.send_text(
            bot, user_id, "Проверьте данные:\n• " + "\n• ".join(errs), source="funnel"
        )
        await state.clear()
        return
    try:
        # Вход B: резолвим lead_id текущего tg-юзера в активном тенанте. None — юзер не
        # лид этого тенанта (чистый клуб-лид-магнит), тогда участник без привязки, как было.
        lead_id = await db.get_lead_id(user_id, messenger="tg")
        # Important #2 (upsert-семантика вступления): повторный /club тем же лидом НЕ плодит
        # дубли карточек. Дубль ломал адресацию intro (панель шлёт intro на ОДНОГО из членов
        # одного лида, второй «повисал»). Если у лида уже есть карточка — переиспользуем её id
        # и обновляем профиль; иначе создаём нового члена как раньше.
        existing = await db.club_member_by_lead(lead_id) if lead_id is not None else None
        if existing is not None:
            mid = str(existing["id"])
        else:
            mid = await db.club_member_create(
                display_name=reg["display_name"], city=reg["city"], okved=reg["okved"],
                lead_id=lead_id, tg_user_id=user_id, messenger="tg",
            )
        await db.club_profile_upsert(
            mid, offering=reg["offering"], seeking=reg["seeking"],
            chain_position=reg["chain_position"], okved_seek="",
        )
        text_hash = hashlib.sha256((data.get("club_consent_text") or "").encode("utf-8")).hexdigest()
        await db.club_consent_record(doc_type="club_join", member_id=mid, lead_id=lead_id,
                                     text_hash=text_hash, channel="tg")
        await messaging.send_text(bot, user_id, texts.CLUB_DONE, source="funnel")
    except Exception as e:
        logger.warning("club: не удалось сохранить карточку: %s", e)
        await messaging.send_text(bot, user_id, texts.CLUB_ERROR, source="system")
    finally:
        await state.clear()


def _intro_decide_kb(intro_id: str, privacy_url: str = "") -> InlineKeyboardMarkup:
    """Кнопки «Принять»/«Отклонить» под приглашением на знакомство + (опц.) Политика ПДн
    (пер-тенантный privacy_url, фолбэк config.PRIVACY_URL — L3-intro-policy-link-absent).
    callback_data несёт intro_id — callback переперепроверит участие (from ИЛИ to) +
    status='requested' (защита от утёкшей/устаревшей кнопки), поэтому доверять самому intro_id
    в кнопке безопасно."""
    rows = [[
        InlineKeyboardButton(text=texts.CLUB_INTRO_OK_BTN, callback_data=f"intro_ok:{intro_id}"),
        InlineKeyboardButton(text=texts.CLUB_INTRO_NO_BTN, callback_data=f"intro_no:{intro_id}"),
    ]]
    url = privacy_url or config.PRIVACY_URL
    if url:
        rows.append([InlineKeyboardButton(text=texts.PRIVACY_BTN, url=url)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _intro_side_accepted(intro: dict, member_id) -> bool:
    """Приняла ли УЖЕ свою сторону этот член (from_accepted_at/to_accepted_at по его стороне
    не NULL). Двусторонний accept: панель шлёт deep-link ОБОИМ, каждый принимает СВОЮ сторону
    — этот хелпер защищает от повторного открытия ссылки той же стороной."""
    if str(intro["from_member"]) == str(member_id):
        return intro.get("from_accepted_at") is not None
    if str(intro["to_member"]) == str(member_id):
        return intro.get("to_accepted_at") is not None
    return False


async def _club_intro_open(message: Message, intro_id: str) -> None:
    """Открытие приглашения на знакомство по deep-link ?start=intro_<id> (Task 8 — двусторонний).
    Панель шлёт ссылку ОБОИМ участникам, поэтому кликнувший должен быть членом клуба (лид →
    club_member_by_lead) И участником intro (from_member ИЛИ to_member) в статусе 'requested' —
    иначе мягкий отказ (утёкшая/чужая ссылка). Имя бизнеса второй стороны НЕ раскрываем: контакты
    — только после взаимного accept (club_intro_reveal гейтит status='accepted'). До accept
    показываем лишь факт предложения + кнопки. Если эта сторона УЖЕ приняла свою — напоминаем,
    что ждём второго, без кнопок."""
    lead_id = await db.get_lead_id(message.from_user.id, messenger="tg")
    if lead_id is None:
        await messaging.reply_text(message, texts.CLUB_INTRO_UNAVAILABLE, source="system")
        return
    intro = await db.club_intro_get(intro_id)
    if intro is None:
        await messaging.reply_text(message, texts.CLUB_INTRO_NOT_FOUND, source="system")
        return
    member = await db.club_member_by_lead(lead_id)
    if member is None:
        await messaging.reply_text(message, texts.CLUB_INTRO_UNAVAILABLE, source="system")
        return
    # 🔒 Любая сторона (from ИЛИ to) — участник intro; чужой клик отсекаем.
    if str(member["id"]) not in (str(intro["from_member"]), str(intro["to_member"])):
        await messaging.reply_text(message, texts.CLUB_INTRO_NOT_YOURS, source="system")
        return
    if intro["status"] != "requested":
        await messaging.reply_text(message, texts.CLUB_INTRO_ALREADY, source="system")
        return
    # Эта сторона уже приняла свою (ждём второго) — напоминаем, без кнопок.
    if _intro_side_accepted(intro, member["id"]):
        await messaging.reply_text(message, texts.CLUB_INTRO_ALREADY_CONFIRMED, source="funnel")
        return
    # ПОКАЗЫВАЕМ ровно тот consent-текст, что будет хэширован в consent_events (показано==хэшировано,
    # L3-intro-shown-neq-hashed). Он называет оператора+ИНН, перечисляет реально раскрываемый при
    # взаимном accept состав (название/город/ОКВЭД/имя/телефон) и порядок отзыва. Имя инициатора
    # НЕ раскрываем — только факт предложения (красная линия; контакты — после обоюдного accept).
    cfg = await db.get_funnel_config(db.tenant_id())
    operator = (cfg.get("company_name") or config.CLUB_OPERATOR_NAME or "").strip()
    operator_inn = (cfg.get("operator_inn") or "").strip()
    operator_email = (cfg.get("operator_email") or "").strip()
    privacy_url = (cfg.get("privacy_url") or cfg.get("legal_privacy_url") or "").strip()
    # Симметрично club_join (F2): без реквизитов оператора не показываем accept — иначе
    # хэшируем юридически ничтожное согласие (152-ФЗ ст.9). Intro доступен только после
    # полноценного онбординга, но тенант мог очистить реквизиты — страхуемся.
    if not (operator and operator_inn and operator_email):
        logger.warning("club: intro отклонён — у тенанта %s не настроены реквизиты оператора", db.tenant_id())
        await messaging.reply_text(message, texts.CLUB_INTRO_UNAVAILABLE, source="system")
        return
    text = build_club_consent_text("intro", operator, operator_inn, operator_email)
    await messaging.reply_text(
        message, text, source="funnel", reply_markup=_intro_decide_kb(intro_id, privacy_url)
    )


def _brief_link(token) -> str | None:
    """Абсолютная ссылка на бриф или None при пустом BOT_PUBLIC_BASE_URL (затёртый env).
    Все остальные потребители base URL гардят пустоту — без гарда лид получал бы
    битую ссылку «/brief/x» без хоста."""
    base = (config.BOT_PUBLIC_BASE_URL or "").rstrip("/")
    if not base:
        logger.error("BOT_PUBLIC_BASE_URL пуст — ссылку на бриф отдать нечем")
        return None
    return f"{base}/brief/{token}"


async def _ref_start(message: Message, ref_code: str, state: FSMContext) -> None:
    """Вход реф-потока по ?start=ref_<code>. Резолв партнёра + лёгкий гард (дедуп + rate-limit)."""
    partner = await db.get_partner_by_ref_code(ref_code)
    if partner is None:
        await messaging.reply_text(message, "Ссылка недействительна или отозвана.", source="system")
        return
    uid = message.from_user.id
    dup = await db.find_pending_ref_brief(uid, str(partner["id"]))
    if dup:
        link = _brief_link(dup)
        await messaging.reply_text(
            message,
            (f"Вы уже начали. Заполните бриф: {link}" if link
             else "Вы уже начали. Ссылка на бриф временно недоступна — напишите нам, поможем."),
            source="system")
        return
    if await db.count_recent_ref_tenants(uid, config.REF_RATELIMIT_HOURS) >= config.REF_RATELIMIT_MAX:
        await messaging.reply_text(message, "Слишком много обращений. Попробуйте позже.", source="system")
        return
    await state.update_data(ref_partner_id=str(partner["id"]))
    await state.set_state(PartnerRef.company)
    await messaging.reply_text(message, "Как называется ваша компания?", source="system")


@router.message(PartnerRef.company)
async def on_ref_company(message: Message, state: FSMContext) -> None:
    """Приём названия компании → создание реф-тенанта+брифа + уведомления партнёру/владельцу."""
    company = (message.text or "").strip()[:120]
    if not company:
        await messaging.reply_text(message, "Напишите название компании текстом.", source="system")
        return
    if company.startswith("/"):
        # Это команда (/revoke, /start, /stop...), а не название компании — не создаём
        # мусорный тенант и не глушим команду (в т.ч. 152-ФЗ /revoke). Сбрасываем FSM,
        # чтобы повторная отправка команды дошла до своего Command-хендлера.
        await state.clear()
        await messaging.reply_text(
            message,
            "Похоже, это команда, а не название компании. Ввод отменён — "
            "если хотите заполнить бриф, откройте ссылку заново.",
            source="system")
        return
    data = await state.get_data()
    partner_id = data.get("ref_partner_id")
    await state.clear()
    if not partner_id:
        return
    _tid, token = await db.create_ref_tenant(partner_id, company, message.from_user.id)
    # Уведомления best-effort (не рушат создание, тенант уже создан): партнёру + владельцу.
    try:
        pchat = await db.get_partner_chat_id(partner_id)
        if pchat and pchat.strip():
            await db.enqueue_platform_notify(int(pchat), f"🎯 Новый тенант от тебя: {company}")
    except Exception:  # noqa: BLE001
        logger.warning("partner ref-create notify failed", exc_info=True)
    try:
        ochat = await db.get_owner_chat_id()
        if ochat and ochat.strip():
            await db.enqueue_platform_notify(int(ochat), f"🆕 Новый клиент: {company} (от партнёра)")
    except Exception:  # noqa: BLE001
        logger.warning("owner ref-create notify failed", exc_info=True)
    link = _brief_link(token)
    await messaging.reply_text(
        message,
        (f"Готово! Заполните бриф по ссылке: {link}" if link
         else "Готово! Компания записана. Ссылка на бриф временно недоступна — мы свяжемся с вами."),
        source="system")


@router.message(Command("start", ignore_case=True))
async def cmd_start(message: Message, command: CommandObject, state: FSMContext):
    # Перехват (§4 плана): оператор держит ручное управление → бот молчит. Закрывает
    # единственный обход паузы — повторный /start на тёплом лиде. Логику воронки НЕ трогаем,
    # только не даём ей стартовать на паузе (входящее уже залогировано middleware).
    if await db.is_bot_paused(message.from_user.id):
        return
    # Нормализуем args один раз — оба deep-link'а (club/intro_) регистронезависимы.
    args = (command.args or "").strip()
    # Deep-link ?start=club (регистронезависимо) → воронка «Клуба предпринимателей»
    # (Task 3b). Перехватываем ДО нормализации source: club — не рекламная площадка,
    # а отдельный лид-магнит. Обычный source-путь (reels/dzen/…) не затрагиваем.
    if args.lower() == "club":
        await _club_start(message, state)
        return
    # Deep-link ?start=intro_<intro_id> → приглашение на знакомство в клубе (Task 7b-бот-fsm).
    # Оператор из панели («Предложить знакомство») кладёт члену-цели в lead-канал ссылку
    # t.me/<bot>?start=intro_<id>; здесь член-цель открывает приглашение и жмёт Принять/Отклонить.
    # Перехватываем ДО нормализации source (intro — не рекламная площадка). На паузе не доходим:
    # is_bot_paused отсекает выше (оператор ведёт вручную), как и для club. Регистронезависимо
    # (как club): матчим префикс без учёта регистра, id берём из args ПОСЛЕ префикса как есть —
    # uuid регистронезависим в Postgres, сам id не насилуем.
    if args.lower().startswith("intro_"):
        intro_id = args[len("intro_"):]
        await _club_intro_open(message, intro_id)
        return
    # Deep-link ?start=ref_<code> → вход по партнёрской реферальной ссылке (Task 4).
    # ref_ — action-payload (как club/intro_), НЕ рекламный source: перехватываем ДО
    # нормализации source и делаем early-return, «три места» (VALID_SOURCES) не трогаем.
    if args.lower().startswith("ref_"):
        ref_code = args[len("ref_"):]
        await _ref_start(message, ref_code, state)
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


@router.message(Command("whoami", ignore_case=True))
async def cmd_whoami(message: Message):
    """Отдаёт пользователю его chat_id (Task 3): владелец/партнёр пишет боту /whoami,
    копирует id и вставляет в панель («Интеграции» → «Уведомления владельца»), чтобы
    получать platform_notify. Отдельная команда, а НЕ ветка /start — обычный /start без
    аргумента остаётся входом воронки холодных лидов (source='other'), его не трогаем.
    Без логики паузы/воронки: просто ответ.
    """
    await messaging.reply_text(
        message,
        f"Ваш chat_id: {message.from_user.id}\n"
        "Вставьте его в панели (Интеграции → Уведомления владельца), чтобы получать уведомления.",
        source="system",
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


@router.message(Command("offers_off", ignore_case=True))
async def cmd_offers_off(message: Message):
    """ФЗ-38: отказ от партнёрских предложений клуба БЕЗ выхода из клуба (L2-no-offer-optout).
    Отделено и от согласия на обработку ПДн (членство сохраняется), и от /stop (рассылки школы)."""
    n = await db.club_set_offers_opt_in(message.from_user.id, False)
    await messaging.reply_text(message, texts.OFFERS_OFF_OK if n else texts.OFFERS_NONE, source="system")


@router.message(Command("offers_on", ignore_case=True))
async def cmd_offers_on(message: Message):
    """Вернуть согласие на партнёрские предложения клуба (ФЗ-38 opt-in)."""
    n = await db.club_set_offers_opt_in(message.from_user.id, True)
    await messaging.reply_text(message, texts.OFFERS_ON_OK if n else texts.OFFERS_NONE, source="system")


@router.callback_query(F.data == "unsub")
async def on_unsub(cb: CallbackQuery):
    """Inline-кнопка «Отписаться» (в футере рассылок). Идемпотентно, БЕЗ state-фильтра."""
    await cb.answer()
    await db.set_unsubscribed(cb.from_user.id)
    await messaging.send_text(cb.bot, cb.from_user.id, texts.UNSUBSCRIBED_OK, source="system")


@router.message(Command("revoke", ignore_case=True))
async def cmd_revoke(message: Message):
    """Отзыв согласия на обработку ПДн субъектом (152-ФЗ ст.9 ч.2 — «в любой момент»). ОТЛИЧАЕТСЯ
    от /stop (отписка от рассылок): ставит erase_requested_at + unsubscribed_at + пишет
    consent_events('revoked'); обезличивание ПДн — retention-cron (ERASE_AFTER_DAYS)."""
    lead_id = await db.request_erase(message.from_user.id, channel="tg")
    club_n = await db.club_revoke_member(message.from_user.id, channel="tg")
    if lead_id is not None or club_n:
        await messaging.reply_text(message, texts.REVOKE_OK, source="system")
    else:
        await messaging.reply_text(message, texts.REVOKE_NONE, source="system")


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
    # Принимаем ТОЛЬКО собственный контакт (reply-кнопка request_contact шлёт именно его):
    # пересланный чужой контакт записал бы в лид чужой номер (ПДн третьего лица, 152-ФЗ).
    if message.contact.user_id != message.from_user.id:
        await message.answer(texts.PHONE_BUTTON_HINT, reply_markup=_phone_kb())
        return
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


@router.message(Command("club", ignore_case=True))
async def cmd_club(message: Message, state: FSMContext):
    """Явный вход в воронку клуба командой /club (доп. к deep-link ?start=club)."""
    if await db.is_bot_paused(message.from_user.id):
        return
    await _club_start(message, state)


@router.callback_query(ClubSignup.consent, F.data == "club_consent_yes")
async def on_club_consent(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await state.set_state(ClubSignup.display_name)
    await messaging.send_text(cb.bot, cb.from_user.id, texts.CLUB_ASK_NAME, source="funnel")


@router.message(ClubSignup.display_name, F.text)
async def on_club_display_name(message: Message, state: FSMContext):
    val = (message.text or "").strip()
    if not val:
        await message.answer(texts.CLUB_EMPTY_HINT)
        return
    await state.update_data(display_name=val)
    await state.set_state(ClubSignup.city)
    await messaging.reply_text(message, texts.CLUB_ASK_CITY, source="funnel")


@router.message(ClubSignup.city, F.text)
async def on_club_city(message: Message, state: FSMContext):
    val = (message.text or "").strip()
    if not val:
        await message.answer(texts.CLUB_EMPTY_HINT)
        return
    await state.update_data(city=val)
    await state.set_state(ClubSignup.okved)
    await messaging.reply_text(message, texts.CLUB_ASK_OKVED, source="funnel")


@router.message(ClubSignup.okved, F.text)
async def on_club_okved(message: Message, state: FSMContext):
    val = (message.text or "").strip()
    if not val:
        await message.answer(texts.CLUB_EMPTY_HINT)
        return
    await state.update_data(okved=val)
    await state.set_state(ClubSignup.offering)
    await messaging.reply_text(message, texts.CLUB_ASK_OFFERING, source="funnel")


@router.message(ClubSignup.offering, F.text)
async def on_club_offering(message: Message, state: FSMContext):
    val = (message.text or "").strip()
    if not val:
        await message.answer(texts.CLUB_EMPTY_HINT)
        return
    await state.update_data(offering=val)
    await state.set_state(ClubSignup.seeking)
    await messaging.reply_text(message, texts.CLUB_ASK_SEEKING, source="funnel")


@router.message(ClubSignup.seeking, F.text)
async def on_club_seeking(message: Message, state: FSMContext):
    val = (message.text or "").strip()
    if not val:
        await message.answer(texts.CLUB_EMPTY_HINT)
        return
    await state.update_data(seeking=val)
    await state.set_state(ClubSignup.chain)
    await messaging.reply_text(
        message, texts.CLUB_ASK_CHAIN, source="funnel", reply_markup=_club_chain_kb()
    )


@router.message(StateFilter(
    ClubSignup.display_name, ClubSignup.city, ClubSignup.okved,
    ClubSignup.offering, ClubSignup.seeking,
))
async def on_club_text_wrong(message: Message):
    """Не-текстовый фолбэк для всех текстовых шагов воронки клуба (фото/стикер/voice и т.п.)."""
    await message.answer(texts.CLUB_EMPTY_HINT)


@router.callback_query(ClubSignup.chain, F.data.startswith("club_chain:"))
async def on_club_chain(cb: CallbackQuery, state: FSMContext):
    code = (cb.data or "").split(":", 1)[1]
    await state.update_data(chain_position=code)
    await cb.answer()
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await _club_finish(cb.from_user.id, cb.bot, state)


async def _intro_resolve(cb: CallbackQuery, intro_id: str) -> tuple[dict, dict] | None:
    """Повторно резолвит члена кликнувшего + intro и ПЕРЕПРОВЕРЯЕТ, что кликнувший — УЧАСТНИК
    этого intro (from_member ИЛИ to_member) в статусе 'requested' (защита от подделки/устаревшей
    кнопки при callback, не только при open). Двусторонний: принять может ЛЮБАЯ сторона.
    Возвращает (member, intro) при успехе; иначе показывает cb.answer(alert) и возвращает None."""
    lead_id = await db.get_lead_id(cb.from_user.id, messenger="tg")
    if lead_id is None:
        await cb.answer(texts.CLUB_INTRO_UNAVAILABLE, show_alert=True)
        return None
    intro = await db.club_intro_get(intro_id)
    if intro is None:
        await cb.answer(texts.CLUB_INTRO_NOT_FOUND, show_alert=True)
        return None
    member = await db.club_member_by_lead(lead_id)
    if member is None:
        await cb.answer(texts.CLUB_INTRO_UNAVAILABLE, show_alert=True)
        return None
    # 🔒 Кликнувший — участник intro (любая сторона); чужой не примет/не отклонит чужое.
    if str(member["id"]) not in (str(intro["from_member"]), str(intro["to_member"])):
        await cb.answer(texts.CLUB_INTRO_NOT_YOURS, show_alert=True)
        return None
    if intro["status"] != "requested":
        await cb.answer(texts.CLUB_INTRO_ALREADY, show_alert=True)
        return None
    return member, intro


async def _intro_decline(cb: CallbackQuery) -> None:
    """Отклонение приглашения любой стороной (Task 8). Один decline убивает intro для обеих
    сторон (db.club_intro_decline проверяет status='requested' и участие в WHERE). Контакты
    НЕ раскрываем."""
    intro_id = (cb.data or "").split(":", 1)[1]
    resolved = await _intro_resolve(cb, intro_id)
    if resolved is None:
        return
    member, _intro = resolved
    await db.club_intro_decline(intro_id, member["id"])
    await cb.answer()
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await messaging.send_text(cb.bot, cb.from_user.id, texts.CLUB_INTRO_DECLINED, source="funnel")


async def _intro_accept(cb: CallbackQuery) -> None:
    """Принятие приглашения СТОРОНОЙ кликнувшего (Task 8 — двусторонний). Каждая сторона
    принимает свою; контакты доставляются ТОЛЬКО когда ОБЕ приняли (res["both"]) — reveal
    сам гейтит status='accepted' (красная линия 152-ФЗ). Пока принял только один — шлём
    «ждём второго», контакты НЕ раскрываем."""
    intro_id = (cb.data or "").split(":", 1)[1]
    resolved = await _intro_resolve(cb, intro_id)
    if resolved is None:
        return
    member, intro = resolved
    cfg = await db.get_funnel_config(db.tenant_id())
    operator = (cfg.get("company_name") or config.CLUB_OPERATOR_NAME or "").strip()
    # Хэшируем РОВНО ту строку, что показана субъекту в _club_intro_open (показано==хэшировано):
    # тот же build_club_consent_text('intro', operator, inn, email) детерминирован по cfg.
    consent_text = build_club_consent_text("intro", operator,
                                           (cfg.get("operator_inn") or "").strip(),
                                           (cfg.get("operator_email") or "").strip())
    text_hash = hashlib.sha256(consent_text.encode("utf-8")).hexdigest()
    res = await db.club_intro_accept_side(intro_id, member["id"], text_hash=text_hash)
    await cb.answer()
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    # res["ok"]=False → сообщение по причине. not_open → уже решён другой стороной/повтор;
    # not_found → пропал; not_party → чужое (страховка, resolve уже отсёк).
    if not res.get("ok"):
        reason = res.get("reason")
        if reason == "not_party":
            await messaging.send_text(cb.bot, cb.from_user.id, texts.CLUB_INTRO_NOT_YOURS, source="funnel")
        elif reason == "not_found":
            await messaging.send_text(cb.bot, cb.from_user.id, texts.CLUB_INTRO_NOT_FOUND, source="funnel")
        else:  # not_open
            await messaging.send_text(cb.bot, cb.from_user.id, texts.CLUB_INTRO_ALREADY, source="funnel")
        return
    # Принял, но второй ещё нет → ждём. Контакты НЕ раскрываем.
    if not res.get("both"):
        await messaging.send_text(cb.bot, cb.from_user.id, texts.CLUB_INTRO_WAIT_OTHER, source="funnel")
        return
    # 🔴 ОБА приняли → взаимное согласие. Раскрываем контакты ОБОИМ.
    # «Другая сторона» — по res["side"]: кликнул from → второй=to_member, и наоборот.
    side = res.get("side")
    if side == "from":
        other_member_id = intro["to_member"]
    else:  # "to" (или страховка)
        other_member_id = intro["from_member"]
    reveal = await db.club_intro_reveal(intro_id)
    from_contact = reveal.get("from") if reveal else None
    to_contact = reveal.get("to") if reveal else None
    # Кликнувшему (этот юзер) — контакт ДРУГОЙ стороны. Guard: reveal/поле None (член удалён,
    # 7a Minor #2) — деградируем мягко, не падаем. Изолировано в try/except: сбой доставки
    # этому юзеру НЕ должен блокировать доставку второй стороне (best-effort обе).
    my_other_contact = to_contact if side == "from" else from_contact
    try:
        if my_other_contact:
            card = texts.club_intro_contact_card(my_other_contact)
            await messaging.send_text(
                cb.bot, cb.from_user.id, texts.CLUB_INTRO_MATCH.format(card=card), source="funnel"
            )
        else:
            await messaging.send_text(
                cb.bot, cb.from_user.id, texts.CLUB_INTRO_MATCH_NO_CONTACT, source="funnel"
            )
    except Exception:
        logger.warning(
            "intro %s: не удалось доставить контакт кликнувшему member=%s",
            intro_id, member["id"],
        )
    # Второй стороне — в её lead-канал контакт ЭТОГО юзера. Если недостижима (chan None: не
    # промоушен из лида / нет tg_user_id) — пропускаем, не падаем (лог). Изолировано в свой
    # try/except — сбой здесь не влияет на уже отправленную доставку выше.
    chan = await db.club_member_lead_channel(other_member_id)
    if chan is None:
        logger.info("intro %s: вторая сторона member=%s недостижима — контакт не доставлен",
                    intro_id, other_member_id)
        return
    other_side_contact = from_contact if side == "from" else to_contact
    try:
        if other_side_contact:
            card = texts.club_intro_contact_card(other_side_contact)
            await messaging.send_text(
                cb.bot, chan, texts.CLUB_INTRO_MATCH.format(card=card), source="funnel"
            )
        else:
            await messaging.send_text(cb.bot, chan, texts.CLUB_INTRO_MATCH_NO_CONTACT, source="funnel")
    except Exception:
        logger.warning(
            "intro %s: не удалось доставить контакт второй стороне member=%s",
            intro_id, other_member_id,
        )


@router.callback_query(F.data.startswith("intro_ok:"))
async def on_intro_ok(cb: CallbackQuery):
    await _intro_accept(cb)


@router.callback_query(F.data.startswith("intro_no:"))
async def on_intro_no(cb: CallbackQuery):
    await _intro_decline(cb)


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
    # 152-ФЗ (SL §6, путь 6, fail-closed): авто-диалог Лии — ТОЛЬКО для инбаунд-лида (opt-in).
    # Аутбаунд-сигнал / раздача тенант-0 (provenance != 'inbound_optin') = субъект без согласия →
    # авто-контакт запрещён (152-ФЗ / ФЗ-38). Не отвечаем и не шлём триггер-канед — не контактируем.
    # Легальный выход (§7): провести через consent-funnel; при захвате согласия provenance
    # повышается до 'inbound_optin' и авто-диалог разблокируется штатно. На инбаунде — no-op.
    # NB: явный роутинг в funnel ОТСЮДА отложен (funnel.send_consent как единицы пока нет; голый
    # return — fail-closed; реактивный inbound входит в согласие через /start cmd_start). Подключить с B-FWD.
    if await db.lead_provenance(message.from_user.id) != "inbound_optin":
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
    # Слой B: детерминированные триггеры клиента (стоп-слова / кол-во сообщений). Первый
    # сработавший → ответ клиенту + уведомление менеджерам, ИИ на этот ход пропускаем. Нет
    # настроенных триггеров (Школа) → False → обычный ИИ-поток (поведение не меняется).
    # Слой C: движок канал-агностичен → TriggerCtx (TG); ctx переиспользуем для fire_intent ниже.
    trig_ctx = triggers.TriggerCtx(
        messenger="tg", external_id=message.from_user.id, text=message.text or "",
        reply=lambda body: messaging.send_text(
            bot, message.from_user.id, body, source="trigger", rich=True),
        notifier_fallback_bot=bot)
    if await triggers.handle_text(trig_ctx):
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
        kb_context = await kb.retrieve_context(user_text, db.tenant_id(), lead_persona)  # тенант Школы (default contextvar) — KB ингестится под него
        user_text = kb.augment(user_text, kb_context)
    # Wave 5: контекст диалога — историей сообщений (OpenAI-эндпоинт агента серверного
    # parent_message_id не имеет). Текущее входящее уже залогировано middleware → исключаем
    # его по message_id, финальным user-turn идёт user_text (возможно с RAG-контекстом).
    history = await db.get_ai_history(
        message.from_user.id,
        exclude_tg_message_id=message.message_id,
        limit=config.AI_HISTORY_MESSAGES,
    )
    # Слой B: intent-триггеры тенанта → их описания подмешиваем в системный промпт, Лия эмитит
    # [[TRIGGER:N]] при срабатывании. Нет intent-триггеров (Школа) → ai_cfg без изменений.
    intent_trigs = await db.get_active_triggers(db.tenant_id(), types=("intent",))
    if intent_trigs:
        ai_cfg = {**ai_cfg, "system_prompt": (ai_cfg.get("system_prompt") or "")
                  + "\n\n" + triggers.build_intent_addendum(intent_trigs)}
    answer, msg_id, esc_payload, trig_idxs = await ai.ask_ai(
        user_text, data.get("ai_parent_id"), ai_cfg, history=history
    )
    if msg_id:
        await state.update_data(ai_parent_id=msg_id)
    # A3: служебный маркер эскалации уже вырезан в ai.ask_ai (единая точка). esc_payload != None
    # → горячий лид; передаём менеджерам ПОСЛЕ отправки ответа клиенту.
    # Гонка Лии (§4): ask_liya мог идти до 30с — оператор мог включить паузу за это
    # время. Повторно проверяем ПЕРЕД отправкой; на паузе ответ не шлём.
    if await db.is_bot_paused(message.from_user.id):
        return
    # rich=True: ответ Лии — markdown(LLM)→Telegram-HTML с фолбэком на plain (красивый текст, §8.7).
    await messaging.send_text(bot, message.from_user.id, answer, source="liya", rich=True)
    # A3: горячий лид → карточка менеджерам в ТГ-группу/тему (дедуп; не роняет ответ клиенту).
    if esc_payload is not None:
        await escalation.escalate(bot, message.from_user.id, esc_payload)
    # Слой B: сработавшие intent-триггеры → уведомление менеджерам (+ опц. пауза).
    if trig_idxs:
        await triggers.fire_intent(trig_ctx, intent_trigs, trig_idxs)


@router.message(StateFilter(None), F.document)
async def on_document(message: Message, bot: Bot):
    """Слой B: лид прислал документ в свободном диалоге → триггер типа documents (если настроен).
    На паузе / без триггера — ничего (как было раньше: документы в свободном состоянии не
    обрабатывались)."""
    if await db.is_bot_paused(message.from_user.id):
        return
    ctx = triggers.TriggerCtx(
        messenger="tg", external_id=message.from_user.id, text=message.caption or "",
        reply=lambda body: messaging.send_text(
            bot, message.from_user.id, body, source="trigger", rich=True),
        notifier_fallback_bot=bot)
    await triggers.handle_document(ctx)


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
