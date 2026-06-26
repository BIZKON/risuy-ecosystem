#!/usr/bin/env python3
"""Smoke панели: set_funnel_config / get_funnel_config_panel — валидирующая запись и чтение
конфига воронки (tenant_settings) на risuy_dev. Throwaway-тенант, чистка каскадом.

Запуск (owner-DSN risuy_dev + заглушки config панели):
  FUNNEL_SMOKE_DSN="postgresql://gen_user:<pw>@HOST:5432/risuy_dev?sslmode=require" \
  DATABASE_URL="$FUNNEL_SMOKE_DSN" SESSION_SECRET="$(python3 -c 'import secrets;print(secrets.token_urlsafe(48))')" \
  ADMIN_USERNAME=smoke ADMIN_PASSWORD_HASH='$argon2id$v=19$m=65536,t=3,p=4$c21va2VzbW9rZQ$c21va2VzbW9rZXNtb2tl' \
  PYTHONPATH=admin-panel ./.venv-smoke/bin/python scripts/funnel_panel_smoke.py
"""
import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)  # пакет shared (validate_funnel_fields / FUNNEL_KEYS внутри db)

import db  # noqa: E402  (admin-panel на PYTHONPATH)

DSN = os.environ.get("FUNNEL_SMOKE_DSN") or os.environ.get("DATABASE_URL")
if not DSN or "risuy_dev" not in DSN:
    raise SystemExit("Задайте DSN на risuy_dev (защита от прода).")

SLUG = "smoke-funnel-panel"
VALID = {
    "funnel_enabled": "1", "welcome_text": "Здравствуйте от тенанта!",
    "operator_name": "ООО Ромашка", "operator_inn": "7700000000", "operator_email": "info@romashka.ru",
    "privacy_url": "https://romashka.ru/privacy", "leadmagnet_kind": "link",
    "leadmagnet_url": "https://romashka.ru/lead.pdf", "leadmagnet_caption": "Ваш материал",
}


async def main() -> None:
    await db.init()
    fails: list[str] = []
    async with db.pool.acquire() as c:
        async def drop() -> None:
            await c.execute("delete from tenants where slug = $1", SLUG)  # cascade → tenant_settings

        await drop()
        tid = await c.fetchval(
            "insert into tenants (slug, name, status) values ($1, 'SMOKE panel', 'active') returning id",
            SLUG)
        try:
            # 1) валидный набор → запись без ошибок, round-trip совпал
            errs = await db.set_funnel_config(tid, VALID, actor="smoke", ip=None, user_agent=None)
            if errs:
                fails.append(f"валидный набор дал ошибки: {errs}")
            got = await db.get_funnel_config_panel(tid)
            for k, v in VALID.items():
                if got.get(k) != v:
                    fails.append(f"round-trip {k}: ожидал {v!r}, получил {got.get(k)!r}")

            # 2) невалидный набор (кривой ИНН + нет лид-магнита) → ошибки и НИЧЕГО не перезаписано
            bad = {"funnel_enabled": "1", "operator_name": "X", "operator_inn": "abc",
                   "operator_email": "info@romashka.ru", "leadmagnet_kind": ""}
            errs2 = await db.set_funnel_config(tid, bad, actor="smoke", ip=None, user_agent=None)
            if not errs2:
                fails.append("невалидный набор не дал ошибок")
            got2 = await db.get_funnel_config_panel(tid)
            if got2.get("operator_inn") != VALID["operator_inn"]:
                fails.append("невалидная запись перетёрла данные (должна быть атомарно отклонена)")
        finally:
            await drop()

    if fails:
        print("\n".join("❌ " + f for f in fails))
        raise SystemExit(1)
    print("🟢 funnel_panel_smoke зелёный")


if __name__ == "__main__":
    asyncio.run(main())
