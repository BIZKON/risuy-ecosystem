#!/usr/bin/env python3
"""Smoke: db.get_funnel_product — tenant-scoped ридер продукта-материала воронки (risuy_dev).
Бот=owner обходит RLS → проверяем ЯВНЫЙ фильтр tenant_id + kind='lead_magnet'.
Два throwaway-тенанта: продукт тенанта A виден под A, НЕ виден под B; не-lead_magnet → None.

Запуск:
  FUNNEL_SMOKE_DSN="postgresql://gen_user:<pw>@HOST:5432/risuy_dev?sslmode=require" \
  PYTHONPATH=bot-telegram BOT_TOKEN=x DATABASE_URL="$FUNNEL_SMOKE_DSN" CHANNEL_ID=-100 \
  CHANNEL_URL=https://t.me/x GUIDE_URL=https://x ./.venv-smoke/bin/python scripts/funnel_product_smoke.py
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

SLUG_A, SLUG_B = "smoke-funnel-prod-a", "smoke-funnel-prod-b"


async def main() -> None:
    await db.init()
    fails: list[str] = []
    async with db.pool.acquire() as c:
        async def drop() -> None:
            # products.tenant_id — FK БЕЗ on delete cascade → удаляем продукты ПЕРВЫМИ, потом тенанта.
            await c.execute(
                "delete from products where tenant_id in "
                "(select id from tenants where slug = any($1::text[]))", [SLUG_A, SLUG_B])
            await c.execute("delete from tenants where slug = any($1::text[])", [SLUG_A, SLUG_B])

        async def mk_tenant(slug):
            return await c.fetchval(
                "insert into tenants (slug, name, status) values ($1, 'SMOKE prod', 'active') returning id", slug)

        async def mk_product(tid, kind, file_tg_id):
            return await c.fetchval(
                "insert into products (name, kind, currency, status, created_by, tenant_id, file_tg_id, file_mime) "
                "values ('Лид-магнит', $1, 'RUB', 'active', 'smoke', $2, $3, 'application/pdf') returning id",
                kind, tid, file_tg_id)

        await drop()
        ta, tb = await mk_tenant(SLUG_A), await mk_tenant(SLUG_B)
        try:
            pid_lm = await mk_product(ta, "lead_magnet", "FILEID-A")
            pid_other = await mk_product(ta, "tripwire", "FILEID-X")  # валидный не-lead_magnet вид

            tok = db.current_tenant_id.set(ta)
            try:
                r = await db.get_funnel_product(pid_lm)
                if not (r and r["file_tg_id"] == "FILEID-A"):
                    fails.append(f"под тенантом A lead_magnet не прочитался: {r}")
                if await db.get_funnel_product(pid_other) is not None:
                    fails.append("не-lead_magnet продукт не должен возвращаться")
            finally:
                db.current_tenant_id.reset(tok)

            # под ДРУГИМ тенантом продукт A НЕ виден (явный tenant-фильтр, не RLS)
            tok = db.current_tenant_id.set(tb)
            try:
                if await db.get_funnel_product(pid_lm) is not None:
                    fails.append("КРОСС-ТЕНАНТ: продукт A виден под тенантом B (фильтр не сработал!)")
            finally:
                db.current_tenant_id.reset(tok)
        finally:
            await drop()

    if fails:
        print("\n".join("❌ " + f for f in fails))
        raise SystemExit(1)
    print("🟢 funnel_product_smoke зелёный")


if __name__ == "__main__":
    asyncio.run(main())
