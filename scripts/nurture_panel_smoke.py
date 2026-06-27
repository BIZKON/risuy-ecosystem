#!/usr/bin/env python3
"""Smoke панели: set_tenant_nurture / get_tenant_nurture_panel — валидирующая запись и чтение конфига
дожима (tenant_settings) на risuy_dev. Throwaway-тенант, чистка каскадом.

Запуск (owner-DSN risuy_dev + заглушки config панели):
  NURTURE_SMOKE_DSN="postgresql://gen_user:<pw>@HOST:5432/risuy_dev?sslmode=require" \
  DATABASE_URL="$NURTURE_SMOKE_DSN" SESSION_SECRET="$(python3 -c 'import secrets;print(secrets.token_urlsafe(48))')" \
  ADMIN_USERNAME=smoke ADMIN_PASSWORD_HASH='$argon2id$v=19$m=65536,t=3,p=4$c21va2VzbW9rZQ$c21va2VzbW9rZXNtb2tl' \
  PYTHONPATH=admin-panel ./.venv-smoke/bin/python scripts/nurture_panel_smoke.py
"""
import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)  # пакет shared (shared.nurture внутри db)

import db  # noqa: E402  (admin-panel на PYTHONPATH)

DSN = os.environ.get("NURTURE_SMOKE_DSN") or os.environ.get("DATABASE_URL")
if not DSN or "risuy_dev" not in DSN:
    raise SystemExit("Задайте DSN на risuy_dev (защита от прода).")

SLUG = "smoke-nurture-panel"
STEPS = [{"delay_seconds": 7200, "text": "к1"}, {"delay_seconds": 86400, "text": "к2"}]


async def main() -> None:
    await db.init()
    fails: list[str] = []
    async with db.pool.acquire() as c:
        async def drop() -> None:
            await c.execute("delete from tenants where slug = $1", SLUG)  # cascade → tenant_settings

        await drop()
        tid = await c.fetchval(
            "insert into tenants (slug, name, status) values ($1, 'SMOKE nurture', 'active') returning id",
            SLUG)
        try:
            # 1) валидный набор → запись без ошибок, round-trip совпал
            errs = await db.set_tenant_nurture(tid, True, STEPS, actor="smoke", ip=None, user_agent=None)
            if errs:
                fails.append(f"валидный набор дал ошибки: {errs}")
            got = await db.get_tenant_nurture_panel(tid)
            if not (got["enabled"] and got["steps"] == STEPS):
                fails.append(f"round-trip не совпал: {got}")
            # хранится в формате, который читает бот: nurture_enabled='1' + nurture_steps JSON
            raw = {r["key"]: r["value"] for r in await c.fetch(
                "select key, value from tenant_settings where tenant_id=$1 and key like 'nurture_%'", tid)}
            if raw.get("nurture_enabled") != "1":
                fails.append(f"nurture_enabled не '1': {raw}")

            # 2) невалидный набор (присутствующий шаг без текста) → ошибки, НИЧЕГО не перезаписано
            errs2 = await db.set_tenant_nurture(
                tid, True, [{"delay_seconds": 3600, "text": ""}], actor="smoke", ip=None, user_agent=None)
            if not errs2:
                fails.append("невалидный набор не дал ошибок")
            got2 = await db.get_tenant_nurture_panel(tid)
            if got2["steps"] != STEPS:
                fails.append("невалидная запись перетёрла данные (должна быть атомарно отклонена)")

            # 3) выключение + очистка шагов → enabled False, steps []
            errs3 = await db.set_tenant_nurture(tid, False, [], actor="smoke", ip=None, user_agent=None)
            if errs3:
                fails.append(f"выключение дало ошибки: {errs3}")
            got3 = await db.get_tenant_nurture_panel(tid)
            if got3["enabled"] is not False or got3["steps"] != []:
                fails.append(f"после выключения ожидал enabled=False/steps=[]: {got3}")
        finally:
            await drop()

    if fails:
        print("\n".join("❌ " + f for f in fails))
        raise SystemExit(1)
    print("🟢 nurture_panel_smoke зелёный")


if __name__ == "__main__":
    asyncio.run(main())
