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
               tg_user_id, max_user_id, notes, survey, erase_requested_at
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
