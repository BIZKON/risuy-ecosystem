"""Слой доступа к Postgres через asyncpg. Простой пул + функции по шагам воронки."""
import contextvars
import json
import logging
import uuid

import asyncpg

import config

pool: asyncpg.Pool | None = None

# Разрешённые колонки касаний — защита от подстановки имени колонки в SQL.
_FOLLOWUP_COLS = {"follow_up_1_at", "follow_up_2_at", "follow_up_3_at"}

# ── Тенант-контекст (Wave 3, ТЗ §5.4) ─────────────────────────────────────────
# Каждая вставка в tenant-scoped таблицу пишет tenant_id ЯВНО (DEFAULT снимается
# миграцией Wave 3). Мультиплекс выставляет current_tenant_id per-polling-таска;
# главный env-бот (Школа) живёт без контекста — фолбэк _default_tenant_id
# (резолв по slug при старте; до резолва вставки шли бы с NULL — поэтому init()
# не поднимает бота, пока тенант не найден).
current_tenant_id: contextvars.ContextVar[uuid.UUID | None] = contextvars.ContextVar(
    "current_tenant_id", default=None
)
_default_tenant_id: uuid.UUID | None = None


def tenant_id() -> uuid.UUID | None:
    """Активный тенант: контекст мультиплекс-таски, иначе env-тенант (Школа)."""
    return current_tenant_id.get() or _default_tenant_id


def default_tenant_id() -> uuid.UUID | None:
    """Тенант env-бота (Школа) — мультиплекс исключает его из своего реестра."""
    return _default_tenant_id


async def init() -> None:
    global pool, _default_tenant_id
    # max_size=10 (§5.4 плана): воркеры рассылки/outbox + polling-хендлеры воронки
    # делят один пул; при 5 voronka голодала. Соединение НИКОГДА не держится через
    # await send — claim/запись результата идут отдельными короткими транзакциями.
    pool = await asyncpg.create_pool(config.DATABASE_URL, min_size=1, max_size=10)
    _default_tenant_id = await pool.fetchval(
        "select id from tenants where slug = $1", config.DEFAULT_TENANT_SLUG
    )
    if _default_tenant_id is None:
        # Без тенанта вставки невозможны (tenant_id NOT NULL) — падаем громко на
        # старте, а не молча теряем лидов посреди воронки.
        raise RuntimeError(
            f"Тенант по умолчанию '{config.DEFAULT_TENANT_SLUG}' не найден в tenants"
        )


async def close() -> None:
    if pool:
        await pool.close()


# ── Слой C: идентичность лида по каналу (гибрид — колонка на канал + helper) ──────────
# Колонка идентичности лида в зависимости от мессенджера. messenger — из НАШЕГО белого списка
# (драйверы каналов), не из пользовательского ввода → безопасная f-string-интерполяция в SQL.
# Telegram-путь не меняется (messenger='tg' → tg_user_id, как было).
_CHANNEL_USER_COL = {"tg": "tg_user_id", "max": "max_user_id", "vk": "vk_user_id", "web": "web_session_id"}

# Колонка АДРЕСА ОТВЕТА (куда слать исходящее) по каналу. Для tg/vk совпадает с идентичностью
# (peer_id == user_id в личке VK); для MAX — отдельный max_chat_id (recipient.chat_id ≠ user_id
# в личке, персистится при входящем). C3: исходящая доставка адресуется этой колонкой.
# ⚠️ ЗЕРКАЛО в admin-panel/db.py::_BROADCAST_REPLY_COL (панель — отдельный процесс, импорт невозможен).
# Расходится → панель материализует/предпросматривает не тот адрес. Меняешь карту — синхронно там.
_CHANNEL_REPLY_COL = {"tg": "tg_user_id", "max": "max_chat_id", "vk": "vk_user_id"}


# Фолбэк на tg_user_id для неизвестного messenger — намеренный (TG как дефолт-канал), НО он
# молчаливый: новый канал, забытый в карте, тихо писал бы/читал TG-колонку → кросс-канальный
# мисроут. Поэтому логируем неизвестный messenger (валидный ввод today — только tg/vk/max от
# драйверов; строгая валидация значения messenger — на границе панели, _BROADCAST_MESSENGER_SET).
def _resolve_channel_col(table_map: dict, messenger: str) -> str:
    col = table_map.get(messenger)
    if col is None:
        logging.getLogger(__name__).warning(
            "db: неизвестный messenger=%r → фолбэк tg_user_id (риск кросс-канального мисроута)", messenger)
        return "tg_user_id"
    return col


def _user_col(messenger: str) -> str:
    return _resolve_channel_col(_CHANNEL_USER_COL, messenger)


def _reply_addr_col(messenger: str) -> str:
    return _resolve_channel_col(_CHANNEL_REPLY_COL, messenger)


async def upsert_start(tg_user_id: int, source: str, *, messenger: str = "tg") -> None:
    """Создаёт лид при /start (любого канала). При повторном /start источник не перетираем
    (first-touch). tg_user_id — внешний id пользователя В ЭТОМ канале (для tg — Telegram id,
    для vk — from_id, для max — user_id); кладётся в колонку канала."""
    col = _user_col(messenger)
    async with pool.acquire() as c:
        await c.execute(
            f"insert into leads ({col}, messenger, source, status, tenant_id) "
            f"values ($1, $2, $3, 'new', $4) "
            f"on conflict (tenant_id, {col}) do update set updated_at = now()",
            tg_user_id, messenger, source, tenant_id(),
        )


async def set_consent(tg_user_id: int, value: bool) -> None:
    async with pool.acquire() as c:
        await c.execute(
            "update leads set consent = $2 where tg_user_id = $1 and tenant_id = $3",
            tg_user_id, value, tenant_id(),
        )


async def set_name(tg_user_id: int, name: str) -> None:
    async with pool.acquire() as c:
        await c.execute(
            "update leads set name = $2 where tg_user_id = $1 and tenant_id = $3",
            tg_user_id, name, tenant_id(),
        )


async def set_phone(tg_user_id: int, phone: str, phone_hash: str) -> None:
    async with pool.acquire() as c:
        await c.execute(
            "update leads set phone = $2, phone_hash = $3 "
            "where tg_user_id = $1 and tenant_id = $4",
            tg_user_id, phone, phone_hash, tenant_id(),
        )


async def set_subscribed(tg_user_id: int, value: bool) -> None:
    async with pool.acquire() as c:
        await c.execute(
            "update leads set subscribed = $2 where tg_user_id = $1 and tenant_id = $3",
            tg_user_id, value, tenant_id(),
        )


async def mark_guide_sent(tg_user_id: int) -> None:
    """Фиксируем выдачу гайда один раз (guide_sent_at не перетираем при повторе)."""
    async with pool.acquire() as c:
        await c.execute(
            """
            update leads
            set status = 'guide_sent', guide_sent_at = coalesce(guide_sent_at, now())
            where tg_user_id = $1 and tenant_id = $2
            """,
            tg_user_id, tenant_id(),
        )


async def get_due_followups(col: str, delay_seconds: int) -> list[int]:
    """tg_user_id лидов, которым пора отправить касание col (ещё не отправляли)."""
    assert col in _FOLLOWUP_COLS
    # +2 фильтра (§4 плана): прогрев = маркетинг, подавляется отпиской и ручным
    # перехватом. Фильтр на ЧТЕНИИ — касание не помечается отправленным, resume бесплатный.
    # tenant-скоуп ($2): прогрев — Школин воркер (tenant_id() = тенант по умолчанию), не
    # должен задеть лидов других тенантов (у тенант-ботов своей воронки пока нет, item B).
    q = f"""
        select tg_user_id from leads
        where messenger = 'tg'
          and tenant_id = $2
          and tg_user_id is not null
          and guide_sent_at is not null
          and {col} is null
          and unsubscribed_at is null
          and bot_paused = false
          and guide_sent_at + make_interval(secs => $1) <= now()
        limit 100
    """
    async with pool.acquire() as c:
        rows = await c.fetch(q, float(delay_seconds), tenant_id())
    return [r["tg_user_id"] for r in rows]


async def mark_followup_sent(col: str, tg_user_id: int) -> None:
    assert col in _FOLLOWUP_COLS
    q = (f"update leads set {col} = now(), status = 'nurturing' "
         "where tg_user_id = $1 and tenant_id = $2")
    async with pool.acquire() as c:
        await c.execute(q, tg_user_id, tenant_id())


# ── Item B: tenant-aware дожим (мульти-тенант, отдельно от School-пути выше) ──────
# School-дожим (get_due_followups/mark_followup_sent) НЕ трогаем: его якорь — guide_sent_at
# (выдача лид-магнита), тексты/задержки захардкожены. Здесь — per-tenant дожим: конфиг в
# tenant_settings, якорь — ВРЕМЯ ПОСЛЕДНЕГО ВХОДЯЩЕГО лида (молчит → касание; ответил → серия
# перезапускается, т.к. касание «протухает» относительно нового входящего). Без DDL: время
# последнего входящего считаем из messages, факт отправки храним в тех же follow_up_1..3_at.
async def get_tenant_nurture(tid) -> dict:
    """Конфиг дожима тенанта из tenant_settings. Возвращает {"enabled": bool, "steps": [...]},
    steps = [{"delay_seconds": int, "text": str}, ...] (до 3 — по числу колонок follow_up_1..3_at).
    Бот=owner, RLS обходит. Сбой/нет строк/выключено/пустые шаги → {"enabled": False, "steps": []}."""
    out = {"enabled": False, "steps": []}
    if tid is None:
        return out
    try:
        async with pool.acquire() as c:
            rows = await c.fetch(
                "select key, value from tenant_settings where tenant_id = $1 and key = any($2::text[])",
                tid, ["nurture_enabled", "nurture_steps"])
    except Exception:  # noqa: BLE001 — дожиг не должен падать из-за БД
        logging.getLogger(__name__).warning("Не прочитал конфиг дожима тенанта %s", tid, exc_info=True)
        return out
    kv = {r["key"]: (r["value"] or "") for r in rows}
    if not (kv.get("nurture_enabled") or "").strip():
        return out
    try:
        raw = json.loads(kv.get("nurture_steps") or "[]")
    except Exception:  # noqa: BLE001 — битый JSON → дожим выключен (не угадываем)
        return out
    steps = []
    for s in (raw if isinstance(raw, list) else [])[:3]:
        if not isinstance(s, dict):
            continue
        try:
            d = int(s.get("delay_seconds") or 0)
        except (TypeError, ValueError):
            continue
        t = (s.get("text") or "").strip()
        if d > 0 and t:
            steps.append({"delay_seconds": d, "text": t})
    out["enabled"] = bool(steps)
    out["steps"] = steps
    return out


async def get_due_tenant_followups(
    tid, col: str, delay_seconds: int, prev_col: str | None = None,
) -> list[int]:
    """tg_user_id лидов тенанта tid, кому пора касание col дожима (только TG; vk/max — следующий
    инкремент). last_in = max(created_at) входящих лида (молчит → дожим; ответил → касание col
    «протухает» относит. нового входящего → серия перезапускается).

    КАСАНИЯ — ЦЕПОЧКА (защита от залпа всех шагов в один тик и от обратного порядка при нелогичных
    задержках, ревью): шаг 1 (prev_col=None) якорится на last_in; шаг N>1 якорится на ВРЕМЕНИ
    ПРЕДЫДУЩЕГО касания (prev_col) — кумулятивная пауza delay ПОСЛЕ него, и предыдущее касание
    обязано быть сделано ДЛЯ ТЕКУЩЕЙ активности (prev_col >= last_in). Так за тик уходит максимум
    одно касание лиду (только что проставленный prev_col + delay > now → следующий шаг ждёт тик).

    Стоп: отписка / ручная пауза / эскалация (передан менеджеру) / конверсия."""
    assert col in _FOLLOWUP_COLS
    assert prev_col is None or prev_col in _FOLLOWUP_COLS
    if prev_col is None:
        anchor_gate = "x.last_in + make_interval(secs => $1) <= now()"
    else:
        anchor_gate = (
            f"l.{prev_col} is not null and l.{prev_col} >= x.last_in "
            f"and l.{prev_col} + make_interval(secs => $1) <= now()"
        )
    q = f"""
        select l.tg_user_id
        from leads l
        join lateral (
            select max(m.created_at) as last_in
            from messages m
            where m.lead_id = l.id and m.direction = 'in'
        ) x on true
        where l.tenant_id = $2
          and l.messenger = 'tg'
          and l.tg_user_id is not null
          and l.unsubscribed_at is null
          and l.bot_paused = false
          and l.escalated_at is null
          and l.status <> 'converted'
          and x.last_in is not null
          and (l.{col} is null or l.{col} < x.last_in)
          and {anchor_gate}
        limit 100
    """
    async with pool.acquire() as c:
        rows = await c.fetch(q, float(delay_seconds), tid)
    return [r["tg_user_id"] for r in rows]


async def mark_tenant_followup_sent(tid, col: str, tg_user_id: int) -> None:
    """Помечаем касание дожима отправленным (status → nurturing, кроме уже converted)."""
    assert col in _FOLLOWUP_COLS
    q = (f"update leads set {col} = now(), "
         "status = case when status = 'converted' then status else 'nurturing' end "
         "where tg_user_id = $1 and tenant_id = $2 and messenger = 'tg'")
    async with pool.acquire() as c:
        await c.execute(q, tg_user_id, tid)


# ─────────────────────────────────────────────────────────────────────────────
# РАСШИРЕНИЕ: перехват / переписка / outbox-дренаж / рассылки / трекинг / retention.
# Всё под owner-ролью бота. Панель (panel_rw) сюда не ходит — она лишь кладёт задачи
# (outbox/broadcasts/link_tokens) и читает результаты. Источник истины «кому слать» и
# все фактические записи (messages / материализация / статусы / клики) — здесь, в боте.
# ─────────────────────────────────────────────────────────────────────────────

# Допустимые kind для messages/outbox — держим в синхроне с CHECK в schema_panel_ext.sql.
_MSG_KINDS = {
    "text", "photo", "document", "video", "voice",
    "video_note", "audio", "animation", "sticker", "other",
}


# ── Канал лида (для «ИИ-сотрудника на канал») ────────────────────────────────
async def get_lead_source(tg_user_id: int) -> str | None:
    """source лида (метка площадки first-touch) для выбора per-канального ИИ-сотрудника.
    Нет лида/сбой → None (фолбэк на глобальные настройки ИИ — Лия не молчит из-за БД)."""
    try:
        async with pool.acquire() as c:
            return await c.fetchval(
                "select source from leads where tg_user_id = $1 and tenant_id = $2",
                tg_user_id, tenant_id(),
            )
    except Exception as e:  # noqa: BLE001 — выбор персоны не должен ломать авто-ответ
        logging.getLogger(__name__).warning("Не удалось прочитать source лида: %s", e)
        return None


async def get_lead_persona(tg_user_id: int) -> str | None:
    """slug «ИИ-сотрудника», выбранного оператором на ЭТОТ диалог (leads.ai_persona).
    Перекрывает канал/глобал. Нет/сбой → None (наследуется канал/глобал)."""
    try:
        async with pool.acquire() as c:
            return await c.fetchval(
                "select ai_persona from leads where tg_user_id = $1 and tenant_id = $2",
                tg_user_id, tenant_id(),
            )
    except Exception as e:  # noqa: BLE001 — выбор персоны не должен ломать авто-ответ
        logging.getLogger(__name__).warning("Не удалось прочитать ai_persona лида: %s", e)
        return None


# ── A3: эскалация лида менеджерам (дедуп — одна карточка на лид) ───────────────
async def claim_lead_escalation(tg_user_id: int, *, messenger: str = "tg") -> bool:
    """Атомарно «застолбить» эскалацию лида: escalated_at NULL → now(). True — застолбили
    сейчас (первая эскалация), False — уже эскалирован / нет лида / сбой. tenant-scoped.
    Дедуп от гонки двух подряд сообщений: WHERE escalated_at is null делает claim атомарным.
    tg_user_id — внешний id лида в канале messenger."""
    col = _user_col(messenger)
    try:
        async with pool.acquire() as c:
            res = await c.execute(
                f"update leads set escalated_at = now() "
                f"where {col} = $1 and tenant_id = $2 and escalated_at is null",
                tg_user_id, tenant_id(),
            )
        return res.endswith(" 1")
    except Exception as e:  # noqa: BLE001 — эскалация не должна ломать авто-ответ
        logging.getLogger(__name__).warning("claim_lead_escalation: %s", e)
        return False


async def release_lead_escalation(tg_user_id: int, *, messenger: str = "tg") -> None:
    """Откатить claim (escalated_at → NULL), если карточка менеджерам НЕ ушла (сбой отправки) —
    чтобы следующее квалифицирующее сообщение попробовало снова, а лид не «потерялся»."""
    col = _user_col(messenger)
    try:
        async with pool.acquire() as c:
            await c.execute(
                f"update leads set escalated_at = null where {col} = $1 and tenant_id = $2",
                tg_user_id, tenant_id(),
            )
    except Exception as e:  # noqa: BLE001
        logging.getLogger(__name__).warning("release_lead_escalation: %s", e)


async def get_lead_id(tg_user_id: int, *, messenger: str = "tg") -> str | None:
    """id лида (uuid) по внешнему id канала — для ссылки «диалог в панели» в карточке эскалации.
    None — нет лида/сбой (тогда панель-ссылку не кладём, эскалация не падает)."""
    col = _user_col(messenger)
    try:
        async with pool.acquire() as c:
            v = await c.fetchval(
                f"select id from leads where {col} = $1 and tenant_id = $2",
                tg_user_id, tenant_id())
        return str(v) if v else None
    except Exception as e:  # noqa: BLE001
        logging.getLogger(__name__).warning("get_lead_id: %s", e)
        return None


# ── Перехват (bot_paused) ────────────────────────────────────────────────────
async def is_bot_paused(tg_user_id: int, *, messenger: str = "tg") -> bool:
    """True, если оператор взял ручное управление этим лидом. Нет строки → False."""
    col = _user_col(messenger)
    async with pool.acquire() as c:
        val = await c.fetchval(
            f"select coalesce(bot_paused, false) from leads "
            f"where {col} = $1 and tenant_id = $2",
            tg_user_id, tenant_id(),
        )
    return bool(val)


# ── Отписка (152-ФЗ) ─────────────────────────────────────────────────────────
async def set_unsubscribed(external_id: int, messenger: str = "tg") -> None:
    """Идемпотентная отписка: первый момент фиксируем, повторный /stop не перетирает.
    messenger — канал (C3: vk/max отписываются ключевым словом «стоп»). external_id — id лида
    В КАНАЛЕ (tg→tg_user_id, vk→vk_user_id, max→max_user_id)."""
    col = _user_col(messenger)
    async with pool.acquire() as c:
        await c.execute(
            f"update leads set unsubscribed_at = coalesce(unsubscribed_at, now()) "
            f"where {col} = $1 and tenant_id = $2",
            external_id, tenant_id(),
        )


# ── Переписка (messages): резолв lead_id + лог входящих/исходящих ─────────────
async def resolve_lead_id(tg_user_id: int) -> str | None:
    """uuid лида по tg_user_id (может ещё не существовать → None). Мягко, без исключений."""
    async with pool.acquire() as c:
        return await c.fetchval(
            "select id from leads where tg_user_id = $1 and tenant_id = $2", tg_user_id, tenant_id()
        )


async def log_message(
    *,
    tg_user_id: int,
    messenger: str = "tg",
    direction: str,
    kind: str = "text",
    text: str | None = None,
    file_id: str | None = None,
    source: str | None = None,
    tg_message_id: int | None = None,
    lead_id: str | None = None,
) -> None:
    """Пишет одну строку в messages. lead_id мягко резолвится по внешнему id канала, если не
    передан. tg_user_id — внешний id лида В КАНАЛЕ messenger; в messages.tg_user_id кладём его
    ТОЛЬКО для tg (для vk/max колонка NULL — адрес лида = lead_id; messenger хранит канал;
    tg_message_id хранит нативный id сообщения канала). НИКОГДА не бросает наружу — лог переписки
    не должен ронять воронку/Лию/рассылку. Вызывается из middleware (входящие) и messaging."""
    if kind not in _MSG_KINDS:
        kind = "other"
    col = _user_col(messenger)
    try:
        async with pool.acquire() as c:
            if lead_id is None:
                lead_id = await c.fetchval(
                    f"select id from leads where {col} = $1 and tenant_id = $2", tg_user_id, tenant_id()
                )
            await c.execute(
                """
                insert into messages
                    (lead_id, tg_user_id, tg_message_id, direction, kind, text, file_id,
                     source, tenant_id, messenger)
                values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                """,
                lead_id, (tg_user_id if messenger == "tg" else None), tg_message_id,
                direction, kind, text, file_id, source, tenant_id(), messenger,
            )
    except Exception:  # noqa: BLE001 — изоляция: переписка-лог не критична к доставке
        logging.getLogger(__name__).warning(
            "log_message не записан (direction=%s kind=%s tg=%s)",
            direction, kind, tg_user_id, exc_info=True,
        )


# ── Дренаж OUTBOX (точечные ответы оператора) ────────────────────────────────
async def claim_outbox(limit: int) -> list[dict]:
    """Короткая tx: помечает queued→sending, инкремент attempts, ставит claimed_at, commit.

    Соединение возвращается в пул ДО отправки (send идёт без открытой транзакции, §5.4).
    SKIP LOCKED исключает гонку нескольких воркеров/инстансов в пределах одного claim.
    """
    async with pool.acquire() as c:
        rows = await c.fetch(
            """
            update outbox set status = 'sending', attempts = attempts + 1, claimed_at = now()
            where id in (
                select id from outbox
                where status = 'queued' and messenger = 'tg'
                order by id
                limit $1
                for update skip locked
            )
            returning id, lead_id, tg_user_id, kind, text, file_id, attempts, created_at
            """,
            limit,
        )
    return [dict(r) for r in rows]


async def claim_outbox_channels(limit: int) -> list[dict]:
    """C3: дренаж outbox для НЕ-tg каналов (vk/max) — отдельная очередь (TG-путь не трогаем).
    Резолвит адрес ответа из leads (vk→vk_user_id, max→max_chat_id) и erase-флаг в одной tx.
    file_bytes для медиа отдаём как есть (для vk/max шлём байты напрямую, без OPS_CHAT-стейджинга).
    Возвращает строки с messenger/reply_address/tenant_id + байты вложения."""
    async with pool.acquire() as c:
        rows = await c.fetch(
            """
            with claimed as (
                update outbox set status = 'sending', attempts = attempts + 1, claimed_at = now()
                where id in (
                    select id from outbox
                    where status = 'queued' and messenger <> 'tg'
                    order by id
                    limit $1
                    for update skip locked
                )
                returning id, lead_id, messenger, kind, text, file_bytes, file_name, file_mime,
                          attempts, created_at, tenant_id
            )
            select c.*,
                   case c.messenger when 'vk' then l.vk_user_id
                                    when 'max' then l.max_chat_id end as reply_address,
                   l.erase_requested_at
            from claimed c
            join leads l on l.id = c.lead_id
            """,
            limit,
        )
    return [dict(r) for r in rows]


async def note_max_chat_id(max_user_id: int, chat_id: int) -> None:
    """C3: персистит адрес ответа MAX (recipient.chat_id ≠ user_id в личке) на лиде тенанта.
    Пишется при ВХОДЯЩЕМ (multiplex). Без него панель/воркер не смогли бы ответить в личку MAX.
    НЕ бросает — приём сообщения важнее. tenant_id из контекста таски канала."""
    if not chat_id:
        return
    try:
        async with pool.acquire() as c:
            await c.execute(
                "update leads set max_chat_id = $2 where max_user_id = $1 and tenant_id = $3",
                max_user_id, chat_id, tenant_id(),
            )
    except Exception:  # noqa: BLE001 — адрес ответа не критичен к приёму входящего
        logging.getLogger(__name__).warning("note_max_chat_id не записан (user=%s)", max_user_id)


async def outbox_recheck_address(tg_user_id: int) -> str | None:
    """Re-SELECT перед send: причина пропуска или None если слать можно.

    'no_address' — нет tg_user_id (теоретически); 'erased' — отозвал согласие на ПДн.
    consent для ответа на входящее НЕ требуем (клиент сам написал). §5.10.
    """
    async with pool.acquire() as c:
        row = await c.fetchrow(
            "select tg_user_id, erase_requested_at from leads "
            "where tg_user_id = $1 and tenant_id = $2",
            tg_user_id, tenant_id(),
        )
    if row is None or row["tg_user_id"] is None:
        return "no_address"
    if row["erase_requested_at"] is not None:
        return "erased"
    return None


async def mark_outbox_sent(item_id: int) -> None:
    async with pool.acquire() as c:
        await c.execute(
            "update outbox set status = 'sent', sent_at = now(), last_error = null where id = $1",
            item_id,
        )


async def mark_outbox_failed(item_id: int, error: str) -> None:
    async with pool.acquire() as c:
        await c.execute(
            "update outbox set status = 'failed', last_error = $2 where id = $1",
            item_id, error[:500],
        )


async def release_outbox(item_id: int, error: str, max_attempts: int, max_age_hours: int) -> None:
    """Транзиентная ошибка: вернуть в queued, НО с потолком — иначе вечный pending (§5.10).

    Потолок по attempts ИЛИ по возрасту created_at → переводим в failed.
    """
    async with pool.acquire() as c:
        await c.execute(
            """
            update outbox set
                status = case
                    when attempts >= $2 or created_at < now() - make_interval(hours => $3)
                    then 'failed' else 'queued' end,
                last_error = $4
            where id = $1
            """,
            item_id, max_attempts, max_age_hours, error[:500],
        )


async def reclaim_stuck_outbox(after_seconds: int) -> int:
    """Возврат застрявших 'sending' (краш/редеплой) в 'queued'. Возвращает число строк."""
    async with pool.acquire() as c:
        res = await c.execute(
            """
            update outbox set status = 'queued'
            where status = 'sending' and claimed_at < now() - make_interval(secs => $1)
            """,
            float(after_seconds),
        )
    return _affected(res)


# ── РАССЫЛКИ: подхват заявок, материализация, claim, статусы ─────────────────
# Жёсткий, неотменяемый фильтр «кому МОЖНО писать» (§5.1). Применяется И при
# материализации, И повторно перед КАЖДЫМ send. Бот не доверяет панели.
def _audience_where(messenger: str = "tg") -> str:
    """Неотменяемый WHERE «кому можно слать» рассылку для КАНАЛА (§5.2). messenger — из белого
    списка (валидируется панелью/драйверами), не пользовательский ввод → безопасная f-string.
    Адрес-колонка по каналу: tg→tg_user_id, vk→vk_user_id, max→max_chat_id (адрес ответа).
    ⚠️ ДОЛЖНО побайтово совпадать с admin-panel/db.py::_broadcast_audience_where(messenger)."""
    addr = _reply_addr_col(messenger)
    return (f"messenger = '{messenger}' and {addr} is not null and consent = true "
            "and unsubscribed_at is null and erase_requested_at is null and bot_paused = false")


# tg-вариант как константа (обратная совместимость; для tg строка идентична прежней).
_AUDIENCE_WHERE = _audience_where("tg")


async def claim_broadcast_to_send() -> dict | None:
    """Атомарно берёт ОДНУ рассылку из 'queued' с подтверждённым recipient_count в работу.

    queued→sending под FOR UPDATE SKIP LOCKED: только один инстанс материализует.
    recipient_count проставляет панель ДО старта (§7.1 п.6) — если null, не берём
    (полу-записанная заявка). Возврат строки рассылки или None.
    """
    async with pool.acquire() as c:
        async with c.transaction():
            row = await c.fetchrow(
                """
                select id, title, messenger, kind, body_template, recipient_count, product_id,
                       tenant_id
                from broadcasts
                where status = 'queued' and recipient_count is not null
                  and tenant_id = $1
                order by id
                limit 1
                for update skip locked
                """,
                tenant_id(),  # воркер берёт рассылки ТОЛЬКО своего тенанта (бот=owner, RLS обходит)
            )
            if row is None:
                return None
            await c.execute(
                "update broadcasts set status = 'sending', started_at = coalesce(started_at, now()) "
                "where id = $1",
                row["id"],
            )
            return dict(row)


async def materialize_recipients(broadcast_id: int) -> int:
    """INSERT…SELECT получателей по неотменяемому WHERE (§5.2). Идемпотентно (on conflict).

    Детерминированный snapshot до первой отправки. Возвращает число строк в очереди
    получателей (после вставки). per-recipient click_token генерится позже, при первом
    использовании трекинг-ссылки (см. ensure_click_token) — здесь оставляем null.
    """
    # tenant-скоуп: адресаты — ТОЛЬКО лиды тенанта рассылки (бот=owner → RLS на leads его
    # не ограничивает; без этого фильтра рассылка одного клиента ушла бы лидам всех тенантов).
    # C3: канал рассылки задаёт колонку адреса и WHERE. tg-путь без изменений (адрес=tg_user_id);
    # vk/max — денорм messenger + reply_address (vk_user_id / max_chat_id).
    async with pool.acquire() as c:
        messenger = await c.fetchval(
            "select messenger from broadcasts where id = $1", broadcast_id) or "tg"
        where = _audience_where(messenger)
        if messenger == "tg":
            q = f"""
                insert into broadcast_recipients (broadcast_id, lead_id, tg_user_id, tenant_id)
                select $1, id, tg_user_id,
                       (select tenant_id from broadcasts where id = $1)
                from leads
                where {where}
                  and tenant_id = (select tenant_id from broadcasts where id = $1)
                on conflict (broadcast_id, lead_id) do nothing
            """
        else:
            addr = _reply_addr_col(messenger)
            q = f"""
                insert into broadcast_recipients
                    (broadcast_id, lead_id, reply_address, messenger, tenant_id)
                select $1, id, {addr}, '{messenger}',
                       (select tenant_id from broadcasts where id = $1)
                from leads
                where {where}
                  and tenant_id = (select tenant_id from broadcasts where id = $1)
                on conflict (broadcast_id, lead_id) do nothing
            """
        await c.execute(q, broadcast_id)
        cnt = await c.fetchval(
            "select count(*) from broadcast_recipients where broadcast_id = $1", broadcast_id
        )
    return int(cnt)


async def set_broadcast_recipient_count(broadcast_id: int, count: int) -> None:
    async with pool.acquire() as c:
        await c.execute(
            "update broadcasts set recipient_count = $2 where id = $1", broadcast_id, count
        )


async def claim_broadcast_recipients(broadcast_id: int, limit: int) -> list[dict]:
    """Короткая tx: pending→sending батчем, инкремент attempts, claimed_at, commit.

    Соединение в пул ДО отправки. SKIP LOCKED — изоляция в пределах инстанса.
    """
    async with pool.acquire() as c:
        rows = await c.fetch(
            """
            update broadcast_recipients
            set status = 'sending', attempts = attempts + 1, claimed_at = now()
            where id in (
                select id from broadcast_recipients
                where broadcast_id = $1 and status = 'pending'
                order by id
                limit $2
                for update skip locked
            )
            returning id, lead_id, tg_user_id, reply_address, messenger, click_token, attempts
            """,
            broadcast_id, limit,
        )
    return [dict(r) for r in rows]


async def recipient_recheck(lead_id: str, messenger: str = "tg") -> bool:
    """TOCTOU re-check перед КАЖДЫМ send (§5.1): все 4+1 условия ещё держатся?

    True = слать можно. False = отписался/erase/consent отозван/перехват → skipped.
    messenger — канал рассылки (для проверки адреса нужного канала; tg по умолчанию).
    """
    q = f"select 1 from leads where id = $1 and {_audience_where(messenger)}"
    async with pool.acquire() as c:
        return await c.fetchval(q, lead_id) is not None


async def ensure_click_token(recipient_id: int, broadcast_id: int, lead_id: str,
                             target_url: str) -> str:
    """Лениво создаёт per-recipient click_token и регистрирует его в link_tokens.

    Вызывается воркером в момент отправки, только если body_template несёт {link}.
    target_url — единая трекинг-ссылка рассылки (зарегистрирована панелью отдельной
    строкой link_tokens без click_token; здесь делаем per-recipient строку).
    Идемпотентно: если токен уже есть на получателе — возвращаем его.
    """
    import secrets
    async with pool.acquire() as c:
        existing = await c.fetchval(
            "select click_token from broadcast_recipients where id = $1", recipient_id
        )
        if existing:
            return existing
        token = secrets.token_urlsafe(16)
        async with c.transaction():
            await c.execute(
                "insert into link_tokens (token, target_url, broadcast_id, lead_id, tenant_id) "
                "values ($1, $2, $3, $4, (select tenant_id from broadcasts where id = $3)) "
                "on conflict (token) do nothing",
                token, target_url, broadcast_id, lead_id,
            )
            await c.execute(
                "update broadcast_recipients set click_token = $2 where id = $1",
                recipient_id, token,
            )
    return token


async def mark_recipient_sent(recipient_id: int) -> None:
    async with pool.acquire() as c:
        await c.execute(
            "update broadcast_recipients set status = 'sent', sent_at = now(), error = null "
            "where id = $1",
            recipient_id,
        )


async def mark_recipient_failed(recipient_id: int, error: str) -> None:
    async with pool.acquire() as c:
        await c.execute(
            "update broadcast_recipients set status = 'failed', error = $2 where id = $1",
            recipient_id, error[:500],
        )


async def mark_recipient_skipped(recipient_id: int, reason: str) -> None:
    async with pool.acquire() as c:
        await c.execute(
            "update broadcast_recipients set status = 'skipped', error = $2 where id = $1",
            recipient_id, reason[:500],
        )


async def release_recipient(recipient_id: int, error: str, max_attempts: int) -> None:
    """Транзиентная ошибка: вернуть в pending, потолок attempts → failed."""
    async with pool.acquire() as c:
        await c.execute(
            """
            update broadcast_recipients
            set status = case when attempts >= $2 then 'failed' else 'pending' end,
                error = $3
            where id = $1
            """,
            recipient_id, max_attempts, error[:500],
        )


async def reclaim_stuck_recipients(after_seconds: int) -> int:
    """Возврат застрявших 'sending' получателей в 'pending' (краш/редеплой). §5.5."""
    async with pool.acquire() as c:
        res = await c.execute(
            """
            update broadcast_recipients set status = 'pending'
            where status = 'sending' and claimed_at < now() - make_interval(secs => $1)
            """,
            float(after_seconds),
        )
    return _affected(res)


async def broadcast_counts(broadcast_id: int) -> dict:
    """Сводка по получателям: {pending,sending,sent,failed,skipped,total}."""
    async with pool.acquire() as c:
        rows = await c.fetch(
            "select status, count(*) n from broadcast_recipients "
            "where broadcast_id = $1 group by status",
            broadcast_id,
        )
    out = {"pending": 0, "sending": 0, "sent": 0, "failed": 0, "skipped": 0}
    for r in rows:
        out[r["status"]] = r["n"]
    out["total"] = sum(out.values())
    return out


async def get_broadcast_status(broadcast_id: int) -> str | None:
    async with pool.acquire() as c:
        return await c.fetchval("select status from broadcasts where id = $1", broadcast_id)


async def pause_broadcast(broadcast_id: int) -> None:
    """Стоп-кран: sending→paused. Воркер доедает claimed-батч и больше не берёт pending."""
    async with pool.acquire() as c:
        await c.execute(
            "update broadcasts set status = 'paused' where id = $1 and status = 'sending'",
            broadcast_id,
        )


async def finalize_broadcast(broadcast_id: int, totals: dict) -> None:
    """sending→done + итоги. Только если не осталось pending/sending (вызывает воркер)."""
    import json
    async with pool.acquire() as c:
        await c.execute(
            "update broadcasts set status = 'done', finished_at = now(), totals = $2::jsonb "
            "where id = $1 and status = 'sending'",
            broadcast_id, json.dumps(totals),
        )


async def update_broadcast_totals(broadcast_id: int, totals: dict) -> None:
    import json
    async with pool.acquire() as c:
        await c.execute(
            "update broadcasts set totals = $2::jsonb where id = $1",
            broadcast_id, json.dumps(totals),
        )


# ── Файл рассылки: заливка в служебный чат (§5.6) ────────────────────────────
async def get_broadcast_file(broadcast_id: int) -> dict | None:
    async with pool.acquire() as c:
        row = await c.fetchrow(
            "select id, filename, mime, bytes, tg_file_id from broadcast_files "
            "where broadcast_id = $1 order by id limit 1",
            broadcast_id,
        )
    return dict(row) if row else None


async def set_broadcast_file_id(file_row_id: int, tg_file_id: str) -> None:
    """Проставить tg_file_id и ОБНУЛИТЬ bytes (ПДн-гигиена + место). §5.6/§6.5."""
    async with pool.acquire() as c:
        await c.execute(
            "update broadcast_files set tg_file_id = $2, bytes = null where id = $1",
            file_row_id, tg_file_id,
        )


# ── КАТАЛОГ ПРОДУКТОВ (оферов): заливка файла + выдача в рассылке/воронке ──────
# Объекты — db/schema_products.sql (products + broadcasts.product_id + app_settings).
# Инвариант границы доступа (тот же, что у broadcast_files): ПАНЕЛЬ под panel_rw кладёт
# офер и байты файла (products.file), но КОЛОНКУ file_tg_id и обнуление байтов пишет
# БОТ под owner-ролью после первой заливки в OPS_CHAT_ID. file_id переиспускается во
# всех рассылках/выдачах. Эти функции — read-офера + заливочный воркер + чтение
# singleton-настроек воронки; «кому слать» и материализацию они НЕ трогают.

async def get_product(product_id: int) -> dict | None:
    """Полный офер по id (для выдачи в рассылке/воронке). None если нет строки.

    file (bytea) НЕ селектим — он нужен только заливочному воркеру (см.
    list_products_pending_upload). Здесь — поля доставки: name/kind/price/currency/
    caption/link + file_tg_id/file_name/file_mime (по ним выводим тип отправки).
    """
    async with pool.acquire() as c:
        row = await c.fetchrow(
            """
            select id, name, kind, price, currency, caption, link,
                   file_tg_id, file_name, file_mime, status
            from products
            where id = $1
            """,
            product_id,
        )
    return dict(row) if row else None


async def list_sellable_products(limit: int = 20) -> list[dict]:
    """Активные продаваемые оферы ТЕКУЩЕГО тенанта (цена в рублях > 0) — для команды /shop
    тенант-бота (Слой C). tenant-scoped: бот — owner, фильтруем tenant_id() ЯВНО (анти-кросс-тенант).
    Пусто (нет тенанта/товаров) → []."""
    tid = tenant_id()
    if tid is None:
        return []
    async with pool.acquire() as c:
        rows = await c.fetch(
            "select id, name, price, currency from products "
            "where tenant_id = $1 and status = 'active' and price is not null and price > 0 "
            "and coalesce(currency, 'RUB') = 'RUB' order by price, id limit $2",
            tid, limit)
    return [dict(r) for r in rows]


async def get_sellable_product(product_id: int) -> dict | None:
    """Продукт по id ТОЛЬКО если он принадлежит ТЕКУЩЕМУ тенанту и продаваем (active, RUB, price>0).
    Защита от крафтнутого buy:<чужой_product_id> — get_product tenant НЕ фильтрует (бот owner).
    None — нет/чужой/непродаваемый."""
    tid = tenant_id()
    if tid is None:
        return None
    async with pool.acquire() as c:
        row = await c.fetchrow(
            "select id, name, price, currency, status from products where id = $1 and tenant_id = $2",
            product_id, tid)
    if (row is None or row["status"] != "active" or not row["price"] or row["price"] <= 0
            or (row["currency"] or "RUB") != "RUB"):
        return None
    return dict(row)


async def get_broadcast_product(broadcast_id: int) -> dict | None:
    """Офер, привязанный к рассылке (broadcasts.product_id → products), или None.

    Вызывает воркёр рассылок один раз на рассылку, если product_id задан. Архивные
    оферы (status='archived') тоже отдаём — рассылку мог запустить оператор, когда
    офер был активен; гейтит выбор активности панель при привязке, не доставка.
    """
    async with pool.acquire() as c:
        row = await c.fetchrow(
            """
            select p.id, p.name, p.kind, p.price, p.currency, p.caption, p.link,
                   p.file_tg_id, p.file_name, p.file_mime, p.status,
                   p.file is not null as has_file_bytes, p.upload_attempts
            from broadcasts b
            join products p on p.id = b.product_id
            where b.id = $1
            """,
            broadcast_id,
        )
    return dict(row) if row else None


async def list_products_pending_upload(limit: int, max_attempts: int) -> list[dict]:
    """Очередь заливки: продукты с байтами файла, но ещё без file_tg_id (§ schema).

    Покрыто частичным индексом products_pending_upload_idx. Кэп попыток (upload_attempts
    < max_attempts) накладываем здесь, а не в индексе — литерал-лимит захардкодил бы env.
    Битый/отвергаемый Telegram файл после N неудач выпадает из очереди (не зацикливает
    воркёр, не засоряет OPS_CHAT_ID). Возвращает байты для заливки в OPS_CHAT_ID; после
    успеха воркёр зовёт set_product_file_id (проставит file_tg_id + обнулит file). Логику
    «кому слать» не затрагивает — это про офер-файл.
    """
    async with pool.acquire() as c:
        rows = await c.fetch(
            """
            select id, name, file, file_name, file_mime, upload_attempts
            from products
            where file is not null and file_tg_id is null
              and upload_attempts < $2
            order by id
            limit $1
            """,
            limit, max_attempts,
        )
    return [dict(r) for r in rows]


async def bump_product_upload_attempt(product_id: int, error: str) -> None:
    """Инкремент products.upload_attempts + запись последней ошибки заливки (диагностика).

    Вызывается воркёром при неудачной заливке файла офера. По достижении лимита
    (см. list_products_pending_upload) офер перестаёт переселектироваться. Симметрично
    release_outbox/release_recipient, но без возврата в очередь — статус задаёт сам
    предикат очереди (file есть, file_tg_id null, attempts < лимит)."""
    async with pool.acquire() as c:
        await c.execute(
            "update products set upload_attempts = upload_attempts + 1, upload_error = $2 "
            "where id = $1",
            product_id, error[:500],
        )


async def set_product_file_id(product_id: int, tg_file_id: str) -> None:
    """Проставить products.file_tg_id и ОБНУЛИТЬ file (bytea) — однократность заливки
    и гигиена места, симметрично set_broadcast_file_id. Пишет БОТ (owner-роль).
    upload_error чистим (заливка удалась)."""
    async with pool.acquire() as c:
        await c.execute(
            "update products set file_tg_id = $2, file = null, upload_error = null where id = $1",
            product_id, tg_file_id,
        )


async def list_outbox_pending_upload(limit: int, max_attempts: int) -> list[dict]:
    """Очередь заливки: исходящие с байтами вложения, но ещё без file_id (§ schema).

    Покрыто частичным индексом outbox_pending_upload_idx. Кэп попыток (upload_attempts
    < max_attempts) накладываем здесь, а не в индексе — литерал-лимит захардкодил бы env.
    Битый/отвергаемый Telegram файл после N неудач выпадает из очереди (не зацикливает
    воркёр, не засоряет OPS_CHAT_ID). Возвращает байты для заливки в OPS_CHAT_ID; после
    успеха воркёр зовёт set_outbox_file_id (проставит file_id + обнулит file_bytes). Логику
    «кому слать» не затрагивает — это про вложение в личный ответ оператора лиду. kind
    берётся из строки (photo/document/voice/audio), НЕ из MIME (иначе voice → document).
    """
    async with pool.acquire() as c:
        rows = await c.fetch(
            """
            select id, kind, file_bytes, file_name, file_mime, upload_attempts
            from outbox
            where file_bytes is not null and file_id is null
              and messenger = 'tg'
              and upload_attempts < $2
            order by id
            limit $1
            """,
            limit, max_attempts,
        )
    return [dict(r) for r in rows]


async def bump_outbox_upload_attempt(outbox_id: int, error: str) -> None:
    """Инкремент outbox.upload_attempts + запись последней ошибки заливки (диагностика).

    Вызывается воркёром при неудачной заливке вложения личного ответа. По достижении лимита
    (см. list_outbox_pending_upload) исходящее перестаёт переселектироваться. Симметрично
    release_outbox/release_recipient, но без возврата в очередь — статус задаёт сам
    предикат очереди (file_bytes есть, file_id null, attempts < лимит)."""
    async with pool.acquire() as c:
        await c.execute(
            "update outbox set upload_attempts = upload_attempts + 1, upload_error = $2 "
            "where id = $1",
            outbox_id, error[:500],
        )


async def set_outbox_file_id(outbox_id: int, file_id: str, kind: str | None = None) -> None:
    """Проставить outbox.file_id и ОБНУЛИТЬ file_bytes (bytea) — однократность заливки
    и гигиена места, симметрично set_product_file_id. Пишет БОТ (owner-роль).
    upload_error чистим (заливка удалась).

    kind (опц.) ОБНОВЛЯЕМ, если фактический тип заливки разошёлся со строкой: при сбое
    ffmpeg воркёр деградирует voice→audio и заливает как audio (audio-file_id). Без правки
    kind строка осталась бы 'voice', и _drain_outbox послал бы send_voice с audio-file_id —
    Telegram отвергнет (file_id типобинден). Поэтому воркёр передаёт ФИНАЛЬНЫЙ kind."""
    async with pool.acquire() as c:
        if kind is None:
            await c.execute(
                "update outbox set file_id = $2, file_bytes = null, upload_error = null where id = $1",
                outbox_id, file_id,
            )
        else:
            await c.execute(
                "update outbox set file_id = $2, kind = $3, file_bytes = null, "
                "upload_error = null where id = $1",
                outbox_id, file_id, kind,
            )


# ── app_settings: singleton-настройки панели (бот ЧИТАЕТ) ─────────────────────
async def get_app_setting(key: str) -> str | None:
    """Значение singleton-настройки по ключу (или None). value — text (KV-универсальность)."""
    async with pool.acquire() as c:
        return await c.fetchval("select value from app_settings where key = $1", key)


async def get_effective_guide_url() -> str:
    """Эффективная ссылка-гайд для выдачи воронки: app_settings['guide_url'] (пишет
    панель, раздел «Интеграции») ПОВЕРХ env GUIDE_URL. Любой промах — пусто/мусор/не
    http(s)/сбой чтения — фолбэк на config.GUIDE_URL (env остаётся источником истины по
    умолчанию, как у лид-магнит-офера). Зеркалит запись панели (admin-panel/db.py::
    set_guide_url_with_audit, та же валидация). Читается В МОМЕНТ выдачи — правка ссылки
    в панели подхватывается без редеплоя; чтение изолировано → выдача гайда не падает из-за БД."""
    try:
        raw = await get_app_setting("guide_url")
    except Exception as e:  # noqa: BLE001 — сбой чтения настройки не должен ломать выдачу
        logging.getLogger(__name__).warning("Не удалось прочитать guide_url: %s", e)
        return config.GUIDE_URL
    url = (raw or "").strip()
    if url.startswith(("http://", "https://")) and not any(c.isspace() for c in url):
        return url
    return config.GUIDE_URL


async def is_online_payments_enabled() -> bool:
    """Тумблер онлайн-оплаты (пишет панель, «Интеграции»). Дефолт и любой сбой → ВЫКЛ:
    кнопка «Купить» не появляется, пока владелец явно не включил (консервативно —
    деплой кода безопасен до вписывания ключей и включения)."""
    try:
        raw = await get_app_setting("online_payments_enabled")
    except Exception as e:  # noqa: BLE001 — сбой чтения не должен ломать рассылку
        logging.getLogger(__name__).warning("Не удалось прочитать тумблер оплаты: %s", e)
        return False
    return bool((raw or "").strip())


async def get_active_lead_magnet_product() -> dict | None:
    """Офер-лид-магнит для ЗАМЕНЫ GUIDE_URL-заглушки в выдаче воронки, или None (фолбэк).

    Читает app_settings['active_lead_magnet_product_id'] (пишет панель), валидирует:
    значение приводится к bigint, продукт существует, kind='lead_magnet', status='active'
    и у него есть чем выдавать (file_tg_id ИЛИ link). Любой промах (пусто/мусор/архив/
    не лид-магнит/пустой офер) → None: handlers.py падает на текущую выдачу GUIDE_URL
    без изменений (env остаётся источником истины по умолчанию, решение владельца).
    Файл без file_tg_id (бот ещё не залил) → пока трактуем как «не готов» и тоже фолбэк,
    чтобы воронка не зависала на заливке; дозальётся воркером и подхватится со след. раза.
    """
    raw = await get_app_setting("active_lead_magnet_product_id")
    if not raw:
        return None
    try:
        product_id = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    async with pool.acquire() as c:
        row = await c.fetchrow(
            """
            select id, name, kind, price, currency, caption, link,
                   file_tg_id, file_name, file_mime, status
            from products
            where id = $1 and kind = 'lead_magnet' and status = 'active'
            """,
            product_id,
        )
    if row is None:
        return None
    prod = dict(row)
    # Выдавать нечем (файл ещё не залит И ссылки нет) → фолбэк на GUIDE_URL.
    if not prod.get("file_tg_id") and not prod.get("link"):
        return None
    return prod


# ── app_settings: настройки ИИ (бот ЧИТАЕТ; пишет панель, раздел «ИИ-агенты») ──
_AI_SETTING_KEYS = (
    "ai_enabled", "ai_backend", "ai_agent_id", "ai_model",
    "ai_gateway_base_url", "ai_system_prompt", "ai_fallback_text",
    "kb_enabled",  # RF-RAG: подмешивать справку из базы знаний (по умолчанию ВЫКЛ)
)
_AI_BACKENDS = ("cloud_ai", "gateway")


async def get_ai_overrides(source: str | None = None, persona: str | None = None) -> dict:
    """Настройки ИИ из app_settings ПОВЕРХ env (пишет панель). Одним запросом.
    Отсутствие строки → дефолт: enabled=True (сохранить поведение «только env»),
    backend='cloud_ai', agent_id='' (→ config.AGENT_ID), model/gateway_base_url='' (→
    дефолты ai.py), system_prompt='', fallback='' (→ хардкод ai._FALLBACK). Ключи доступа
    (TIMEWEB_AI_TOKEN / AI_GATEWAY_TOKEN) в app_settings НЕ лежат (секреты) — только env.
    Любой сбой чтения трактуем как «нет переопределений»: ИИ не должен молчать из-за БД.
    Логика/дефолты ДОЛЖНЫ совпадать с панелью (admin-panel/db.py::get_ai_settings).

    Выбор «ИИ-сотрудника» — три уровня, по возрастанию приоритета:
      • глобальный  — ai_agent_id / ai_system_prompt (раздел «ИИ-агенты»);
      • канал       — source лида → ai_agent_id__<source> / ai_system_prompt__<source>
                      (панель → «Каналы»); ПОБЕЖДАЕТ глобальный;
      • диалог      — persona (leads.ai_persona, оператор в «Диалогах») → реестры
                      ai_persona_agent__<persona> / ai_persona_prompt__<persona>;
                      ПОБЕЖДАЕТ канал. Пусто/нет ключа на любом уровне → берётся нижний."""
    keys = list(_AI_SETTING_KEYS) + ["ai_persona"]  # + глобальная активная персона компании
    src = (source or "").strip()
    per = (persona or "").strip()
    if src:
        keys += [f"ai_agent_id__{src}", f"ai_system_prompt__{src}"]
    if per:
        keys += [f"ai_persona_agent__{per}", f"ai_persona_prompt__{per}"]
    try:
        async with pool.acquire() as c:
            rows = await c.fetch(
                "select key, value from app_settings where key = any($1::text[])",
                keys,
            )
    except Exception as e:  # noqa: BLE001 — сбой чтения не должен ломать авто-ответ
        logging.getLogger(__name__).warning("Не удалось прочитать настройки ИИ: %s", e)
        return {"enabled": True, "backend": "cloud_ai", "agent_id": "", "model": "",
                "gateway_base_url": "", "system_prompt": "", "fallback": "", "kb_enabled": False}
    kv = {r["key"]: (r["value"] or "") for r in rows}
    enabled_raw = kv.get("ai_enabled")  # None=нет строки; ''=выключено явно
    backend = (kv.get("ai_backend") or "").strip()
    if backend not in _AI_BACKENDS:
        backend = "cloud_ai"
    agent_id = (kv.get("ai_agent_id") or "").strip()
    system_prompt = kv.get("ai_system_prompt") or ""
    # Глобальная активная персона компании (раздел «ИИ-агенты» → выбранный агент): её
    # роль-промпт — ДЕФОЛТ для всех диалогов, когда нет более специфичной привязки. Промпт
    # живёт в персоне (как у всех агентов), общая база знаний — на самом агенте; здесь лишь
    # подставляем активного агента как глобальный дефолт. Приоритет: диалог > канал >
    # активная персона > глобальный ai_system_prompt. Второй запрос — только когда персона
    # задана и глобального промпта нет (типичный кейс «один активный агент на компанию»).
    active = (kv.get("ai_persona") or "").strip()
    if active and not system_prompt:
        try:
            async with pool.acquire() as c:
                arows = await c.fetch(
                    "select key, value from app_settings where key = any($1::text[])",
                    [f"ai_persona_prompt__{active}", f"ai_persona_agent__{active}"],
                )
            akv = {r["key"]: (r["value"] or "") for r in arows}
            system_prompt = akv.get(f"ai_persona_prompt__{active}") or system_prompt
            agent_id = (akv.get(f"ai_persona_agent__{active}") or "").strip() or agent_id
        except Exception as e:  # noqa: BLE001 — сбой не должен ломать авто-ответ
            logging.getLogger(__name__).warning("Не удалось прочитать активную персону: %s", e)
    if src:  # канал перекрывает глобал/активную персону
        agent_id = (kv.get(f"ai_agent_id__{src}") or "").strip() or agent_id
        system_prompt = (kv.get(f"ai_system_prompt__{src}") or "") or system_prompt
    if per:  # персона диалога перекрывает канал и глобал
        agent_id = (kv.get(f"ai_persona_agent__{per}") or "").strip() or agent_id
        system_prompt = (kv.get(f"ai_persona_prompt__{per}") or "") or system_prompt
    return {
        "enabled": True if enabled_raw is None else bool(enabled_raw.strip()),
        "backend": backend,
        "agent_id": agent_id,
        "model": (kv.get("ai_model") or "").strip(),
        "gateway_base_url": (kv.get("ai_gateway_base_url") or "").strip(),
        "system_prompt": system_prompt,
        "fallback": kv.get("ai_fallback_text") or "",
        "kb_enabled": bool((kv.get("kb_enabled") or "").strip()),
    }


async def get_ai_history(
    tg_user_id: int, *, messenger: str = "tg",
    exclude_tg_message_id: int | None = None, limit: int = 10,
) -> list[dict]:
    """Последние ходы диалога лида для контекста OpenAI-эндпоинта агента (Wave 5).
    Возвращает [{"role": "user"|"assistant", "content": str}] в ХРОНОЛОГИЧЕСКОМ порядке.

    Раньше контекст держал серверный parent_message_id нативного /call; OpenAI-эндпоинт
    серверной памяти не имеет — историю шлём явно. Маппинг ролей: входящие (direction='in')
    → user; исходящие Лии/оператора (source in liya|manual) → assistant. Прочие исходящие
    (воронка/рассылки/системные) — НЕ диалог, не берём.

    Текущее входящее (его уже залогировал LoggingMiddleware ДО хендлера) исключаем по
    exclude_tg_message_id — его текст (возможно дополненный RAG-контекстом) вызывающий
    добавит финальным user-turn сам. tenant-scoped через tenant_id() (бот — owner, RLS
    обходит, но фильтруем явно). Сбой/нет истории → [] (Лия отвечает без контекста, не
    молчит из-за БД)."""
    if limit <= 0:
        return []
    # tg: по tg_user_id (как было, поведение байт-в-байт). vk/max: messages.tg_user_id=NULL →
    # матчим по lead_id (резолв из leads по колонке канала); tg_message_id хранит нативный id
    # сообщения канала, поэтому exclude работает одинаково.
    if messenger == "tg":
        lead_match = "tg_user_id = $1 and tenant_id = $2"
    else:
        col = _user_col(messenger)
        lead_match = f"lead_id = (select id from leads where {col} = $1 and tenant_id = $2)"
    try:
        async with pool.acquire() as c:
            rows = await c.fetch(
                f"""
                select direction, source, text
                from messages
                where {lead_match}
                  and kind = 'text' and text is not null and text <> ''
                  and (direction = 'in' or source in ('liya', 'manual'))
                  and ($3::bigint is null or not (direction = 'in' and tg_message_id = $3))
                order by id desc
                limit $4
                """,
                tg_user_id, tenant_id(), exclude_tg_message_id, limit,
            )
    except Exception:  # noqa: BLE001 — контекст не должен ломать авто-ответ Лии
        logging.getLogger(__name__).warning(
            "Не удалось прочитать историю диалога ИИ (tg=%s)", tg_user_id, exc_info=True)
        return []
    # rows идут от новых к старым (id desc) → разворачиваем в хронологию.
    out: list[dict] = []
    for r in reversed(rows):
        role = "user" if r["direction"] == "in" else "assistant"
        out.append({"role": role, "content": r["text"]})
    return out


# ── Мультиплекс (Wave 3, ТЗ §5.4): реестр тенантов + секреты + настройки ──────
async def list_active_tenants() -> list[dict]:
    """Живые тенанты для реестра мультиплекса (бот — owner, RLS обходит)."""
    async with pool.acquire() as c:
        rows = await c.fetch("select id, slug from tenants where status = 'active'")
    return [dict(r) for r in rows]


async def get_tenant_secret(tid, key_name: str) -> str | None:
    """Расшифрованный секрет тенанта из vault. Обновляет last_used_at (витрина
    «Ключей» панели). Значение НИКОГДА не логируется (§8.5); VaultError поднимается
    наружу без plaintext'а. None — секрет не задан."""
    from shared import vault
    async with pool.acquire() as c:
        row = await c.fetchrow(
            "update tenant_secrets set last_used_at = now() "
            "where tenant_id = $1 and key_name = $2 "
            "returning ciphertext, nonce, key_version",
            tid, key_name,
        )
    if row is None:
        return None
    # aad зеркалит запись панели: f"{tenant_id}:{key_name}" (uuid в канонике).
    return vault.decrypt(
        bytes(row["ciphertext"]), bytes(row["nonce"]), row["key_version"],
        aad=f"{tid}:{key_name}",
    )


async def get_tenant_shop_creds(tid=None) -> tuple[str, str] | None:
    """(shop_id, secret_key) магазина ЮKassa тенанта из vault — креды для create_payment
    (Слой C: бот тенанта принимает оплату на СВОЙ счёт). None если не заданы ОБА ключа или
    сбой расшифровки (тогда касса считается ненастроенной → кнопка «Купить» не показывается).
    tid=None → активный тенант (db.tenant_id() из contextvar мультиплекса)."""
    tid = tid or tenant_id()
    if tid is None:
        return None
    try:
        shop_id = await get_tenant_secret(tid, "shop_yookassa_shop_id")
        secret = await get_tenant_secret(tid, "shop_yookassa_secret_key")
    except Exception:  # noqa: BLE001 — сбой vault не должен ронять диалог/воронку
        logging.getLogger(__name__).warning("get_tenant_shop_creds: сбой чтения vault", exc_info=True)
        return None
    if shop_id and secret:
        return (shop_id, secret)
    return None


async def get_tenant_setting(tid, key: str) -> str | None:
    async with pool.acquire() as c:
        return await c.fetchval(
            "select value from tenant_settings where tenant_id = $1 and key = $2",
            tid, key,
        )


async def get_tenant_ai_overrides(tid) -> dict:
    """Настройки ИИ тенант-бота из tenant_settings (зеркало get_ai_overrides, но
    tenant-scoped и без слоёв канал/персона — v1 мультиплекса). Дефолты те же:
    enabled=True, backend='cloud_ai'; kb_enabled выключен (RAG Школы не делится).
    Сбой чтения → «нет переопределений», Лия тенанта не молчит из-за БД."""
    keys = ["ai_enabled", "ai_backend", "ai_agent_id", "ai_model",
            "ai_gateway_base_url", "ai_system_prompt", "ai_fallback_text"]
    try:
        async with pool.acquire() as c:
            rows = await c.fetch(
                "select key, value from tenant_settings "
                "where tenant_id = $1 and key = any($2::text[])",
                tid, keys,
            )
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).warning(
            "Не удалось прочитать настройки ИИ тенанта %s", tid, exc_info=True)
        rows = []
    kv = {r["key"]: (r["value"] or "") for r in rows}
    enabled_raw = kv.get("ai_enabled")
    backend = (kv.get("ai_backend") or "").strip()
    if backend not in _AI_BACKENDS:
        backend = "cloud_ai"
    return {
        "enabled": True if enabled_raw is None else bool(enabled_raw.strip()),
        "backend": backend,
        "agent_id": (kv.get("ai_agent_id") or "").strip(),
        "model": (kv.get("ai_model") or "").strip(),
        "gateway_base_url": (kv.get("ai_gateway_base_url") or "").strip(),
        "system_prompt": kv.get("ai_system_prompt") or "",
        "fallback": kv.get("ai_fallback_text") or "",
        "kb_enabled": False,
    }


async def get_demo_chat_cfg(slug: str = "demo-sandbox") -> dict | None:
    """Конфиг ИИ демо-тенанта для ВЕБ-чата на сайте (зеркало Telegram-демо): system_prompt + model
    (gateway-бэкенд). None — демо-тенанта нет или Лия выключена. Бот=owner, RLS обходит; читаем
    ровно ai_enabled/ai_system_prompt/ai_model демо-тенанта по слагу."""
    async with pool.acquire() as c:
        rows = await c.fetch(
            "select t.id as tid, s.key, s.value from tenant_settings s "
            "join tenants t on t.id = s.tenant_id "
            "where t.slug = $1 and s.key = any($2::text[])",
            slug, ["ai_enabled", "ai_system_prompt", "ai_model", "ai_fallback_text"],
        )
    if not rows:
        return None
    kv = {r["key"]: (r["value"] or "") for r in rows}
    if not (kv.get("ai_enabled") or "").strip():
        return None
    return {
        "tid": rows[0]["tid"],          # для веб-эскалации горячего лида (адрес — escalation.resolve)
        "system_prompt": kv.get("ai_system_prompt") or "",
        "model": (kv.get("ai_model") or "").strip(),
        "fallback": kv.get("ai_fallback_text") or "",
    }


# ── A3 Слой A: per-tenant адрес эскалации (карточка горячего лида в свою ТГ-группу) ──
async def get_tenant_escalation(tid) -> dict:
    """Куда тенант шлёт карточку эскалации (из tenant_settings, пишет панель «Мой ИИ-сотрудник»).
    Бот — owner, RLS обходит, фильтрует tenant_id явно. Сбой/нет строк → выключено (эскалация не
    должна падать из-за БД). chat_id/topic_id — int или None (нечисловое игнорируем)."""
    out = {"enabled": False, "chat_id": None, "topic_id": None}
    if tid is None:
        return out
    try:
        async with pool.acquire() as c:
            rows = await c.fetch(
                "select key, value from tenant_settings "
                "where tenant_id = $1 and key = any($2::text[])",
                tid, ["escalation_enabled", "escalation_chat_id", "escalation_topic_id"],
            )
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).warning(
            "Не удалось прочитать настройки эскалации тенанта %s", tid, exc_info=True)
        return out
    kv = {r["key"]: (r["value"] or "") for r in rows}
    out["enabled"] = bool((kv.get("escalation_enabled") or "").strip())
    cid = (kv.get("escalation_chat_id") or "").strip()
    if cid.lstrip("-").isdigit():
        out["chat_id"] = int(cid)
    top = (kv.get("escalation_topic_id") or "").strip()
    if top.isdigit():
        out["topic_id"] = int(top)
    return out


# ── Слой B: движок триггеров (tenant_triggers). Бот owner, фильтрует tenant_id явно. ──
_TRIGGER_COLS = ("id, type, action, stopwords, intent_desc, msg_count, "
                 "notify_chat_id, notify_topic_id, reply_text")


async def get_active_triggers(tid, types: tuple[str, ...] | None = None) -> list[dict]:
    """Активные триггеры тенанта для движка (опц. фильтр по типам). Порядок — position,
    created_at. Сбой/нет → [] (триггеры не должны ронять обработку сообщения)."""
    if tid is None:
        return []
    try:
        async with pool.acquire() as c:
            if types:
                rows = await c.fetch(
                    f"select {_TRIGGER_COLS} from tenant_triggers "
                    "where tenant_id = $1 and enabled = true and type = any($2::text[]) "
                    "order by position, created_at",
                    tid, list(types))
            else:
                rows = await c.fetch(
                    f"select {_TRIGGER_COLS} from tenant_triggers "
                    "where tenant_id = $1 and enabled = true order by position, created_at",
                    tid)
        return [dict(r) for r in rows]
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).warning("get_active_triggers: сбой чтения", exc_info=True)
        return []


async def count_inbound_messages(tg_user_id: int, *, messenger: str = "tg") -> int:
    """Кол-во входящих сообщений лида (триггер message_count). tenant-scoped (owner фильтрует).
    tg: по tg_user_id (как было); vk/max: по lead_id (messages.tg_user_id=NULL)."""
    if messenger == "tg":
        match = "tg_user_id = $1 and tenant_id = $2"
    else:
        col = _user_col(messenger)
        match = f"lead_id = (select id from leads where {col} = $1 and tenant_id = $2)"
    try:
        async with pool.acquire() as c:
            v = await c.fetchval(
                f"select count(*) from messages where {match} and direction = 'in'",
                tg_user_id, tenant_id())
        return int(v or 0)
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).warning("count_inbound_messages: сбой", exc_info=True)
        return 0


async def pause_lead(tg_user_id: int, *, messenger: str = "tg") -> None:
    """Поставить диалог на паузу (bot_paused=true) — действие триггера notify_reply_pause.
    Дальше Лия молчит, отвечает оператор (is_bot_paused это проверяет). tenant-scoped."""
    col = _user_col(messenger)
    try:
        async with pool.acquire() as c:
            await c.execute(
                f"update leads set bot_paused = true where {col} = $1 and tenant_id = $2",
                tg_user_id, tenant_id())
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).warning("pause_lead: сбой", exc_info=True)


# ── Метеринг (Wave 3): мягкая пауза ИИ при пустом кошельке prepaid-тенанта ────
async def is_ai_wallet_blocked() -> bool:
    """True — кошелёк активного тенанта пуст и ИИ на мягкой паузе (флаг ставит
    снапшот-воркер, снимает топап-вебхук панели). Тенант без плана (Школа до
    Wave 4) флага не получает никогда — §8.7 держится при любом балансе.
    Сбой чтения трактуем как «не заблокирован»: Лия не должна молчать из-за БД."""
    tid = tenant_id()
    if tid is None:
        return False
    try:
        async with pool.acquire() as c:
            v = await c.fetchval(
                "select value from tenant_settings "
                "where tenant_id = $1 and key = 'ai_wallet_blocked'",
                tid,
            )
        return bool((v or "").strip())
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).warning("Не удалось прочитать ai_wallet_blocked", exc_info=True)
        return False


async def set_ai_wallet_blocked(tid, on: bool, *, conn=None) -> None:
    """Ставит/снимает флаг паузы ИИ тенанта (бот — owner, RLS обходит).

    conn — если передан, работаем на нём (вызов ИЗНУТРИ удержанного соединения:
    нельзя брать второе из пула max_size=10, иначе дедлок — финдинг ревью №6).
    Без conn — берём своё короткое соединение."""
    async def _do(c) -> None:
        if on:
            await c.execute(
                "insert into tenant_settings (tenant_id, key, value) "
                "values ($1, 'ai_wallet_blocked', '1') "
                "on conflict (tenant_id, key) do update set value = '1', updated_at = now()",
                tid,
            )
        else:
            await c.execute(
                "delete from tenant_settings where tenant_id = $1 and key = 'ai_wallet_blocked'",
                tid,
            )
    if conn is not None:
        await _do(conn)
    else:
        async with pool.acquire() as c:
            await _do(c)


async def kb_search(
    embedding: list[float], persona: str | None = None,
    *, top_k: int = 4, max_distance: float = 0.55,
) -> list[str]:
    """Top-k чанков базы знаний по косинусной близости (pgvector `<=>`) + фильтр по роли.
    Возвращает тексты ближайших чанков. Общая справка (metadata.role_tag пуст) видна ВСЕМ
    ролям; чанки конкретной персоны — только ей. max_distance отсекает нерелевантное
    (косинусная дистанция: 0 — идентично, 2 — противоположно; для e5 релевантное ~0.1–0.3).
    Бот ходит под owner-ролью — грант на kb_chunks не нужен. Сбой/нет таблицы (DDL не
    применён) → исключение пробрасываем: kb.retrieve_context его ловит и отключает RAG."""
    if not embedding or pool is None:
        return []
    vec = "[" + ",".join(f"{x:.6f}" for x in embedding) + "]"
    per = (persona or "").strip()
    async with pool.acquire() as c:
        rows = await c.fetch(
            """
            select content
              from kb_chunks
             where embedding is not null
               and (coalesce(metadata->>'role_tag', '') = '' or metadata->>'role_tag' = $2)
               and (embedding <=> $1::vector) <= $3
             order by embedding <=> $1::vector
             limit $4
            """,
            vec, per, max_distance, top_k,
        )
    return [r["content"] for r in rows]


# ── app_settings: НЕ-секретный снимок конфигурации бота (бот ПИШЕТ owner-ролью) ──
# Ключи статуса рантайма (бот пишет, панель ЧИТАЕТ — разделы «Интеграции»/«Каналы»).
# ДОЛЖНЫ совпадать с admin-panel/config.py::RUNTIME_STATUS_*_KEY. У панели и бота РАЗНОЕ
# окружение, поэтому общий канал статуса — только app_settings. Секреты СЮДА НЕ кладём:
# для токена/прокси публикуем булев флаг присутствия ("1"/""), а не значение.
_RUNTIME_STATUS_KEYS = (
    "bot_username", "gate_channel_url", "bot_guide_url_env", "bot_proxy_set",
    "bot_agent_token_set", "bot_gateway_token_set", "bot_public_base_url",
    "bot_shop_yookassa_set",
)


async def publish_runtime_status(
    *, bot_username: str, gate_channel_url: str, guide_url_env: str,
    proxy_set: bool, agent_token_set: bool, gateway_token_set: bool,
    public_base_url: str, shop_yookassa_set: bool = False,
) -> None:
    """Публикует НЕ-секретный снимок конфигурации бота в app_settings, чтобы панель честно
    показывала статус интеграций и строила deep-link'и (t.me/<bot_username>?start=<source>).
    Вызывается на СТАРТЕ (bot.py, после get_me); сбой изолируется вызывающим — не валит
    запуск. updated_at строки bot_username = «последний раз бот публиковал статус» (heartbeat).
    Пишем owner-ролью (бот владеет app_settings) — грантов панели не требует."""
    pairs = (
        ("bot_username", (bot_username or "").lstrip("@")),
        ("gate_channel_url", gate_channel_url or ""),
        ("bot_guide_url_env", guide_url_env or ""),
        ("bot_proxy_set", "1" if proxy_set else ""),
        ("bot_agent_token_set", "1" if agent_token_set else ""),
        ("bot_gateway_token_set", "1" if gateway_token_set else ""),
        ("bot_public_base_url", public_base_url or ""),
        ("bot_shop_yookassa_set", "1" if shop_yookassa_set else ""),
    )
    async with pool.acquire() as c:
        async with c.transaction():
            for key, value in pairs:
                await c.execute(
                    """
                    insert into app_settings (key, value) values ($1, $2)
                    on conflict (key) do update set value = excluded.value
                    """,
                    key, value,
                )


# ── Трекинг /r/<token>: чтение токена + лог клика (пишет БОТ) ─────────────────
async def get_link_token(token: str) -> dict | None:
    """target_url + контекст по токену. None если нет. Вызывается обработчиком /r."""
    async with pool.acquire() as c:
        row = await c.fetchrow(
            "select token, target_url, broadcast_id, lead_id from link_tokens where token = $1",
            token,
        )
    return dict(row) if row else None


async def log_link_click(token: str, broadcast_id, lead_id, ua: str | None, ip: str | None) -> None:
    """Лог клика. ua обрезается [:512]. Вызывать fire-and-forget — редирект важнее лога."""
    async with pool.acquire() as c:
        await c.execute(
            "insert into link_clicks (token, broadcast_id, lead_id, ua, ip, tenant_id) "
            "values ($1, $2, $3, $4, $5::inet, "
            "        (select tenant_id from link_tokens where token = $1))",
            token, broadcast_id, lead_id,
            (ua[:512] if ua else None),
            ip,
        )


# ── Retention: обезличивание по отзыву + TTL переписки (§6.4) ─────────────────
async def due_for_erase(after_days: int) -> list[str]:
    """uuid лидов, у которых erase_requested_at + N дней <= now() и ещё есть ПДн."""
    async with pool.acquire() as c:
        rows = await c.fetch(
            """
            select id from leads
            where erase_requested_at is not null
              and tenant_id = $2
              and erase_requested_at + make_interval(days => $1) <= now()
              and (name is not null or phone is not null
                   or phone_hash is not null or notes is not null)
            limit 100
            """,
            after_days, tenant_id(),  # стираем ПДн ТОЛЬКО своего тенанта (152-ФЗ; бот=owner обходит RLS)
        )
    return [r["id"] for r in rows]


async def erase_lead(lead_id: str, actor: str = "retention-cron") -> None:
    """Обезличивает лид и его ПДн-производные одной транзакцией + аудит 'lead_erased'.

    leads-строки НЕ удаляются (обезличиваются in-place), поэтому ON DELETE CASCADE не
    срабатывает — чистим производные вручную: переписку удаляем (обезличивать нечего),
    клики обезличиваем (lead_id→null, факт клика для агрегатов остаётся), PII-историю
    в admin_audit чистим по lead_id. broadcast_recipients оставляем как агрегат, но
    рвём связь с ПДн (tg_user_id обнуляем). action='lead_erased' — доказательство срока для РКН.
    """
    import json
    async with pool.acquire() as c:
        async with c.transaction():
            await c.execute(
                "update leads set name = null, phone = null, phone_hash = null, notes = null "
                "where id = $1",
                lead_id,
            )
            # Переписка — ПДн целиком, обезличить нечего → удаляем.
            await c.execute("delete from messages where lead_id = $1", lead_id)
            # Клики — рвём связь с субъектом, агрегат по broadcast остаётся.
            await c.execute("update link_clicks set lead_id = null where lead_id = $1", lead_id)
            # Получатели рассылок — обнуляем прямой идентификатор адреса.
            await c.execute(
                "update broadcast_recipients set tg_user_id = 0 where lead_id = $1", lead_id
            )
            # Чистим PII-детали в аудите по этому лиду (detail может нести len/факты — не текст,
            # но на всякий случай обнуляем detail у не-системных записей этого лида).
            await c.execute(
                "update admin_audit set detail = null where lead_id = $1", lead_id
            )
            await c.execute(
                "insert into admin_audit (actor, action, lead_id, detail) "
                "values ($1, 'lead_erased', $2, $3::jsonb)",
                actor, lead_id, json.dumps({"by": "retention-cron"}),
            )


async def purge_old_message_text(ttl_days: int) -> int:
    """Абсолютный TTL: обнуляет text/file_id у messages старше N дней (самый объёмный ПДн).

    Строки оставляем (агрегаты тредов/направление), чистим только содержимое. §6.4.
    """
    async with pool.acquire() as c:
        res = await c.execute(
            """
            update messages set text = null, file_id = null
            where created_at < now() - make_interval(days => $1)
              and tenant_id = $2
              and (text is not null or file_id is not null)
            """,
            ttl_days, tenant_id(),  # чистим переписку ТОЛЬКО своего тенанта (бот=owner обходит RLS)
        )
    return _affected(res)


def _affected(status: str) -> int:
    """Число строк из command tag asyncpg вида 'UPDATE 7' / 'DELETE 3'."""
    try:
        return int(status.split()[-1])
    except (ValueError, IndexError, AttributeError):
        return 0


# ── Заказы: онлайн-оплата продаж школы (Phase 1B, бот пишет owner-ролью) ──────
# Поток: клик «Купить» → pending-заказ + платёж ЮKassa (handlers.on_buy) → лид платит →
# вебхук ПАНЕЛИ матчит заказ по provider_payment_id и отмечает paid + converted.
# Бот заказы только создаёт/связывает с платежом; «оплачено» он НЕ проставляет.

async def get_lead_for_purchase(tg_user_id: int, *, messenger: str = "tg") -> dict | None:
    """Лид для оформления заказа: id (FK заказа), name (описание платежа), phone (чек
    54-ФЗ, если включён). None — лида нет (заказ без лида не оформляем). Слой C: идентичность
    по каналу (tg/vk/max) через _user_col — заказ оформляется на правильного лида тенанта."""
    col = _user_col(messenger)
    async with pool.acquire() as c:
        row = await c.fetchrow(
            f"select id, name, phone from leads where {col} = $1 and tenant_id = $2",
            tg_user_id, tenant_id(),
        )
    return dict(row) if row else None


async def create_or_reuse_pending_order(
    lead_id, product_id: int, amount, currency: str, *, reuse_minutes: int,
) -> dict:
    """Pending-заказ под клик «Купить»: вернуть НЕДАВНИЙ существующий или создать новый.

    Анти-двойное-списание: повторный клик в пределах reuse_minutes возвращает ТОТ ЖЕ
    заказ с той же ссылкой на оплату (payment_url) — новый платёж не создаётся, два
    окна оплаты не живут одновременно. Более старые pending этого лида на этот же
    продукт помечаем failed (их платежи в ЮKassa истекают сами, ~1 час) — лента
    «Платежей» не копит вечный pending. Возвращает
    {id, payment_url, reused}: reused=True → звонящий шлёт payment_url как есть.
    """
    async with pool.acquire() as c:
        async with c.transaction():
            fresh = await c.fetchrow(
                """
                select id, payment_url from orders
                where lead_id = $1 and product_id = $2
                  and status = 'pending' and source = 'yookassa'
                  and payment_url is not null
                  and created_at >= now() - make_interval(mins => $3)
                order by created_at desc
                limit 1
                for update
                """,
                lead_id, product_id, reuse_minutes,
            )
            if fresh is not None:
                return {"id": fresh["id"], "payment_url": fresh["payment_url"], "reused": True}
            # Протухшие pending на тот же продукт → failed (новый клик = новый платёж).
            await c.execute(
                """
                update orders set status = 'failed', note = coalesce(note, 'просрочен (повторный клик)')
                where lead_id = $1 and product_id = $2
                  and status = 'pending' and source = 'yookassa'
                """,
                lead_id, product_id,
            )
            row = await c.fetchrow(
                """
                insert into orders (lead_id, product_id, amount, currency, status, source,
                                    created_by, tenant_id)
                values ($1, $2, $3, $4, 'pending', 'yookassa', 'bot',
                        (select tenant_id from leads where id = $1))
                returning id
                """,
                lead_id, product_id, amount, currency,
            )
            return {"id": row["id"], "payment_url": None, "reused": False}


async def set_order_payment(order_id, payment_id: str, payment_url: str) -> None:
    """Связать заказ с платежом ЮKassa (id для матча в вебхуке + ссылка для повтор-клика)."""
    async with pool.acquire() as c:
        await c.execute(
            "update orders set provider_payment_id = $2, payment_url = $3 where id = $1",
            order_id, payment_id, payment_url,
        )


async def mark_order_failed(order_id, note: str) -> None:
    """Пометить заказ failed (платёж не создался: ЮKassa недоступна/отвергла). Лид уже
    получил мягкий фолбэк-текст; нота — для ленты «Платежей» оператора."""
    async with pool.acquire() as c:
        await c.execute(
            "update orders set status = 'failed', note = $2 where id = $1 and status = 'pending'",
            order_id, note[:300],
        )


async def mark_stale_yookassa_orders_failed(hours: int) -> int:
    """Просроченные pending-заказы онлайн-оплаты → failed (retention-цикл, раз в час).
    Платёж в ЮKassa к этому моменту давно истёк; вебхук paid таких заказов не тронет
    (он матчит по provider_payment_id и идемпотентен по статусу)."""
    async with pool.acquire() as c:
        res = await c.execute(
            """
            update orders set status = 'failed', note = coalesce(note, 'не оплачен (истёк срок)')
            where status = 'pending' and source = 'yookassa'
              and tenant_id = $2
              and created_at < now() - make_interval(hours => $1)
            """,
            hours, tenant_id(),  # просрочка заказов ТОЛЬКО своего тенанта
        )
    return _affected(res)
