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

    Пригоден = status='active' ЛИБО status='floodwait' с ИСТЁКШИМ floodwait_until
    (самовосстановление пула: раньше floodwait-аккаунт выпадал навсегда — status-фильтр
    'active' делал под-условие floodwait_until мёртвым, [critic-fix I2]). Выданный
    истёкший-floodwait тем же UPDATE возвращается в active (floodwait_until=null).
    None — если нет пригодного. Расшифровка session через shared.vault (AAD={id}:session);
    битый конверт → аккаунт демотируется (не крутится в цикле) + last_error БЕЗ session, None.
    """
    from shared import vault  # ленивый импорт (shared/ в образе рядом с engine/)

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            update engine.accounts
               set last_used_at = now(), status = 'active', floodwait_until = null
            where id = (
                select id from engine.accounts
                where channel = $1
                  and (
                    status = 'active'
                    or (status = 'floodwait' and floodwait_until is not null
                        and floodwait_until < now())
                  )
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
        # Битый конверт (чужой мастер-ключ/повреждение/рассинхрон AAD): снимаем аккаунт с
        # выдачи, чтобы claim не выбирал его снова и снова. last_error — КЛАСС ошибки, БЕЗ
        # session/ciphertext (гарантия §8.5 vault). Оператор перешифрует (CLI) → mark_active.
        await _mark_decrypt_failed(pool, row["id"])
        logger.warning("Аккаунт %s: расшифровка session не прошла — снят с выдачи", row["id"])
        return None
    return Account(id=str(row["id"]), proxy_ref=row["proxy_ref"], session_string=session)


async def _mark_decrypt_failed(pool, account_id) -> None:
    """Демотировать аккаунт с битым vault-конвертом: status='banned' + last_error='decrypt_failed'.

    Нет отдельного статуса под decrypt-fail (DDL CHECK = warmup|active|floodwait|banned) →
    переиспользуем 'banned' как «вне выдачи», а last_error='decrypt_failed' отличает причину
    от реального бана (аудит/восстановление). Возврат в пул — после ротации + mark_account_active.
    """
    async with pool.acquire() as conn:
        await conn.execute(
            "update engine.accounts set status='banned', last_error='decrypt_failed' where id=$1",
            account_id,
        )
    logger.info("Аккаунт %s → снят (decrypt_failed)", account_id)


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
