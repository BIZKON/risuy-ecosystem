"""Пул userbot/VK-аккаунтов engine.accounts: выборка живого + статус-переходы.

engine.accounts — платформенная таблица (без RLS, owner engine_rw): set_tenant не нужен.
Секреты (session_string) расшифровываются ТОЛЬКО в память воркера через shared.vault;
last_error пишется БЕЗ session (гарантия §8.5 vault). AAD конверта = {account_id}:session
(этот id — persisted id строки; CLI ротации сохраняет id, иначе AAD разошёлся бы с decrypt).
"""
from __future__ import annotations

import dataclasses
import logging

logger = logging.getLogger("engine.accounts")


@dataclasses.dataclass(frozen=True)
class Account:
    id: str
    proxy_ref: str | None
    session_string: str  # расшифровано в памяти; НЕ логировать


async def claim_account(pool, channel: str):
    """Взять живой аккаунт канала (FOR UPDATE SKIP LOCKED — без гонки коллекторов).

    Живой = status='active' и не в активном floodwait. None, если нет пригодного.
    Расшифровка session через shared.vault (AAD={id}:session). Ошибка vault → None + warning.
    """
    from shared import vault  # ленивый импорт (shared/ в образе рядом с engine/)

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            update engine.accounts set last_used_at = now()
            where id = (
                select id from engine.accounts
                where channel = $1 and status = 'active'
                  and (floodwait_until is null or floodwait_until < now())
                order by last_used_at nulls first
                limit 1 for update skip locked
            )
            returning id, ciphertext, nonce, key_version, proxy_ref
            """,
            channel,
        )
    if row is None:
        logger.warning("Нет пригодного аккаунта канала %s", channel)
        return None
    try:
        session = vault.decrypt(
            bytes(row["ciphertext"]), bytes(row["nonce"]), row["key_version"],
            aad=f"{row['id']}:session",
        )
    except Exception:  # noqa: BLE001 — vault.VaultError и пр.; значение НЕ логируем
        logger.warning("Аккаунт %s: расшифровка session не прошла", row["id"])
        return None
    return Account(id=str(row["id"]), proxy_ref=row["proxy_ref"], session_string=session)


async def mark_account_floodwait(pool, account_id: str, until) -> None:
    """status='floodwait' до until; last_error='floodwait' (БЕЗ session/секретов)."""
    async with pool.acquire() as conn:
        await conn.execute(
            "update engine.accounts set status='floodwait', floodwait_until=$2, "
            "last_error='floodwait' where id=$1",
            account_id, until,
        )
    logger.info("Аккаунт %s → floodwait до %s", account_id, until)


async def mark_account_banned(pool, account_id: str, reason: str) -> None:
    """status='banned'; reason — класс ошибки (без session), обрезан до 200 символов."""
    async with pool.acquire() as conn:
        await conn.execute(
            "update engine.accounts set status='banned', last_error=$2 where id=$1",
            account_id, reason[:200],  # reason — класс ошибки, БЕЗ session
        )
    logger.info("Аккаунт %s → banned", account_id)


async def mark_account_active(pool, account_id: str) -> None:
    """warmup/floodwait → active; чистит floodwait_until и last_error."""
    async with pool.acquire() as conn:
        await conn.execute(
            "update engine.accounts set status='active', floodwait_until=null, last_error=null "
            "where id=$1",
            account_id,
        )
    logger.info("Аккаунт %s → active", account_id)
