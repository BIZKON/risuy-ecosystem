#!/usr/bin/env python3
"""Smoke: db.get_funnel_config читает пер-тенант настройки воронки из tenant_settings (risuy_dev).
Создаёт throwaway-тенант, сидит ключи, проверяет сборку cfg + генерацию consent_text, чистит
за собой (удаление тенанта каскадом сносит tenant_settings).

Запуск (owner-DSN risuy_dev + заглушки env бота):
  FUNNEL_SMOKE_DSN="postgresql://gen_user:<pw>@HOST:5432/risuy_dev?sslmode=require" \
  PYTHONPATH=bot-telegram BOT_TOKEN=x DATABASE_URL="$FUNNEL_SMOKE_DSN" CHANNEL_ID=-100 \
  CHANNEL_URL=https://t.me/x GUIDE_URL=https://x ./.venv-smoke/bin/python scripts/funnel_config_smoke.py
"""
import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)  # пакет shared (ленивый импорт build_consent_text внутри db.get_funnel_config)

import db  # noqa: E402  (bot-telegram на PYTHONPATH)

DSN = os.environ.get("FUNNEL_SMOKE_DSN") or os.environ.get("DATABASE_URL")
if not DSN or "risuy_dev" not in DSN:
    raise SystemExit("Задайте DSN на risuy_dev (защита от прода).")

SLUG = "smoke-funnel-cfg"


async def main() -> None:
    await db.init()
    fails: list[str] = []
    async with db.pool.acquire() as c:
        async def drop_tenant() -> None:
            await c.execute("delete from tenants where slug = $1", SLUG)  # cascade → tenant_settings

        await drop_tenant()
        tid = await c.fetchval(
            "insert into tenants (slug, name, status) values ($1, 'SMOKE воронка', 'active') returning id",
            SLUG)
        try:
            # 1) тенант БЕЗ ключей → enabled=False (Школа/непровиженный путь)
            cfg0 = await db.get_funnel_config(tid)
            if cfg0["enabled"]:
                fails.append("без ключей funnel enabled должно быть False")
            if cfg0["consent_text"]:
                fails.append("без operator_* consent_text должен быть пустым")

            # 2) засев ключей воронки
            pairs = {
                "funnel_enabled": "1", "welcome_text": "Привет от тенанта!",
                "operator_name": "ИП Тест Т.Т.", "operator_inn": "770000000000", "operator_email": "t@t.ru",
                "privacy_url": "https://t.ru/p", "phone_step_enabled": "1",
                "leadmagnet_kind": "link", "leadmagnet_url": "https://t.ru/guide.pdf",
                "leadmagnet_caption": "Лови гайд",
            }
            for k, v in pairs.items():
                await c.execute(
                    "insert into tenant_settings (tenant_id, key, value) values ($1, $2, $3)", tid, k, v)

            cfg = await db.get_funnel_config(tid)
            if not cfg["enabled"]:
                fails.append("enabled должно быть True")
            if cfg["welcome_text"] != "Привет от тенанта!":
                fails.append(f"welcome_text: {cfg['welcome_text']!r}")
            ct = cfg["consent_text"]
            for must in ("ИП Тест Т.Т.", "770000000000", "t@t.ru"):
                if must not in ct:
                    fails.append(f"в consent_text нет {must!r}")
            if "политик" not in ct.lower():
                fails.append("privacy_url задан, а политики в согласии нет")
            if cfg["leadmagnet"]["kind"] != "link":
                fails.append(f"leadmagnet kind: {cfg['leadmagnet']['kind']!r}")
            if cfg["leadmagnet"]["url"] != "https://t.ru/guide.pdf":
                fails.append(f"leadmagnet url: {cfg['leadmagnet']['url']!r}")
            if cfg["phone_step"] is not True:
                fails.append("phone_step должно быть True")
            if cfg["company_name"] != "ИП Тест Т.Т.":
                fails.append(f"company_name фолбэк на operator_name не сработал: {cfg['company_name']!r}")
            if cfg["gate"]["enabled"]:
                fails.append("gate.enabled без ключа должно быть False")
        finally:
            await drop_tenant()

    if fails:
        print("\n".join("❌ " + f for f in fails))
        raise SystemExit(1)
    print("🟢 funnel_config_smoke зелёный")


if __name__ == "__main__":
    asyncio.run(main())
