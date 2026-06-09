"""Слой доступа к Postgres через asyncpg (роль panel_rw).

Зеркалит bot-telegram/db.py: простой пул min_size=1/max_size=5, инициализация
в FastAPI lifespan. Жёсткие правила (§2 плана):

  * ТОЛЬКО позиционные параметры $1..$n. Никакой f-string интерполяции ввода.
  * Динамическая колонка/направление сортировки — через allow-list-множества
    (паттерн _FOLLOWUP_COLS бота), не сырая подстановка.
  * Список и счётчики НЕ селектят сырой phone — только хвост right(...,2) прямо в SQL.
    Полный номер физически не покидает Postgres вне /reveal и /export-full.
  * Единый build_filters() — один источник WHERE для счётчиков/списка/экспорта,
    чтобы цифры дашборда всегда совпадали с видимыми строками.

Эта роль (panel_rw) имеет ровно: SELECT + UPDATE(status,notes,erase_requested_at)
на leads, INSERT на admin_audit/admin_sessions/admin_login_throttle, UPDATE на
сессии/троттл; БЕЗ update/delete на admin_audit (честный append-only, §3.6).
"""
from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timezone
from typing import Any

import asyncpg

import config

pool: asyncpg.Pool | None = None

# --- Allow-list'ы для динамики в SQL (никогда не подставляем имя/направление сырьём) ---
# Канон значений — в config (единый источник, совпадает со схемой/ботом). Здесь —
# быстрые frozenset'ы для валидации фильтров/UPDATE как defence-in-depth.
STATUSES: tuple[str, ...] = config.STATUSES
SOURCES: tuple[str, ...] = config.SOURCES
MESSENGERS: tuple[str, ...] = config.MESSENGERS
_STATUS_SET = frozenset(STATUSES)
_SOURCE_SET = frozenset(SOURCES)
_MESSENGER_SET = frozenset(MESSENGERS)

# Каталог продуктов (оферов): allow-list'ы видов/статусов/валют — defence-in-depth
# поверх CHECK'ов схемы (products_kind_chk / products_status_chk) и валидации хендлера.
_PRODUCT_KIND_SET = frozenset(config.PRODUCT_KINDS)
_PRODUCT_STATUS_SET = frozenset(config.PRODUCT_STATUSES)
_PRODUCT_CURRENCY_SET = frozenset(config.PRODUCT_CURRENCIES)

# Платежи / заказы: allow-list'ы статусов — defence-in-depth поверх orders_status_chk.
_ORDER_STATUS_SET = frozenset(config.ORDER_STATUSES)
# Биллинг сервиса (подписка): статусы счёта — defence-in-depth поверх service_invoices_status_chk.
_SERVICE_INVOICE_STATUS_SET = frozenset(config.SERVICE_INVOICE_STATUSES)

# Сортировка списка: ключ из query-string → готовый ORDER BY фрагмент (без ввода).
_SORT_SQL: dict[str, str] = {
    "created_desc": "created_at desc",
    "updated_desc": "updated_at desc",
}
DEFAULT_SORT = "created_desc"


# --------------------------------------------------------------------------- #
# Пул
# --------------------------------------------------------------------------- #
async def init() -> None:
    global pool
    pool = await asyncpg.create_pool(config.DATABASE_URL, min_size=1, max_size=5)


async def close() -> None:
    global pool
    if pool:
        await pool.close()
        pool = None


# --------------------------------------------------------------------------- #
# Нормализация телефона оператора → phone_hash (ИДЕНТИЧНО боту!)
# bot-telegram/handlers.py::_phone_hash: sha256(только цифры).hexdigest().
# Любое расхождение → поиск молча вернёт пусто. phone_hash unsalted sha256 ⇒
# обратим брутфорсом ⇒ по 152-ФЗ это ПДн ⇒ как «защиту» не используем, в UI не светим.
# --------------------------------------------------------------------------- #
def phone_query_hash(raw: str) -> str | None:
    """Хеш поисковой строки телефона. None, если цифр нет (нечего искать по хешу)."""
    digits = "".join(ch for ch in raw if ch.isdigit())
    if not digits:
        return None
    return hashlib.sha256(digits.encode()).hexdigest()


# --------------------------------------------------------------------------- #
# Единый filter-builder (§2). Один источник WHERE для счётчиков/списка/экспорта.
# Возвращает (where_sql, params, next_idx): фрагмент без слова WHERE, всегда
# непустой (минимум "true"), и индекс следующего свободного $-плейсхолдера.
#
# include_status=False — вариант для счётчиков дашборда (status фильтруется через
# count(*) filter(...), а не в WHERE), чтобы все цифры считались одним проходом
# по той же выборке прочих фильтров.
# --------------------------------------------------------------------------- #
def build_filters(
    *,
    status: str | None = None,
    source: str | None = None,
    messenger: str | None = None,
    consent: bool | None = None,
    dt_from: datetime | None = None,
    dt_to: datetime | None = None,
    q_hash: str | None = None,
    q_name: str | None = None,
    erase_pending: bool | None = None,
    include_status: bool = True,
    start_idx: int = 1,
) -> tuple[str, list[Any], int]:
    clauses: list[str] = []
    params: list[Any] = []
    i = start_idx

    def add(clause_tpl: str, value: Any) -> None:
        nonlocal i
        clauses.append(clause_tpl.format(i=i))
        params.append(value)
        i += 1

    if include_status and status is not None:
        # Defence-in-depth: невалидный статус не уходит в SQL как фильтр.
        if status in _STATUS_SET:
            add("status = ${i}", status)
        else:
            clauses.append("false")  # неизвестный статус → пустая выборка, не ошибка
    if source is not None and source in _SOURCE_SET:
        add("source = ${i}", source)
    if messenger is not None and messenger in _MESSENGER_SET:
        add("messenger = ${i}", messenger)
    if consent is not None:
        add("consent = ${i}", consent)
    if dt_from is not None:
        add("created_at >= ${i}", dt_from)
    if dt_to is not None:
        add("created_at < ${i}", dt_to)
    if q_hash is not None:
        add("phone_hash = ${i}", q_hash)
    if q_name:
        # ILIKE по имени; экранируем спецсимволы LIKE, значение всё равно параметризовано.
        escaped = q_name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        add("name ilike '%' || ${i} || '%'", escaped)
    if erase_pending is True:
        clauses.append("erase_requested_at is not null")
    elif erase_pending is False:
        clauses.append("erase_requested_at is null")

    where_sql = " and ".join(clauses) if clauses else "true"
    return where_sql, params, i


# --------------------------------------------------------------------------- #
# Дашборд: счётчики одним проходом (§2). status считается через count filter,
# поэтому в WHERE его НЕ кладём (include_status=False).
# --------------------------------------------------------------------------- #
async def dashboard_counts(filters: dict[str, Any]) -> asyncpg.Record:
    where_sql, params, _ = build_filters(include_status=False, **filters)
    q = f"""
        select
            count(*)                                               as total,
            count(*) filter (where status = 'new')                 as new,
            count(*) filter (where status = 'guide_sent')          as guide_sent,
            count(*) filter (where status = 'nurturing')           as nurturing,
            count(*) filter (where status = 'converted')           as converted,
            count(*) filter (where status = 'lost')                as lost,
            count(*) filter (where consent)                        as consent_yes,
            count(*) filter (where subscribed)                     as subscribed_yes,
            count(*) filter (where created_at >= now() - interval '7 days') as new_7d,
            count(*) filter (where erase_requested_at is not null) as erase_pending
        from leads
        where {where_sql}
    """
    async with pool.acquire() as c:
        return await c.fetchrow(q, *params)


async def dashboard_by_source(filters: dict[str, Any]) -> list[asyncpg.Record]:
    """Распределение по площадкам (reels|dzen|youtube|vk|max|other)."""
    where_sql, params, _ = build_filters(include_status=False, **filters)
    q = f"""
        select source, count(*) as cnt
        from leads
        where {where_sql}
        group by source
        order by cnt desc, source asc
    """
    async with pool.acquire() as c:
        return await c.fetch(q, *params)


# --------------------------------------------------------------------------- #
# Список лидов (§2). НЕ селектит сырой phone — только хвост phone_tail (2 цифры)
# прямо в SQL. Опечатка {{ lead.phone }} в шаблоне не сольёт больше 2 цифр.
# --------------------------------------------------------------------------- #
_LIST_SELECT = """
    select id, created_at, updated_at, messenger, source, name,
           right(regexp_replace(coalesce(phone,''), '\\D', '', 'g'), 2) as phone_tail,
           phone is not null and phone <> '' as has_phone,
           consent, subscribed, status, erase_requested_at
    from leads
"""


async def count_leads(filters: dict[str, Any]) -> int:
    where_sql, params, _ = build_filters(**filters)
    q = f"select count(*) from leads where {where_sql}"
    async with pool.acquire() as c:
        return int(await c.fetchval(q, *params))


async def list_leads(
    filters: dict[str, Any], *, sort: str, limit: int, offset: int
) -> list[asyncpg.Record]:
    where_sql, params, next_i = build_filters(**filters)
    order_sql = _SORT_SQL.get(sort, _SORT_SQL[DEFAULT_SORT])  # allow-list, не ввод
    q = f"""
        {_LIST_SELECT}
        where {where_sql}
        order by {order_sql}
        limit ${next_i} offset ${next_i + 1}
    """
    params = params + [limit, offset]
    async with pool.acquire() as c:
        return await c.fetch(q, *params)


# --------------------------------------------------------------------------- #
# Список ДИАЛОГОВ (раздел «Диалоги», мессенджер-вид). То же, что list_leads, но с
# последним сообщением (для превью+времени) и счётчиком «без ответа» — число
# входящих, пришедших ПОСЛЕ последнего исходящего (либо всех входящих, если
# оператор/бот ни разу не отвечал). Сортировка — по активности (последнее
# сообщение, иначе дата создания). panel_rw: только SELECT на leads+messages.
#
# Колонки последнего сообщения переименованы ВНУТРИ латерали (last_text/last_kind/
# last_direction/last_msg_source/last_at), чтобы НЕ конфликтовать с одноимёнными
# колонками leads (source/created_at) — иначе build_filters с unqualified `source`
# дал бы «ambiguous column».
# --------------------------------------------------------------------------- #
_DIALOG_SELECT = """
    select l.id, l.created_at, l.updated_at, l.messenger, l.source, l.name,
           right(regexp_replace(coalesce(l.phone,''), '\\D', '', 'g'), 2) as phone_tail,
           l.phone is not null and l.phone <> '' as has_phone,
           l.consent, l.subscribed, l.status, l.erase_requested_at,
           l.bot_paused, l.unsubscribed_at, l.tg_user_id,
           lm.last_text, lm.last_kind, lm.last_direction, lm.last_at,
           coalesce(u.unread, 0) as unread
    from leads l
    left join lateral (
        select m.text as last_text, m.kind as last_kind,
               m.direction as last_direction, m.created_at as last_at
        from messages m
        where m.lead_id = l.id
        order by m.created_at desc, m.id desc
        limit 1
    ) lm on true
    left join lateral (
        select count(*) as unread
        from messages m
        where m.lead_id = l.id and m.direction = 'in'
          and m.created_at > coalesce(
              (select max(mo.created_at) from messages mo
               where mo.lead_id = l.id and mo.direction = 'out'),
              'epoch'::timestamptz)
    ) u on true
"""


async def list_dialogs(
    filters: dict[str, Any], *, limit: int, offset: int
) -> list[asyncpg.Record]:
    where_sql, params, next_i = build_filters(**filters)
    q = f"""
        {_DIALOG_SELECT}
        where {where_sql}
        order by coalesce(lm.last_at, l.created_at) desc, l.created_at desc
        limit ${next_i} offset ${next_i + 1}
    """
    params = params + [limit, offset]
    async with pool.acquire() as c:
        return await c.fetch(q, *params)


async def count_unanswered_dialogs() -> int:
    """Сколько лидов с непрочитанным входящим (последнее сообщение — от клиента,
    оператор ещё не ответил). Бейдж раздела «Диалоги» в сайдбаре."""
    q = """
        select count(*) from leads l
        where exists (
            select 1 from messages m
            where m.lead_id = l.id and m.direction = 'in'
              and m.created_at > coalesce(
                  (select max(mo.created_at) from messages mo
                   where mo.lead_id = l.id and mo.direction = 'out'),
                  'epoch'::timestamptz)
        )
    """
    async with pool.acquire() as c:
        return int(await c.fetchval(q))


# --------------------------------------------------------------------------- #
# Карточка (§2). Default — БЕЗ полного телефона, только хвост. Полный номер
# берётся отдельным запросом reveal_phone() лишь внутри POST /reveal.
# --------------------------------------------------------------------------- #
async def get_lead(lead_id) -> asyncpg.Record | None:
    q = """
        select id, created_at, updated_at, messenger, source, name,
               right(regexp_replace(coalesce(phone,''), '\\D', '', 'g'), 2) as phone_tail,
               phone is not null and phone <> '' as has_phone,
               phone_hash, consent, subscribed, status,
               guide_sent_at, follow_up_1_at, follow_up_2_at, follow_up_3_at,
               tg_user_id, max_user_id, notes, survey, erase_requested_at,
               bot_paused, unsubscribed_at
        from leads
        where id = $1
    """
    async with pool.acquire() as c:
        return await c.fetchrow(q, lead_id)


async def reveal_phone(lead_id) -> str | None:
    """Полный номер. Вызывается ТОЛЬКО из POST /reveal ПОСЛЕ записи аудита (§3.8)."""
    async with pool.acquire() as c:
        return await c.fetchval("select phone from leads where id = $1", lead_id)


# --------------------------------------------------------------------------- #
# UPDATE — ровно 2 колонки (status, notes), в ОДНОЙ транзакции с аудитом (§3.6).
# updated_at бампается триггером trg_leads_updated_at — вручную не трогаем.
# status валидируется здесь как defence-in-depth (помимо хендлера).
# detail аудита по notes — без текста (ПДн!): только маркер изменения/длины.
# --------------------------------------------------------------------------- #
async def update_lead_with_audit(
    lead_id,
    *,
    new_status: str,
    new_notes: str | None,
    actor: str,
    ip: str | None,
    user_agent: str | None,
) -> asyncpg.Record | None:
    if new_status not in _STATUS_SET:
        raise ValueError(f"Недопустимый статус: {new_status!r}")
    async with pool.acquire() as c:
        async with c.transaction():
            old = await c.fetchrow(
                "select status, notes from leads where id = $1 for update", lead_id
            )
            if old is None:
                return None  # транзакция откатится, аудит не пишется
            row = await c.fetchrow(
                """
                update leads set status = $1, notes = $2
                where id = $3
                returning id, status, notes, updated_at
                """,
                new_status, new_notes, lead_id,
            )
            old_notes = old["notes"] or ""
            new_notes_str = new_notes or ""
            detail = {
                # status — НЕ ПДн → можно хранить old/new полностью.
                "status": {"old": old["status"], "new": new_status,
                           "changed": old["status"] != new_status},
                # notes — ПДн («договорённости, контекст») → только факт и длины.
                "notes": {"changed": old_notes != new_notes_str,
                          "len_old": len(old_notes), "len_new": len(new_notes_str)},
            }
            await _insert_audit(
                c, actor=actor, action="lead_update", lead_id=lead_id,
                ip=ip, user_agent=user_agent, detail=detail,
            )
            return row


# --------------------------------------------------------------------------- #
# 152-ФЗ: пометка «принять отзыв согласия / запрос субъекта» (§3.9).
# Выставляет erase_requested_at = now() (если ещё не стоит) — НЕ удаляет данные
# мгновенно; обезличивание делает cron через ERASE_AFTER_DAYS. Аудит в той же тр-ции.
# --------------------------------------------------------------------------- #
async def request_erase_with_audit(
    lead_id,
    *,
    actor: str,
    ip: str | None,
    user_agent: str | None,
) -> asyncpg.Record | None:
    async with pool.acquire() as c:
        async with c.transaction():
            row = await c.fetchrow(
                """
                update leads
                set erase_requested_at = coalesce(erase_requested_at, now())
                where id = $1
                returning id, erase_requested_at
                """,
                lead_id,
            )
            if row is None:
                return None
            await _insert_audit(
                c, actor=actor, action="lead_erase_requested", lead_id=lead_id,
                ip=ip, user_agent=user_agent,
                detail={"erase_requested_at": row["erase_requested_at"].isoformat()},
            )
            return row


# --------------------------------------------------------------------------- #
# Аудит. append-only вставка; роль panel_rw не имеет update/delete на admin_audit.
# detail сериализуем в jsonb через ::jsonb из текстового параметра (без PII по
# контракту вызывающих). default-сериализатор гасит datetime → isoformat.
# --------------------------------------------------------------------------- #
def _json_default(o: Any) -> Any:
    if isinstance(o, datetime):
        return o.isoformat()
    return str(o)


async def _insert_audit(
    conn: asyncpg.Connection,
    *,
    actor: str,
    action: str,
    lead_id=None,
    ip: str | None = None,
    user_agent: str | None = None,
    detail: dict | None = None,
) -> None:
    """Вставка строки аудита НА ПЕРЕДАННОМ соединении (для общей транзакции с мутацией)."""
    detail_json = json.dumps(detail or {}, ensure_ascii=False, default=_json_default)
    await conn.execute(
        """
        insert into admin_audit (actor, action, lead_id, ip, user_agent, detail)
        values ($1, $2, $3, $4::inet, $5, $6::jsonb)
        """,
        actor, action, lead_id, ip, user_agent, detail_json,
    )


async def audit(
    *,
    actor: str,
    action: str,
    lead_id=None,
    ip: str | None = None,
    user_agent: str | None = None,
    detail: dict | None = None,
) -> None:
    """Самостоятельная (вне внешней транзакции) запись аудита.

    Для reveal/export используется как fail-closed барьер: вызываем ДО отдачи ПДн;
    если INSERT упадёт — исключение поднимется и ПДн не раскроется (§3.6).
    """
    async with pool.acquire() as c:
        await _insert_audit(
            c, actor=actor, action=action, lead_id=lead_id,
            ip=ip, user_agent=user_agent, detail=detail,
        )


# --------------------------------------------------------------------------- #
# Курсорные выборки для CSV-стрима (§3.11). Память плоская: yield по строке
# внутри транзакции с server-side cursor. Маска/полный — два разных запроса,
# каждый со своим row-cap (limit). survey/phone_hash исключены из обоих.
# --------------------------------------------------------------------------- #
async def stream_export_masked(filters: dict[str, Any], *, row_cap: int):
    where_sql, params, next_i = build_filters(**filters)
    q = f"""
        select created_at, updated_at, name,
               right(regexp_replace(coalesce(phone,''), '\\D', '', 'g'), 2) as phone_tail,
               phone is not null and phone <> '' as has_phone,
               messenger, source, status, consent, subscribed,
               guide_sent_at, erase_requested_at
        from leads
        where {where_sql}
        order by created_at desc
        limit ${next_i}
    """
    params = params + [row_cap]
    async with pool.acquire() as c:
        async with c.transaction():
            async for rec in c.cursor(q, *params):
                yield rec


async def stream_export_full(filters: dict[str, Any], *, row_cap: int):
    """CSV с ПОЛНЫМ телефоном (gated, отдельный аудит export_full)."""
    where_sql, params, next_i = build_filters(**filters)
    q = f"""
        select created_at, updated_at, name, phone,
               messenger, source, status, consent, subscribed,
               guide_sent_at, erase_requested_at
        from leads
        where {where_sql}
        order by created_at desc
        limit ${next_i}
    """
    params = params + [row_cap]
    async with pool.acquire() as c:
        async with c.transaction():
            async for rec in c.cursor(q, *params):
                yield rec


# =========================================================================== #
# РАСШИРЕНИЕ: переписка / перехват / рассылки / аналитика (план §3-§7).
#
# Инвариант (panel_rw, без BOT_TOKEN): панель ЧИТАЕТ messages/аналитику и ПИШЕТ
# только в очереди (outbox / broadcasts / broadcast_files / link_tokens) и флаг
# leads.bot_paused. Фактическую отправку, материализацию получателей и клики ведёт
# БОТ под owner-ролью. Все мутации идут В ОДНОЙ транзакции с аудитом (паттерн
# update_lead_with_audit). detail аудита — БЕЗ ПДн (факт/длины/счётчики).
# =========================================================================== #

# --------------------------------------------------------------------------- #
# Блок ПЕРЕХВАТ: bot_paused. UPDATE одной колонки в транзакции с аудитом.
# Грант: update(bot_paused) на leads. updated_at бампается триггером — не трогаем.
# --------------------------------------------------------------------------- #
async def set_bot_paused_with_audit(
    lead_id,
    *,
    paused: bool,
    actor: str,
    ip: str | None,
    user_agent: str | None,
) -> asyncpg.Record | None:
    """Перехват/возврат: leads.bot_paused = paused. Аудит bot_paused|bot_resumed.

    Возвращает строку с id/bot_paused/tg_user_id, либо None если лид не найден
    (транзакция откатывается, аудит не пишется). По образцу update_lead_with_audit:
    SELECT … FOR UPDATE → UPDATE → _insert_audit в общей транзакции.
    """
    async with pool.acquire() as c:
        async with c.transaction():
            old = await c.fetchrow(
                "select bot_paused from leads where id = $1 for update", lead_id
            )
            if old is None:
                return None
            row = await c.fetchrow(
                """
                update leads set bot_paused = $1
                where id = $2
                returning id, bot_paused, tg_user_id
                """,
                paused, lead_id,
            )
            await _insert_audit(
                c, actor=actor,
                action="bot_paused" if paused else "bot_resumed",
                lead_id=lead_id, ip=ip, user_agent=user_agent,
                detail={"lead_id": str(lead_id),
                        "changed": bool(old["bot_paused"]) != paused},
            )
            return row


# --------------------------------------------------------------------------- #
# Блок ПЕРЕПИСКА: тред сообщений лида (читает панель; пишет БОТ). По lead_id ASC,
# cap последними THREAD_CAP (берём «хвост» — последние N, затем разворачиваем в
# хронологию для отрисовки). text рендерится |e (autoescape) — здесь не трогаем.
# --------------------------------------------------------------------------- #
async def get_thread(lead_id, *, cap: int) -> list[asyncpg.Record]:
    """Лента переписки лида (вход/исход), последние `cap`, в хронологическом порядке.

    Берём последние cap по created_at DESC (limit), затем переворачиваем в Python,
    чтобы старые были сверху, новые снизу — без отдельного индекса по возрастанию.
    """
    q = """
        select id, direction, kind, text, file_id, source, created_at, tg_message_id
        from messages
        where lead_id = $1
        order by created_at desc, id desc
        limit $2
    """
    async with pool.acquire() as c:
        rows = await c.fetch(q, lead_id, cap)
    return list(reversed(rows))


# --------------------------------------------------------------------------- #
# Ручной ответ оператора → INSERT в outbox ('queued'). НЕ прямой Telegram:
# реально шлёт бот (worker-дренаж). tg_user_id денормализуем из лида (грант
# панели — без update на phone/tg_user_id, читать можно). Аудит manual_reply
# с {len} (без текста) в той же транзакции. Возвращает (outbox_id) или None.
# --------------------------------------------------------------------------- #
async def enqueue_manual_reply(
    lead_id,
    *,
    text: str,
    actor: str,
    ip: str | None,
    user_agent: str | None,
    attachments: list[dict] | None = None,
) -> int | None:
    """Поставить ручной ответ в outbox. None, если лид не найден / без tg_user_id / пусто.

    Ответ может нести ТЕКСТ и/или НЕСКОЛЬКО вложений (файлы + голос). Telegram шлёт
    каждое вложение ОТДЕЛЬНЫМ сообщением (документ+голос не комбинируются), поэтому
    кладём по строке outbox на «кусок»: сначала текстовая строка (если есть текст),
    затем по строке на каждое вложение в порядке списка (порядок отправки = возрастание
    outbox.id). Текст ОТДЕЛЬНОЙ строкой, а не подписью к файлу — чтобы длинный текст не
    резался лимитом caption (1024) и порядок был предсказуем.

    attachments = [{"kind","bytes","name","mime"}, …]; kind ('photo'|'document'|'voice'|
    'audio') проставил хендлер по подтверждённому magic-byte MIME. Байты кладёт панель;
    file_id проставит БОТ после заливки в OPS_CHAT_ID (как у продуктов). Адресность
    (tg_user_id) проверяем здесь; erase-фильтр + re-check бот делает перед отправкой
    (§5.10). Аудит — БЕЗ байтов и текста: только len текста и виды вложений. Возвращает
    число поставленных строк (≥1) или None если лиду нельзя написать / нечего слать.
    """
    attachments = attachments or []
    if not text and not attachments:
        return None
    async with pool.acquire() as c:
        async with c.transaction():
            lead = await c.fetchrow(
                "select tg_user_id from leads where id = $1 for update", lead_id
            )
            if lead is None or lead["tg_user_id"] is None:
                return None  # нет лида/адреса → ничего не ставим (бот всё равно не отправит)
            tg = lead["tg_user_id"]
            count = 0
            if text:
                await c.execute(
                    "insert into outbox (lead_id, tg_user_id, kind, text, status, created_by) "
                    "values ($1, $2, 'text', $3, 'queued', $4)",
                    lead_id, tg, text, actor,
                )
                count += 1
            for a in attachments:
                await c.execute(
                    """
                    insert into outbox
                        (lead_id, tg_user_id, kind, text, status, created_by,
                         file_bytes, file_name, file_mime)
                    values ($1, $2, $3, null, 'queued', $4, $5, $6, $7)
                    """,
                    lead_id, tg, a["kind"], actor, a["bytes"], a["name"], a["mime"],
                )
                count += 1
            await _insert_audit(
                c, actor=actor, action="manual_reply", lead_id=lead_id,
                ip=ip, user_agent=user_agent,
                detail={"rows": count, "len": len(text or ""),
                        "attachments": [a["kind"] for a in attachments]},
            )
            return count


# =========================================================================== #
# Блок РАССЫЛКИ: композер + CRUD заявки + аналитика.
#
# Аудитория описывается ПОДМНОЖЕСТВОМ build_filters (messenger/source/consent/
# exclude_unsubscribed) — НЕ сырой SQL. Канон «кому можно» (consent/tg_user_id/
# unsubscribed/erase) бот применяет повторно при материализации (§5.1). Панель
# даёт предпросмотр количества тем же фильтром, что бот возьмёт как snapshot-базу.
# =========================================================================== #

# Канон значений messenger рассылки. tg активна; max — disabled-задел (план §11.4).
BROADCAST_MESSENGERS: tuple[str, ...] = ("tg",)            # реально отправляемые
_BROADCAST_MESSENGER_SET = frozenset(BROADCAST_MESSENGERS)
BROADCAST_STATUSES: tuple[str, ...] = (
    "draft", "queued", "sending", "paused", "done", "canceled",
)
_BROADCAST_STATUS_SET = frozenset(BROADCAST_STATUSES)


def _broadcast_audience_where(
    audience: dict[str, Any], *, start_idx: int = 1
) -> tuple[str, list[Any], int]:
    """WHERE «кандидаты рассылки» из ПОДМНОЖЕСТВА фильтров аудитории.

    Жёсткое ядро «кому МОЖНО слать» (план §5.1) ВСЕГДА включено — даже если панель
    передаст пустую аудиторию, отправка останется в рамках согласия/подписки:
        messenger='tg' and tg_user_id is not null and consent = true
        and erase_requested_at is null and bot_paused = false
    ВАЖНО: ядро должно ПОБАЙТОВО совпадать с _AUDIENCE_WHERE бота (bot-telegram/db.py),
    включая bot_paused=false (план §11.2, решение владельца ДА — промо не идёт поверх
    живого ручного диалога). Иначе предпросмотр/cap-гейт/recipient_count считаются по
    суперсету, а бот разошлёт МЕНЬШЕ — «точное число» при превышении лимита и аудит
    разъезжаются с реальностью. Поверх ядра — операторские сужения (source/status) и
    опц. исключение отписанных (exclude_unsubscribed, вкл. по умолчанию). Значения через
    allow-list+$-плейсхолдеры. Возвращает (where_sql, params, next_idx).
    """
    clauses: list[str] = [
        "messenger = 'tg'",
        "tg_user_id is not null",
        "consent = true",
        "erase_requested_at is null",
        "bot_paused = false",
    ]
    params: list[Any] = []
    i = start_idx

    def add(clause_tpl: str, value: Any) -> None:
        nonlocal i
        clauses.append(clause_tpl.format(i=i))
        params.append(value)
        i += 1

    source = audience.get("source")
    if source is not None and source in _SOURCE_SET:
        add("source = ${i}", source)
    status = audience.get("status")
    if status is not None and status in _STATUS_SET:
        add("status = ${i}", status)

    # Исключить отписанных (по умолчанию True). Если оператор снимет — отписанные
    # всё равно НЕ получат: бот режет unsubscribed на своей стороне (§5.1). Здесь —
    # лишь честный предпросмотр и snapshot-база.
    if audience.get("exclude_unsubscribed", True):
        clauses.append("unsubscribed_at is null")

    return " and ".join(clauses), params, i


async def count_broadcast_audience(audience: dict[str, Any]) -> int:
    """Предпросмотр числа получателей по фильтру аудитории (тем же WHERE, что бот)."""
    where_sql, params, _ = _broadcast_audience_where(audience)
    q = f"select count(*) from leads where {where_sql}"
    async with pool.acquire() as c:
        return int(await c.fetchval(q, *params))


async def count_recent_draft_broadcasts(*, within_hours: int) -> int:
    """Сколько черновиков рассылок создано за окно (анти-флуд композера, §6.5)."""
    q = """
        select count(*) from broadcasts
        where created_at >= now() - ($1 || ' hours')::interval
    """
    async with pool.acquire() as c:
        return int(await c.fetchval(q, str(within_hours)))


async def create_broadcast_with_audit(
    *,
    title: str | None,
    messenger: str,
    kind: str,
    body_template: str,
    audience: dict[str, Any],
    recipient_estimate: int,
    file_meta: dict[str, Any] | None,
    target_url: str | None,
    product_id: int | None = None,
    actor: str,
    ip: str | None,
    user_agent: str | None,
) -> int:
    """Создать черновик рассылки + опц. файл + опц. трекинг-токен + опц. привязку офера.
    Аудит broadcast_create.

    ОДНА транзакция: insert broadcasts(status='draft') → опц. insert broadcast_files(bytes)
    → опц. insert link_tokens(token, target_url, broadcast_id) → _insert_audit. Получателей
    НЕ материализует (это делает бот при queued, §5.2). Возвращает broadcasts.id.

    messenger/kind валидируются здесь как defence-in-depth (хендлер тоже проверит).
    audience_filter кладём как jsonb — подмножество фильтров, не сырой SQL.
    product_id (опц.): привязка офера из каталога. Пишем ТОЛЬКО если офер существует и
    status='active' — иначе тихо null (хендлер мусор уже отфильтровал, здесь финальный
    барьер «не связать архивный/несуществующий»). broadcasts.product_id есть on delete set null.
    """
    if messenger not in _BROADCAST_MESSENGER_SET:
        raise ValueError(f"Недопустимый мессенджер рассылки: {messenger!r}")
    audience_json = json.dumps(audience, ensure_ascii=False, default=_json_default)
    async with pool.acquire() as c:
        async with c.transaction():
            # Привязка офера: подтверждаем active в той же транзакции; иначе null.
            product_ref: int | None = None
            if product_id is not None:
                ok = await c.fetchval(
                    "select 1 from products where id = $1 and status = 'active'", product_id
                )
                product_ref = product_id if ok else None
            bid = await c.fetchval(
                """
                insert into broadcasts
                    (title, messenger, kind, body_template, audience_filter,
                     status, recipient_count, created_by, product_id)
                values ($1, $2, $3, $4, $5::jsonb, 'draft', null, $6, $7)
                returning id
                """,
                title, messenger, kind, body_template, audience_json, actor, product_ref,
            )
            bid = int(bid)
            if file_meta is not None:
                await c.execute(
                    """
                    insert into broadcast_files (broadcast_id, filename, mime, bytes)
                    values ($1, $2, $3, $4)
                    """,
                    bid, file_meta.get("filename"), file_meta.get("mime"),
                    file_meta.get("bytes"),
                )
            token: str | None = None
            if target_url:
                token = secrets.token_urlsafe(16)
                await c.execute(
                    """
                    insert into link_tokens (token, target_url, broadcast_id)
                    values ($1, $2, $3)
                    """,
                    token, target_url, bid,
                )
            await _insert_audit(
                c, actor=actor, action="broadcast_create", ip=ip, user_agent=user_agent,
                detail={
                    "broadcast_id": bid,
                    "messenger": messenger,
                    "kind": kind,
                    # аудитория — факт фильтров, без значений ПДн (их тут и нет):
                    "audience": {k: audience.get(k) for k in
                                 ("source", "status", "exclude_unsubscribed")
                                 if k in audience},
                    "recipient_estimate": int(recipient_estimate),
                    "has_file": file_meta is not None,
                    "has_link": bool(target_url),
                    "has_product": product_ref is not None,
                },
            )
            return bid


async def list_broadcasts(*, limit: int, offset: int) -> list[asyncpg.Record]:
    """Список рассылок со сводными счётчиками получателей (для раздела /broadcasts).

    Счётчики берём агрегатом по broadcast_recipients (пишет бот). Для свежесозданных
    draft без получателей — нули. recipient_count (план) — материализуется ботом.
    """
    q = """
        select
            b.id, b.title, b.messenger, b.kind, b.status,
            b.recipient_count, b.created_by, b.created_at,
            b.started_at, b.finished_at,
            coalesce(r.total, 0)   as r_total,
            coalesce(r.sent, 0)    as r_sent,
            coalesce(r.failed, 0)  as r_failed,
            coalesce(r.skipped, 0) as r_skipped
        from broadcasts b
        left join (
            select broadcast_id,
                   count(*)                            as total,
                   count(*) filter (where status = 'sent')    as sent,
                   count(*) filter (where status = 'failed')  as failed,
                   count(*) filter (where status = 'skipped') as skipped
            from broadcast_recipients
            group by broadcast_id
        ) r on r.broadcast_id = b.id
        order by b.created_at desc
        limit $1 offset $2
    """
    async with pool.acquire() as c:
        return await c.fetch(q, limit, offset)


async def count_broadcasts() -> int:
    async with pool.acquire() as c:
        return int(await c.fetchval("select count(*) from broadcasts"))


async def get_broadcast(broadcast_id: int) -> asyncpg.Record | None:
    """Карточка рассылки. id типизирован int в хендлере → мусор не дойдёт до SQL."""
    q = """
        select id, title, messenger, kind, body_template, audience_filter,
               status, recipient_count, created_by, created_at,
               started_at, finished_at, totals, product_id
        from broadcasts
        where id = $1
    """
    async with pool.acquire() as c:
        return await c.fetchrow(q, broadcast_id)


def decode_audience(audience_filter: Any) -> dict[str, Any]:
    """audience_filter из БД → dict. asyncpg отдаёт jsonb СТРОКОЙ (кодек не зарегистрирован,
    как и для survey/totals — пишем json.dumps + ::jsonb). Парсим безопасно; мусор/None → {}.
    Совместимо и со случаем, если когда-нибудь подключат json-кодек (тогда уже dict)."""
    if audience_filter is None:
        return {}
    if isinstance(audience_filter, dict):
        return audience_filter
    if isinstance(audience_filter, (str, bytes)):
        try:
            val = json.loads(audience_filter)
            return val if isinstance(val, dict) else {}
        except (ValueError, TypeError):
            return {}
    return {}


async def broadcast_recipient_stats(broadcast_id: int) -> asyncpg.Record:
    """Честные 4 метрики (§6.1): отправлено/не доставлено/клики/отписки + всего.

    sent/failed/skipped — count filter по broadcast_recipients (пишет бот). clicks —
    отдельный count по link_clicks в окне [started_at, +∞) (TTL-окно для CTR считает
    хендлер). unsubs — отписки после старта рассылки. «Открытий» НЕТ — Telegram их
    боту не отдаёт (план §6.1).
    """
    q = """
        select
            count(*)                                   as total,
            count(*) filter (where status = 'sent')    as sent,
            count(*) filter (where status = 'failed')  as failed,
            count(*) filter (where status = 'pending') as pending,
            count(*) filter (where status = 'sending') as sending,
            count(*) filter (where status = 'skipped') as skipped
        from broadcast_recipients
        where broadcast_id = $1
    """
    async with pool.acquire() as c:
        return await c.fetchrow(q, broadcast_id)


async def broadcast_click_count(broadcast_id: int) -> int:
    """Число кликов по трекинг-ссылке рассылки (если ссылка есть). Знаменатель CTR=sent."""
    async with pool.acquire() as c:
        return int(await c.fetchval(
            "select count(*) from link_clicks where broadcast_id = $1", broadcast_id
        ))


async def broadcast_unsub_count(broadcast_id: int) -> int:
    """Отписки среди получателей рассылки ПОСЛЕ её старта (§6.1).

    Считаем лидов-получателей этой рассылки, у кого unsubscribed_at >= started_at.
    Если рассылка ещё не стартовала — 0.
    """
    q = """
        select count(*)
        from broadcast_recipients br
        join broadcasts b on b.id = br.broadcast_id
        join leads l on l.id = br.lead_id
        where br.broadcast_id = $1
          and b.started_at is not null
          and l.unsubscribed_at is not null
          and l.unsubscribed_at >= b.started_at
    """
    async with pool.acquire() as c:
        return int(await c.fetchval(q, broadcast_id))


async def list_broadcast_recipients(
    broadcast_id: int, *, limit: int, offset: int
) -> list[asyncpg.Record]:
    """Получатели рассылки для детального разреза (телефон маскируется в шаблоне).

    НЕ селектит сырой phone — только хвост phone_tail (2 цифры) прямо в SQL, как
    список лидов. click отмечаем подзапросом exists по link_clicks на per-recipient
    токен. status — фактический исход (sent|failed|skipped|pending|sending).
    """
    q = """
        select
            br.lead_id, br.tg_user_id, br.status, br.error, br.sent_at,
            l.name,
            right(regexp_replace(coalesce(l.phone,''), '\\D', '', 'g'), 2) as phone_tail,
            l.phone is not null and l.phone <> '' as has_phone,
            exists (
                select 1 from link_clicks lc
                where lc.token = br.click_token
            ) as clicked
        from broadcast_recipients br
        join leads l on l.id = br.lead_id
        where br.broadcast_id = $1
        order by br.status, br.sent_at desc nulls last, br.id
        limit $2 offset $3
    """
    async with pool.acquire() as c:
        return await c.fetch(q, broadcast_id, limit, offset)


async def broadcast_link(broadcast_id: int) -> asyncpg.Record | None:
    """Трекинг-ссылка рассылки (target_url), если регистрировалась. Для UI аналитики."""
    q = """
        select target_url, count(*) over () as n
        from link_tokens
        where broadcast_id = $1
        limit 1
    """
    async with pool.acquire() as c:
        return await c.fetchrow(q, broadcast_id)


# --------------------------------------------------------------------------- #
# ЗАПУСК рассылки (мощное действие, §7.1): draft→queued ровно один раз +
# recipient_count пишется ДО старта + аудит broadcast_send. Защита от двойного
# запуска — условный UPDATE … where status='draft' returning (0 строк → 409).
# Получателей материализует БОТ при подхвате queued (панель их не пишет).
# --------------------------------------------------------------------------- #
async def queue_broadcast_with_audit(
    broadcast_id: int,
    *,
    recipient_count: int,
    actor: str,
    ip: str | None,
    user_agent: str | None,
) -> str | None:
    """Перевести draft→queued + записать recipient_count + аудит broadcast_send.

    Возвращает:
      * "queued"   — успех (была draft, стала queued);
      * "conflict" — статус был не draft (уже запущена/отменена) → хендлер отдаёт 409;
      * None       — рассылки нет → 404.
    Условный UPDATE атомарно отсекает двойной запуск (гонка двух вкладок).
    """
    async with pool.acquire() as c:
        async with c.transaction():
            exists = await c.fetchrow(
                "select status from broadcasts where id = $1 for update", broadcast_id
            )
            if exists is None:
                return None
            row = await c.fetchrow(
                """
                update broadcasts
                set status = 'queued', recipient_count = $2
                where id = $1 and status = 'draft'
                returning id
                """,
                broadcast_id, recipient_count,
            )
            if row is None:
                return "conflict"  # статус был не draft
            await _insert_audit(
                c, actor=actor, action="broadcast_send", ip=ip, user_agent=user_agent,
                detail={"broadcast_id": broadcast_id, "recipient_count": recipient_count},
            )
            return "queued"


async def resume_broadcast_with_audit(
    broadcast_id: int,
    *,
    actor: str,
    ip: str | None,
    user_agent: str | None,
) -> str | None:
    """Возобновить ПРИОСТАНОВЛЕННУЮ рассылку: paused→sending. Аудит broadcast_resume.

    Закрывает терминальный тупик: circuit-breaker (§5.9) и «файл не готов» (§5.5) ставят
    broadcasts.status='paused', а воркёр подхватывает только 'queued'/'sending'. Без этого
    пути транзиентный всплеск (краткий сбой Telegram в начале кампании) замораживал рассылку
    навсегда — оставались неотправленные pending. Условный UPDATE … where status='paused'
    атомарно отсекает гонку/повтор. Получателей НЕ перематериализуем (snapshot уже есть);
    воркёр доберёт оставшиеся pending. recipient_count трогать не нужно — он стоит с запуска.

    Грант: update on broadcasts (panel_rw) уже покрывает. Возвращает
    "resumed" | "conflict" (статус был не paused) | None (404), как queue_broadcast_with_audit.
    """
    async with pool.acquire() as c:
        async with c.transaction():
            exists = await c.fetchrow(
                "select status from broadcasts where id = $1 for update", broadcast_id
            )
            if exists is None:
                return None
            row = await c.fetchrow(
                """
                update broadcasts set status = 'sending'
                where id = $1 and status = 'paused'
                returning id
                """,
                broadcast_id,
            )
            if row is None:
                return "conflict"  # статус был не paused (draft/queued/sending/done/canceled)
            await _insert_audit(
                c, actor=actor, action="broadcast_resume", ip=ip, user_agent=user_agent,
                detail={"broadcast_id": broadcast_id},
            )
            return "resumed"


async def cancel_broadcast_with_audit(
    broadcast_id: int,
    *,
    actor: str,
    ip: str | None,
    user_agent: str | None,
) -> str | None:
    """Отменить рассылку: → canceled из состояний draft|queued|paused. Аудит broadcast_cancel.

    sending не отменяем здесь как «стоп-кран на лету» (это делает бот через paused);
    из панели отменяем только ещё-не-идущие (draft|queued|paused). Возвращает
    "canceled" | "conflict" | None (404), как queue_broadcast_with_audit.
    """
    async with pool.acquire() as c:
        async with c.transaction():
            exists = await c.fetchrow(
                "select status from broadcasts where id = $1 for update", broadcast_id
            )
            if exists is None:
                return None
            row = await c.fetchrow(
                """
                update broadcasts set status = 'canceled'
                where id = $1 and status in ('draft', 'queued', 'paused')
                returning id
                """,
                broadcast_id,
            )
            if row is None:
                return "conflict"
            await _insert_audit(
                c, actor=actor, action="broadcast_cancel", ip=ip, user_agent=user_agent,
                detail={"broadcast_id": broadcast_id, "from_status": exists["status"]},
            )
            return "canceled"


# =========================================================================== #
# Блок ПРОДУКТЫ (каталог оферов): CRUD + список для селектора композера + привязка
# офера к рассылке. Объекты — db/schema_products.sql, гранты — db/panel_role.sql.
#
# Инвариант (panel_rw, без BOT_TOKEN):
#   • Панель пишет name/kind/price/currency/caption/link/file(+name/mime)/status и
#     created_by — это ровно столбцы из `grant insert/update … on products`. Колонку
#     file_tg_id панель НЕ пишет (её проставляет БОТ после первой заливки в OPS_CHAT_ID);
#     отсутствие file_tg_id в наших SQL — НЕ забывчивость, а соблюдение column-level гранта.
#   • Байты файла кладём в products.file; бот зальёт в Telegram и обнулит bytes.
#   • «Файл И/ИЛИ ссылка, но хотя бы одно» проверяет ХЕНДЛЕР (UX-сообщение), не CHECK.
#   • Архивация — status='archived' (delete на products панели не выдан, строки живут).
#   • Все мутации — В ОДНОЙ транзакции с аудитом (product_create|product_update|
#     product_archive), detail без ПДн (это контент-офер, ПДн субъектов не несёт).
# =========================================================================== #

async def list_products(*, include_archived: bool = True) -> list[asyncpg.Record]:
    """Список оферов для раздела «Продукты» (карточки). НЕ селектит сырые байты file —
    только наличие файла (file is not null OR file_tg_id is not null) и метаданные.

    include_archived=True показывает и архивные (с бейджем). Сортировка: активные
    раньше архивных, внутри — свежие сверху (products_created_idx).
    """
    where = "" if include_archived else "where status = 'active'"
    q = f"""
        select
            id, name, kind, price, currency, caption, link,
            (file is not null or file_tg_id is not null) as has_file,
            file_tg_id is not null as file_ready,
            file_name, file_mime, status, created_by, created_at, updated_at
        from products
        {where}
        order by (status = 'active') desc, created_at desc
    """
    async with pool.acquire() as c:
        return await c.fetch(q)


async def get_product(product_id: int) -> asyncpg.Record | None:
    """Карточка/конструктор офера. НЕ селектит сырые байты file (могут быть мегабайты) —
    только наличие/готовность файла + метаданные. id типизирован int в хендлере.
    """
    q = """
        select
            id, name, kind, price, currency, caption, link,
            (file is not null or file_tg_id is not null) as has_file,
            file is not null as has_bytes,
            file_tg_id is not null as file_ready,
            length(file) as file_size,
            file_name, file_mime, status, created_by, created_at, updated_at
        from products
        where id = $1
    """
    async with pool.acquire() as c:
        return await c.fetchrow(q, product_id)


async def list_products_for_select() -> list[asyncpg.Record]:
    """Активные оферы для селектора в композере рассылки. Лёгкая выборка (без байт).

    Только active (архивные нельзя привязать к новой рассылке). Метаданные для
    предпросмотра выбранного офера прямо в форме (название/вид/цена/файл/ссылка).
    """
    q = """
        select
            id, name, kind, price, currency, caption, link,
            (file is not null or file_tg_id is not null) as has_file,
            file_name, file_mime
        from products
        where status = 'active'
        order by kind, created_at desc
    """
    async with pool.acquire() as c:
        return await c.fetch(q)


def _validate_product_fields(
    *, kind: str, currency: str, status: str
) -> None:
    """Defence-in-depth: вид/валюта/статус против allow-list ДО SQL (CHECK тоже ловит)."""
    if kind not in _PRODUCT_KIND_SET:
        raise ValueError(f"Недопустимый вид продукта: {kind!r}")
    if currency not in _PRODUCT_CURRENCY_SET:
        raise ValueError(f"Недопустимая валюта: {currency!r}")
    if status not in _PRODUCT_STATUS_SET:
        raise ValueError(f"Недопустимый статус продукта: {status!r}")


def _product_audit_detail(
    *, product_id: int, kind: str, status: str,
    has_price: bool, has_link: bool, file_action: str,
) -> dict:
    """detail аудита продукта БЕЗ ПДн/байт: только факты (вид/статус/наличие полей)."""
    return {
        "product_id": product_id,
        "kind": kind,
        "status": status,
        "has_price": has_price,
        "has_link": has_link,
        "file": file_action,  # 'kept' | 'replaced' | 'cleared' | 'none' | 'added'
    }


async def create_product_with_audit(
    *,
    name: str,
    kind: str,
    price,                       # Decimal | None
    currency: str,
    caption: str | None,
    link: str | None,
    file_meta: dict | None,     # {"bytes","filename","mime"} | None
    status: str,
    actor: str,
    ip: str | None,
    user_agent: str | None,
) -> int:
    """Создать офер + опц. байты файла. Аудит product_create в той же транзакции.

    file_tg_id НЕ трогаем (его проставит бот). price — numeric(12,2) или NULL.
    Возвращает products.id. Хотя бы одно из (file|link) гарантирует ХЕНДЛЕР.
    """
    _validate_product_fields(kind=kind, currency=currency, status=status)
    fb = file_meta or {}
    file_bytes = fb.get("bytes")
    async with pool.acquire() as c:
        async with c.transaction():
            pid = await c.fetchval(
                """
                insert into products
                    (name, kind, price, currency, caption, link,
                     file, file_name, file_mime, status, created_by)
                values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                returning id
                """,
                name, kind, price, currency, caption, link,
                file_bytes, fb.get("filename"), fb.get("mime"), status, actor,
            )
            pid = int(pid)
            await _insert_audit(
                c, actor=actor, action="product_create", ip=ip, user_agent=user_agent,
                detail=_product_audit_detail(
                    product_id=pid, kind=kind, status=status,
                    has_price=price is not None, has_link=bool(link),
                    file_action="added" if file_bytes else "none",
                ),
            )
            return pid


async def update_product_with_audit(
    product_id: int,
    *,
    name: str,
    kind: str,
    price,                       # Decimal | None
    currency: str,
    caption: str | None,
    link: str | None,
    file_meta: dict | None,     # {"bytes","filename","mime"} | None — новый файл (заменить)
    clear_file: bool,           # True → снять текущий файл (и bytes, и сбросить метаданные)
    status: str,
    actor: str,
    ip: str | None,
    user_agent: str | None,
) -> asyncpg.Record | None:
    """Обновить офер. Аудит product_update в той же транзакции. None, если офер не найден.

    Семантика файла (панель НЕ пишет file_tg_id — это колонка бота):
      • file_meta задан → ЗАМЕНА: пишем новые file/file_name/file_mime. (file_tg_id у
        бота протух бы — но его сброс делает БОТ под owner-ролью; панель его не трогает.
        Поэтому замена файла из панели рассчитана на оферы БЕЗ ещё-готового file_tg_id;
        для уже залитых правьте через архив+новый офер — см. UX-подсказку в форме.)
      • clear_file=True и нет нового файла → снимаем bytes и метаданные (file=null,
        file_name=null, file_mime=null). file_tg_id (если бот успел) НЕ трогаем грантом.
      • иначе → файл не меняем (правка только текстовых полей/цены/ссылки/статуса).
    «Хотя бы одно из (file|link)» проверяет ХЕНДЛЕР с учётом текущего состояния офера.
    """
    _validate_product_fields(kind=kind, currency=currency, status=status)
    fb = file_meta or {}
    new_bytes = fb.get("bytes")
    async with pool.acquire() as c:
        async with c.transaction():
            old = await c.fetchrow(
                "select id, status from products where id = $1 for update", product_id
            )
            if old is None:
                return None
            if new_bytes:
                file_action = "replaced"
                # Новый файл → file_tg_id протух; его сброс делает БОТ (column-level грант),
                # но счётчик попыток заливки СБРАСЫВАЕМ в 0 здесь: новый файл заслуживает
                # свежий бюджет, иначе исчерпанный старым битым файлом upload_attempts
                # навсегда исключил бы новый годный файл из очереди заливки.
                row = await c.fetchrow(
                    """
                    update products set
                        name = $2, kind = $3, price = $4, currency = $5,
                        caption = $6, link = $7, status = $8,
                        file = $9, file_name = $10, file_mime = $11,
                        upload_attempts = 0, upload_error = null
                    where id = $1
                    returning id, name, kind, status, updated_at
                    """,
                    product_id, name, kind, price, currency, caption, link, status,
                    new_bytes, fb.get("filename"), fb.get("mime"),
                )
            elif clear_file:
                file_action = "cleared"
                # Файл снят → очереди заливки больше нет; счётчик попыток обнуляем для чистоты.
                row = await c.fetchrow(
                    """
                    update products set
                        name = $2, kind = $3, price = $4, currency = $5,
                        caption = $6, link = $7, status = $8,
                        file = null, file_name = null, file_mime = null,
                        upload_attempts = 0, upload_error = null
                    where id = $1
                    returning id, name, kind, status, updated_at
                    """,
                    product_id, name, kind, price, currency, caption, link, status,
                )
            else:
                file_action = "kept"
                row = await c.fetchrow(
                    """
                    update products set
                        name = $2, kind = $3, price = $4, currency = $5,
                        caption = $6, link = $7, status = $8
                    where id = $1
                    returning id, name, kind, status, updated_at
                    """,
                    product_id, name, kind, price, currency, caption, link, status,
                )
            await _insert_audit(
                c, actor=actor, action="product_update", ip=ip, user_agent=user_agent,
                detail=_product_audit_detail(
                    product_id=product_id, kind=kind, status=status,
                    has_price=price is not None, has_link=bool(link),
                    file_action=file_action,
                ),
            )
            return row


async def archive_product_with_audit(
    product_id: int,
    *,
    actor: str,
    ip: str | None,
    user_agent: str | None,
) -> str | None:
    """Архивировать офер: status='active'→'archived'. Аудит product_archive.

    Условный UPDATE … where status='active' атомарно отсекает повтор/гонку. Возвращает
    "archived" | "conflict" (был уже archived) | None (404). delete не используем — офер
    остаётся (ссылки/история рассылок не рвутся), просто скрыт из выбора композера.
    """
    async with pool.acquire() as c:
        async with c.transaction():
            exists = await c.fetchrow(
                "select status from products where id = $1 for update", product_id
            )
            if exists is None:
                return None
            row = await c.fetchrow(
                """
                update products set status = 'archived'
                where id = $1 and status = 'active'
                returning id
                """,
                product_id,
            )
            if row is None:
                return "conflict"
            await _insert_audit(
                c, actor=actor, action="product_archive", ip=ip, user_agent=user_agent,
                detail={"product_id": product_id, "from_status": exists["status"]},
            )
            return "archived"


async def set_broadcast_product_with_audit(
    broadcast_id: int,
    product_id: int | None,
    *,
    actor: str,
    ip: str | None,
    user_agent: str | None,
) -> str | None:
    """Привязать/отвязать офер к УЖЕ существующему черновику: UPDATE broadcasts.product_id.

    Используется маршрутом /broadcasts/{id}/product (сменить/снять офер на ещё не
    запущенной рассылке). Только status='draft' — после queued состав сообщения
    зафиксирован. product_id=None отвязывает. Если задан — проверяем, что офер
    существует и active (нельзя привязать архивный/несуществующий). Возвращает:
      "set" | "conflict" (рассылка не draft) | "bad_product" (офер не active/нет) |
      None (рассылки нет, 404). Аудит broadcast_product_set. Грант — update(product_id).
    """
    async with pool.acquire() as c:
        async with c.transaction():
            b = await c.fetchrow(
                "select status from broadcasts where id = $1 for update", broadcast_id
            )
            if b is None:
                return None
            if b["status"] != "draft":
                return "conflict"
            if product_id is not None:
                ok = await c.fetchval(
                    "select 1 from products where id = $1 and status = 'active'", product_id
                )
                if not ok:
                    return "bad_product"
            await c.execute(
                "update broadcasts set product_id = $2 where id = $1",
                broadcast_id, product_id,
            )
            await _insert_audit(
                c, actor=actor, action="broadcast_product_set", ip=ip, user_agent=user_agent,
                detail={"broadcast_id": broadcast_id, "product_id": product_id},
            )
            return "set"


# =========================================================================== #
# Блок APP_SETTINGS: singleton-настройки панели (KV). Грант — select/insert/update
# на app_settings (panel_role.sql). Сейчас единственный ключ — активный лид-магнит-
# офер воронки (ЗАМЕНА GUIDE_URL-заглушки): панель ПИШЕТ, бот ЧИТАЕТ
# (bot-telegram/db.py::get_active_lead_magnet_product). Запись симметрична чтению
# бота: валидируем kind='lead_magnet' и status='active' ДО upsert, иначе бот-сторона
# всё равно отвергнет значение и воронка молча уйдёт на GUIDE_URL.
# =========================================================================== #

# Ключ singleton-настройки «активный лид-магнит-офер воронки». ДОЛЖЕН совпадать с
# ключом, который читает бот (bot-telegram/db.py::get_active_lead_magnet_product).
LEAD_MAGNET_SETTING_KEY = "active_lead_magnet_product_id"


async def get_app_setting(key: str) -> str | None:
    """Значение singleton-настройки по ключу (или None). value — text (KV-универсальность).

    Зеркалит bot-telegram/db.py::get_app_setting — но под panel_rw (тот же грант select).
    """
    async with pool.acquire() as c:
        return await c.fetchval("select value from app_settings where key = $1", key)


async def get_active_lead_magnet() -> asyncpg.Record | None:
    """Текущий активный лид-магнит-офер воронки для индикации в UI (или None).

    Читает app_settings[LEAD_MAGNET_SETTING_KEY], приводит к id и возвращает строку
    продукта (id/name/kind/status/наличие файла/ссылки), чтобы products.html показал,
    какой офер сейчас выдаётся воронкой. Невалидное/устаревшее значение (пусто/мусор/
    продукт удалён) → None (UI покажет «не назначен»). НЕ чистит мусорное значение —
    только читает (бот-сторона и так трактует промах как фолбэк на GUIDE_URL).
    """
    raw = await get_app_setting(LEAD_MAGNET_SETTING_KEY)
    if not raw:
        return None
    try:
        product_id = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    async with pool.acquire() as c:
        return await c.fetchrow(
            """
            select id, name, kind, status,
                   (file is not null or file_tg_id is not null) as has_file,
                   file_tg_id is not null as file_ready, link
            from products
            where id = $1
            """,
            product_id,
        )


async def set_active_lead_magnet_with_audit(
    product_id: int | None,
    *,
    actor: str,
    ip: str | None,
    user_agent: str | None,
) -> str:
    """Назначить/снять активный лид-магнит-офер воронки (upsert app_settings + аудит).

    product_id=None → снять (удаляем строку настройки → бот фолбэчит на GUIDE_URL).
    Иначе валидируем СИММЕТРИЧНО боту: офер существует, kind='lead_magnet', status=
    'active' и у него есть чем выдавать (file/file_tg_id ИЛИ link) — иначе бот всё равно
    вернёт фолбэк, поэтому не даём назначить «пустой»/не-тот офер (понятная ошибка в UI).
    Возвращает: "set" | "cleared" | "bad_product" (не лид-магнит/не active/нечем выдавать/
    нет такого). Запись и аудит — в ОДНОЙ транзакции (паттерн остальных мутаций).
    Грант: select/insert/update on app_settings (panel_role.sql) — delete тоже покрыт?
    НЕТ: delete на app_settings панели НЕ выдан, поэтому «снять» = upsert пустого value
    (бот трактует пустое значение как отсутствие настройки → фолбэк), строку не удаляем.
    """
    async with pool.acquire() as c:
        async with c.transaction():
            if product_id is None:
                # «Снять»: пишем пустое value (delete на app_settings не грантован панели).
                # get_active_lead_magnet_product у бота на пустом value возвращает None → GUIDE_URL.
                await c.execute(
                    """
                    insert into app_settings (key, value) values ($1, '')
                    on conflict (key) do update set value = excluded.value
                    """,
                    LEAD_MAGNET_SETTING_KEY,
                )
                await _insert_audit(
                    c, actor=actor, action="lead_magnet_set", ip=ip, user_agent=user_agent,
                    detail={"product_id": None, "cleared": True},
                )
                return "cleared"
            # Назначение: подтверждаем lead_magnet+active+есть-чем-выдавать в той же транзакции.
            ok = await c.fetchrow(
                """
                select (file is not null or file_tg_id is not null) as has_file, link
                from products
                where id = $1 and kind = 'lead_magnet' and status = 'active'
                """,
                product_id,
            )
            if ok is None:
                return "bad_product"
            if not ok["has_file"] and not (ok["link"] or "").strip():
                # Ни файла, ни ссылки — выдавать нечем, бот вернёт фолбэк → не назначаем.
                return "bad_product"
            await c.execute(
                """
                insert into app_settings (key, value) values ($1, $2)
                on conflict (key) do update set value = excluded.value
                """,
                LEAD_MAGNET_SETTING_KEY, str(product_id),
            )
            await _insert_audit(
                c, actor=actor, action="lead_magnet_set", ip=ip, user_agent=user_agent,
                detail={"product_id": product_id, "cleared": False},
            )
            return "set"


async def list_lead_magnet_products() -> list[asyncpg.Record]:
    """Активные лид-магнит-оферы, ГОДНЫЕ в выдачу воронки (есть файл ИЛИ ссылка) — для
    селектора «Выдавать в воронке» на /products. Узкая выборка (без байт)."""
    q = """
        select id, name,
               (file is not null or file_tg_id is not null) as has_file,
               file_tg_id is not null as file_ready, link
        from products
        where kind = 'lead_magnet' and status = 'active'
          and (file is not null or file_tg_id is not null or link is not null)
        order by created_at desc
    """
    async with pool.acquire() as c:
        return await c.fetch(q)


# =========================================================================== #
# Блок ИИ-АГЕНТЫ (раздел «ИИ-агенты»): настройки Лии в app_settings (KV) +
# просмотр её ответов. Панель ПИШЕТ ключи (ai_enabled/ai_agent_id/ai_fallback_text),
# бот ЧИТАЕТ их ПОВЕРХ env (bot-telegram/db.py::get_ai_overrides). Токен Timeweb AI
# здесь НЕ хранится (секрет → env бота). Грант — select/insert/update on app_settings
# (panel_role.sql), как у лид-магнита/флага отмены; delete не грантован → «выключено»/
# пусто пишем пустым value. Метрика «ответов Лии» = messages.source='liya' (тот же
# источник, что и счётчик тарифа в «Подписке»).
# =========================================================================== #

_AI_SETTING_KEYS = (
    config.AI_ENABLED_SETTING_KEY,
    config.AI_BACKEND_SETTING_KEY,
    config.AI_AGENT_ID_SETTING_KEY,
    config.AI_MODEL_SETTING_KEY,
    config.AI_GATEWAY_URL_SETTING_KEY,
    config.AI_SYSTEM_PROMPT_SETTING_KEY,
    config.AI_FALLBACK_SETTING_KEY,
)


async def get_ai_settings() -> dict:
    """Текущие настройки ИИ из app_settings (одним запросом). Отсутствие строки → дефолт:
    enabled=True (сохранить текущее поведение env-только), backend='cloud_ai', agent_id=''
    (→ env AGENT_ID), model/gateway_base_url → дефолты config, system_prompt='', fallback=''
    (→ хардкод-фолбэк бота). Зеркалит чтение бота (get_ai_overrides) — логика и дефолты
    ДОЛЖНЫ совпадать с ним."""
    async with pool.acquire() as c:
        rows = await c.fetch(
            "select key, value from app_settings where key = any($1::text[])",
            list(_AI_SETTING_KEYS),
        )
    kv = {r["key"]: (r["value"] or "") for r in rows}
    enabled_raw = kv.get(config.AI_ENABLED_SETTING_KEY)  # None=нет строки; ''=выключено явно
    backend = (kv.get(config.AI_BACKEND_SETTING_KEY) or "").strip()
    if backend not in config.AI_BACKENDS:
        backend = config.AI_DEFAULT_BACKEND
    return {
        "enabled": True if enabled_raw is None else bool(enabled_raw.strip()),
        "backend": backend,
        "agent_id": (kv.get(config.AI_AGENT_ID_SETTING_KEY) or "").strip(),
        "model": (kv.get(config.AI_MODEL_SETTING_KEY) or "").strip() or config.AI_DEFAULT_MODEL,
        "gateway_base_url": (kv.get(config.AI_GATEWAY_URL_SETTING_KEY) or "").strip()
                            or config.AI_DEFAULT_GATEWAY_URL,
        "system_prompt": kv.get(config.AI_SYSTEM_PROMPT_SETTING_KEY) or "",
        "fallback": kv.get(config.AI_FALLBACK_SETTING_KEY) or "",
    }


async def set_ai_settings(
    *, enabled: bool, backend: str, agent_id: str, model: str,
    gateway_base_url: str, system_prompt: str, fallback: str,
    actor: str, ip: str | None, user_agent: str | None,
) -> None:
    """Сохранить настройки ИИ (upsert ключей app_settings) + аудит — в ОДНОЙ транзакции
    (паттерн остальных мутаций). «Выключено»/пустые поля пишем пустым value (delete на
    app_settings панели не грантован, как у лид-магнита/флага отмены). Длины/валидность
    уже проверены вызывающим (app.py). Аудит — без текстов промпта/фолбэка (только флаги)."""
    pairs = (
        (config.AI_ENABLED_SETTING_KEY, "1" if enabled else ""),
        (config.AI_BACKEND_SETTING_KEY, backend),
        (config.AI_AGENT_ID_SETTING_KEY, agent_id),
        (config.AI_MODEL_SETTING_KEY, model),
        (config.AI_GATEWAY_URL_SETTING_KEY, gateway_base_url),
        (config.AI_SYSTEM_PROMPT_SETTING_KEY, system_prompt),
        (config.AI_FALLBACK_SETTING_KEY, fallback),
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
            await _insert_audit(
                c, actor=actor, action="ai_settings_set", ip=ip, user_agent=user_agent,
                detail={"enabled": enabled, "backend": backend,
                        "agent_id": agent_id or None, "model": model or None,
                        "system_prompt_set": bool(system_prompt),
                        "fallback_set": bool(fallback)},
            )


async def ai_activity_summary(since) -> asyncpg.Record:
    """Сводка активности Лии для статус-карточек: всего ответов, за окно [since, now),
    и время последнего. Один проход по messages (source='liya', direction='out')."""
    q = """
        select
            count(*)                                  as total,
            count(*) filter (where created_at >= $1)  as recent,
            max(created_at)                           as last_at
        from messages
        where source = 'liya' and direction = 'out'
    """
    async with pool.acquire() as c:
        return await c.fetchrow(q, since)


async def list_liya_messages(*, limit: int = 20) -> list[asyncpg.Record]:
    """Последние ответы Лии (лента «Что отвечает Лия»): текст + лид (имя/id) + время.
    Read-only; грант select on messages/leads у panel_rw есть (как в «Диалогах»). Текст —
    ПДн-поток (retention-cron его чистит), показываем в закрытой сессией панели."""
    q = """
        select m.id, m.lead_id, m.text, m.created_at, l.name as lead_name
        from messages m
        left join leads l on l.id = m.lead_id
        where m.source = 'liya' and m.direction = 'out'
        order by m.created_at desc, m.id desc
        limit $1
    """
    async with pool.acquire() as c:
        return await c.fetch(q, limit)


# =========================================================================== #
# ПЛАТЕЖИ / ЗАКАЗЫ (раздел «Платежи», schema_orders.sql). Phase 1A: панель
# фиксирует продажи руками (source='manual'), читает для дашборда. Бот в 1A не
# участвует. panel_rw: SELECT + INSERT/UPDATE на колонках (provider_payment_id —
# нет, его пишет бот в 1B). Аудит order_create/order_status в той же транзакции.
# =========================================================================== #

async def revenue_summary() -> asyncpg.Record:
    """Сводка по выручке одним проходом (как dashboard_counts).

    Суммы агрегируются по ВСЕМ валютам в одну цифру — допущение MVP (школа продаёт
    в ₽; валюта на каждом заказе видна в ленте). Когда появятся не-RUB продажи —
    разнести по currency. paid_* — только оплаченные; refunded — отдельно.
    """
    q = """
        select
            coalesce(sum(amount) filter (where status = 'paid'), 0)            as paid_total,
            coalesce(sum(amount) filter (where status = 'paid'
                     and created_at >= now() - interval '30 days'), 0)         as paid_30d,
            coalesce(sum(amount) filter (where status = 'paid'
                     and created_at >= now() - interval '7 days'), 0)          as paid_7d,
            coalesce(sum(amount) filter (where status = 'refunded'), 0)        as refunded_total,
            count(*) filter (where status = 'paid')                            as paid_count,
            count(*) filter (where status = 'pending')                         as pending_count,
            count(*) filter (where status = 'refunded')                        as refunded_count,
            count(*)                                                           as total_count
        from orders
    """
    async with pool.acquire() as c:
        return await c.fetchrow(q)


async def count_orders(*, status: str | None = None) -> int:
    if status and status in _ORDER_STATUS_SET:
        q, args = "select count(*) from orders where status = $1", (status,)
    else:
        q, args = "select count(*) from orders", ()
    async with pool.acquire() as c:
        return int(await c.fetchval(q, *args))


async def list_orders(
    *, limit: int, offset: int, status: str | None = None
) -> list[asyncpg.Record]:
    """Лента заказов: имя лида и название офера подтягиваем left join (заказ может
    быть без лида/офера). Только метаданные — без байт файла офера."""
    where = "where o.status = $3" if (status and status in _ORDER_STATUS_SET) else ""
    q = f"""
        select o.id, o.amount, o.currency, o.status, o.source, o.note,
               o.created_at, o.paid_at,
               o.lead_id, l.name as lead_name,
               o.product_id, p.name as product_name
        from orders o
        left join leads    l on l.id = o.lead_id
        left join products p on p.id = o.product_id
        {where}
        order by o.created_at desc
        limit $1 offset $2
    """
    args = [limit, offset] + ([status] if where else [])
    async with pool.acquire() as c:
        return await c.fetch(q, *args)


async def list_recent_leads_for_select(*, limit: int = 200) -> list[asyncpg.Record]:
    """Последние лиды для селектора «привязать к лиду» в форме записи продажи.
    Только id + имя + дата (телефон не нужен — выбираем по имени)."""
    q = """
        select id, name, created_at
        from leads
        order by created_at desc
        limit $1
    """
    async with pool.acquire() as c:
        return await c.fetch(q, limit)


async def create_order_with_audit(
    *,
    lead_id,                      # uuid | None
    product_id: int | None,
    amount,                       # Decimal
    currency: str,
    status: str,
    note: str | None,
    mark_converted: bool,
    actor: str,
    ip: str | None,
    user_agent: str | None,
) -> str:
    """Записать продажу (source='manual') + аудит в той же транзакции. Опционально
    переводит лид в 'converted' (грант update(status) на leads есть). paid_at
    проставляется при status='paid'. Возвращает orders.id (uuid-строкой)."""
    if status not in _ORDER_STATUS_SET:
        raise ValueError(f"Недопустимый статус заказа: {status!r}")
    if currency not in _PRODUCT_CURRENCY_SET:
        raise ValueError(f"Недопустимая валюта: {currency!r}")
    async with pool.acquire() as c:
        async with c.transaction():
            oid = await c.fetchval(
                """
                insert into orders
                    (lead_id, product_id, amount, currency, status, source,
                     note, created_by, paid_at)
                values ($1, $2, $3, $4, $5, 'manual', $6, $7,
                        case when $5 = 'paid' then now() else null end)
                returning id
                """,
                lead_id, product_id, amount, currency, status, note, actor,
            )
            converted = False
            if mark_converted and lead_id is not None:
                upd = await c.fetchval(
                    "update leads set status = 'converted' where id = $1 returning id",
                    lead_id,
                )
                converted = upd is not None
            await _insert_audit(
                c, actor=actor, action="order_create", lead_id=lead_id,
                ip=ip, user_agent=user_agent,
                detail={
                    "order_id": str(oid),
                    "amount": str(amount), "currency": currency, "status": status,
                    "has_lead": lead_id is not None, "has_product": product_id is not None,
                    "marked_converted": converted,
                },
            )
            return str(oid)


async def set_order_status_with_audit(
    order_id,
    *,
    new_status: str,
    actor: str,
    ip: str | None,
    user_agent: str | None,
) -> asyncpg.Record | None:
    """Сменить статус заказа (возврат/правка). paid_at ставим при переходе в 'paid'
    (если ещё не стоял). Аудит order_status в той же транзакции. None — заказ не найден."""
    if new_status not in _ORDER_STATUS_SET:
        raise ValueError(f"Недопустимый статус заказа: {new_status!r}")
    async with pool.acquire() as c:
        async with c.transaction():
            old = await c.fetchrow(
                "select status from orders where id = $1 for update", order_id
            )
            if old is None:
                return None
            row = await c.fetchrow(
                """
                update orders
                set status = $1,
                    paid_at = case when $1 = 'paid' then coalesce(paid_at, now()) else paid_at end
                where id = $2
                returning id, status
                """,
                new_status, order_id,
            )
            await _insert_audit(
                c, actor=actor, action="order_status", ip=ip, user_agent=user_agent,
                detail={"order_id": str(order_id),
                        "status": {"old": old["status"], "new": new_status}},
            )
            return row


# =========================================================================== #
# БИЛЛИНГ СЕРВИСА / ПОДПИСКА по ТАРИФАМ (раздел «Подписка», schema_service.sql).
# B2B: школа платит агентству. Метрика = сообщения ИИ (messages.source='liya') за
# период. Тарифы — в config. Текущий тариф/период = последний ОПЛАЧЕННЫЙ счёт; флаг
# отмены — app_settings. Панель INSERT счёта при выборе тарифа + UPDATE статуса/карты
# из вебхука ЮKassa (перепроверка платежа — в хендлере через yookassa.get_payment).
# =========================================================================== #

_INVOICE_COLS = (
    "id, period_start, period_end, plan_key, plan_name, quota, plan_amount, "
    "overage_count, overage_amount, amount, currency, status, "
    "yookassa_payment_id, card_last4, paid_at, created_at"
)


async def count_ai_messages(period_start, period_end=None) -> int:
    """Сообщения, сгенерированные ИИ (Лия), за период [start, end|now). Метрика тарифа.
    period_start/end — date (timestamptz сравнивается с date по полуночи UTC)."""
    if period_end is None:
        q = ("select count(*) from messages "
             "where source = 'liya' and direction = 'out' and created_at >= $1")
        args = (period_start,)
    else:
        q = ("select count(*) from messages "
             "where source = 'liya' and direction = 'out' "
             "and created_at >= $1 and created_at < $2")
        args = (period_start, period_end)
    async with pool.acquire() as c:
        return int(await c.fetchval(q, *args))


async def get_latest_paid_invoice() -> asyncpg.Record | None:
    """Последний ОПЛАЧЕННЫЙ счёт — из него выводим текущий тариф и активный период."""
    q = f"""
        select {_INVOICE_COLS} from service_invoices
        where status = 'paid'
        order by period_end desc, paid_at desc
        limit 1
    """
    async with pool.acquire() as c:
        return await c.fetchrow(q)


async def list_service_invoices(*, limit: int = 60) -> list[asyncpg.Record]:
    """Счета-периоды + использование (сообщений ИИ) за окно каждого периода — одним
    запросом через lateral (для столбцов Использовано/Осталось/Превышение в истории)."""
    q = f"""
        select {', '.join('i.' + col.strip() for col in _INVOICE_COLS.split(','))},
               coalesce(u.used, 0) as used
        from service_invoices i
        left join lateral (
            select count(*) as used
            from messages m
            where m.source = 'liya' and m.direction = 'out'
              and m.created_at >= i.period_start and m.created_at < i.period_end
        ) u on true
        order by i.created_at desc
        limit $1
    """
    async with pool.acquire() as c:
        return await c.fetch(q, limit)


async def get_service_invoice(invoice_id) -> asyncpg.Record | None:
    q = f"select {_INVOICE_COLS} from service_invoices where id = $1"
    async with pool.acquire() as c:
        return await c.fetchrow(q, invoice_id)


async def create_period_invoice(
    *, period_start, period_end, plan_key: str, plan_name: str, quota: int | None,
    plan_amount, overage_count: int, overage_amount, amount, currency: str,
    actor: str, ip: str | None, user_agent: str | None,
) -> str:
    """Создать счёт 'pending' за период тарифа (со снимком квоты/превышения) + аудит."""
    if currency not in _PRODUCT_CURRENCY_SET:
        raise ValueError(f"Недопустимая валюта: {currency!r}")
    async with pool.acquire() as c:
        async with c.transaction():
            iid = await c.fetchval(
                """
                insert into service_invoices
                    (period_start, period_end, plan_key, plan_name, quota, plan_amount,
                     overage_count, overage_amount, amount, currency, status, created_by)
                values ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,'pending',$11)
                returning id
                """,
                period_start, period_end, plan_key, plan_name, quota, plan_amount,
                overage_count, overage_amount, amount, currency, actor,
            )
            await _insert_audit(
                c, actor=actor, action="service_invoice_create",
                ip=ip, user_agent=user_agent,
                detail={"invoice_id": str(iid), "plan": plan_key,
                        "amount": str(amount), "overage": overage_count,
                        "period": [str(period_start), str(period_end)]},
            )
            return str(iid)


async def attach_yookassa_payment(invoice_id, payment_id: str) -> None:
    """Привязать id платежа ЮKassa к pending-счёту (для перепроверки в вебхуке)."""
    async with pool.acquire() as c:
        await c.execute(
            "update service_invoices set yookassa_payment_id = $1 "
            "where id = $2 and status = 'pending'",
            payment_id, invoice_id,
        )


async def mark_service_invoice_paid_by_payment(
    payment_id: str, *, card_last4: str | None = None, actor: str = "yookassa-webhook"
) -> asyncpg.Record | None:
    """Отметить счёт оплаченным по id платежа ЮKassa (идемпотентно). None — счёт не найден.
    Вызывается ИЗ ВЕБХУКА ПОСЛЕ перепроверки платежа через API ЮKassa (status=succeeded)."""
    async with pool.acquire() as c:
        async with c.transaction():
            row = await c.fetchrow(
                "select id, status from service_invoices "
                "where yookassa_payment_id = $1 for update",
                payment_id,
            )
            if row is None:
                return None
            if row["status"] == "paid":
                return row  # идемпотентно: повторный вебхук — no-op
            upd = await c.fetchrow(
                """
                update service_invoices
                set status = 'paid', paid_at = coalesce(paid_at, now()),
                    card_last4 = coalesce($2, card_last4)
                where id = $1
                returning id, status, plan_key, period_end
                """,
                row["id"], card_last4,
            )
            await _insert_audit(
                c, actor=actor, action="service_invoice_paid",
                detail={"invoice_id": str(row["id"]), "payment_id": payment_id},
            )
            return upd


async def is_subscription_canceled() -> bool:
    raw = await get_app_setting(config.SERVICE_CANCEL_SETTING_KEY)
    return bool(raw and raw.strip())


async def set_subscription_canceled(
    canceled: bool, *, actor: str, ip: str | None, user_agent: str | None
) -> None:
    """Флаг отмены подписки в app_settings (панель пишет; бот не использует). Аудит."""
    value = "1" if canceled else ""
    async with pool.acquire() as c:
        async with c.transaction():
            await c.execute(
                """
                insert into app_settings (key, value) values ($1, $2)
                on conflict (key) do update set value = excluded.value
                """,
                config.SERVICE_CANCEL_SETTING_KEY, value,
            )
            await _insert_audit(
                c, actor=actor,
                action="subscription_cancel" if canceled else "subscription_resume",
                ip=ip, user_agent=user_agent, detail={"canceled": canceled},
            )
