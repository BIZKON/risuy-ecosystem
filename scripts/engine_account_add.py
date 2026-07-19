#!/usr/bin/env python3
"""engine-admin CLI: ввод/ротация session-string userbot/VK-аккаунта в engine.accounts.

Шифрует session мастер-ключом vault (AES-256-GCM, AAD={account_id}:session, тот же shared.vault
что и бот) и пишет конверт (ciphertext/nonce/key_version) в engine.accounts под ролью engine_rw
(owner платформенного пула). session и мастер-ключ НИКОГДА не печатаются и НЕ берутся из argv
(только stdin/--session-file — иначе секрет светится в `ps`).

Ротация по (channel,label) идемпотентна и СОХРАНЯЕТ id строки: конверт перешифровывается с
AAD={существующий_id}:session. Если бы ротация выдавала новый uuid (как наивный
INSERT ON CONFLICT), AAD разошёлся бы с persisted id — и claim_account
(engine/common/accounts.py, decrypt по AAD={row.id}:session) не смог бы расшифровать. Поэтому
id стабилен через весь жизненный цикл аккаунта, session меняется поверх.

round-trip ДО commit: перечитываем конверт в той же tx и расшифровываем обратно; несовпадение →
rollback (гарантия, что коллектор сможет получить session). status'ом здесь не управляем — переходы
warmup/active/floodwait/banned делают db-хелперы engine/common/accounts.py (mark_account_*).

Гард прода: пишем только в эфемерный risuy_dev; боевая БД — лишь при ACCOUNT_ADMIN_ALLOW_PROD=yes.

ЗАПУСК (env: ENGINE_ADMIN_DSN|ENGINE_DSN, VAULT_MASTER_KEY):
  echo "<session_string>" | ENGINE_ADMIN_DSN="postgresql://engine_rw:...@host:5432/risuy_dev" \
      VAULT_MASTER_KEY="<hex64>" python scripts/engine_account_add.py \
      --channel telegram --label userbot-1 --phone-masked "+7***1234" --proxy-ref "socks5://..."
"""
import argparse
import asyncio
import os
import sys
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)  # для shared.vault

import asyncpg  # noqa: E402 — после sys.path.insert (как в connect_demo_bot.py)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Ввод/ротация session-string аккаунта engine.accounts (vault-конверт).")
    p.add_argument("--channel", required=True, choices=("telegram", "vk"),
                   help="канал аккаунта")
    p.add_argument("--label", required=True,
                   help="стабильная метка аккаунта — ключ ротации (channel,label)")
    p.add_argument("--phone-masked", default=None,
                   help="маскированный телефон для аудита (не ПДн-контакт)")
    p.add_argument("--proxy-ref", default=None,
                   help="per-account socks5://... — секрет, НЕ логируется")
    p.add_argument("--session-file", default=None,
                   help="файл с session-string; по умолчанию читаем stdin")
    p.add_argument("--dsn", default=None,
                   help="engine-owner DSN; иначе ENGINE_ADMIN_DSN/ENGINE_DSN из env")
    return p.parse_args()


def _read_session(args: argparse.Namespace) -> str:
    """session ТОЛЬКО из stdin/файла — не из argv (иначе видно в `ps`)."""
    if args.session_file:
        with open(args.session_file, encoding="utf-8") as f:
            raw = f.read()
    else:
        raw = sys.stdin.read()
    session = raw.strip()
    if not session:
        raise SystemExit("Пустая session-string (ждём из stdin или --session-file).")
    return session


async def _run(args: argparse.Namespace, dsn: str, session: str) -> None:
    from shared import vault  # требует VAULT_MASTER_KEY в env
    if not vault.enabled():
        raise SystemExit("VAULT_MASTER_KEY не задан/невалиден в env.")
    conn = await asyncpg.connect(dsn)
    try:
        async with conn.transaction():
            # id стабилен: при ротации переиспользуем существующий (AAD должен совпасть с decrypt).
            existing = await conn.fetchval(
                "select id from engine.accounts where channel=$1 and label=$2 for update",
                args.channel, args.label,
            )
            account_id = str(existing) if existing is not None else str(uuid.uuid4())
            ct, nonce, ver = vault.encrypt(session, aad=f"{account_id}:session")
            if existing is not None:
                # coalesce: не затирать proxy_ref/phone, если флаг не передан; status не трогаем.
                await conn.execute(
                    "update engine.accounts set ciphertext=$2, nonce=$3, key_version=$4, "
                    "proxy_ref=coalesce($5, proxy_ref), phone_masked=coalesce($6, phone_masked) "
                    "where id=$1",
                    account_id, ct, nonce, ver, args.proxy_ref, args.phone_masked,
                )
                action = "ротация"
            else:
                await conn.execute(
                    "insert into engine.accounts "
                    "(id, channel, label, phone_masked, ciphertext, nonce, key_version, proxy_ref, status) "
                    "values ($1,$2,$3,$4,$5,$6,$7,$8,'warmup')",
                    account_id, args.channel, args.label, args.phone_masked, ct, nonce, ver,
                    args.proxy_ref,
                )
                action = "новый"
            # round-trip ДО commit: конверт читается той же tx, расшифровка обязана совпасть.
            row = await conn.fetchrow(
                "select ciphertext, nonce, key_version from engine.accounts where id=$1",
                account_id,
            )
            back = vault.decrypt(
                bytes(row["ciphertext"]), bytes(row["nonce"]), row["key_version"],
                aad=f"{account_id}:session",
            )
            if back != session:  # значение НЕ печатаем
                raise SystemExit("round-trip mismatch — откат (claim_account не смог бы расшифровать).")
    finally:
        await conn.close()
    print(f"accounts: OK ({action}) id={account_id} channel={args.channel} label={args.label}")


def main() -> None:
    args = _parse_args()
    dsn = args.dsn or os.environ.get("ENGINE_ADMIN_DSN") or os.environ.get("ENGINE_DSN")
    if not dsn:
        raise SystemExit("Нужен --dsn или ENGINE_ADMIN_DSN/ENGINE_DSN в env (engine-owner DSN).")
    if "/risuy_dev" not in dsn.split("?")[0] and os.environ.get("ACCOUNT_ADMIN_ALLOW_PROD") != "yes":
        raise SystemExit("ОТКАЗ: DSN не risuy_dev. Для боевой БД явно: ACCOUNT_ADMIN_ALLOW_PROD=yes.")
    session = _read_session(args)  # из stdin/файла, НЕ из argv
    asyncio.run(_run(args, dsn, session))


if __name__ == "__main__":
    main()
