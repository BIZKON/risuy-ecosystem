#!/usr/bin/env python3
"""Smoke: seed_default_funnel — идемпотентный сид дефолт-воронки новому тенанту (risuy_dev).
Throwaway-тенант; проверяет: дефолты засеяны, funnel_enabled НЕ выставлен (воронка выкл),
operator_* пустые (тенант заполняет), повторный сид НЕ перетирает пользовательское значение.

Запуск (owner-DSN risuy_dev + заглушки config панели):
  FUNNEL_SMOKE_DSN="postgresql://gen_user:<pw>@HOST:5432/risuy_dev?sslmode=require" \
  DATABASE_URL="$FUNNEL_SMOKE_DSN" SESSION_SECRET="$(python3 -c 'import secrets;print(secrets.token_urlsafe(48))')" \
  ADMIN_USERNAME=smoke ADMIN_PASSWORD_HASH='$argon2id$v=19$m=65536,t=3,p=4$c21va2VzbW9rZQ$c21va2VzbW9rZXNtb2tl' \
  PYTHONPATH=admin-panel ./.venv-smoke/bin/python scripts/leadmagnet_seed_smoke.py
"""
import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)  # пакет shared (FUNNEL_KEYS внутри get_funnel_config_panel)

import db  # noqa: E402  (admin-panel на PYTHONPATH)

DSN = os.environ.get("FUNNEL_SMOKE_DSN") or os.environ.get("DATABASE_URL")
if not DSN or "risuy_dev" not in DSN:
    raise SystemExit("Задайте DSN на risuy_dev (защита от прода).")

SLUG = "smoke-funnel-seed"


async def main() -> None:
    await db.init()
    fails: list[str] = []
    async with db.pool.acquire() as c:
        async def drop() -> None:
            await c.execute("delete from tenants where slug = $1", SLUG)

        await drop()
        tid = await c.fetchval(
            "insert into tenants (slug, name, status) values ($1, 'SMOKE seed', 'active') returning id",
            SLUG)
        try:
            await db.seed_default_funnel(tid)
            v = await db.get_funnel_config_panel(tid)
            if v.get("welcome_text") != db._FUNNEL_SEED_DEFAULTS["welcome_text"]:
                fails.append("welcome_text не засеян дефолтом")
            if v.get("leadmagnet_kind") != "link":
                fails.append(f"leadmagnet_kind дефолт не 'link': {v.get('leadmagnet_kind')!r}")
            if v.get("data_purpose") != db._FUNNEL_SEED_DEFAULTS["data_purpose"]:
                fails.append("data_purpose не засеян")
            if v.get("funnel_enabled"):
                fails.append("funnel_enabled НЕ должен быть выставлен (воронка выкл до настройки)")
            if v.get("operator_name"):
                fails.append("operator_name должен быть пустым (тенант заполняет сам)")

            # идемпотентность: тенант отредактировал приветствие → повторный сид не перетирает
            await c.execute(
                "update tenant_settings set value = 'МОЁ приветствие' "
                "where tenant_id = $1 and key = 'welcome_text'", tid)
            await db.seed_default_funnel(tid)
            v2 = await db.get_funnel_config_panel(tid)
            if v2.get("welcome_text") != "МОЁ приветствие":
                fails.append("повторный сид перетёр пользовательское значение (должен on conflict do nothing)")
        finally:
            await drop()

    if fails:
        print("\n".join("❌ " + f for f in fails))
        raise SystemExit(1)
    print("🟢 leadmagnet_seed_smoke зелёный")


if __name__ == "__main__":
    asyncio.run(main())
