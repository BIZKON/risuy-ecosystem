#!/usr/bin/env python3
"""Подключить Telegram-бот к ДЕМО-тенанту (slug='demo-sandbox'): шифрует токен в vault тем же
shared.vault (AES-256-GCM, мастер-ключ VAULT_MASTER_KEY из env) и пишет в tenant_secrets под ключом
'telegram_bot_token'. Мультиплекс (_reconcile, hot-reload) поднимет per-tenant бот ≤интервала сверки.

🟥 БЕЗОПАСНОСТЬ:
  • Трогает ТОЛЬКО демо-тенант (slug='demo-sandbox'); другие тенанты/секреты не затрагиваются.
  • Гард прода: на боевой `risuy` — лишь при SEED_ALLOW_PROD=yes.
  • Токен и мастер-ключ НИКОГДА не печатаются. Round-trip-проверка: после записи расшифровывает обратно
    тем же aad — гарантия, что бот сможет расшифровать (иначе демо просто не ответит, никого не ломая).
  • Идемпотентно (on conflict update). aad = "{tenant_id}:telegram_bot_token" (зеркалит bot/db.py).

ЗАПУСК (нужны в env: DEMO_DSN, DEMO_BOT_TOKEN, VAULT_MASTER_KEY):
  SEED_ALLOW_PROD=yes DEMO_DSN="...risuy..." DEMO_BOT_TOKEN="123:AA..." VAULT_MASTER_KEY="<hex>" \
      ./.venv-smoke/bin/python scripts/connect_demo_bot.py
"""
import asyncio
import os
import sys

import asyncpg

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)  # для shared.vault

SLUG = "demo-sandbox"
KEY_NAME = "telegram_bot_token"
DSN = os.environ.get("DEMO_DSN")
TOKEN = os.environ.get("DEMO_BOT_TOKEN")
if not DSN or not TOKEN:
    raise SystemExit("Нужны DEMO_DSN и DEMO_BOT_TOKEN в env.")
DBNAME = DSN.split("?")[0].rstrip("/").split("/")[-1]
if DBNAME == "risuy" and os.environ.get("SEED_ALLOW_PROD") != "yes":
    raise SystemExit("ОТКАЗ: боевой risuy. Для прода явно: SEED_ALLOW_PROD=yes.")


async def main():
    from shared import vault  # требует VAULT_MASTER_KEY в env
    if not vault.enabled():
        raise SystemExit("VAULT_MASTER_KEY не задан/невалиден в env.")
    print(f"connect_demo_bot · база={DBNAME} · slug={SLUG} · key={KEY_NAME}")
    c = await asyncpg.connect(DSN)
    try:
        tid = await c.fetchval("select id from tenants where slug = $1", SLUG)
        if tid is None:
            raise SystemExit("Демо-тенанта нет — сначала seed_demo_tenant.py.")
        aad = f"{tid}:{KEY_NAME}"
        ct, nonce, ver = vault.encrypt(TOKEN, aad=aad)
        async with c.transaction():
            await c.execute("select set_config('app.tenant_id', $1, true)", str(tid))
            await c.execute(
                """
                insert into tenant_secrets (tenant_id, key_name, ciphertext, nonce, key_version)
                values ($1, $2, $3, $4, $5)
                on conflict (tenant_id, key_name) do update
                set ciphertext = excluded.ciphertext, nonce = excluded.nonce,
                    key_version = excluded.key_version, created_at = now(), last_used_at = null
                """,
                tid, KEY_NAME, ct, nonce, ver,
            )
        # round-trip: бот расшифрует тем же ключом/aad?
        row = await c.fetchrow(
            "select ciphertext, nonce, key_version from tenant_secrets where tenant_id=$1 and key_name=$2",
            tid, KEY_NAME)
        back = vault.decrypt(bytes(row["ciphertext"]), bytes(row["nonce"]), row["key_version"], aad=aad)
        ok = back == TOKEN
        print(f"✅ токен записан в vault демо-тенанта {tid}; round-trip={'OK' if ok else 'FAIL'}")
        if not ok:
            raise SystemExit("❌ round-trip mismatch — бот не сможет расшифровать. Проверь VAULT_MASTER_KEY.")
        print("   Мультиплекс поднимет per-tenant бот в течение интервала сверки. Напиши боту в ЛС для теста.")
    finally:
        await c.close()


if __name__ == "__main__":
    asyncio.run(main())
