#!/usr/bin/env python3
"""Смоук Слоя C — касса тенанта (Phase 2): tenant-ИЗОЛЯЦИЯ продаваемых продуктов (защита от
крафтнутого buy:<чужой_product_id>) + продаваемость-фильтры + get_tenant_shop_creds (None-path).
bot-telegram/db.py на risuy_dev. На прод НЕ запускать.

Запуск: KASSA_SMOKE_DSN="postgresql://<owner>@<host>:5432/risuy_dev?sslmode=require" \
        PYTHONPATH=. ./.venv-smoke/bin/python scripts/kassa_smoke.py
"""
import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "bot-telegram"))
os.environ.setdefault("BOT_TOKEN", "0:smoke")
os.environ.setdefault("DATABASE_URL", "postgresql://x/y")
os.environ.setdefault("CHANNEL_ID", "-1001234567890")
os.environ.setdefault("CHANNEL_URL", "https://t.me/smoke")
os.environ.setdefault("GUIDE_URL", "https://example.org/guide")

import asyncpg    # noqa: E402
import db         # noqa: E402  (bot-telegram/db.py)

DSN = os.environ.get("KASSA_SMOKE_DSN")
if not DSN or "/risuy_dev" not in DSN.split("?")[0]:
    raise SystemExit("Задайте KASSA_SMOKE_DSN на risuy_dev.")

FAILS: list[str] = []


def check(name, cond, detail=""):
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail != "" else ""))
    if not cond:
        FAILS.append(name)


async def main() -> None:
    db.pool = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    ta = tb = None
    try:
        async with db.pool.acquire() as c:
            ta = await c.fetchval("insert into tenants(slug,name,status) values('smoke-kassa-a','A','active') returning id")
            tb = await c.fetchval("insert into tenants(slug,name,status) values('smoke-kassa-b','B','active') returning id")
            # Тенант A: продаваемый + архивный + нулевая цена + USD (непродаваемые по фильтру).
            pa_ok = await c.fetchval("insert into products(name,kind,price,currency,status,tenant_id) values('Курс А','main',9900,'RUB','active',$1) returning id", ta)
            pa_arch = await c.fetchval("insert into products(name,kind,price,currency,status,tenant_id) values('Архив','main',5000,'RUB','archived',$1) returning id", ta)
            pa_free = await c.fetchval("insert into products(name,kind,price,currency,status,tenant_id) values('Бесплатный','main',0,'RUB','active',$1) returning id", ta)
            pa_usd = await c.fetchval("insert into products(name,kind,price,currency,status,tenant_id) values('Долларовый','main',100,'USD','active',$1) returning id", ta)
            # Тенант B: продаваемый — ЧУЖОЙ для A (тест анти-кросс-тенант).
            pb_ok = await c.fetchval("insert into products(name,kind,price,currency,status,tenant_id) values('Курс B','main',7700,'RUB','active',$1) returning id", tb)

        db.current_tenant_id.set(ta)
        print("1. list_sellable_products (тенант A):")
        sell = await db.list_sellable_products()
        ids = {p["id"] for p in sell}
        check("содержит продаваемый продукт A", pa_ok in ids)
        check("исключены архивный/бесплатный/USD A", not ({pa_arch, pa_free, pa_usd} & ids))
        check("НЕ содержит продукт тенанта B (изоляция)", pb_ok not in ids, f"ids={ids}")

        print("2. get_sellable_product (тенант A) — защита от buy:<чужой_id>:")
        check("свой продаваемый → возвращён", (await db.get_sellable_product(pa_ok)) is not None)
        check("🔒 ЧУЖОЙ продукт B → None (анти-кросс-тенант)", (await db.get_sellable_product(pb_ok)) is None)
        check("архивный → None", (await db.get_sellable_product(pa_arch)) is None)
        check("нулевая цена → None", (await db.get_sellable_product(pa_free)) is None)
        check("USD → None (касса рублёвая)", (await db.get_sellable_product(pa_usd)) is None)

        print("3. get_tenant_shop_creds (касса не подключена → None, без падения):")
        check("нет ключей в vault → None", (await db.get_tenant_shop_creds()) is None)

        # tenant_id() == None → пустой результат (без падения)
        db.current_tenant_id.set(None)
        check("без тенанта: list_sellable_products → []", (await db.list_sellable_products()) == [])
        check("без тенанта: get_sellable_product → None", (await db.get_sellable_product(pa_ok)) is None)
    finally:
        async with db.pool.acquire() as c:
            for t in (ta, tb):
                if t:
                    await c.execute("delete from products where tenant_id = $1", t)
                    await c.execute("delete from tenants where id = $1", t)
        await db.pool.close()

    print()
    if FAILS:
        print(f"❌ ПРОВАЛЫ ({len(FAILS)}): " + ", ".join(FAILS)); sys.exit(1)
    print("✅ kassa smoke — все проверки зелёные")


if __name__ == "__main__":
    asyncio.run(main())
