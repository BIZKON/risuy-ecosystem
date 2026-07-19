#!/usr/bin/env python3
"""Смоук S3 DoD 2-3: accounts-пул — vault round-trip + claim FOR UPDATE SKIP LOCKED + статусы.

Под ролью engine_rw (owner engine.accounts, платформенный пул без RLS). Проверяет:
  (1) round-trip: uuid+encrypt(AAD)+insert → claim_account расшифровал ИСХОДНЫЙ session;
  (2) ротация: перешифровка того же (channel,label) с СОХРАНЕНИЕМ id → claim отдаёт новый session;
  (3) floodwait: mark_account_floodwait(until=now+1ч) → claim=None; mark_account_active → снова отдаёт;
  (4) banned: mark_account_banned → claim=None; last_error БЕЗ session (только класс ошибки);
  (5) нет живого: claim пустого канала → None без исключения;
  (6) регресс изоляции: panel_rw НЕ имеет select/insert/update/delete на engine.accounts;
  (7) SKIP LOCKED: строка, залоченная параллельной tx, пропускается — claim берёт другую;
  (8) floodwait авто-истёк: floodwait_until в прошлом → claim возвращает аккаунт + status→active;
  (9) decrypt-fail: битый конверт → claim демотирует (status≠active) + last_error='decrypt_failed'.

Гард DSN: только эфемерный risuy_dev. Самоочистка (channel уникален на прогон + подчистка префикса).
ENV: ENGINE_ACCOUNTS_SMOKE_DSN (engine_rw), VAULT_MASTER_KEY (hex-64).
"""
import asyncio
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)  # для shared.vault + engine.common.accounts

import asyncpg  # noqa: E402

DSN = os.environ.get("ENGINE_ACCOUNTS_SMOKE_DSN")
if not DSN or "/risuy_dev" not in DSN.split("?")[0]:
    raise SystemExit("Задайте ENGINE_ACCOUNTS_SMOKE_DSN на эфемерном risuy_dev (роль engine_rw).")

# Уникальный канал на прогон → изоляция от любых прочих строк engine.accounts.
CH = f"smoke-s3-{uuid.uuid4().hex[:8]}"
LABEL_PREFIX = "smoke-s3-acc-"


async def _insert_account(pool, vault, label: str, session: str, status: str = "active") -> str:
    acc_id = str(uuid.uuid4())
    ct, nonce, ver = vault.encrypt(session, aad=f"{acc_id}:session")
    async with pool.acquire() as c:
        await c.execute(
            "insert into engine.accounts (id, channel, label, ciphertext, nonce, key_version, status) "
            "values ($1,$2,$3,$4,$5,$6,$7)",
            acc_id, CH, label, ct, nonce, ver, status,
        )
    return acc_id


async def _rotate_account(pool, vault, label: str, session: str) -> str:
    """Ротация как в CLI: найти существующий id, перешифровать под ним, UPDATE (id стабилен)."""
    async with pool.acquire() as c:
        acc_id = str(await c.fetchval(
            "select id from engine.accounts where channel=$1 and label=$2", CH, label))
        ct, nonce, ver = vault.encrypt(session, aad=f"{acc_id}:session")
        await c.execute(
            "update engine.accounts set ciphertext=$2, nonce=$3, key_version=$4 where id=$1",
            acc_id, ct, nonce, ver,
        )
    return acc_id


async def _ciphertext(pool, acc_id: str) -> bytes:
    async with pool.acquire() as c:
        return bytes(await c.fetchval("select ciphertext from engine.accounts where id=$1", acc_id))


async def _cleanup(pool) -> None:
    async with pool.acquire() as c:
        await c.execute("delete from engine.accounts where channel=$1 or label like $2",
                        CH, LABEL_PREFIX + "%")


async def main() -> None:
    from shared import vault
    if not vault.enabled():
        raise SystemExit("VAULT_MASTER_KEY не задан/невалиден в env.")
    from engine.common import accounts

    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    try:
        await _cleanup(pool)

        # (1) round-trip: один живой аккаунт → claim отдаёт исходный session.
        secret1 = "SESSION-ALPHA-" + uuid.uuid4().hex
        id1 = await _insert_account(pool, vault, LABEL_PREFIX + "a", secret1, status="active")
        acc = await accounts.claim_account(pool, CH)
        assert acc is not None, "(1) claim не нашёл живой аккаунт"
        assert acc.id == id1, f"(1) claim вернул чужой id {acc.id} != {id1}"
        assert acc.session_string == secret1, "(1) round-trip session не совпал"

        # (2) ротация: перешифровка того же (channel,label), id стабилен → новый ciphertext+session.
        ct_before = await _ciphertext(pool, id1)
        secret2 = "SESSION-BETA-" + uuid.uuid4().hex
        id_rot = await _rotate_account(pool, vault, LABEL_PREFIX + "a", secret2)
        assert id_rot == id1, f"(2) ротация сменила id {id_rot} != {id1} (AAD рассинхрон!)"
        assert await _ciphertext(pool, id1) != ct_before, "(2) ciphertext не изменился после ротации"
        acc = await accounts.claim_account(pool, CH)
        assert acc is not None and acc.session_string == secret2, "(2) claim не отдал новый session"

        # (3) floodwait: не выбирается до floodwait_until; active → снова выбирается.
        until = datetime.now(timezone.utc) + timedelta(hours=1)
        await accounts.mark_account_floodwait(pool, id1, until)
        assert await accounts.claim_account(pool, CH) is None, "(3) floodwait-аккаунт выбран (не должен)"
        await accounts.mark_account_active(pool, id1)
        acc = await accounts.claim_account(pool, CH)
        assert acc is not None and acc.session_string == secret2, "(3) после active claim молчит"

        # (4) banned → None; last_error — только класс ошибки, БЕЗ session.
        await accounts.mark_account_banned(pool, id1, "UserDeactivatedError")
        assert await accounts.claim_account(pool, CH) is None, "(4) banned-аккаунт выбран (не должен)"
        async with pool.acquire() as c:
            st, err = await c.fetchrow(
                "select status, last_error from engine.accounts where id=$1", id1)
        assert st == "banned" and err == "UserDeactivatedError", "(4) статус/last_error не выставлены"
        assert secret1 not in (err or "") and secret2 not in (err or ""), "(4) session утёк в last_error!"

        # (5) нет живого аккаунта канала → None без исключения.
        assert await accounts.claim_account(pool, CH + "-empty") is None, "(5) пустой канал вернул не None"

        # (6) регресс изоляции: panel_rw слеп к пулу (не ломать engine_tenant_isolation_smoke).
        async with pool.acquire() as c:
            for priv in ("select", "insert", "update", "delete"):
                has = await c.fetchval(
                    "select has_table_privilege('panel_rw','engine.accounts',$1)", priv)
                assert has is False, f"(6) panel_rw НЕ должен иметь {priv} на engine.accounts"

        # (7) FOR UPDATE SKIP LOCKED: залоченная строка пропускается, claim берёт другую.
        await _cleanup(pool)
        idA = await _insert_account(pool, vault, LABEL_PREFIX + "c1", "S-C1-" + uuid.uuid4().hex)
        idB = await _insert_account(pool, vault, LABEL_PREFIX + "c2", "S-C2-" + uuid.uuid4().hex)
        lock_conn = await pool.acquire()
        try:
            tr = lock_conn.transaction()
            await tr.start()
            locked = str(await lock_conn.fetchval(
                "select id from engine.accounts where channel=$1 and status='active' "
                "and (floodwait_until is null or floodwait_until < now()) "
                "order by last_used_at nulls first, id limit 1 for update skip locked", CH))
            assert locked in (idA, idB), "(7) не удалось залочить тестовую строку"
            acc = await accounts.claim_account(pool, CH)  # своё соединение из пула
            assert acc is not None, "(7) claim=None при одной свободной строке (SKIP LOCKED сломан)"
            assert acc.id != locked, f"(7) claim вернул залоченную строку {locked} (SKIP LOCKED не работает)"
            await tr.rollback()
        finally:
            await pool.release(lock_conn)

        # (8) floodwait авто-истёк ([critic-fix I2]): floodwait_until в ПРОШЛОМ → claim
        #     сам возвращает аккаунт в пул И переводит его обратно в active (самовосстановление;
        #     раньше status='floodwait' навсегда выпадал из выборки, floodwait_until было мёртвым).
        await _cleanup(pool)
        secret_fw = "SESSION-FW-" + uuid.uuid4().hex
        id_fw = await _insert_account(pool, vault, LABEL_PREFIX + "fw", secret_fw, status="active")
        past = datetime.now(timezone.utc) - timedelta(minutes=1)
        await accounts.mark_account_floodwait(pool, id_fw, past)
        acc = await accounts.claim_account(pool, CH)
        assert acc is not None and acc.id == id_fw, "(8) истёкший floodwait не авто-возвращён claim'ом"
        assert acc.session_string == secret_fw, "(8) session истёкшего floodwait не расшифрован"
        async with pool.acquire() as c:
            st, fw = await c.fetchrow(
                "select status, floodwait_until from engine.accounts where id=$1", id_fw)
        assert st == "active" and fw is None, \
            f"(8) claim не вернул истёкший floodwait в active (status={st}, floodwait_until={fw})"

        # (9) битый vault-конверт ([critic-fix I2]): claim демотирует аккаунт (не крутит в цикле)
        #     — status≠active, last_error='decrypt_failed' (БЕЗ session) — и возвращает None.
        await _cleanup(pool)
        secret_bad = "SESSION-DECFAIL-" + uuid.uuid4().hex
        id_bad = await _insert_account(pool, vault, LABEL_PREFIX + "bad", secret_bad, status="active")
        async with pool.acquire() as c:  # портим ciphertext (GCM-тег не сойдётся → decrypt бросит)
            await c.execute("update engine.accounts set ciphertext=$2 where id=$1", id_bad, b"\x00" * 48)
        assert await accounts.claim_account(pool, CH) is None, "(9) битый конверт: claim не вернул None"
        async with pool.acquire() as c:
            st, err = await c.fetchrow(
                "select status, last_error from engine.accounts where id=$1", id_bad)
        assert st != "active", f"(9) битый аккаунт не демотирован (status={st}) — будет крутиться в цикле"
        assert err == "decrypt_failed", f"(9) last_error={err!r} (ждали decrypt_failed)"
        assert secret_bad not in (err or ""), "(9) session/секрет утёк в last_error!"

        await _cleanup(pool)
    finally:
        try:
            await _cleanup(pool)
        finally:
            await pool.close()
    print("engine_accounts_smoke: OK (round-trip + ротация(id-стабильна) + floodwait/banned/active + "
          "нет-живого + panel_rw-изоляция + SKIP LOCKED + floodwait-авто-возврат + decrypt-fail-демотация)")


asyncio.run(main())
