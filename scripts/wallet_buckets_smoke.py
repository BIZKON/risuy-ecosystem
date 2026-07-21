#!/usr/bin/env python3
"""Смоук T-1B-1 (два бакета кошелька): credit_wallets → included/period_end/topup + бэкфилл.
На risuy_dev. На прод НЕ запускать (создаёт/удаляет smoke-тенанта).

Проверяет (после применения db/schema_metering_v2_buckets.sql):
  1. Колонки: included_microrub (bigint, not null, default 0), included_period_end (timestamptz,
     nullable), topup_microrub (bigint, not null, default 0). balance_microrub сохранён.
  2. Бэкфилл: balance_microrub → topup_microrub (где topup ещё 0); balance не тронут;
     повторный прогон идемпотентен (topup не задваивается).

RED до миграции (колонок нет) → GREEN после. Запуск:
  WALLET_BUCKETS_SMOKE_DSN="postgresql://<owner>@<host>:5432/risuy_dev?sslmode=require" \
      python3 scripts/wallet_buckets_smoke.py
"""
import asyncio
import os
import sys

import asyncpg

DSN = os.environ.get("WALLET_BUCKETS_SMOKE_DSN")
if not DSN or "/risuy_dev" not in DSN.split("?")[0]:
    raise SystemExit("Задайте WALLET_BUCKETS_SMOKE_DSN на risuy_dev.")

FAILS: list[str] = []
SLUG = "smoke-buckets"

# ожидаемые колонки: имя → (data_type, is_nullable, column_default содержит)
EXPECTED = {
    "included_microrub":   ("bigint", "NO", "0"),
    "included_period_end": ("timestamp with time zone", "YES", None),
    "topup_microrub":      ("bigint", "NO", "0"),
}


def check(name, cond, detail=""):
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail != "" else ""))
    if not cond:
        FAILS.append(name)


async def main() -> None:
    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
    tid = None
    try:
        async with pool.acquire() as c:
            # ── #1 колонки ──
            print("1. credit_wallets — колонки двух бакетов:")
            cols = {
                r["column_name"]: r for r in await c.fetch(
                    "select column_name, data_type, is_nullable, column_default "
                    "from information_schema.columns where table_name='credit_wallets'")
            }
            check("balance_microrub сохранён", "balance_microrub" in cols)
            for name, (dtype, nullable, default_has) in EXPECTED.items():
                col = cols.get(name)
                check(f"{name} существует", col is not None)
                if col is None:
                    continue
                check(f"{name}: тип {dtype}", col["data_type"] == dtype, col["data_type"])
                check(f"{name}: nullable={nullable}", col["is_nullable"] == nullable, col["is_nullable"])
                if default_has is not None:
                    check(f"{name}: default {default_has}",
                          (col["column_default"] or "").startswith(default_has), str(col["column_default"]))

            # ── #2 бэкфилл (scoped на smoke-тенанта) ──
            print("2. Бэкфилл balance → topup (идемпотентно):")
            try:
                tid = await c.fetchval(
                    "insert into tenants(slug,name,status) values($1,'Смоук buckets','active') "
                    "on conflict (slug) do update set status='active' returning id", SLUG)
                await c.execute("delete from credit_wallets where tenant_id=$1", tid)
                await c.execute(
                    "insert into credit_wallets(tenant_id, balance_microrub) values ($1, 1000000)", tid)
                # зеркало бэкфилла миграции, scoped на тест-тенанта (без побочек на реальные строки)
                bf = ("update credit_wallets set topup_microrub = balance_microrub "
                      "where tenant_id=$1 and balance_microrub<>0 and topup_microrub=0")
                await c.execute(bf, tid)
                r = await c.fetchrow(
                    "select balance_microrub, topup_microrub, included_microrub "
                    "from credit_wallets where tenant_id=$1", tid)
                check("topup == balance (1_000_000)", r["topup_microrub"] == 1_000_000, str(r["topup_microrub"]))
                check("balance не тронут (1_000_000)", r["balance_microrub"] == 1_000_000)
                check("included == 0 (пул пуст)", r["included_microrub"] == 0)
                await c.execute(bf, tid)  # повтор
                r2 = await c.fetchrow(
                    "select topup_microrub from credit_wallets where tenant_id=$1", tid)
                check("повторный бэкфилл идемпотентен (topup == 1_000_000)",
                      r2["topup_microrub"] == 1_000_000, str(r2["topup_microrub"]))
            except (asyncpg.UndefinedColumnError, asyncpg.UndefinedTableError) as e:
                check("бэкфилл-тест выполним (колонки есть)", False, type(e).__name__)

        print()
        if FAILS:
            print(f"❌ ПРОВАЛЫ ({len(FAILS)}): " + ", ".join(FAILS))
            sys.exit(1)
        print("✅ wallet_buckets smoke — все проверки зелёные (два бакета 1B)")
    finally:
        if tid is not None:
            async with pool.acquire() as c:
                await c.execute("delete from credit_wallets where tenant_id=$1", tid)
                await c.execute("delete from tenants where id=$1 and slug=$2", tid, SLUG)
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
