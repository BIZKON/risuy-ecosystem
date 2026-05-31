"""Слой доступа к Postgres через asyncpg. Простой пул + функции по шагам воронки."""
import asyncpg

import config

pool: asyncpg.Pool | None = None

# Разрешённые колонки касаний — защита от подстановки имени колонки в SQL.
_FOLLOWUP_COLS = {"follow_up_1_at", "follow_up_2_at", "follow_up_3_at"}


async def init() -> None:
    global pool
    pool = await asyncpg.create_pool(config.DATABASE_URL, min_size=1, max_size=5)


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
    q = f"""
        select tg_user_id from leads
        where messenger = 'tg'
          and tg_user_id is not null
          and guide_sent_at is not null
          and {col} is null
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
