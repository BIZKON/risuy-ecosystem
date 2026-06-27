#!/usr/bin/env python3
"""Smoke: публичные юр-страницы тенанта — get_legal_doc_data(slug) + legal_privacy_url в
get_funnel_config (risuy_dev). Реквизиты заполнены → данные есть; нет → None. Throwaway-тенанты.

Запуск (BOT_PUBLIC_BASE_URL нужен для legal_privacy_url):
  FUNNEL_SMOKE_DSN="postgresql://gen_user:<pw>@HOST:5432/risuy_dev?sslmode=require" \
  PYTHONPATH=bot-telegram BOT_TOKEN=x DATABASE_URL="$FUNNEL_SMOKE_DSN" CHANNEL_ID=-100 \
  CHANNEL_URL=https://t.me/x GUIDE_URL=https://x BOT_PUBLIC_BASE_URL=https://bot.example \
  ./.venv-smoke/bin/python scripts/legal_page_smoke.py
"""
import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import db  # noqa: E402

DSN = os.environ.get("FUNNEL_SMOKE_DSN") or os.environ.get("DATABASE_URL")
if not DSN or "risuy_dev" not in DSN:
    raise SystemExit("Задайте DSN на risuy_dev (защита от прода).")

SLUG = "smoke-legal"
SLUG_EMPTY = "smoke-legal-empty"


async def main() -> None:
    await db.init()
    fails: list[str] = []
    async with db.pool.acquire() as c:
        async def drop() -> None:
            await c.execute("delete from tenant_settings where tenant_id in (select id from tenants where slug = any($1::text[]))", [SLUG, SLUG_EMPTY])
            await c.execute("delete from tenants where slug = any($1::text[])", [SLUG, SLUG_EMPTY])

        await drop()
        tid = await c.fetchval("insert into tenants (slug,name,status) values ($1,'SMOKE legal','active') returning id", SLUG)
        for k, v in {"operator_name": "ИП Тест Т.Т.", "operator_inn": "770000000000",
                     "operator_email": "a@b.ru", "operator_ogrn": "304770000000017",
                     "operator_address": "г. Москва", "funnel_enabled": "1"}.items():
            await c.execute("insert into tenant_settings (tenant_id,key,value) values ($1,$2,$3)", tid, k, v)
        await c.execute("insert into tenants (slug,name,status) values ($1,'SMOKE empty','active')", SLUG_EMPTY)
        try:
            kv = await db.get_legal_doc_data(SLUG)
            if not kv or kv.get("operator_name") != "ИП Тест Т.Т." or kv.get("operator_ogrn") != "304770000000017":
                fails.append(f"get_legal_doc_data(заполнен): {kv}")
            if await db.get_legal_doc_data(SLUG_EMPTY) is not None:
                fails.append("тенант без operator-реквизитов → должно быть None")
            if await db.get_legal_doc_data("nope-" + SLUG) is not None:
                fails.append("несуществующий slug → должно быть None")

            cfg = await db.get_funnel_config(tid)
            lpu = cfg.get("legal_privacy_url")
            if not lpu or f"/legal/{SLUG}/privacy" not in lpu:
                fails.append(f"legal_privacy_url не собран: {lpu}")
        finally:
            await drop()

    if fails:
        print("\n".join("❌ " + f for f in fails))
        raise SystemExit(1)
    print("🟢 legal_page_smoke зелёный")


if __name__ == "__main__":
    asyncio.run(main())
