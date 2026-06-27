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

import contextvars
import hashlib
import json
import logging
import secrets
from datetime import datetime, timezone
from typing import Any

import asyncpg

import config

pool: asyncpg.Pool | None = None

# ── Харденинг №2: tenant-контекст сессии для RLS на leads/messages/outbox ──────
# panel_rw без bypassrls видит строки tenant-scoped таблиц ТОЛЬКО после
# set_config('app.tenant_id'). Раньше его ставили поштучно (деньги/секреты). Чтобы под
# RLS leads/messages НЕ пропустить ни одного запроса (любой непокрытый → пустой экран),
# ставим app.tenant_id ЦЕНТРАЛИЗОВАННО на КАЖДОМ acquire пула из contextvar активного
# тенанта сессии (его кладёт require_session per-request). Явные set_config(..., true)
# в денежных функциях/скане платформы перекрывают это внутри своей транзакции — ок.
# Бот ходит owner-ролью и RLS обходит (§8.7), его этот хук не касается.
_active_tenant: contextvars.ContextVar = contextvars.ContextVar("panel_active_tenant", default=None)


def set_active_tenant(tenant_id) -> None:
    """Запоминает активный тенант запроса (зовёт require_session). Дальше каждый acquire
    пула проставит его в app.tenant_id (RLS). None → не ставим (GUC после reset пуст →
    current_setting=NULL → RLS отдаёт 0 строк без ошибки касту '' → uuid)."""
    _active_tenant.set(str(tenant_id) if tenant_id else None)


async def get_tenant_id_by_slug(slug: str):
    """tenant_id по slug (раздел «Демо-монитор» резолвит demo-sandbox). tenants — реестр, не
    RLS-scoped (платформа листает всех в «Клиентах») → находит независимо от активного тенанта.
    None — нет такого тенанта."""
    async with pool.acquire() as c:
        return await c.fetchval("select id from tenants where slug = $1", slug)


async def _apply_tenant_guc(conn: asyncpg.Connection) -> None:
    """pool setup: ставит app.tenant_id из contextvar на каждый чек-аут соединения.
    На release asyncpg делает RESET ALL → GUC очищается, утечки тенанта между запросами
    нет. Пусто/None → НЕ ставим (после reset уже NULL; '' сломал бы каст ::uuid в политике)."""
    tid = _active_tenant.get()
    if tid:
        await conn.execute("select set_config('app.tenant_id', $1, false)", tid)

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
    pool = await asyncpg.create_pool(
        config.DATABASE_URL, min_size=1, max_size=5, setup=_apply_tenant_guc)


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
        -- «Непрочитанное» = входящие НОВЕЕ момента, когда оператор в последний раз
        -- ОТВЕТИЛ (исходящее) ЛИБО ОТКРЫЛ диалог (аудит lead_view/thread_view). Открытие
        -- карточки пишет admin_audit ДО рендера списка → бейдж гаснет в том же ответе.
        -- Важно для веб-лидов демо: ответить им нельзя (композер скрыт), и раньше бейдж
        -- залипал навсегда — теперь снимается просмотром.
        select count(*) as unread
        from messages m
        where m.lead_id = l.id and m.direction = 'in'
          and m.created_at > greatest(
              coalesce((select max(mo.created_at) from messages mo
                        where mo.lead_id = l.id and mo.direction = 'out'),
                       'epoch'::timestamptz),
              coalesce((select max(a.at) from admin_audit a
                        where a.lead_id = l.id
                          and a.action in ('lead_view', 'thread_view')),
                       'epoch'::timestamptz))
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
    """Сколько лидов с НЕПРОЧИТАННЫМ входящим: есть входящее новее момента, когда
    оператор в последний раз ответил (исходящее) ЛИБО открыл диалог (аудит просмотра).
    Бейдж раздела «Диалоги» в сайдбаре. Снимается ответом ИЛИ открытием карточки
    (паритет с per-row бейджем в _DIALOG_SELECT — см. там комментарий)."""
    q = """
        select count(*) from leads l
        where exists (
            select 1 from messages m
            where m.lead_id = l.id and m.direction = 'in'
              and m.created_at > greatest(
                  coalesce((select max(mo.created_at) from messages mo
                            where mo.lead_id = l.id and mo.direction = 'out'),
                           'epoch'::timestamptz),
                  coalesce((select max(a.at) from admin_audit a
                            where a.lead_id = l.id
                              and a.action in ('lead_view', 'thread_view')),
                           'epoch'::timestamptz))
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
               tg_user_id, max_user_id, vk_user_id, max_chat_id, notes, survey,
               erase_requested_at, bot_paused, unsubscribed_at, ai_persona,
               -- C3: можно ли ответить лиду в его канале (есть адрес доставки)?
               (case messenger when 'tg' then tg_user_id
                               when 'vk' then vk_user_id
                               when 'max' then max_chat_id end) is not null as can_reply
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
            # C3: канал лида определяет адрес доставки. tg → tg_user_id (как было); vk → vk_user_id;
            # max → max_chat_id (адрес ответа в личке). Нет адреса в канале лида → не ставим
            # (бот всё равно не доставит). outbox.messenger → канальный дренаж воркера.
            lead = await c.fetchrow(
                "select messenger, tg_user_id, vk_user_id, max_chat_id "
                "from leads where id = $1 for update",
                lead_id,
            )
            if lead is None:
                return None
            messenger = lead["messenger"] or "tg"
            addr = {"tg": lead["tg_user_id"], "vk": lead["vk_user_id"],
                    "max": lead["max_chat_id"]}.get(messenger)
            if addr is None:
                return None  # нет адреса в канале лида → ничего не ставим
            tg = lead["tg_user_id"] if messenger == "tg" else None  # tg_user_id только для tg
            count = 0
            if text:
                await c.execute(
                    "insert into outbox (lead_id, tg_user_id, messenger, kind, text, status, "
                    "                    created_by, tenant_id) "
                    "values ($1, $2, $3, 'text', $4, 'queued', $5, "
                    "        (select tenant_id from leads where id = $1))",
                    lead_id, tg, messenger, text, actor,
                )
                count += 1
            for a in attachments:
                # defence-in-depth: kind машинно-выводится _read_reply_file (magic-byte), но не пускаем
                # неизвестный в outbox молча. ⚠️ набор синхронен с bot _MSG_KINDS (bot-telegram/db.py) и
                # канальными vk/max_media_type_for_kind. Неизвестный → document (универсален во всех
                # каналах), graceful — без 500 и без молчаливой потери вложения.
                a_kind = a.get("kind") if a.get("kind") in ("photo", "document", "voice", "audio") else "document"
                if a_kind != a.get("kind"):
                    logging.getLogger(__name__).warning(
                        "enqueue_manual_reply: неизвестный kind вложения %r → document", a.get("kind"))
                await c.execute(
                    """
                    insert into outbox
                        (lead_id, tg_user_id, messenger, kind, text, status, created_by,
                         file_bytes, file_name, file_mime, tenant_id)
                    values ($1, $2, $3, $4, null, 'queued', $5, $6, $7, $8,
                            (select tenant_id from leads where id = $1))
                    """,
                    lead_id, tg, messenger, a_kind, actor, a["bytes"], a["name"], a["mime"],
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

# Канон значений messenger рассылки. C3: tg/vk/max активны (доставка — канальными драйверами
# через реестр multiplex; для vk/max нужен поднятый канал тенанта). Адрес-колонка по каналу — ниже.
BROADCAST_MESSENGERS: tuple[str, ...] = ("tg", "vk", "max")  # реально отправляемые
_BROADCAST_MESSENGER_SET = frozenset(BROADCAST_MESSENGERS)
# Колонка адреса доставки по каналу (зеркало bot-telegram/db.py::_CHANNEL_REPLY_COL).
_BROADCAST_REPLY_COL = {"tg": "tg_user_id", "vk": "vk_user_id", "max": "max_chat_id"}
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
    разъезжаются с реальностью. ⚠️ unsubscribed_at is null теперь В ЯДРЕ ВСЕГДА (порядок клауз ==
    bot _audience_where; решение владельца) → предпросмотр/recipient_count == реальной доставке.
    Поверх ядра — операторские сужения (source/status). Значения через allow-list+$-плейсхолдеры.
    Возвращает (where_sql, params, next_idx).
    """
    # C3: канал аудитории из фильтра (tg по умолчанию). Адрес-колонка по каналу — зеркало
    # bot-telegram/db.py::_audience_where(messenger). Для tg ядро ПОБАЙТОВО прежнее.
    messenger = audience.get("messenger")
    if messenger not in _BROADCAST_MESSENGER_SET:
        messenger = "tg"
    addr_col = _BROADCAST_REPLY_COL[messenger]
    # ⚠️ ЯДРО ПОБАЙТОВО == bot-telegram/db.py::_audience_where(messenger): тот же порядок клауз,
    # unsubscribed_at is null В ЯДРЕ ВСЕГДА (решение владельца — бот всё равно режет отписанных,
    # теперь предпросмотр/recipient_count/cap совпадают с реальной доставкой). Golden-smoke сверяет.
    clauses: list[str] = [
        f"messenger = '{messenger}'",
        f"{addr_col} is not null",
        "consent = true",
        "unsubscribed_at is null",
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

    # unsubscribed_at — теперь в ЯДРЕ (выше), всегда. Тумблер exclude_unsubscribed больше НЕ влияет
    # на WHERE (бот в любом случае режет отписанных); оставлен в audience лишь для совместимости/аудита.
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
    tenant_id,                    # uuid активного тенанта сессии (Wave 3)
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
                     status, recipient_count, created_by, product_id, tenant_id)
                values ($1, $2, $3, $4, $5::jsonb, 'draft', null, $6, $7, $8)
                returning id
                """,
                title, messenger, kind, body_template, audience_json, actor, product_ref,
                tenant_id,
            )
            bid = int(bid)
            if file_meta is not None:
                await c.execute(
                    """
                    insert into broadcast_files (broadcast_id, filename, mime, bytes, tenant_id)
                    values ($1, $2, $3, $4,
                            (select tenant_id from broadcasts where id = $1))
                    """,
                    bid, file_meta.get("filename"), file_meta.get("mime"),
                    file_meta.get("bytes"),
                )
            token: str | None = None
            if target_url:
                token = secrets.token_urlsafe(16)
                await c.execute(
                    """
                    insert into link_tokens (token, target_url, broadcast_id, tenant_id)
                    values ($1, $2, $3,
                            (select tenant_id from broadcasts where id = $3))
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
    tenant_id,                  # uuid активного тенанта сессии (Wave 3)
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
                     file, file_name, file_mime, status, created_by, tenant_id)
                values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
                returning id
                """,
                name, kind, price, currency, caption, link,
                file_bytes, fb.get("filename"), fb.get("mime"), status, actor, tenant_id,
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
    config.AI_PERSONA_SETTING_KEY,
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
        # Slug активной «должности» (бейдж в UI; бот ключ не читает). Неизвестный → "".
        "persona": (kv.get(config.AI_PERSONA_SETTING_KEY) or "").strip()
                   if (kv.get(config.AI_PERSONA_SETTING_KEY) or "").strip() in config.PERSONA_PRESETS
                   else "",
    }


async def set_ai_settings(
    *, enabled: bool, backend: str, agent_id: str, model: str,
    gateway_base_url: str, system_prompt: str, fallback: str,
    actor: str, ip: str | None, user_agent: str | None, persona: str = "",
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
        (config.AI_PERSONA_SETTING_KEY, persona),
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
                        "fallback_set": bool(fallback),
                        "persona": persona or None},
            )


# ── RF-RAG: своя база знаний (pgvector) — раздел «Базы знаний» (загрузка файлов) ──
def _vec_literal(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


async def get_kb_enabled() -> bool:
    """Тумблер RAG (app_settings['kb_enabled']). Бот читает его же при retrieval."""
    async with pool.acquire() as c:
        v = await c.fetchval(
            "select value from app_settings where key = $1", config.KB_ENABLED_SETTING_KEY
        )
    return bool((v or "").strip())


async def set_kb_enabled(enabled: bool, *, actor: str, ip: str | None, user_agent: str | None) -> None:
    async with pool.acquire() as c:
        async with c.transaction():
            await c.execute(
                "insert into app_settings (key, value) values ($1, $2) "
                "on conflict (key) do update set value = excluded.value",
                config.KB_ENABLED_SETTING_KEY, "1" if enabled else "",
            )
            await _insert_audit(
                c, actor=actor, action="kb_enabled_set", ip=ip, user_agent=user_agent,
                detail={"enabled": enabled},
            )


async def kb_list_documents() -> list[asyncpg.Record]:
    """Документы базы знаний + число чанков (для списка с удалением)."""
    async with pool.acquire() as c:
        return await c.fetch(
            """
            select d.id, d.title, d.source, d.role_tag, d.created_by,
                   to_char(d.created_at, 'YYYY-MM-DD HH24:MI') as created,
                   count(ch.id) as chunks
              from kb_documents d
              left join kb_chunks ch on ch.document_id = d.id
             group by d.id
             order by d.created_at desc
            """
        )


async def kb_insert_document(
    *, title: str, source: str, role_tag: str, content: str,
    chunks: list[str], embeddings: list[list[float]],
    tenant_id,
    actor: str, ip: str | None, user_agent: str | None,
) -> int:
    """Документ + его чанки (с эмбеддингами) в ОДНОЙ транзакции + аудит. Возвращает число
    чанков. role_tag '' → NULL (общая справка для всех ролей). Вектор кладём text→::vector.
    tenant_id — активный тенант сессии (Wave 3); чанки наследуют тенанта документа."""
    async with pool.acquire() as c:
        async with c.transaction():
            doc = await c.fetchval(
                "insert into kb_documents (title, source, role_tag, content, created_by, "
                "                          tenant_id) "
                "values ($1, $2, $3, $4, $5, $6) returning id",
                title, source, (role_tag or None), content, actor, tenant_id,
            )
            await c.executemany(
                "insert into kb_chunks (document_id, chunk_index, content, embedding, metadata, "
                "                       tenant_id) "
                "values ($1, $2, $3, $4::vector, $5::jsonb, "
                "        (select tenant_id from kb_documents where id = $1))",
                [
                    (doc, i, ch, _vec_literal(emb),
                     json.dumps({"role_tag": role_tag or "", "title": title[:120], "source": source},
                                ensure_ascii=False))
                    for i, (ch, emb) in enumerate(zip(chunks, embeddings))
                ],
            )
            await _insert_audit(
                c, actor=actor, action="kb_doc_upload", ip=ip, user_agent=user_agent,
                detail={"title": title[:120], "chunks": len(chunks), "role_tag": role_tag or None},
            )
    return len(chunks)


async def kb_delete_document(doc_id: str, *, actor: str, ip: str | None, user_agent: str | None) -> bool:
    """Удалить документ (каскад чистит чанки) + аудит. Возвращает True, если что-то удалено."""
    async with pool.acquire() as c:
        async with c.transaction():
            row = await c.fetchrow(
                "delete from kb_documents where id = $1::uuid returning title", doc_id
            )
            if row:
                await _insert_audit(
                    c, actor=actor, action="kb_doc_delete", ip=ip, user_agent=user_agent,
                    detail={"doc_id": str(doc_id), "title": (row["title"] or "")[:120]},
                )
    return bool(row)


# ── «ИИ-сотрудник на канал» (страница «Каналы») ──────────────────────────────
async def get_channel_personas(sources: tuple[str, ...]) -> dict:
    """{source: persona_slug} назначений «ИИ-сотрудника» по каналам + реестр агентов
    персон {slug: access_id}. Одним запросом. Неизвестные слуги отфильтрованы."""
    keys = [config.CHANNEL_PERSONA_KEY.format(source=s) for s in sources]
    keys += [config.PERSONA_AGENT_REGISTRY_KEY.format(slug=p) for p in config.PERSONA_PRESETS]
    async with pool.acquire() as c:
        rows = await c.fetch(
            "select key, value from app_settings where key = any($1::text[])", keys
        )
    kv = {r["key"]: (r["value"] or "").strip() for r in rows}
    personas = {}
    for s in sources:
        v = kv.get(config.CHANNEL_PERSONA_KEY.format(source=s), "")
        personas[s] = v if v in config.PERSONA_PRESETS else ""
    agents = {
        p: kv.get(config.PERSONA_AGENT_REGISTRY_KEY.format(slug=p), "")
        for p in config.PERSONA_PRESETS
    }
    return {"personas": personas, "agents": agents}


async def get_persona_agent(slug: str) -> str:
    """access_id ранее созданного агента персоны (или "" если ещё не создавался)."""
    async with pool.acquire() as c:
        v = await c.fetchval(
            "select value from app_settings where key = $1",
            config.PERSONA_AGENT_REGISTRY_KEY.format(slug=slug),
        )
    return (v or "").strip()


async def get_persona_role(slug: str) -> dict:
    """Всё для страницы управления ролью: роль/задачи/поведение (бывш. единая «инструкция»),
    знания, эффективный промпт, access_id и числовой id агента. Если ничего не задано — в
    «поведение» подставляется каркас из PERSONA_PRESETS (его и редактируют). Старое единое поле
    «инструкция» (legacy) мигрируется в «поведение»."""
    keys = {
        "role": config.PERSONA_ROLE_KEY.format(slug=slug),
        "tasks": config.PERSONA_TASKS_KEY.format(slug=slug),
        "behavior": config.PERSONA_BEHAVIOR_KEY.format(slug=slug),
        "instruction": config.PERSONA_INSTRUCTION_KEY.format(slug=slug),  # legacy
        "knowledge": config.PERSONA_KNOWLEDGE_KEY.format(slug=slug),
        "access_id": config.PERSONA_AGENT_REGISTRY_KEY.format(slug=slug),
        "nid": config.PERSONA_AGENT_NID_KEY.format(slug=slug),
    }
    async with pool.acquire() as c:
        rows = await c.fetch(
            "select key, value from app_settings where key = any($1::text[])",
            list(keys.values()),
        )
    kv = {r["key"]: (r["value"] or "") for r in rows}
    preset = config.PERSONA_PRESETS.get(slug) or {}
    role = kv.get(keys["role"], "")
    tasks = kv.get(keys["tasks"], "")
    behavior_saved = kv.get(keys["behavior"], "") or kv.get(keys["instruction"], "")  # миграция legacy
    is_default = not (role or tasks or behavior_saved)
    return {
        "role": role,
        "tasks": tasks,
        "behavior": behavior_saved if behavior_saved else (preset.get("prompt") or ""),
        "is_default": is_default,
        "knowledge": kv.get(keys["knowledge"], ""),
        "access_id": kv.get(keys["access_id"], "").strip(),
        "nid": kv.get(keys["nid"], "").strip(),
    }


async def save_persona_agent(slug: str, access_id: str, numeric_id, prompt: str) -> None:
    """Запомнить агента персоны: access_id (вызов ботом) + числовой id (PATCH промпта) +
    эффективный промпт (gateway/per-lead). Один агент на персону — общий для каналов/диалогов."""
    pairs = (
        (config.PERSONA_AGENT_REGISTRY_KEY.format(slug=slug), access_id),
        (config.PERSONA_AGENT_NID_KEY.format(slug=slug), str(numeric_id or "")),
        (config.PERSONA_PROMPT_REGISTRY_KEY.format(slug=slug), prompt),
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


async def set_persona_role(
    slug: str, *, role: str, tasks: str, behavior: str, knowledge: str, prompt: str,
    actor: str, ip: str | None, user_agent: str | None,
) -> None:
    """Сохранить роль/задачи/поведение + знания роли + эффективный промпт (склейку, читает бот)
    — в одной транзакции с аудитом. Пуш на живого cloud-ai агента (если создан) делает
    вызывающий через API. Legacy-ключ инструкции больше не пишем (поведение его заменяет)."""
    pairs = (
        (config.PERSONA_ROLE_KEY.format(slug=slug), role),
        (config.PERSONA_TASKS_KEY.format(slug=slug), tasks),
        (config.PERSONA_BEHAVIOR_KEY.format(slug=slug), behavior),
        (config.PERSONA_KNOWLEDGE_KEY.format(slug=slug), knowledge),
        (config.PERSONA_PROMPT_REGISTRY_KEY.format(slug=slug), prompt),
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
                c, actor=actor, action="persona_role_set", ip=ip, user_agent=user_agent,
                detail={"persona": slug, "role_len": len(role), "tasks_len": len(tasks),
                        "behavior_len": len(behavior), "knowledge_len": len(knowledge)},
            )


async def set_lead_persona(
    lead_id, persona: str, *, actor: str, ip: str | None, user_agent: str | None
) -> None:
    """«ИИ-сотрудник диалога»: записать выбор персоны на конкретного лида (leads.ai_persona)
    + аудит, в ОДНОЙ транзакции. persona="" → NULL (сброс на канал/глобал)."""
    async with pool.acquire() as c:
        async with c.transaction():
            await c.execute(
                "update leads set ai_persona = $2 where id = $1",
                lead_id, (persona or None),
            )
            await _insert_audit(
                c, actor=actor, action="lead_persona_set", lead_id=lead_id,
                ip=ip, user_agent=user_agent, detail={"persona": persona or None},
            )


async def set_channel_persona(
    *, source: str, persona: str, agent_access_id: str, prompt: str,
    actor: str, ip: str | None, user_agent: str | None,
) -> None:
    """Назначить «ИИ-сотрудника» каналу: персона + агент (cloud_ai) + промпт (gateway) —
    в ОДНОЙ транзакции с аудитом. persona=""/пустые значения = сброс на «как у всех»
    (delete панели не грантован — пишем пустые value, бот трактует их как «нет оверрайда»)."""
    pairs = (
        (config.CHANNEL_PERSONA_KEY.format(source=source), persona),
        (config.CHANNEL_AGENT_KEY.format(source=source), agent_access_id),
        (config.CHANNEL_PROMPT_KEY.format(source=source), prompt),
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
                c, actor=actor, action="channel_persona_set", ip=ip, user_agent=user_agent,
                detail={"source": source, "persona": persona or None,
                        "agent_set": bool(agent_access_id)},
            )


async def persona_dialog_stats() -> list[asyncpg.Record]:
    """Сырьё аналитики «по ИИ-сотрудникам»: лиды и конверсия (status='converted') в разрезе
    (ai_persona, source). Эффективную персону каждой группы резолвит app.py (нужны канал- и
    глобал-назначения из app_settings — их в SQL нет). Read-only (грант select на leads)."""
    q = """
        select ai_persona, source,
               count(*)                                     as leads,
               count(*) filter (where status = 'converted') as converted
        from leads
        group by ai_persona, source
    """
    async with pool.acquire() as c:
        return await c.fetch(q)


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
# ИНТЕГРАЦИИ (раздел «Интеграции»): ссылка-гайд через app_settings (закрытие
# GUIDE_URL-заглушки) + чтение НЕ-секретного снимка конфигурации бота, который бот
# публикует на старте (bot-telegram/db.py::publish_runtime_status). Панель и бот живут
# в РАЗНОМ окружении — общий канал статуса только app_settings. Грант select/insert/
# update on app_settings уже есть (как у лид-магнита/ИИ); DDL/новых грантов НЕ требует.
# =========================================================================== #

async def get_guide_url_setting() -> str | None:
    """Переопределение ссылки-гайда из app_settings['guide_url'] (или None → бот фолбэчит
    на env GUIDE_URL). Read-only под panel_rw (грант select)."""
    raw = await get_app_setting(config.GUIDE_URL_SETTING_KEY)
    return (raw or "").strip() or None


async def set_guide_url_with_audit(
    url: str | None, *, actor: str, ip: str | None, user_agent: str | None,
) -> str:
    """Задать/снять переопределение ссылки-гайда (upsert app_settings['guide_url'] + аудит).

    url пусто/None → снять: пишем пустой value (delete на app_settings панели не грантован,
    как у лид-магнита; бот трактует пустое как «нет override» → фолбэк на env GUIDE_URL).
    Иначе валидируем СИММЕТРИЧНО боту (get_effective_guide_url): http(s)-схема, без пробелов,
    длина ≤ GUIDE_URL_MAX — иначе бот всё равно отвергнет значение и уйдёт на env, поэтому
    не даём сохранить заведомо-битую ссылку (понятная ошибка в UI). Возвращает
    "set" | "cleared" | "bad_url". Запись и аудит — в ОДНОЙ транзакции (паттерн мутаций).
    Ссылка-гайд — операционный конфиг (публичный URL GetCourse), не ПДн → пишем её в аудит."""
    clean = (url or "").strip()
    if clean and (
        len(clean) > config.GUIDE_URL_MAX
        or not clean.startswith(config.LINK_HINT_SCHEMES)
        or any(c.isspace() for c in clean)
    ):
        return "bad_url"
    async with pool.acquire() as c:
        async with c.transaction():
            await c.execute(
                """
                insert into app_settings (key, value) values ($1, $2)
                on conflict (key) do update set value = excluded.value
                """,
                config.GUIDE_URL_SETTING_KEY, clean,
            )
            await _insert_audit(
                c, actor=actor, action="guide_url_set", ip=ip, user_agent=user_agent,
                detail={"url": clean or None, "cleared": not clean},
            )
    return "set" if clean else "cleared"


_RUNTIME_STATUS_KEYS = (
    config.RUNTIME_BOT_USERNAME_KEY, config.RUNTIME_GATE_CHANNEL_KEY,
    config.RUNTIME_GUIDE_ENV_KEY, config.RUNTIME_PROXY_SET_KEY,
    config.RUNTIME_AGENT_TOKEN_KEY, config.RUNTIME_GATEWAY_TOKEN_KEY,
    config.RUNTIME_PUBLIC_BASE_KEY, config.RUNTIME_SHOP_YK_KEY,
)


async def get_runtime_status() -> dict:
    """Снимок конфигурации бота из app_settings (бот публикует на старте one-shot). Значения
    НЕ-секретные (для токена/прокси — булев флаг присутствия). heartbeat_at = updated_at
    строки bot_username (когда бот последний раз публиковался). Нет строк → бот ещё не
    публиковал статус (старый образ/не перезапускался после деплоя) → published=False,
    панель покажет подсказку. Read-only (грант select)."""
    async with pool.acquire() as c:
        rows = await c.fetch(
            "select key, value, updated_at from app_settings where key = any($1::text[])",
            list(_RUNTIME_STATUS_KEYS),
        )
    kv = {r["key"]: r for r in rows}

    def _val(key: str) -> str:
        r = kv.get(key)
        return (r["value"] or "").strip() if r else ""

    hb_row = kv.get(config.RUNTIME_BOT_USERNAME_KEY)
    return {
        "published": bool(kv),
        "bot_username": _val(config.RUNTIME_BOT_USERNAME_KEY),
        "gate_channel_url": _val(config.RUNTIME_GATE_CHANNEL_KEY),
        "guide_url_env": _val(config.RUNTIME_GUIDE_ENV_KEY),
        "proxy_set": _val(config.RUNTIME_PROXY_SET_KEY) == "1",
        "agent_token_set": _val(config.RUNTIME_AGENT_TOKEN_KEY) == "1",
        "gateway_token_set": _val(config.RUNTIME_GATEWAY_TOKEN_KEY) == "1",
        "public_base_url": _val(config.RUNTIME_PUBLIC_BASE_KEY),
        "shop_yookassa_set": _val(config.RUNTIME_SHOP_YK_KEY) == "1",
        "heartbeat_at": hb_row["updated_at"] if hb_row else None,
    }


# =========================================================================== #
# КАНАЛЫ (раздел «Каналы»): read-only атрибуция по площадке (source) — лиды и
# конверсия — для deep-link'ов воронки (?start=<source>). Зеркалит dashboard_by_source,
# но + converted. conv% и итоги считает презентер app.py. Read-only (грант select).
# =========================================================================== #

async def attribution_by_source() -> list[asyncpg.Record]:
    """Атрибуция по площадкам: лиды и конверсия (status='converted') на каждый source.
    Один проход group by (как dashboard_by_source). НЕ фильтруем — это сводка по всей базе.
    Деление на ноль/формат conv% — в презентере (app.py), не в SQL."""
    q = """
        select source,
               count(*)                                     as leads,
               count(*) filter (where status = 'converted') as converted
        from leads
        group by source
        order by leads desc, source asc
    """
    async with pool.acquire() as c:
        return await c.fetch(q)


async def total_link_clicks() -> int:
    """Всего кликов по трекинг-ссылкам рассылок (/r) — вспомогательная метрика «Каналов».
    Это клики по ССЫЛКАМ В РАССЫЛКАХ (per-broadcast), НЕ атрибуция площадки воронки —
    отдельная ось, помечаем как таковую в UI. Грант select on link_clicks у panel_rw есть."""
    async with pool.acquire() as c:
        return await c.fetchval("select count(*) from link_clicks") or 0


# =========================================================================== #
# КОМАНДА (раздел «Команда»): мульти-оператор + роли (schema_team.sql). env-админ
# здесь НЕ хранится (bootstrap-суперюзер вне БД, см. auth.authenticate). panel_rw:
# SELECT + точечные INSERT/UPDATE (db/panel_role.sql), без DELETE — деактивация.
# Аудит team_* в той же транзакции; plain-пароли НЕ логируем (только факт + роль).
# `res.endswith(' 0')` ловит «0 строк затронуто» (INSERT 0 0 при конфликте / UPDATE 0).
# =========================================================================== #

async def get_admin_user(username: str) -> asyncpg.Record | None:
    """Юзер панели по логину (для auth.authenticate): username/password_hash/role/active или None."""
    async with pool.acquire() as c:
        return await c.fetchrow(
            "select username, password_hash, role, active from admin_users where username = $1",
            username,
        )


async def list_admin_users() -> list[asyncpg.Record]:
    """Список команды для /team (БЕЗ password_hash). Свежие сверху."""
    async with pool.acquire() as c:
        return await c.fetch(
            """
            select username, role, active, created_at, created_by, updated_at
            from admin_users
            order by created_at desc
            """
        )


async def create_admin_user_with_audit(
    username: str, password_hash: str, role: str, *,
    actor: str, ip: str | None, user_agent: str | None,
) -> str:
    """Создать оператора (INSERT + аудит, одна транзакция). "created" | "exists".
    username/role уже нормализованы/валидны вызывающим (app.py)."""
    async with pool.acquire() as c:
        async with c.transaction():
            res = await c.execute(
                """
                insert into admin_users (username, password_hash, role, active, created_by)
                values ($1, $2, $3, true, $4)
                on conflict (username) do nothing
                """,
                username, password_hash, role, actor,
            )
            if res.endswith(" 0"):   # INSERT 0 0 → логин занят
                return "exists"
            await _insert_audit(
                c, actor=actor, action="team_user_create", ip=ip, user_agent=user_agent,
                detail={"username": username, "role": role},
            )
    return "created"


# =========================================================================== #
# Парадная «ИИ-Агент Про»: клиентские учётки (self-serve регистрация + соц-вход).
# Переиспользуем admin_users(role='admin') + memberships(role='owner') + tenants
# (status='provisioning'); внешний способ входа маппится через account_identities.
# RLS-изоляция данных нового тенанта уже обеспечена (leads/messages/… под RLS).
# =========================================================================== #
async def find_identity(provider: str, external_id: str) -> asyncpg.Record | None:
    """Найти учётку по внешнему идентификатору (вход через email/телефон/ВК/ТГ).
    Резолв ДО сессии → таблица БЕЗ RLS. None — идентичность не зарегистрирована."""
    async with pool.acquire() as c:
        return await c.fetchrow(
            "select id, username, verified, display_name from account_identities "
            "where provider = $1 and external_id = $2",
            provider, external_id,
        )


async def resolve_username_by_email(email: str) -> str | None:
    """email → username учётки (для входа клиента по email в /login). Без требования
    verified: пароль — секрет-гейт (верификация email = Фаза 2, для сброса/анти-сквоттинга)."""
    e = (email or "").strip().lower()
    if not e:
        return None
    async with pool.acquire() as c:
        return await c.fetchval(
            "select username from account_identities where provider = 'email' and external_id = $1",
            e,
        )


async def touch_identity_login(identity_id: int) -> None:
    """Отметить факт входа через эту идентичность (last_login_at). Не критично к гонке."""
    async with pool.acquire() as c:
        await c.execute(
            "update account_identities set last_login_at = now() where id = $1", identity_id
        )


async def create_client_account(
    *, provider: str, external_id: str, name: str, password_hash: str,
    display_name: str | None = None, verified: bool = False,
    ip: str | None = None, user_agent: str | None = None,
) -> tuple[str, str]:
    """Создать клиентскую учётку ОДНОЙ транзакцией: tenant(provisioning) + admin_user(operator)
    + membership(owner) + account_identity. Возврат (username, tenant_id). Зеркалит
    create_admin_user_with_audit + аудит 'client_signup'. Уникальность (provider,external_id)
    защищает от дубля — вызывающий ОБЯЗАН сперва проверить find_identity (иначе тут IntegrityError).
    password_hash: реальный (email-регистрация) ИЛИ неюзабельный случайный (ТГ/ВК — без пароля)."""
    token = secrets.token_hex(10)            # 20 hex-символов → username/slug под ^[a-z0-9_-]+$
    username = f"client_{token}"
    slug = f"client-{token}"
    safe_name = (name or "").strip()[:120] or "Новый клиент"
    async with pool.acquire() as c:
        async with c.transaction():
            tenant_id = await c.fetchval(
                "insert into tenants (slug, name, status) values ($1, $2, 'provisioning') returning id",
                slug, safe_name,
            )
            # role='operator': клиент — оператор СВОЕГО кабинета, НЕ платформенный 'admin'.
            # 'admin' в этой кодовой базе = платформенный супер (env-админ); выдать его
            # публичной учётке = захват платформы + межтенантная утечка (ревью, critical).
            # Владение тенантом фиксирует membership(role='owner') ниже.
            await c.execute(
                "insert into admin_users (username, password_hash, role, active, created_by) "
                "values ($1, $2, 'operator', true, 'self-signup')",
                username, password_hash,
            )
            await c.execute(
                "insert into memberships (tenant_id, username, role) values ($1, $2, 'owner')",
                tenant_id, username,
            )
            await c.execute(
                "insert into account_identities (provider, external_id, username, verified, display_name) "
                "values ($1, $2, $3, $4, $5)",
                provider, external_id, username, verified, (display_name or None),
            )
            await _insert_audit(
                c, actor=username, action="client_signup", ip=ip, user_agent=user_agent,
                detail={"provider": provider, "tenant_id": str(tenant_id), "verified": verified},
            )
    return username, str(tenant_id)


async def set_admin_user_role_with_audit(
    username: str, role: str, *, actor: str, ip: str | None, user_agent: str | None,
) -> bool:
    """Сменить роль оператора (UPDATE + аудит). False — нет такого юзера."""
    async with pool.acquire() as c:
        async with c.transaction():
            res = await c.execute(
                "update admin_users set role = $2, updated_at = now() where username = $1",
                username, role,
            )
            if res.endswith(" 0"):
                return False
            await _insert_audit(
                c, actor=actor, action="team_user_role", ip=ip, user_agent=user_agent,
                detail={"username": username, "role": role},
            )
    return True


async def set_admin_user_active_with_audit(
    username: str, active: bool, *, actor: str, ip: str | None, user_agent: str | None,
) -> bool:
    """Активировать/деактивировать оператора (UPDATE + аудит). False — нет такого юзера.
    Деактивация = «увольнение» вместо DELETE: вход закрыт, строки/аудит сохранены.
    При деактивации ЖИВЫЕ сессии актора ревокаются В ТОЙ ЖЕ транзакции — иначе уже
    выданная сессия доживала бы до idle/потолка (выявлено сквозной проверкой на проде)."""
    async with pool.acquire() as c:
        async with c.transaction():
            res = await c.execute(
                "update admin_users set active = $2, updated_at = now() where username = $1",
                username, active,
            )
            if res.endswith(" 0"):
                return False
            if not active:
                # Немедленный выброс: следующий же запрос деактивированного → 303 /login.
                await c.execute(
                    "update admin_sessions set revoked = true where actor = $1 and revoked = false",
                    username,
                )
            await _insert_audit(
                c, actor=actor, action="team_user_active", ip=ip, user_agent=user_agent,
                detail={"username": username, "active": active},
            )
    return True


async def set_admin_user_password_with_audit(
    username: str, password_hash: str, *, actor: str, ip: str | None, user_agent: str | None,
) -> bool:
    """Сбросить пароль оператора (UPDATE хеша + аудит). False — нет такого юзера. Plain НЕ логируем."""
    async with pool.acquire() as c:
        async with c.transaction():
            res = await c.execute(
                "update admin_users set password_hash = $2, updated_at = now() where username = $1",
                username, password_hash,
            )
            if res.endswith(" 0"):
                return False
            await _insert_audit(
                c, actor=actor, action="team_user_password", ip=ip, user_agent=user_agent,
                detail={"username": username},
            )
    return True


# =========================================================================== #
# Раздел «Профиль» (личный кабинет клиента: свой профиль, безопасность, способы входа).
# Все операции — над СОБСТВЕННОЙ учёткой (actor == username); кросс-аккаунтных правок нет.
# account_identities — БЕЗ RLS (резолв до сессии), admin_users/admin_sessions — тоже.
# =========================================================================== #
async def get_account(username: str) -> asyncpg.Record | None:
    """Учётка для «Профиля»: роль/активность/даты (БЕЗ password_hash). None — нет в admin_users
    (например, env-админ — платформенный супер вне БД: его профиль рендерится как read-only)."""
    async with pool.acquire() as c:
        return await c.fetchrow(
            "select username, role, active, created_at, updated_at from admin_users where username = $1",
            username,
        )


async def list_account_identities(username: str) -> list[asyncpg.Record]:
    """Способы входа учётки (email/телефон/ВК/ТГ) для «Профиля». Свежие сверху по дате привязки."""
    async with pool.acquire() as c:
        return await c.fetch(
            "select provider, external_id, verified, display_name, created_at, last_login_at "
            "from account_identities where username = $1 order by created_at",
            username,
        )


async def set_account_display_name_with_audit(
    username: str, display_name: str | None, *, ip: str | None, user_agent: str | None,
) -> bool:
    """Обновить отображаемое имя клиента (по ВСЕМ его личностям — это одно имя пользователя)
    + аудит. False — у учётки нет ни одной личности (нечего обновлять, напр. env-админ).
    display_name уже обрезан/нормализован вызывающим (app.py); пустое → NULL."""
    name = (display_name or "").strip() or None
    async with pool.acquire() as c:
        async with c.transaction():
            res = await c.execute(
                "update account_identities set display_name = $2 where username = $1",
                username, name,
            )
            if res.endswith(" 0"):
                return False
            await _insert_audit(
                c, actor=username, action="account_display_name", ip=ip, user_agent=user_agent,
                detail={"username": username},
            )
    return True


async def change_own_password_with_audit(
    username: str, password_hash: str, *, ip: str | None, user_agent: str | None,
) -> bool:
    """Сменить СВОЙ пароль (UPDATE хеша + аудит). actor==username (самообслуживание, не /team).
    False — нет такого юзера. Текущий пароль вызывающий ОБЯЗАН проверить ДО вызова. Plain НЕ логируем."""
    async with pool.acquire() as c:
        async with c.transaction():
            res = await c.execute(
                "update admin_users set password_hash = $2, updated_at = now() where username = $1",
                username, password_hash,
            )
            if res.endswith(" 0"):
                return False
            await _insert_audit(
                c, actor=username, action="account_password_change", ip=ip, user_agent=user_agent,
                detail={},
            )
    return True


async def revoke_all_sessions_with_audit(
    username: str, *, keep_sid: str | None = None, ip: str | None = None, user_agent: str | None = None,
) -> int:
    """Завершить ВСЕ сеансы пользователя («выйти на всех устройствах»). keep_sid — сессия,
    которую оставить живой (None → ревокать все, включая текущую). Возврат: число ревокнутых.
    Только над своими сессиями (actor==username); чужие не трогаем."""
    async with pool.acquire() as c:
        async with c.transaction():
            if keep_sid:
                res = await c.execute(
                    "update admin_sessions set revoked = true "
                    "where actor = $1 and revoked = false and sid <> $2",
                    username, keep_sid,
                )
            else:
                res = await c.execute(
                    "update admin_sessions set revoked = true where actor = $1 and revoked = false",
                    username,
                )
            await _insert_audit(
                c, actor=username, action="account_sessions_revoke_all", ip=ip, user_agent=user_agent,
                detail={"kept_current": bool(keep_sid)},
            )
    # 'UPDATE N' → N
    try:
        return int(res.rsplit(" ", 1)[1])
    except (ValueError, IndexError):
        return 0


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
    tenant_id,                    # uuid сессии — фолбэк, когда продажа без лида (Wave 3)
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
                     note, created_by, paid_at, tenant_id)
                values ($1, $2, $3, $4, $5, 'manual', $6, $7,
                        case when $5 = 'paid' then now() else null end,
                        coalesce((select tenant_id from leads where id = $1), $8))
                returning id
                """,
                lead_id, product_id, amount, currency, status, note, actor, tenant_id,
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
                "select status, lead_id, tenant_id from orders where id = $1 for update", order_id
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
            # #10: единый путь к 'paid'. Ручной перевод оператором ТОЖЕ конвертит лида (как вебхук
            # mark_order_paid_by_payment) — иначе при офлайн-оплате лид навсегда без конверсии (гонка).
            # «Спасибо» здесь НЕ шлём: ручной статус ≠ онлайн-оплата (лид мог заплатить вне бота),
            # авто-сообщение было бы неожиданным. orders НЕ под RLS, leads под RLS → ставим app.tenant_id
            # из заказа явно (как в вебхук-ветке), иначе конвертация отвергнется (0 строк).
            converted = False
            if new_status == "paid" and old["status"] != "paid" and old["lead_id"] is not None:
                await c.execute("select set_config('app.tenant_id', $1, true)", str(old["tenant_id"]))
                await c.execute("update leads set status = 'converted' where id = $1", old["lead_id"])
                converted = True
            await _insert_audit(
                c, actor=actor, action="order_status", ip=ip, user_agent=user_agent,
                detail={"order_id": str(order_id),
                        "status": {"old": old["status"], "new": new_status},
                        "lead_converted": converted},
            )
            return row


# ── Phase 1B: онлайн-оплата продаж школы (вебхук + «счёт из диалога») ─────────

async def _apply_order_paid(c, row, payment_id: str) -> asyncpg.Record:
    """Общее ядро «заказ оплачен» (в ОТКРЫТОЙ транзакции c): orders.paid (+paid_at, +бэкфилл
    provider_payment_id) → лид 'converted' → «спасибо» В КАНАЛ лида через outbox (доставит бот) →
    аудит. Идемпотентно по status. row: (id, lead_id, status, tenant_id). Используется обеими
    ветками матча вебхука (by_payment / by_order_id, #9) — единый источник логики оплаты."""
    if row["status"] == "paid":
        return row  # повторный вебхук / двойной матч — no-op
    # Вебхук без сессии → centralized-хук app.tenant_id не поставил. orders не под RLS, тенант берём
    # из заказа (== тенант лида) и ставим ЯВНО — иначе RLS на leads/outbox отверг бы конвертацию.
    await c.execute("select set_config('app.tenant_id', $1, true)", str(row["tenant_id"]))
    upd = await c.fetchrow(
        """
        update orders
        set status = 'paid', paid_at = coalesce(paid_at, now()),
            provider_payment_id = coalesce(provider_payment_id, $2)
        where id = $1
        returning id, lead_id, product_id, amount, currency
        """,
        row["id"], payment_id,
    )
    lead = None
    if upd["lead_id"] is not None:
        lead = await c.fetchrow(
            "update leads set status = 'converted' where id = $1 "
            "returning messenger, tg_user_id, vk_user_id, max_chat_id",
            upd["lead_id"],
        )
        if lead is not None:
            # #31: «спасибо» в КАНАЛ лида (vk/max-покупатель тоже получит). messenger в outbox →
            # канальный дренаж воркера; для vk/max tg_user_id=NULL, адрес резолвит воркер из leads.
            m = lead["messenger"] or "tg"
            addr = {"tg": lead["tg_user_id"], "vk": lead["vk_user_id"],
                    "max": lead["max_chat_id"]}.get(m)
            if addr is not None:
                await c.execute(
                    "insert into outbox (lead_id, tg_user_id, messenger, kind, text, status, "
                    "                    created_by, tenant_id) "
                    "values ($1, $2, $3, 'text', $4, 'queued', 'yookassa-webhook', "
                    "        (select tenant_id from leads where id = $1))",
                    upd["lead_id"], (lead["tg_user_id"] if m == "tg" else None), m,
                    config.ORDER_PAID_MESSAGE,
                )
    await _insert_audit(
        c, actor="yookassa-webhook", action="order_paid",
        detail={"order_id": str(upd["id"]), "payment_id": payment_id,
                "amount": str(upd["amount"]), "lead_converted": lead is not None},
    )
    return upd


async def mark_order_paid_by_payment(payment_id: str) -> asyncpg.Record | None:
    """Отметить ЗАКАЗ оплаченным по id платежа ЮKassa (вебхук, ОСНОВНОЙ матч по provider_payment_id).
    Идемпотентно; None — заказа с таким provider_payment_id нет (→ вызывающий пробует фолбэк #9)."""
    async with pool.acquire() as c:
        async with c.transaction():
            # B7/RLS: сперва узнаём тенанта через SECURITY DEFINER (обход RLS), ставим app.tenant_id,
            # затем RLS-скоупленный SELECT FOR UPDATE найдёт заказ. До enable RLS — поведение прежнее.
            tenant = await c.fetchval("select order_tenant_for_payment($1)", payment_id)
            if tenant is None:
                return None
            await c.execute("select set_config('app.tenant_id', $1, true)", str(tenant))
            row = await c.fetchrow(
                "select id, lead_id, status, tenant_id from orders "
                "where provider_payment_id = $1 for update",
                payment_id,
            )
            if row is None:
                return None
            return await _apply_order_paid(c, row, payment_id)


async def mark_order_paid_by_order_id(
    order_id,
    payment_id: str,
    *,
    expected_amount=None,
    expected_meta_order_id: str | None = None,
) -> asyncpg.Record | None:
    """#9 ФОЛБЭК вебхука: заказ НЕ сматчился по provider_payment_id (id платежа не успел записаться
    в orders при создании), но в metadata платежа есть order_id. Платёж УЖЕ верифицирован
    вызывающим через API ЮKassa кредами магазина — здесь применяем результат и БЭКФИЛЛИМ
    provider_payment_id. None — заказа с таким id нет. Идемпотентно (по status).

    W2 (аудит, defense-in-depth): order_id фолбэка пришёл из ТЕЛА вебхука (недоверенный хинт). Перед
    пометкой 'paid' СВЯЗЫВАЕМ верифицированный по API платёж с найденным заказом:
      • expected_meta_order_id — metadata.order_id САМОГО платежа (из API-ответа) — должен совпасть с id
        заказа (легитимный платёж создаётся с metadata.order_id=order.id во всех 3 точках create_payment);
      • expected_amount — amount.value платежа — должен совпасть с orders.amount (сумма позиции).
    Иначе это не наш матч (чужой/несоответствующий succeeded-платёж того же магазина + произвольный
    order_id в теле) → None, заказ НЕ помечается оплаченным. Сверка применяется только если значение
    передано (None → пропуск; обратная совместимость со старым вызовом)."""
    async with pool.acquire() as c:
        async with c.transaction():
            # B7/RLS: тенант через SECURITY DEFINER (обход RLS) → app.tenant_id → RLS-скоупленный select.
            tenant = await c.fetchval("select order_tenant_by_id($1)", order_id)
            if tenant is None:
                return None
            await c.execute("select set_config('app.tenant_id', $1, true)", str(tenant))
            row = await c.fetchrow(
                "select id, lead_id, status, tenant_id, amount from orders where id = $1 for update",
                order_id,
            )
            if row is None:
                return None
            # W2: верифицированный платёж должен ссылаться ИМЕННО на этот заказ и на его сумму.
            if expected_meta_order_id is not None and str(expected_meta_order_id) != str(row["id"]):
                logging.getLogger(__name__).warning(
                    "Фолбэк #9: metadata.order_id платежа %s (%s) ≠ заказу %s — НЕ помечаем paid",
                    payment_id, expected_meta_order_id, row["id"],
                )
                return None
            if expected_amount is not None:
                from shared import money
                if money.rub_to_micro(expected_amount) != money.rub_to_micro(row["amount"]):
                    logging.getLogger(__name__).warning(
                        "Фолбэк #9: сумма платежа %s (%s) ≠ сумме заказа %s (%s) — НЕ помечаем paid",
                        payment_id, expected_amount, row["id"], row["amount"],
                    )
                    return None
            return await _apply_order_paid(c, row, payment_id)


# B7/RLS: discovery-чтения orders ВЕБХУКОМ (сессионно-без app.tenant_id) идут через SECURITY DEFINER-
# функции (migrate_rls_discovery_fns.sql) — они исполняются под владельцем orders и обходят RLS, иначе
# после enable RLS вернули бы 0 строк (заказ не найден → оплата зависла). До enable RLS поведение
# идентично прямому SELECT. Тенант-скоуп — по уникальному аргументу (payment_id/order_id).
async def order_exists_for_payment(payment_id: str) -> bool:
    """Есть ли заказ с таким платежом (выбор ветки вебхука: заказ vs счёт подписки)."""
    async with pool.acquire() as c:
        return bool(await c.fetchval("select order_exists_for_payment($1)", payment_id))


async def get_order_tenant_for_payment(payment_id: str) -> str | None:
    """tenant_id заказа по id платежа ЮKassa, либо None (платёж не наш / не заказ). Слой C:
    вебхук по нему выбирает, КАКИМИ кредами верифицировать платёж (магазин тенанта vs Школы).
    Через SECURITY DEFINER order_tenant_for_payment (обход RLS для sessionless-вебхука)."""
    async with pool.acquire() as c:
        v = await c.fetchval("select order_tenant_for_payment($1)", payment_id)
    return str(v) if v else None


async def get_order_tenant_by_id(order_id: str) -> str | None:
    """#9 фолбэк: tenant_id заказа по ЕГО id (из metadata.order_id платежа), либо None. Защищён от
    кривого/не-uuid order_id (вернёт None, не бросит). Через SECURITY DEFINER order_tenant_by_id
    (обход RLS для sessionless-вебхука). Платёж всё равно верифицируется вызывающим через API."""
    async with pool.acquire() as c:
        try:
            v = await c.fetchval("select order_tenant_by_id($1::uuid)", order_id)
        except Exception:  # noqa: BLE001 — невалидный uuid и т.п.: не наш заказ
            return None
    return str(v) if v else None


async def create_invoice_order_with_audit(
    lead_id, product_id: int, amount, currency: str,
    *, actor: str, ip: str | None, user_agent: str | None,
) -> asyncpg.Record | None:
    """Pending-заказ для «счёта из диалога» (оператор выставляет лиду счёт). None — лид не найден /
    нет адреса в КАНАЛЕ лида (счёт некому доставить). Возвращает (id, messenger, addr).
    #31: канал-агностично — vk/max-лиду тоже можно выставить счёт. Платёж создаёт вызывающий
    (app.py, create_shop_payment) ПОСЛЕ; затем set_order_payment_panel + outbox-сообщение."""
    async with pool.acquire() as c:
        async with c.transaction():
            lead = await c.fetchrow(
                "select messenger, tg_user_id, vk_user_id, max_chat_id "
                "from leads where id = $1 for update",
                lead_id,
            )
            if lead is None:
                return None
            m = lead["messenger"] or "tg"
            addr = {"tg": lead["tg_user_id"], "vk": lead["vk_user_id"],
                    "max": lead["max_chat_id"]}.get(m)
            if addr is None:
                return None  # нет адреса в канале лида
            row = await c.fetchrow(
                """
                insert into orders (lead_id, product_id, amount, currency, status, source,
                                    created_by, tenant_id)
                values ($1, $2, $3, $4, 'pending', 'yookassa', $5,
                        (select tenant_id from leads where id = $1))
                returning id
                """,
                lead_id, product_id, amount, currency, actor,
            )
            await _insert_audit(
                c, actor=actor, action="order_invoice_create", ip=ip, user_agent=user_agent,
                lead_id=lead_id,
                detail={"order_id": str(row["id"]), "product_id": product_id,
                        "amount": str(amount), "messenger": m},
            )
            return await c.fetchrow(
                "select id, $1::text as messenger, $2::bigint as addr from orders where id = $3",
                m, addr, row["id"],
            )


async def set_order_payment_panel(order_id, payment_id: str, payment_url: str) -> None:
    """Связать заказ-«счёт» с платежом ЮKassa (зеркало бот-стороны, но под panel_rw —
    column-гранты на provider_payment_id/payment_url выданы в 1B)."""
    async with pool.acquire() as c:
        await c.execute(
            "update orders set provider_payment_id = $2, payment_url = $3 where id = $1",
            order_id, payment_id, payment_url,
        )


async def enqueue_invoice_message(lead_id, messenger: str, addr: int, text: str, *, actor: str) -> None:
    """Положить лиду сообщение-счёт в outbox (ссылку на оплату доставит БОТ). #31: канал-агностично —
    messenger в outbox → канальный дренаж воркера; для vk/max tg_user_id=NULL (адрес резолвит воркер)."""
    async with pool.acquire() as c:
        await c.execute(
            "insert into outbox (lead_id, tg_user_id, messenger, kind, text, status, created_by, tenant_id) "
            "values ($1, $2, $3, 'text', $4, 'queued', $5, "
            "        (select tenant_id from leads where id = $1))",
            lead_id, (addr if messenger == "tg" else None), messenger, text, actor,
        )


async def get_online_payments_enabled() -> bool:
    """Тумблер онлайн-оплаты из app_settings (нет строки/пусто → ВЫКЛ — зеркало бота)."""
    raw = await get_app_setting(config.ONLINE_PAYMENTS_SETTING_KEY)
    return bool((raw or "").strip())


async def set_online_payments_with_audit(
    enabled: bool, *, actor: str, ip: str | None, user_agent: str | None,
) -> None:
    """Включить/выключить онлайн-оплату (upsert app_settings + аудит, одна транзакция)."""
    async with pool.acquire() as c:
        async with c.transaction():
            await c.execute(
                """
                insert into app_settings (key, value) values ($1, $2)
                on conflict (key) do update set value = excluded.value
                """,
                config.ONLINE_PAYMENTS_SETTING_KEY, "1" if enabled else "",
            )
            await _insert_audit(
                c, actor=actor, action="online_payments_set", ip=ip, user_agent=user_agent,
                detail={"enabled": enabled},
            )


async def list_priced_products_for_invoice() -> list[asyncpg.Record]:
    """Активные оферы с рублёвой ценой — селектор «выставить счёт» в диалоге."""
    q = """
        select id, name, price, currency
        from products
        where status = 'active' and price is not null and price > 0 and currency = 'RUB'
        order by kind desc, created_at desc
    """
    async with pool.acquire() as c:
        return await c.fetch(q)


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


async def service_revenue_total():
    """Сумма ОПЛАЧЕННЫХ счетов подписки по ВСЕЙ платформе (выручка сервиса ЗА ВСЁ ВРЕМЯ) —
    блок «Экономика» (admin). service_invoices теперь под RLS (tenant-scoped) → суммируем
    СКАНОМ по тенантам с set_config (panel_rw без bypassrls), как platform_summary. N мал.
    Сканим ВСЕ статусы (включая suspended/canceled) — выручка all-time не должна терять
    ушедших плательщиков (иначе маржа занижается: себестоимость токенов их ещё включает)."""
    async with pool.acquire() as c:
        tenants = await c.fetch("select id from tenants")
    total = 0
    for t in tenants:
        try:
            async with pool.acquire() as c:
                async with c.transaction():
                    await c.execute("select set_config('app.tenant_id', $1, true)", str(t["id"]))
                    v = await c.fetchval(
                        "select coalesce(sum(amount), 0) from service_invoices where status = 'paid'")
                    total += (v or 0)
        except Exception:  # noqa: BLE001 — сбой одного тенанта не валит экономику
            logging.getLogger(__name__).warning(
                "service_revenue_total: сбой по тенанту %s", t["id"], exc_info=True)
            continue
    return total


async def list_service_invoices(*, limit: int = 60) -> list[asyncpg.Record]:
    """ОПЛАЧЕННЫЕ счета-периоды + использование (сообщений ИИ) за окно каждого периода —
    одним запросом через lateral (столбцы Использовано/Осталось/Превышение в истории).
    Показываем ТОЛЬКО status='paid': pending создаётся при «Выбрать тариф» ДО оплаты
    (нужен, чтобы вебхук привязал платёж) и в истории покупок мелькать не должен."""
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
        where i.status = 'paid'
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
    *, tenant_id, period_start, period_end, plan_key: str, plan_name: str, quota: int | None,
    plan_amount, overage_count: int, overage_amount, amount, currency: str,
    actor: str, ip: str | None, user_agent: str | None,
) -> str:
    """Создать счёт 'pending' за период тарифа (со снимком квоты/превышения) + аудит.
    tenant_id обязателен (service_invoices под RLS): set_config в транзакции, чтобы
    INSERT прошёл with_check политики tenant_isolation (как payments/subscriptions)."""
    if currency not in _PRODUCT_CURRENCY_SET:
        raise ValueError(f"Недопустимая валюта: {currency!r}")
    if not tenant_id:
        raise ValueError("create_period_invoice: tenant_id обязателен (RLS)")
    async with pool.acquire() as c:
        async with c.transaction():
            await c.execute("select set_config('app.tenant_id', $1, true)", str(tenant_id))
            iid = await c.fetchval(
                """
                insert into service_invoices
                    (tenant_id, period_start, period_end, plan_key, plan_name, quota, plan_amount,
                     overage_count, overage_amount, amount, currency, status, created_by)
                values ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,'pending',$12)
                returning id
                """,
                tenant_id, period_start, period_end, plan_key, plan_name, quota, plan_amount,
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
    payment_id: str, *, tenant_id, card_last4: str | None = None, actor: str = "yookassa-webhook"
) -> asyncpg.Record | None:
    """Отметить счёт оплаченным по id платежа ЮKassa (идемпотентно). None — счёт не найден.
    Вызывается ИЗ ВЕБХУКА ПОСЛЕ перепроверки платежа через API ЮKassa (status=succeeded).
    tenant_id (из metadata платежа) обязателен — service_invoices под RLS, вебхук без сессии:
    set_config в транзакции, чтобы SELECT/UPDATE прошли политику (как mark_topup_succeeded)."""
    if not tenant_id:
        raise ValueError("mark_service_invoice_paid_by_payment: tenant_id обязателен (RLS)")
    async with pool.acquire() as c:
        async with c.transaction():
            await c.execute("select set_config('app.tenant_id', $1, true)", str(tenant_id))
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


def _cancel_key(tenant_id) -> str:
    """Ключ флага отмены подписки В РАЗРЕЗЕ ТЕНАНТА. app_settings — глобальная таблица
    (key PK, без RLS), поэтому изолируем суффиксом tenant_id, а не строкой-на-всех
    (иначе отмена одного клиента гасила бы подписку всем — та же утечка, что у service_invoices)."""
    return f"{config.SERVICE_CANCEL_SETTING_KEY}:{tenant_id}"


async def is_subscription_canceled(tenant_id) -> bool:
    if not tenant_id:
        return False
    raw = await get_app_setting(_cancel_key(tenant_id))
    return bool(raw and raw.strip())


async def set_subscription_canceled(
    tenant_id, canceled: bool, *, actor: str, ip: str | None, user_agent: str | None
) -> None:
    """Per-tenant флаг отмены подписки в app_settings (панель пишет; бот не использует). Аудит."""
    if not tenant_id:
        raise ValueError("set_subscription_canceled: tenant_id обязателен")
    value = "1" if canceled else ""
    async with pool.acquire() as c:
        async with c.transaction():
            await c.execute(
                """
                insert into app_settings (key, value) values ($1, $2)
                on conflict (key) do update set value = excluded.value
                """,
                _cancel_key(tenant_id), value,
            )
            await _insert_audit(
                c, actor=actor,
                action="subscription_cancel" if canceled else "subscription_resume",
                ip=ip, user_agent=user_agent, detail={"canceled": canceled, "tenant_id": str(tenant_id)},
            )


# ── Раздел клиента «Мой ИИ-сотрудник»: per-tenant конфиг ИИ в tenant_settings ──
# Клиент правит ТОЛЬКО свои инструкции + фолбэк + тумблер; бот их читает в мультиплексе
# (bot-telegram/db.py::get_tenant_ai_overrides). tenant_settings под RLS (deny-by-default):
# каждый запрос — в транзакции ПОСЛЕ set_config('app.tenant_id') (panel_rw без bypassrls,
# зеркало денежных/секрет-функций). Инфра-ключи (ai_backend/ai_agent_id/ai_model/
# ai_gateway_base_url) НЕ трогаем — их провижионит владелец; читаем лишь факт «бот привязан»
# (есть ли ai_agent_id) для баннера в UI. Дефолты совпадают с get_tenant_ai_overrides бота:
# enabled=True при отсутствии строки (ИИ-сотрудник не «молчит» из-за пустого конфига).
_TENANT_AI_KEYS = (
    config.AI_ENABLED_SETTING_KEY,
    config.AI_SYSTEM_PROMPT_SETTING_KEY,
    config.AI_FALLBACK_SETTING_KEY,
    config.AI_AGENT_ID_SETTING_KEY,   # read-only тут: «бот подключён?» (не пишем)
)


async def get_tenant_ai_config(tenant_id) -> dict:
    """Конфиг ИИ-сотрудника тенанта из tenant_settings (для раздела /my-agent). Зеркалит
    дефолты get_tenant_ai_overrides бота: enabled=True при отсутствии строки. provisioned —
    привязан ли бот (ai_agent_id задан владельцем); агент клиент не видит/не правит."""
    if not tenant_id:
        return {"enabled": True, "system_prompt": "", "fallback": "", "provisioned": False}
    async with pool.acquire() as c:
        async with c.transaction():
            await c.execute("select set_config('app.tenant_id', $1, true)", str(tenant_id))
            rows = await c.fetch(
                "select key, value from tenant_settings "
                "where tenant_id = $1 and key = any($2::text[])",
                tenant_id, list(_TENANT_AI_KEYS),
            )
    kv = {r["key"]: (r["value"] or "") for r in rows}
    enabled_raw = kv.get(config.AI_ENABLED_SETTING_KEY)  # None=нет строки; ''=выключено явно
    return {
        "enabled": True if enabled_raw is None else bool(enabled_raw.strip()),
        "system_prompt": kv.get(config.AI_SYSTEM_PROMPT_SETTING_KEY) or "",
        "fallback": kv.get(config.AI_FALLBACK_SETTING_KEY) or "",
        "provisioned": bool((kv.get(config.AI_AGENT_ID_SETTING_KEY) or "").strip()),
    }


async def set_tenant_ai_config(
    tenant_id, *, enabled: bool, system_prompt: str, fallback: str,
    actor: str, ip: str | None, user_agent: str | None,
) -> None:
    """Сохранить конфиг ИИ-сотрудника тенанта (upsert 3 ключей tenant_settings) + аудит —
    ОДНОЙ транзакцией под set_config('app.tenant_id') (RLS). Пишем ТОЛЬКО клиентские ключи;
    инфра-ключи (agent_id/backend/model) НЕ трогаем (провижининг владельца). «Выключено»/
    пусто — пустым value (delete на tenant_settings не нужен; чтение трактует '' как выкл/нет).
    Длины уже проверены вызывающим (app.py). Аудит — без текста промпта/фолбэка (только флаги)."""
    if not tenant_id:
        raise ValueError("set_tenant_ai_config: tenant_id обязателен")
    pairs = (
        (config.AI_ENABLED_SETTING_KEY, "1" if enabled else ""),
        (config.AI_SYSTEM_PROMPT_SETTING_KEY, system_prompt),
        (config.AI_FALLBACK_SETTING_KEY, fallback),
    )
    async with pool.acquire() as c:
        async with c.transaction():
            await c.execute("select set_config('app.tenant_id', $1, true)", str(tenant_id))
            for key, value in pairs:
                await c.execute(
                    """
                    insert into tenant_settings (tenant_id, key, value)
                    values ($1, $2, $3)
                    on conflict (tenant_id, key) do update
                        set value = excluded.value, updated_at = now()
                    """,
                    tenant_id, key, value,
                )
            await _insert_audit(
                c, actor=actor, action="tenant_ai_config_set", ip=ip, user_agent=user_agent,
                detail={"tenant_id": str(tenant_id), "enabled": enabled,
                        "system_prompt_set": bool(system_prompt), "fallback_set": bool(fallback)},
            )


# ── A3 Слой A: per-tenant адрес эскалации (тот же tenant_settings, RLS) ──────
_TENANT_ESCALATION_KEYS = (
    config.ESCALATION_ENABLED_SETTING_KEY,
    config.ESCALATION_CHAT_ID_SETTING_KEY,
    config.ESCALATION_TOPIC_ID_SETTING_KEY,
)


async def get_tenant_escalation_config(tenant_id) -> dict:
    """Адрес эскалации тенанта из tenant_settings (для блока «Эскалация» в /my-agent). Зеркалит
    bot-telegram/db.py::get_tenant_escalation. Нет строки → выключено и пусто (клиент задаёт сам)."""
    if not tenant_id:
        return {"enabled": False, "chat_id": "", "topic_id": ""}
    async with pool.acquire() as c:
        async with c.transaction():
            await c.execute("select set_config('app.tenant_id', $1, true)", str(tenant_id))
            rows = await c.fetch(
                "select key, value from tenant_settings "
                "where tenant_id = $1 and key = any($2::text[])",
                tenant_id, list(_TENANT_ESCALATION_KEYS),
            )
    kv = {r["key"]: (r["value"] or "") for r in rows}
    return {
        "enabled": bool((kv.get(config.ESCALATION_ENABLED_SETTING_KEY) or "").strip()),
        "chat_id": kv.get(config.ESCALATION_CHAT_ID_SETTING_KEY) or "",
        "topic_id": kv.get(config.ESCALATION_TOPIC_ID_SETTING_KEY) or "",
    }


async def set_tenant_escalation_config(
    tenant_id, *, enabled: bool, chat_id: str, topic_id: str,
    actor: str, ip: str | None, user_agent: str | None,
) -> None:
    """Сохранить адрес эскалации тенанта (upsert 3 ключей tenant_settings) + аудит — ОДНОЙ
    транзакцией под set_config('app.tenant_id') (RLS). chat_id/topic_id — операционный конфиг
    (id группы, не ПДн/секрет), но в аудит кладём только факт/значение chat_id (как guide_url).
    Формат/валидность уже проверены вызывающим (app.py)."""
    if not tenant_id:
        raise ValueError("set_tenant_escalation_config: tenant_id обязателен")
    pairs = (
        (config.ESCALATION_ENABLED_SETTING_KEY, "1" if enabled else ""),
        (config.ESCALATION_CHAT_ID_SETTING_KEY, chat_id),
        (config.ESCALATION_TOPIC_ID_SETTING_KEY, topic_id),
    )
    async with pool.acquire() as c:
        async with c.transaction():
            await c.execute("select set_config('app.tenant_id', $1, true)", str(tenant_id))
            for key, value in pairs:
                await c.execute(
                    """
                    insert into tenant_settings (tenant_id, key, value)
                    values ($1, $2, $3)
                    on conflict (tenant_id, key) do update
                        set value = excluded.value, updated_at = now()
                    """,
                    tenant_id, key, value,
                )
            await _insert_audit(
                c, actor=actor, action="tenant_escalation_set", ip=ip, user_agent=user_agent,
                detail={"tenant_id": str(tenant_id), "enabled": enabled,
                        "chat_id": chat_id or None, "topic_id": topic_id or None},
            )


# ── Конструктор воронки выдачи лид-магнита (tenant_settings, RLS). Раздел панели «Лид-магнит». ──
async def get_funnel_config_panel(tenant_id) -> dict:
    """Сырые значения ключей конструктора воронки для предзаполнения формы панели.
    Под set_config('app.tenant_id') (RLS). Возвращает {key: value} по всем FUNNEL_KEYS
    (отсутствующие — пустая строка), чтобы шаблон не падал на missing-ключах."""
    from shared.leadmagnet import FUNNEL_KEYS
    out = {k: "" for k in FUNNEL_KEYS}
    if not tenant_id:
        return out
    async with pool.acquire() as c:
        async with c.transaction():
            await c.execute("select set_config('app.tenant_id', $1, true)", str(tenant_id))
            rows = await c.fetch(
                "select key, value from tenant_settings where tenant_id = $1 and key = any($2::text[])",
                tenant_id, FUNNEL_KEYS,
            )
    for r in rows:
        out[r["key"]] = r["value"] or ""
    return out


async def get_tenant_legal_urls(tenant_id) -> dict:
    """Публичные ссылки на юр-страницы тенанта ({'privacy','consent'}) для показа в панели.

    Собирает {bot_public_base_url}/legal/{slug}/{doc} — ТЕ ЖЕ URL, что отдаёт бот (_legal_page) и
    строит get_funnel_config. Пусто, если бот ещё не опубликовал публичный base (app_settings) или у
    тенанта нет slug → панель не рисует кнопку, тенант не получает битую ссылку. Read-only;
    tenants/app_settings — глобальные реестры (не tenant-scoped RLS)."""
    from shared.leadmagnet import legal_doc_url
    out = {"privacy": "", "consent": ""}
    if not tenant_id:
        return out
    async with pool.acquire() as c:
        slug = await c.fetchval("select slug from tenants where id = $1", tenant_id)
        base = await c.fetchval(
            "select value from app_settings where key = $1", config.RUNTIME_PUBLIC_BASE_KEY)
    out["privacy"] = legal_doc_url(base, slug, "privacy")
    out["consent"] = legal_doc_url(base, slug, "consent")
    return out


async def set_funnel_config(
    tenant_id, fields: dict, *, actor: str, ip: str | None, user_agent: str | None,
) -> list[str]:
    """Сохранить конфиг воронки (upsert ключей FUNNEL_KEYS) + аудит ОДНОЙ транзакцией под RLS.

    Валидирует через общий shared/leadmagnet.validate_funnel_fields. При ошибках НИЧЕГО не пишет
    и возвращает список человекочитаемых ошибок. Пустой список = успех. Текст согласия НЕ хранится
    как свободный ввод — в боте он генерится из структурных полей (operator_*) тем же модулем."""
    from shared.leadmagnet import FUNNEL_KEYS, validate_funnel_fields
    if not tenant_id:
        raise ValueError("set_funnel_config: tenant_id обязателен")
    errs = validate_funnel_fields(fields)
    if errs:
        return errs
    pairs = [(k, str(fields.get(k) or "").strip()) for k in FUNNEL_KEYS]
    async with pool.acquire() as c:
        async with c.transaction():
            await c.execute("select set_config('app.tenant_id', $1, true)", str(tenant_id))
            for key, value in pairs:
                await c.execute(
                    "insert into tenant_settings (tenant_id, key, value) values ($1, $2, $3) "
                    "on conflict (tenant_id, key) do update "
                    "set value = excluded.value, updated_at = now()",
                    tenant_id, key, value,
                )
            await _insert_audit(
                c, actor=actor, action="tenant_funnel_set", ip=ip, user_agent=user_agent,
                detail={"tenant_id": str(tenant_id),
                        "funnel_enabled": bool(str(fields.get("funnel_enabled") or "").strip()),
                        "leadmagnet_kind": (fields.get("leadmagnet_kind") or None)},
            )
    return []


# ── Дожим (nurture): чтение для формы панели + валидирующая запись (контракт shared.nurture) ──
async def get_tenant_nurture_panel(tid) -> dict:
    """Конфиг дожима для предзаполнения формы: {"enabled": bool, "steps": [{delay_seconds, text}]}.
    enabled здесь = состояние ТУМБЛЕРА (nurture_enabled), а не «есть валидные шаги» — чтобы форма
    показывала реальное положение галки. Шаги — через канонический shared-парсер (как у бота)."""
    from shared.nurture import parse_nurture_steps
    out = {"enabled": False, "steps": []}
    if not tid:
        return out
    async with pool.acquire() as c:
        async with c.transaction():
            await c.execute("select set_config('app.tenant_id', $1, true)", str(tid))
            rows = await c.fetch(
                "select key, value from tenant_settings where tenant_id = $1 and key = any($2::text[])",
                tid, ["nurture_enabled", "nurture_steps"])
    kv = {r["key"]: (r["value"] or "") for r in rows}
    out["enabled"] = bool((kv.get("nurture_enabled") or "").strip())
    out["steps"] = parse_nurture_steps(kv.get("nurture_steps") or "[]")
    return out


async def set_tenant_nurture(
    tid, enabled: bool, raw_steps: list, *, actor: str, ip: str | None, user_agent: str | None,
) -> list[str]:
    """Сохранить конфиг дожима (nurture_enabled + nurture_steps JSON) + аудит ОДНОЙ транзакцией под RLS.
    Валидирует через shared.nurture.normalize_and_validate; при ошибках НИЧЕГО не пишет и возвращает
    список человекочитаемых ошибок (пустой = успех). raw_steps — [{delay_seconds:int|None, text:str}]."""
    from shared.nurture import normalize_and_validate
    if not tid:
        raise ValueError("set_tenant_nurture: tenant_id обязателен")
    clean, errs = normalize_and_validate(enabled, raw_steps)
    if errs:
        return errs
    pairs = (("nurture_enabled", "1" if enabled else ""),
             ("nurture_steps", json.dumps(clean, ensure_ascii=False)))
    async with pool.acquire() as c:
        async with c.transaction():
            await c.execute("select set_config('app.tenant_id', $1, true)", str(tid))
            for key, value in pairs:
                await c.execute(
                    "insert into tenant_settings (tenant_id, key, value) values ($1, $2, $3) "
                    "on conflict (tenant_id, key) do update "
                    "set value = excluded.value, updated_at = now()",
                    tid, key, value)
            await _insert_audit(
                c, actor=actor, action="tenant_nurture_set", ip=ip, user_agent=user_agent,
                detail={"tenant_id": str(tid), "enabled": enabled, "steps": len(clean)})
    return []


# ── Реестр согласий (consent_events, 152-ФЗ): чтение для карточки лида + CSV-экспорт ──
async def list_lead_consent_events(lead_id) -> list[asyncpg.Record]:
    """История согласий лида из реестра consent_events (152-ФЗ ст. 9): кто/когда/действие/версия.
    Tenant-scoped через RLS (app.tenant_id из GUC сессии); panel_rw имеет SELECT. Свежие сверху."""
    async with pool.acquire() as c:
        return await c.fetch(
            "select action, doc_type, doc_version, text_hash, channel, occurred_at "
            "from consent_events where lead_id = $1 order by occurred_at desc, id desc",
            lead_id,
        )


async def stream_consent_events(*, row_cap: int):
    """Курсорный стрим реестра согласий активного тенанта для CSV (RLS по app.tenant_id). Свежие сверху.
    Доказательная выгрузка для РКН (ст. 9): occurred_at/action/версия/хэш текста/канал/lead."""
    async with pool.acquire() as c:
        async with c.transaction():
            async for r in c.cursor(
                "select occurred_at, action, doc_type, doc_version, text_hash, channel, lead_id "
                "from consent_events order by occurred_at desc, id desc limit $1",
                row_cap,
            ):
                yield r


# ── Слой B: CRUD триггеров клиента (tenant_triggers, RLS). Раздел панели «Триггеры». ──
_TRIGGER_SELECT = ("id, type, action, stopwords, intent_desc, msg_count, "
                   "notify_chat_id, notify_topic_id, reply_text, enabled, position")


async def list_tenant_triggers(tenant_id) -> list[asyncpg.Record]:
    """Все триггеры тенанта (для раздела «Триггеры»). Под set_config('app.tenant_id') (RLS)."""
    if not tenant_id:
        return []
    async with pool.acquire() as c:
        async with c.transaction():
            await c.execute("select set_config('app.tenant_id', $1, true)", str(tenant_id))
            return await c.fetch(
                f"select {_TRIGGER_SELECT} from tenant_triggers where tenant_id = $1 "
                "order by type, position, created_at", tenant_id)


async def count_tenant_triggers(tenant_id) -> int:
    """Число триггеров тенанта (для лимита анти-абьюза)."""
    if not tenant_id:
        return 0
    async with pool.acquire() as c:
        async with c.transaction():
            await c.execute("select set_config('app.tenant_id', $1, true)", str(tenant_id))
            return int(await c.fetchval(
                "select count(*) from tenant_triggers where tenant_id = $1", tenant_id) or 0)


async def create_tenant_trigger(
    tenant_id, *, type_: str, action: str, stopwords: list[str], intent_desc: str,
    msg_count: int | None, notify_chat_id: str, notify_topic_id: int | None, reply_text: str,
    actor: str, ip: str | None, user_agent: str | None,
) -> None:
    """Создать триггер тенанта (insert tenant_triggers) + аудит под set_config('app.tenant_id')
    (RLS). Валидность/длины уже проверены вызывающим (app.py). position = max+1."""
    if not tenant_id:
        raise ValueError("create_tenant_trigger: tenant_id обязателен")
    async with pool.acquire() as c:
        async with c.transaction():
            await c.execute("select set_config('app.tenant_id', $1, true)", str(tenant_id))
            pos = int(await c.fetchval(
                "select coalesce(max(position), 0) + 1 from tenant_triggers "
                "where tenant_id = $1 and type = $2", tenant_id, type_) or 1)
            await c.execute(
                """
                insert into tenant_triggers
                    (tenant_id, type, action, stopwords, intent_desc, msg_count,
                     notify_chat_id, notify_topic_id, reply_text, position)
                values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                """,
                tenant_id, type_, action, stopwords, intent_desc, msg_count,
                notify_chat_id, notify_topic_id, reply_text, pos,
            )
            await _insert_audit(
                c, actor=actor, action="tenant_trigger_create", ip=ip, user_agent=user_agent,
                detail={"tenant_id": str(tenant_id), "type": type_, "action": action,
                        "chat_id": notify_chat_id or None},
            )


async def delete_tenant_trigger(
    tenant_id, trigger_id, *, actor: str, ip: str | None, user_agent: str | None,
) -> bool:
    """Удалить триггер тенанта по id (RLS дополнительно скоупит по tenant_id). True — удалён."""
    if not tenant_id:
        return False
    async with pool.acquire() as c:
        async with c.transaction():
            await c.execute("select set_config('app.tenant_id', $1, true)", str(tenant_id))
            res = await c.execute(
                "delete from tenant_triggers where id = $1 and tenant_id = $2",
                trigger_id, tenant_id)
            if res.endswith(" 0"):
                return False
            await _insert_audit(
                c, actor=actor, action="tenant_trigger_delete", ip=ip, user_agent=user_agent,
                detail={"tenant_id": str(tenant_id), "trigger_id": str(trigger_id)},
            )
            return True


# ── Reseller-платформа Wave 1: tenancy + vault (ТЗ §4.1/§4.5) ────────────────
# RLS: tenant_secrets закрыт политикой по current_setting('app.tenant_id') —
# каждый запрос к нему идёт в транзакции ПОСЛЕ set_config(..., is_local=true).
# tenants/memberships — без RLS (резолв доступов ДО установки контекста).

async def list_tenants_for(actor: str, role: str) -> list[asyncpg.Record]:
    """Тенанты, доступные актору. Платформенный супер (ТОЛЬКО env-админ) — все живые; любой
    БД-юзер (оператор/клиент-владелец) — строго по memberships. ⚠️ Ветвление по ЛИЧНОСТИ
    (actor==ADMIN_USERNAME), НЕ по role: self-serve клиент имеет свою учётку в admin_users и
    при ветвлении по role='admin' получил бы ВСЕ тенанты → межтенантная утечка (ревью, critical)."""
    async with pool.acquire() as c:
        if actor == config.ADMIN_USERNAME:
            return await c.fetch(
                "select id, slug, name, status from tenants "
                "where status in ('provisioning','active') order by created_at"
            )
        return await c.fetch(
            """
            select t.id, t.slug, t.name, t.status
            from tenants t join memberships m on m.tenant_id = t.id
            where m.username = $1 and t.status in ('provisioning','active')
            order by t.created_at
            """,
            actor,
        )


async def tenant_accessible(actor: str, role: str, tenant_id) -> bool:
    rows = await list_tenants_for(actor, role)
    return any(str(r["id"]) == str(tenant_id) for r in rows)


async def set_session_tenant(sid: str, tenant_id) -> None:
    async with pool.acquire() as c:
        await c.execute(
            "update admin_sessions set active_tenant_id = $2 where sid = $1",
            sid, tenant_id,
        )


async def platform_summary() -> dict:
    """Платформенная сводка по ВСЕМ подключённым клиентам (тенантам) — раздел admin
    на дашборде (ТЗ §6 «сводка по всем тенантам»). Только для роли admin (вызывающий
    гейтит): экономику платформы клиент-оператор не видит.

    Деньги лежат в tenant-scoped таблицах под RLS (`credit_wallets`/`usage_ledger`/
    `payments`, политика `app.tenant_id`), panel_rw без bypassrls → читаем СКАНОМ по
    тенантам (set_config('app.tenant_id') в транзакции, как list_tenant_secrets/renewal).
    N тенантов мал (цель 3–5), N+1 acquire допустим. Всё в µRUB (int) — форматирует
    вызывающий. Сбой одного тенанта логируется и не валит сводку.

    Возвращает: clients (число живых тенантов), totals (payments/charged/cost/margin/
    wallet по всем) и tenants (та же разбивка на каждого). Определения:
      • payments — сумма succeeded-платежей ЮKassa (подписки + пополнения), деньги «зашли»;
      • charged  — начислено клиентам за ИИ (с наценкой ×множитель), списано из кошельков;
      • cost     — НАША себестоимость токенов Timeweb (до наценки);
      • margin   — charged − cost (заработок на наценке метеринга);
      • wallet   — остаток предоплаты на кошельках (минус = postpaid/переходник Школы)."""
    async with pool.acquire() as c:
        tenants = await c.fetch(
            "select id, slug, name, status from tenants "
            "where status in ('provisioning', 'active') order by created_at"
        )
    rows: list[dict] = []
    tot = {"payments": 0, "charged": 0, "cost": 0, "wallet": 0}
    for t in tenants:
        try:
            async with pool.acquire() as c:
                async with c.transaction():
                    await c.execute(
                        "select set_config('app.tenant_id', $1, true)", str(t["id"]))
                    pay = int(await c.fetchval(
                        "select coalesce(sum(amount_microrub), 0) from payments "
                        "where status = 'succeeded'") or 0)
                    led = await c.fetchrow(
                        "select coalesce(sum(charged_microrub), 0) as charged, "
                        "coalesce(sum(cost_microrub), 0) as cost from usage_ledger")
                    bal = int(await c.fetchval(
                        "select coalesce(sum(balance_microrub), 0) from credit_wallets") or 0)
        except Exception:  # noqa: BLE001 — сбой одного тенанта не валит всю сводку
            logging.getLogger(__name__).warning(
                "platform_summary: сбой по тенанту %s", t["id"], exc_info=True)
            continue
        charged, cost = int(led["charged"]), int(led["cost"])
        rows.append({
            "name": t["name"], "slug": t["slug"], "status": t["status"],
            "payments": pay, "charged": charged, "cost": cost,
            "margin": charged - cost, "wallet": bal,
        })
        tot["payments"] += pay
        tot["charged"] += charged
        tot["cost"] += cost
        tot["wallet"] += bal
    tot["margin"] = tot["charged"] - tot["cost"]
    return {"clients": len(tenants), "tenants": rows, "totals": tot}


async def list_tenant_secrets(tenant_id) -> list[asyncpg.Record]:
    """Метаданные секретов тенанта для UI «Ключи»: ИМЕНА и даты, БЕЗ ciphertext."""
    async with pool.acquire() as c:
        async with c.transaction():
            await c.execute("select set_config('app.tenant_id', $1, true)", str(tenant_id))
            return await c.fetch(
                "select key_name, created_at, last_used_at from tenant_secrets "
                "where tenant_id = $1 order by key_name",
                tenant_id,
            )


async def get_tenant_shop_creds(tenant_id) -> tuple[str, str] | None:
    """(shop_id, secret_key) магазина ЮKassa тенанта из vault — Слой C: вебхук верифицирует
    платёж заказа тенанта ЕГО кредами. None если не заданы ОБА ключа или сбой расшифровки.
    RLS: вебхук без сессии → ставим app.tenant_id ЯВНО (как list_tenant_secrets). AAD расшифровки
    зеркалит запись: f"{tenant_id}:{key_name}"."""
    from shared import vault
    keys = ("shop_yookassa_shop_id", "shop_yookassa_secret_key")
    async with pool.acquire() as c:
        async with c.transaction():
            await c.execute("select set_config('app.tenant_id', $1, true)", str(tenant_id))
            rows = await c.fetch(
                "select key_name, ciphertext, nonce, key_version from tenant_secrets "
                "where tenant_id = $1 and key_name = any($2::text[])",
                tenant_id, list(keys),
            )
    by = {r["key_name"]: r for r in rows}
    if not all(k in by for k in keys):
        return None
    try:
        out = tuple(
            vault.decrypt(bytes(by[k]["ciphertext"]), bytes(by[k]["nonce"]),
                          by[k]["key_version"], aad=f"{tenant_id}:{k}")
            for k in keys
        )
    except Exception:  # noqa: BLE001 — битый секрет/ключ vault: не верифицируем (заказ останется pending)
        logging.getLogger("admin-panel").warning(
            "get_tenant_shop_creds: сбой расшифровки кассы тенанта", exc_info=True)
        return None
    return (out[0], out[1])


async def upsert_tenant_secret(
    tenant_id, key_name: str, ciphertext: bytes, nonce: bytes, key_version: int,
    *, actor: str, ip: str | None, user_agent: str | None,
) -> None:
    """Записать/заменить секрет (vault шифрует ДО вызова — сюда plaintext не попадает).
    Аудит — только key_name (значение в detail НЕ живёт никогда, критерий §8.5)."""
    async with pool.acquire() as c:
        async with c.transaction():
            await c.execute("select set_config('app.tenant_id', $1, true)", str(tenant_id))
            await c.execute(
                """
                insert into tenant_secrets (tenant_id, key_name, ciphertext, nonce, key_version)
                values ($1, $2, $3, $4, $5)
                on conflict (tenant_id, key_name) do update
                set ciphertext = excluded.ciphertext, nonce = excluded.nonce,
                    key_version = excluded.key_version, created_at = now(),
                    last_used_at = null
                """,
                tenant_id, key_name, ciphertext, nonce, key_version,
            )
            await _insert_audit(
                c, actor=actor, action="tenant_secret_set", ip=ip, user_agent=user_agent,
                detail={"tenant_id": str(tenant_id), "key_name": key_name},
            )


async def delete_tenant_secret(
    tenant_id, key_name: str, *, actor: str, ip: str | None, user_agent: str | None,
) -> bool:
    async with pool.acquire() as c:
        async with c.transaction():
            await c.execute("select set_config('app.tenant_id', $1, true)", str(tenant_id))
            res = await c.execute(
                "delete from tenant_secrets where tenant_id = $1 and key_name = $2",
                tenant_id, key_name,
            )
            deleted = res.endswith("1")
            if deleted:
                await _insert_audit(
                    c, actor=actor, action="tenant_secret_delete", ip=ip, user_agent=user_agent,
                    detail={"tenant_id": str(tenant_id), "key_name": key_name},
                )
            return deleted


# ── Reseller Wave 2a: кошелёк + платежи платформы + дедуп вебхука (ТЗ §4.3/§5.3) ──
# Все tenant-scoped запросы — в транзакции после set_config('app.tenant_id') (RLS).

async def get_wallet_balance(tenant_id) -> int:
    """Баланс кошелька тенанта в µRUB (0 — кошелька ещё нет: создаётся первым пополнением)."""
    async with pool.acquire() as c:
        async with c.transaction():
            await c.execute("select set_config('app.tenant_id', $1, true)", str(tenant_id))
            v = await c.fetchval(
                "select balance_microrub from credit_wallets where tenant_id = $1", tenant_id)
            return int(v or 0)


async def create_platform_payment(
    tenant_id, *, type_: str, amount_microrub: int, idempotence_key: str,
) -> str:
    """Запись pending-платежа платформы (топап/подписка) ДО похода в ЮKassa.
    Возвращает id строки (он же — наш Idempotence-Key запроса к ЮKassa)."""
    async with pool.acquire() as c:
        async with c.transaction():
            await c.execute("select set_config('app.tenant_id', $1, true)", str(tenant_id))
            return str(await c.fetchval(
                """
                insert into payments (tenant_id, type, idempotence_key, amount_microrub, status)
                values ($1, $2, $3, $4, 'pending')
                on conflict (idempotence_key) do update set idempotence_key = excluded.idempotence_key
                returning id
                """,
                tenant_id, type_, idempotence_key, amount_microrub,
            ))


async def attach_platform_payment_yk(payment_row_id, tenant_id, yookassa_payment_id: str) -> None:
    async with pool.acquire() as c:
        async with c.transaction():
            await c.execute("select set_config('app.tenant_id', $1, true)", str(tenant_id))
            await c.execute(
                "update payments set yookassa_payment_id = $2 where id = $1",
                payment_row_id, yookassa_payment_id,
            )


async def mark_topup_succeeded(tenant_id, yookassa_payment_id: str, raw: dict) -> bool:
    """Вебхук-ветка топапа: платёж → succeeded + кошелёк += amount. ОДНА транзакция,
    идемпотентно (повторный вебхук того же платежа кошелёк НЕ пополняет повторно).
    Кошелёк блокируется for update (гонка с параллельным списанием metering)."""
    async with pool.acquire() as c:
        async with c.transaction():
            await c.execute("select set_config('app.tenant_id', $1, true)", str(tenant_id))
            row = await c.fetchrow(
                "select id, amount_microrub, status from payments "
                "where yookassa_payment_id = $1 for update",
                yookassa_payment_id,
            )
            if row is None or row["status"] == "succeeded":
                return False                       # неизвестный или уже зачтён — no-op
            await c.execute(
                "update payments set status = 'succeeded', captured_at = now(), raw = $2 "
                "where id = $1",
                row["id"], json.dumps(raw)[:100_000],
            )
            await c.execute("select 1 from credit_wallets where tenant_id = $1 for update", tenant_id)
            await c.execute(
                """
                insert into credit_wallets (tenant_id, balance_microrub, updated_at)
                values ($1, $2, now())
                on conflict (tenant_id) do update
                set balance_microrub = credit_wallets.balance_microrub + excluded.balance_microrub,
                    updated_at = now()
                """,
                tenant_id, int(row["amount_microrub"]),
            )
            # Wave 3: деньги пришли → мягкая пауза ИИ снимается (флаг ставит
            # снапшот-воркер бота при балансе ≤ 0; здесь — единственная точка снятия).
            await c.execute(
                "delete from tenant_settings "
                "where tenant_id = $1 and key = 'ai_wallet_blocked'",
                tenant_id,
            )
            await _insert_audit(
                c, actor="yookassa-webhook", action="wallet_topup",
                detail={"tenant_id": str(tenant_id), "payment_id": yookassa_payment_id,
                        "amount_microrub": int(row["amount_microrub"])},
            )
            return True


# ── Wave 2b: автосписания рекуррента (cron в lifespan панели) ────────────────
# RLS: subscriptions tenant-scoped → cron перебирает тенантов и сканит каждого
# ПОСЛЕ set_config('app.tenant_id') (panel_rw без bypassrls). tenants — без RLS.
async def list_active_tenants_for_renewal() -> list[asyncpg.Record]:
    """Активные тенанты — кандидаты для скана автосписаний (tenants без RLS)."""
    async with pool.acquire() as c:
        return await c.fetch("select id from tenants where status = 'active'")


async def list_due_renewals(tenant_id, *, retry_hours: int, max_attempts: int) -> list[asyncpg.Record]:
    """Подписки тенанта, готовые к автосписанию: живые, с сохранённой картой,
    истёкший период, не превышен потолок попыток, прошёл backoff после прошлой.
    Отдаёт цену/код плана и email для чека (всё для безакцептного платежа)."""
    async with pool.acquire() as c:
        async with c.transaction():
            await c.execute("select set_config('app.tenant_id', $1, true)", str(tenant_id))
            return await c.fetch(
                """
                select s.id, s.current_period_end, s.receipt_email,
                       s.yookassa_payment_method_id, s.charge_attempts,
                       p.code as plan_code, p.price_microrub
                from subscriptions s join plans p on p.id = s.plan_id
                where s.tenant_id = $1
                  and s.status in ('active', 'past_due')
                  and s.yookassa_payment_method_id is not null
                  and s.current_period_end <= now()
                  and s.charge_attempts < $2
                  and (s.last_charge_attempt_at is null
                       or s.last_charge_attempt_at < now() - make_interval(hours => $3))
                order by s.current_period_end
                """,
                tenant_id, max_attempts, retry_hours,
            )


async def bump_renewal_attempt(tenant_id, subscription_id) -> None:
    """Отметить попытку автосписания (backoff + потолок): attempts++, last_attempt=now."""
    async with pool.acquire() as c:
        async with c.transaction():
            await c.execute("select set_config('app.tenant_id', $1, true)", str(tenant_id))
            await c.execute(
                "update subscriptions set charge_attempts = charge_attempts + 1, "
                "last_charge_attempt_at = now() where id = $1", subscription_id)


async def mark_renewal_failed(tenant_id, subscription_id, *, max_attempts: int) -> bool:
    """Неуспех автосписания: при достижении потолка попыток → canceled, иначе past_due.
    Возвращает True, если подписка ушла в canceled (нужен ops-алерт «подписка отменена»)."""
    async with pool.acquire() as c:
        async with c.transaction():
            await c.execute("select set_config('app.tenant_id', $1, true)", str(tenant_id))
            row = await c.fetchrow(
                "update subscriptions set status = "
                "  case when charge_attempts >= $2 then 'canceled' else 'past_due' end "
                "where id = $1 returning status", subscription_id, max_attempts)
    return bool(row and row["status"] == "canceled")


async def renew_subscription(tenant_id, subscription_id, yk_payment_id: str,
                             amount_microrub: int, period_days: int) -> bool:
    """Успешное автосписание: ПРОДЛЕВАЕТ существующую подписку (UPDATE, НЕ новая строка —
    в отличие от первичной activate). Идемпотентно по yookassa_payment_id (payments-журнал).
    Период += period_days от max(текущего конца, now); included_credits начисляются в
    кошелёк; счётчик попыток сброшен; status=active. Возвращает True — продлено (первый
    раз); False — платёж уже обработан (повтор вебхука/гонка cron)."""
    async with pool.acquire() as c:
        async with c.transaction():
            await c.execute("select set_config('app.tenant_id', $1, true)", str(tenant_id))
            row = await c.fetchrow(
                "insert into payments (tenant_id, type, yookassa_payment_id, idempotence_key, "
                "                      amount_microrub, status, captured_at) "
                "values ($1, 'subscription', $2, $3, $4, 'succeeded', now()) "
                "on conflict (yookassa_payment_id) do nothing returning id",
                tenant_id, yk_payment_id, f"renew:{yk_payment_id}", max(int(amount_microrub), 1))
            if row is None:
                return False
            sub = await c.fetchrow(
                "update subscriptions set status = 'active', charge_attempts = 0, "
                "  current_period_start = now(), "
                "  current_period_end = greatest(current_period_end, now()) "
                "                       + make_interval(days => $2) "
                "where id = $1 returning plan_id", subscription_id, period_days)
            if sub is None:
                return False
            inc = int(await c.fetchval(
                "select included_credits_microrub from plans where id = $1", sub["plan_id"]) or 0)
            if inc > 0:
                await c.execute(
                    "select 1 from credit_wallets where tenant_id = $1 for update", tenant_id)
                await c.execute(
                    "insert into credit_wallets (tenant_id, balance_microrub, updated_at) "
                    "values ($1, $2, now()) on conflict (tenant_id) do update "
                    "set balance_microrub = credit_wallets.balance_microrub + excluded.balance_microrub, "
                    "    updated_at = now()", tenant_id, inc)
            await c.execute(
                "delete from tenant_settings where tenant_id = $1 and key = 'ai_wallet_blocked'",
                tenant_id)
            await _insert_audit(
                c, actor="renewal-cron", action="subscription_renewed",
                detail={"tenant_id": str(tenant_id), "subscription_id": str(subscription_id),
                        "payment_id": yk_payment_id, "credited_microrub": inc})
            return True


# Дефолты сида воронки нового тенанта (плейсхолдеры). Реквизиты согласия (operator_*) и сам
# лид-магнит (url/file_id) тенант заполняет сам в разделе «Лид-магнит». funnel_enabled НЕ сеем →
# воронка стартует ВЫКЛЮЧЕННОЙ, пока тенант не настроит и не включит (анти-«полупустая воронка»).
_FUNNEL_SEED_DEFAULTS = {
    "welcome_text": "Здравствуйте! 🌷 Помогу забрать ваш подарок — это займёт минуту.",
    "data_purpose": "отправить материалы и быть на связи по вашему запросу",
    "leadmagnet_kind": "link",
    "leadmagnet_caption": "Готово! 🎉 Лови свой подарок:",
}


async def seed_default_funnel(tenant_id) -> None:
    """Засев дефолт-шаблона воронки выдачи лид-магнита новому тенанту (при активации подписки).

    Идемпотентно и НЕ деструктивно (on conflict do nothing) — на ренью/повторе НЕ перетирает уже
    настроенное тенантом. Под set_config('app.tenant_id') (RLS). funnel_enabled не ставим."""
    if not tenant_id:
        return
    async with pool.acquire() as c:
        async with c.transaction():
            await c.execute("select set_config('app.tenant_id', $1, true)", str(tenant_id))
            for key, value in _FUNNEL_SEED_DEFAULTS.items():
                await c.execute(
                    "insert into tenant_settings (tenant_id, key, value) values ($1, $2, $3) "
                    "on conflict (tenant_id, key) do nothing",
                    tenant_id, key, value)


async def activate_subscription_from_payment(
    tenant_id, plan_code: str, yk_payment_id: str, amount_microrub: int, period_days: int,
    *, payment_method_id: str | None = None, receipt_email: str | None = None,
) -> bool:
    """Wave 4: оплата тарифа → активация подписки + начисление included_credits.

    Одной транзакцией, идемпотентно по yookassa_payment_id (payments-журнал, как
    mark_topup_succeeded). Связывает СТАРУЮ оплату (service_invoices, для UI-витрины
    панели — помечается paid отдельно в вебхуке) с НОВЫМ метерингом:
      • payments(type='subscription', succeeded) — журнал + идемпотентность;
      • subscriptions(plan, active, период) — источник тарифа для get_tenant_plan;
      • tenants.plan_id — фолбэк для get_tenant_plan;
      • credit_wallets += included_credits плана (квота тарифа в кредитах);
      • снятие ai_wallet_blocked (кредиты пришли — пауза ИИ не нужна).
    Возвращает True — активирована (первый раз); False — план не найден/не покупаемый
    (custom) или платёж уже обработан (повтор вебхука). Кошелёк for update — гонка с
    параллельным списанием metering. amount пишем в payments для аудита; кредиты —
    из плана (included_credits), НЕ из amount (overage в кредиты не идёт)."""
    async with pool.acquire() as c:
        async with c.transaction():
            await c.execute("select set_config('app.tenant_id', $1, true)", str(tenant_id))
            plan = await c.fetchrow(
                "select id, included_credits_microrub from plans where code = $1", plan_code)
            if plan is None:
                return False                       # custom/неизвестный план — подписку не активируем
            # Идемпотентность: повторный вебхук того же платежа → строка уже есть → no-op.
            row = await c.fetchrow(
                "insert into payments (tenant_id, type, yookassa_payment_id, idempotence_key, "
                "                      amount_microrub, status, captured_at) "
                "values ($1, 'subscription', $2, $3, $4, 'succeeded', now()) "
                "on conflict (yookassa_payment_id) do nothing returning id",
                tenant_id, yk_payment_id, f"sub:{yk_payment_id}", max(int(amount_microrub), 1))
            if row is None:
                return False                       # уже обработан
            await c.execute(
                "insert into subscriptions (tenant_id, plan_id, status, "
                "                           current_period_start, current_period_end, "
                "                           yookassa_payment_method_id, receipt_email) "
                "values ($1, $2, 'active', now(), now() + make_interval(days => $3), $4, $5)",
                tenant_id, plan["id"], period_days, payment_method_id, receipt_email)
            await c.execute("update tenants set plan_id = $2 where id = $1", tenant_id, plan["id"])
            inc = int(plan["included_credits_microrub"])
            if inc > 0:
                await c.execute(
                    "select 1 from credit_wallets where tenant_id = $1 for update", tenant_id)
                await c.execute(
                    "insert into credit_wallets (tenant_id, balance_microrub, updated_at) "
                    "values ($1, $2, now()) "
                    "on conflict (tenant_id) do update "
                    "set balance_microrub = credit_wallets.balance_microrub + excluded.balance_microrub, "
                    "    updated_at = now()",
                    tenant_id, inc)
            await c.execute(
                "delete from tenant_settings where tenant_id = $1 and key = 'ai_wallet_blocked'",
                tenant_id)
            await _insert_audit(
                c, actor="yookassa-webhook", action="subscription_activated",
                detail={"tenant_id": str(tenant_id), "plan": plan_code,
                        "payment_id": yk_payment_id, "credited_microrub": inc})
    # Активация прошла (первый раз) → засев дефолт-шаблона воронки выдачи лид-магнита новому тенанту.
    # ВНЕ транзакции активации и best-effort: сбой сида НЕ должен откатывать оплату/кредиты.
    try:
        await seed_default_funnel(tenant_id)
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).warning(
            "seed_default_funnel: сбой сида воронки tid=%s", tenant_id, exc_info=True)
    return True


async def list_platform_payments(tenant_id, *, limit: int = 30) -> list[asyncpg.Record]:
    async with pool.acquire() as c:
        async with c.transaction():
            await c.execute("select set_config('app.tenant_id', $1, true)", str(tenant_id))
            return await c.fetch(
                "select type, amount_microrub, status, created_at, captured_at "
                "from payments where tenant_id = $1 order by created_at desc limit $2",
                tenant_id, limit,
            )


# ── Wave 3: раздел «Расход» — лента usage_ledger тенанта ─────────────────────
# Клиенту отдаётся ТОЛЬКО charged (ТЗ §6): себестоимость cost_microrub и
# multiplier этими запросами НЕ ВЫБИРАЮТСЯ вовсе — их нет даже в контексте
# шаблона. Платформенная экономика — отдельный admin-блок «Экономика сервиса».
async def list_usage(tenant_id, *, limit: int = 100) -> list[asyncpg.Record]:
    async with pool.acquire() as c:
        async with c.transaction():
            await c.execute("select set_config('app.tenant_id', $1, true)", str(tenant_id))
            return await c.fetch(
                "select occurred_at, kind, model, units, charged_microrub, "
                "       balance_after_microrub "
                "from usage_ledger where tenant_id = $1 "
                "order by occurred_at desc, id desc limit $2",
                tenant_id, limit,
            )


async def usage_daily(tenant_id, *, days: int = 14) -> list[asyncpg.Record]:
    """Агрегат по дням: число операций + сумма charged (для сводки раздела)."""
    async with pool.acquire() as c:
        async with c.transaction():
            await c.execute("select set_config('app.tenant_id', $1, true)", str(tenant_id))
            return await c.fetch(
                """
                select date_trunc('day', occurred_at) as day,
                       count(*)                       as ops,
                       sum(charged_microrub)          as charged_microrub
                from usage_ledger
                where tenant_id = $1 and occurred_at >= now() - make_interval(days => $2)
                group by 1
                order by 1 desc
                """,
                tenant_id, days,
            )


async def is_tenant_ai_blocked(tenant_id) -> bool:
    """Флаг мягкой паузы ИИ (кошелёк пуст) — для баннера в «Расходе»."""
    async with pool.acquire() as c:
        async with c.transaction():
            await c.execute("select set_config('app.tenant_id', $1, true)", str(tenant_id))
            v = await c.fetchval(
                "select value from tenant_settings "
                "where tenant_id = $1 and key = 'ai_wallet_blocked'",
                tenant_id,
            )
    return bool((v or "").strip())


async def webhook_event_new(external_id: str, event_type: str | None, payload: dict) -> bool:
    """Дедуп входящих уведомлений (webhook_events.external_id unique).
    True — событие новое (обрабатываем); False — повтор (сразу 200)."""
    async with pool.acquire() as c:
        # Слой C: повтор уведомления СБОЙНОГО события (status='failed') → переобрабатываем (ретраи
        # ЮKassa идут ~сутки): транзиентный сбой верификации (сеть / vault / не та касса) не должен
        # навсегда «съесть» оплату → заказ застрял бы pending. 'processed' остаётся дедуплицированным
        # (ветки идемпотентны — оборона в глубину). 'received' (мид-обработка/краш) — не трогаем
        # (без конкурентной двойной обработки). status='received' (не 'pending') — допустимо по CHECK.
        inserted = await c.fetchval(
            """
            insert into webhook_events (external_id, event_type, payload)
            values ($1, $2, $3)
            on conflict (external_id) do update
                set status = 'received', processed_at = null, payload = excluded.payload
                where webhook_events.status = 'failed'
            returning id
            """,
            external_id, event_type, json.dumps(payload)[:100_000],
        )
        return inserted is not None


async def webhook_event_done(external_id: str, ok: bool) -> None:
    async with pool.acquire() as c:
        await c.execute(
            "update webhook_events set status = $2, processed_at = now() where external_id = $1",
            external_id, "processed" if ok else "failed",
        )


async def get_saved_payment_method(tenant_id) -> str | None:
    """Сохранённый способ оплаты автопродления (subscriptions последней живой подписки)."""
    async with pool.acquire() as c:
        async with c.transaction():
            await c.execute("select set_config('app.tenant_id', $1, true)", str(tenant_id))
            return await c.fetchval(
                "select yookassa_payment_method_id from subscriptions "
                "where tenant_id = $1 and status in ('trialing','active','past_due') "
                "and yookassa_payment_method_id is not null "
                "order by created_at desc limit 1",
                tenant_id,
            )


async def detach_payment_method(
    tenant_id, *, actor: str, ip: str | None, user_agent: str | None,
) -> bool:
    """«Отвязать карту»: стереть сохранённый способ оплаты у живых подписок тенанта —
    автопродление выключается (требование ЮKassa к рекурренту: покупатель отвязывает сам)."""
    async with pool.acquire() as c:
        async with c.transaction():
            await c.execute("select set_config('app.tenant_id', $1, true)", str(tenant_id))
            res = await c.execute(
                "update subscriptions set yookassa_payment_method_id = null "
                "where tenant_id = $1 and yookassa_payment_method_id is not null",
                tenant_id,
            )
            n = int(res.split()[-1] or 0)
            if n:
                await _insert_audit(
                    c, actor=actor, action="payment_method_detach", ip=ip, user_agent=user_agent,
                    detail={"tenant_id": str(tenant_id), "subscriptions": n},
                )
            return n > 0
