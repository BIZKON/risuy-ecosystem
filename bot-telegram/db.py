"""Слой доступа к Postgres через asyncpg. Простой пул + функции по шагам воронки."""
import logging

import asyncpg

import config

pool: asyncpg.Pool | None = None

# Разрешённые колонки касаний — защита от подстановки имени колонки в SQL.
_FOLLOWUP_COLS = {"follow_up_1_at", "follow_up_2_at", "follow_up_3_at"}


async def init() -> None:
    global pool
    # max_size=10 (§5.4 плана): воркеры рассылки/outbox + polling-хендлеры воронки
    # делят один пул; при 5 voronka голодала. Соединение НИКОГДА не держится через
    # await send — claim/запись результата идут отдельными короткими транзакциями.
    pool = await asyncpg.create_pool(config.DATABASE_URL, min_size=1, max_size=10)


async def close() -> None:
    if pool:
        await pool.close()


async def upsert_start(tg_user_id: int, source: str) -> None:
    """Создаёт лид при /start. При повторном /start источник не перетираем (first-touch)."""
    async with pool.acquire() as c:
        await c.execute(
            """
            insert into leads (tg_user_id, messenger, source, status)
            values ($1, 'tg', $2, 'new')
            on conflict (tg_user_id) do update set updated_at = now()
            """,
            tg_user_id, source,
        )


async def set_consent(tg_user_id: int, value: bool) -> None:
    async with pool.acquire() as c:
        await c.execute(
            "update leads set consent = $2 where tg_user_id = $1", tg_user_id, value
        )


async def set_name(tg_user_id: int, name: str) -> None:
    async with pool.acquire() as c:
        await c.execute(
            "update leads set name = $2 where tg_user_id = $1", tg_user_id, name
        )


async def set_phone(tg_user_id: int, phone: str, phone_hash: str) -> None:
    async with pool.acquire() as c:
        await c.execute(
            "update leads set phone = $2, phone_hash = $3 where tg_user_id = $1",
            tg_user_id, phone, phone_hash,
        )


async def set_subscribed(tg_user_id: int, value: bool) -> None:
    async with pool.acquire() as c:
        await c.execute(
            "update leads set subscribed = $2 where tg_user_id = $1", tg_user_id, value
        )


async def mark_guide_sent(tg_user_id: int) -> None:
    """Фиксируем выдачу гайда один раз (guide_sent_at не перетираем при повторе)."""
    async with pool.acquire() as c:
        await c.execute(
            """
            update leads
            set status = 'guide_sent', guide_sent_at = coalesce(guide_sent_at, now())
            where tg_user_id = $1
            """,
            tg_user_id,
        )


async def get_due_followups(col: str, delay_seconds: int) -> list[int]:
    """tg_user_id лидов, которым пора отправить касание col (ещё не отправляли)."""
    assert col in _FOLLOWUP_COLS
    # +2 фильтра (§4 плана): прогрев = маркетинг, подавляется отпиской и ручным
    # перехватом. Фильтр на ЧТЕНИИ — касание не помечается отправленным, resume бесплатный.
    q = f"""
        select tg_user_id from leads
        where messenger = 'tg'
          and tg_user_id is not null
          and guide_sent_at is not null
          and {col} is null
          and unsubscribed_at is null
          and bot_paused = false
          and guide_sent_at + make_interval(secs => $1) <= now()
        limit 100
    """
    async with pool.acquire() as c:
        rows = await c.fetch(q, float(delay_seconds))
    return [r["tg_user_id"] for r in rows]


async def mark_followup_sent(col: str, tg_user_id: int) -> None:
    assert col in _FOLLOWUP_COLS
    q = f"update leads set {col} = now(), status = 'nurturing' where tg_user_id = $1"
    async with pool.acquire() as c:
        await c.execute(q, tg_user_id)


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
                "select source from leads where tg_user_id = $1", tg_user_id
            )
    except Exception as e:  # noqa: BLE001 — выбор персоны не должен ломать авто-ответ
        logging.getLogger(__name__).warning("Не удалось прочитать source лида: %s", e)
        return None


# ── Перехват (bot_paused) ────────────────────────────────────────────────────
async def is_bot_paused(tg_user_id: int) -> bool:
    """True, если оператор взял ручное управление этим лидом. Нет строки → False."""
    async with pool.acquire() as c:
        val = await c.fetchval(
            "select coalesce(bot_paused, false) from leads where tg_user_id = $1",
            tg_user_id,
        )
    return bool(val)


# ── Отписка (152-ФЗ) ─────────────────────────────────────────────────────────
async def set_unsubscribed(tg_user_id: int) -> None:
    """Идемпотентная отписка: первый момент фиксируем, повторный /stop не перетирает."""
    async with pool.acquire() as c:
        await c.execute(
            "update leads set unsubscribed_at = coalesce(unsubscribed_at, now()) "
            "where tg_user_id = $1",
            tg_user_id,
        )


# ── Переписка (messages): резолв lead_id + лог входящих/исходящих ─────────────
async def resolve_lead_id(tg_user_id: int) -> str | None:
    """uuid лида по tg_user_id (может ещё не существовать → None). Мягко, без исключений."""
    async with pool.acquire() as c:
        return await c.fetchval(
            "select id from leads where tg_user_id = $1", tg_user_id
        )


async def log_message(
    *,
    tg_user_id: int,
    direction: str,
    kind: str = "text",
    text: str | None = None,
    file_id: str | None = None,
    source: str | None = None,
    tg_message_id: int | None = None,
    lead_id: str | None = None,
) -> None:
    """Пишет одну строку в messages. lead_id мягко резолвится по tg_user_id, если не передан.

    НИКОГДА не бросает наружу — лог переписки не должен ронять воронку/Лию/рассылку.
    Вызывается из middleware (входящие) и messaging-слоя (исходящие).
    """
    if kind not in _MSG_KINDS:
        kind = "other"
    try:
        async with pool.acquire() as c:
            if lead_id is None:
                lead_id = await c.fetchval(
                    "select id from leads where tg_user_id = $1", tg_user_id
                )
            await c.execute(
                """
                insert into messages
                    (lead_id, tg_user_id, tg_message_id, direction, kind, text, file_id, source)
                values ($1, $2, $3, $4, $5, $6, $7, $8)
                """,
                lead_id, tg_user_id, tg_message_id, direction, kind, text, file_id, source,
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
                where status = 'queued'
                order by id
                limit $1
                for update skip locked
            )
            returning id, lead_id, tg_user_id, kind, text, file_id, attempts, created_at
            """,
            limit,
        )
    return [dict(r) for r in rows]


async def outbox_recheck_address(tg_user_id: int) -> str | None:
    """Re-SELECT перед send: причина пропуска или None если слать можно.

    'no_address' — нет tg_user_id (теоретически); 'erased' — отозвал согласие на ПДн.
    consent для ответа на входящее НЕ требуем (клиент сам написал). §5.10.
    """
    async with pool.acquire() as c:
        row = await c.fetchrow(
            "select tg_user_id, erase_requested_at from leads where tg_user_id = $1",
            tg_user_id,
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
_AUDIENCE_WHERE = (
    "messenger = 'tg' and tg_user_id is not null and consent = true "
    "and unsubscribed_at is null and erase_requested_at is null and bot_paused = false"
)


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
                select id, title, messenger, kind, body_template, recipient_count, product_id
                from broadcasts
                where status = 'queued' and recipient_count is not null
                order by id
                limit 1
                for update skip locked
                """
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
    q = f"""
        insert into broadcast_recipients (broadcast_id, lead_id, tg_user_id)
        select $1, id, tg_user_id from leads where {_AUDIENCE_WHERE}
        on conflict (broadcast_id, lead_id) do nothing
    """
    async with pool.acquire() as c:
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
            returning id, lead_id, tg_user_id, click_token, attempts
            """,
            broadcast_id, limit,
        )
    return [dict(r) for r in rows]


async def recipient_recheck(lead_id: str) -> bool:
    """TOCTOU re-check перед КАЖДЫМ send (§5.1): все 4+1 условия ещё держатся?

    True = слать можно. False = отписался/erase/consent отозван/перехват → skipped.
    """
    q = f"select 1 from leads where id = $1 and {_AUDIENCE_WHERE}"
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
                "insert into link_tokens (token, target_url, broadcast_id, lead_id) "
                "values ($1, $2, $3, $4) on conflict (token) do nothing",
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
)
_AI_BACKENDS = ("cloud_ai", "gateway")


async def get_ai_overrides(source: str | None = None) -> dict:
    """Настройки ИИ из app_settings ПОВЕРХ env (пишет панель). Одним запросом.
    Отсутствие строки → дефолт: enabled=True (сохранить поведение «только env»),
    backend='cloud_ai', agent_id='' (→ config.AGENT_ID), model/gateway_base_url='' (→
    дефолты ai.py), system_prompt='', fallback='' (→ хардкод ai._FALLBACK). Ключи доступа
    (TIMEWEB_AI_TOKEN / AI_GATEWAY_TOKEN) в app_settings НЕ лежат (секреты) — только env.
    Любой сбой чтения трактуем как «нет переопределений»: ИИ не должен молчать из-за БД.
    Логика/дефолты ДОЛЖНЫ совпадать с панелью (admin-panel/db.py::get_ai_settings).

    source — канал лида («ИИ-сотрудник на канал», панель → «Каналы»): если для него
    назначена персона, панель кладёт ключи `ai_agent_id__<source>` (cloud_ai: СВОЙ агент
    персоны) и `ai_system_prompt__<source>` (gateway: промпт персоны). Непустой
    per-канальный ключ ПОБЕЖДАЕТ глобальный; пусто/нет строки → глобальное поведение."""
    keys = list(_AI_SETTING_KEYS)
    src = (source or "").strip()
    if src:
        keys += [f"ai_agent_id__{src}", f"ai_system_prompt__{src}"]
    try:
        async with pool.acquire() as c:
            rows = await c.fetch(
                "select key, value from app_settings where key = any($1::text[])",
                keys,
            )
    except Exception as e:  # noqa: BLE001 — сбой чтения не должен ломать авто-ответ
        logging.getLogger(__name__).warning("Не удалось прочитать настройки ИИ: %s", e)
        return {"enabled": True, "backend": "cloud_ai", "agent_id": "", "model": "",
                "gateway_base_url": "", "system_prompt": "", "fallback": ""}
    kv = {r["key"]: (r["value"] or "") for r in rows}
    enabled_raw = kv.get("ai_enabled")  # None=нет строки; ''=выключено явно
    backend = (kv.get("ai_backend") or "").strip()
    if backend not in _AI_BACKENDS:
        backend = "cloud_ai"
    agent_id = (kv.get("ai_agent_id") or "").strip()
    system_prompt = kv.get("ai_system_prompt") or ""
    if src:
        agent_id = (kv.get(f"ai_agent_id__{src}") or "").strip() or agent_id
        system_prompt = (kv.get(f"ai_system_prompt__{src}") or "") or system_prompt
    return {
        "enabled": True if enabled_raw is None else bool(enabled_raw.strip()),
        "backend": backend,
        "agent_id": agent_id,
        "model": (kv.get("ai_model") or "").strip(),
        "gateway_base_url": (kv.get("ai_gateway_base_url") or "").strip(),
        "system_prompt": system_prompt,
        "fallback": kv.get("ai_fallback_text") or "",
    }


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
            "insert into link_clicks (token, broadcast_id, lead_id, ua, ip) "
            "values ($1, $2, $3, $4, $5::inet)",
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
              and erase_requested_at + make_interval(days => $1) <= now()
              and (name is not null or phone is not null
                   or phone_hash is not null or notes is not null)
            limit 100
            """,
            after_days,
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
              and (text is not null or file_id is not null)
            """,
            ttl_days,
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

async def get_lead_for_purchase(tg_user_id: int) -> dict | None:
    """Лид для оформления заказа: id (FK заказа), name (описание платежа), phone (чек
    54-ФЗ, если включён). None — лида нет (заказ без лида не оформляем)."""
    async with pool.acquire() as c:
        row = await c.fetchrow(
            "select id, name, phone from leads where tg_user_id = $1", tg_user_id
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
                insert into orders (lead_id, product_id, amount, currency, status, source, created_by)
                values ($1, $2, $3, $4, 'pending', 'yookassa', 'bot')
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
              and created_at < now() - make_interval(hours => $1)
            """,
            hours,
        )
    return _affected(res)
