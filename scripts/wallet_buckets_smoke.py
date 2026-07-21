#!/usr/bin/env python3
"""Смоук 1B (два бакета кошелька): credit_wallets → included/period_end/topup + списание пул→кошелёк.
На risuy_dev. На прод НЕ запускать (создаёт/удаляет smoke-тенанта, пишет/чистит леджер).

Проверяет:
  T-1B-1 (DDL):
    1. Колонки included_microrub (bigint, not null, default 0), included_period_end (timestamptz,
       nullable), topup_microrub (bigint, not null, default 0). balance_microrub сохранён.
    2. Бэкфилл balance → topup (идемпотентно).
  T-1B-2 (charge_usage списание пул→кошелёк, сгорание по period_end):
    3.1 пул1000+аванс500, charged1200 → пул0/аванс300 (пул гасится первым);
    3.2 period_end в прошлом → пул игнорируется (лениво обнуляется), списание из аванса;
    3.3 оба бакета 0, allow_negative=False → InsufficientCreditsError, бакеты целы, леджер пуст;
    3.4 allow_negative=True → минус на ПОСЛЕДНЕМ бакете (аванс).

RED до миграции/реализации → GREEN после. Запуск:
  WALLET_BUCKETS_SMOKE_DSN="postgresql://<owner>@<host>:5432/risuy_dev?sslmode=require" \
      PYTHONPATH=. python3 scripts/wallet_buckets_smoke.py
"""
import asyncio
import os
import sys

import asyncpg

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared.metering import InsufficientCreditsError, charge_usage  # noqa: E402

DSN = os.environ.get("WALLET_BUCKETS_SMOKE_DSN")
if not DSN or "/risuy_dev" not in DSN.split("?")[0]:
    raise SystemExit("Задайте WALLET_BUCKETS_SMOKE_DSN на risuy_dev.")

FAILS: list[str] = []
SLUG = "smoke-buckets"

EXPECTED = {
    "included_microrub":   ("bigint", "NO", "0"),
    "included_period_end": ("timestamp with time zone", "YES", None),
    "topup_microrub":      ("bigint", "NO", "0"),
}


def check(name, cond, detail=""):
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail != "" else ""))
    if not cond:
        FAILS.append(name)


async def set_buckets(c, tid, *, included: int, topup: int, period: str) -> None:
    """Выставить бакеты кошелька напрямую. period: future|past|none (для included_period_end)."""
    pe = {"future": "now() + interval '30 days'",
          "past": "now() - interval '1 day'",
          "none": "null"}[period]
    await c.execute(
        f"insert into credit_wallets (tenant_id, included_microrub, included_period_end, "
        f"                            topup_microrub, balance_microrub) "
        f"values ($1, $2::bigint, {pe}, $3::bigint, $2::bigint + $3::bigint) "
        f"on conflict (tenant_id) do update set included_microrub = $2::bigint, included_period_end = {pe}, "
        f"topup_microrub = $3::bigint, balance_microrub = $2::bigint + $3::bigint",
        tid, included, topup)


async def buckets(c, tid):
    return await c.fetchrow(
        "select included_microrub, topup_microrub from credit_wallets where tenant_id = $1", tid)


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

            has_buckets = all(k in cols for k in EXPECTED)
            tid = await c.fetchval(
                "insert into tenants(slug,name,status) values($1,'Смоук buckets','active') "
                "on conflict (slug) do update set status='active' returning id", SLUG)

            # ── #2 бэкфилл ──
            print("2. Бэкфилл balance → topup (идемпотентно):")
            if not has_buckets:
                check("бэкфилл-тест выполним (колонки есть)", False, "колонок нет")
            else:
                await c.execute("delete from usage_ledger where tenant_id=$1", tid)
                await c.execute("delete from credit_wallets where tenant_id=$1", tid)
                await c.execute(
                    "insert into credit_wallets(tenant_id, balance_microrub) values ($1, 1000000)", tid)
                bf = ("update credit_wallets set topup_microrub = balance_microrub "
                      "where tenant_id=$1 and balance_microrub<>0 and topup_microrub=0")
                await c.execute(bf, tid)
                r = await c.fetchrow(
                    "select balance_microrub, topup_microrub, included_microrub "
                    "from credit_wallets where tenant_id=$1", tid)
                check("topup == balance (1_000_000)", r["topup_microrub"] == 1_000_000, str(r["topup_microrub"]))
                check("balance не тронут (1_000_000)", r["balance_microrub"] == 1_000_000)
                check("included == 0 (пул пуст)", r["included_microrub"] == 0)
                await c.execute(bf, tid)
                r2 = await c.fetchrow("select topup_microrub from credit_wallets where tenant_id=$1", tid)
                check("повторный бэкфилл идемпотентен", r2["topup_microrub"] == 1_000_000, str(r2["topup_microrub"]))

            # ── #3 списание пул→кошелёк (T-1B-2) ──
            print("3. Списание пул→кошелёк, сгорание по period_end:")
            if not has_buckets:
                check("списание-тест выполним (колонки есть)", False, "колонок нет")
            else:
                DD = {"kind": "other", "resource": "dadata"}  # charged = cost × 3.00

                # 3.1 пул 1000 + аванс 500, charged 1200 → пул 0, аванс 300
                await set_buckets(c, tid, included=1000, topup=500, period="future")
                await charge_usage(c, tid, 400, DD, "smoke:sp:1", allow_negative=False)
                r = await buckets(c, tid)
                check("3.1 пул погашен первым (included==0)", r["included_microrub"] == 0, str(r["included_microrub"]))
                check("3.1 остаток из аванса (topup==300)", r["topup_microrub"] == 300, str(r["topup_microrub"]))

                # 3.2 period_end в прошлом → пул игнор, списание из аванса
                await set_buckets(c, tid, included=1000, topup=500, period="past")
                await charge_usage(c, tid, 100, DD, "smoke:sp:2", allow_negative=False)
                r = await buckets(c, tid)
                check("3.2 сгоревший пул обнулён (included==0)", r["included_microrub"] == 0, str(r["included_microrub"]))
                check("3.2 списано из аванса (topup==200)", r["topup_microrub"] == 200, str(r["topup_microrub"]))

                # 3.3 оба 0, allow_negative=False → ошибка, бакеты целы, леджер пуст
                await set_buckets(c, tid, included=0, topup=0, period="none")
                raised = False
                try:
                    await charge_usage(c, tid, 100, DD, "smoke:sp:3", allow_negative=False)
                except InsufficientCreditsError:
                    raised = True
                r = await buckets(c, tid)
                n = await c.fetchval(
                    "select count(*) from usage_ledger where idempotence_key='smoke:sp:3'")
                check("3.3 InsufficientCreditsError при обоих ≤0", raised)
                check("3.3 бакеты целы (0/0)", r["included_microrub"] == 0 and r["topup_microrub"] == 0)
                check("3.3 леджер не тронут (0 строк)", n == 0, str(n))

                # 3.4 allow_negative=True → минус на авансе
                await set_buckets(c, tid, included=0, topup=0, period="none")
                await charge_usage(c, tid, 100, DD, "smoke:sp:4", allow_negative=True)
                r = await buckets(c, tid)
                check("3.4 минус на авансе (topup==-300)", r["topup_microrub"] == -300, str(r["topup_microrub"]))
                check("3.4 пул не тронут (included==0)", r["included_microrub"] == 0)

        print()
        if FAILS:
            print(f"❌ ПРОВАЛЫ ({len(FAILS)}): " + ", ".join(FAILS))
            sys.exit(1)
        print("✅ wallet_buckets smoke — все проверки зелёные (два бакета 1B)")
    finally:
        if tid is not None:
            async with pool.acquire() as c:
                await c.execute("delete from usage_ledger where tenant_id=$1", tid)
                await c.execute("delete from credit_wallets where tenant_id=$1", tid)
                await c.execute("delete from tenants where id=$1 and slug=$2", tid, SLUG)
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
