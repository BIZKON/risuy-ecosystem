#!/usr/bin/env python3
"""Смоук T-1A-1 (прайс-слой токен-биллинга): resource_pricing + billing_token_rate +
снимок курса в usage_ledger. На risuy_dev. На прод НЕ запускать.

Проверяет (после применения db/schema_billing_v2_pricing.sql):
  1. resource_pricing: 4 наценки (llm=1.000, dadata=3.000, voice=2.000, embedding=3.000);
     LLM=1.000 — инвариант §5 (наценка вшита в КУРС, не в множитель).
  2. billing_token_rate: текущий курс (effective_from<=now, свежайший) == 1_500_000 µRUB/1k.
  3. usage_ledger: колонка token_rate_microrub_per_1k присутствует (снимок курса).
  4. Гранты panel_rw: select+insert на обеих таблицах; update на resource_pricing;
     billing_token_rate БЕЗ update (курс — новой строкой, не правится).

RED до миграции (таблиц/колонки нет) → GREEN после. Запуск:
  PRICING_SMOKE_DSN="postgresql://<owner>@<host>:5432/risuy_dev?sslmode=require" \
      python3 scripts/pricing_smoke.py
"""
import asyncio
import os
import sys
from decimal import Decimal

import asyncpg

DSN = os.environ.get("PRICING_SMOKE_DSN")
if not DSN or "/risuy_dev" not in DSN.split("?")[0]:
    raise SystemExit("Задайте PRICING_SMOKE_DSN на risuy_dev.")

FAILS: list[str] = []


def check(name, cond, detail=""):
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail != "" else ""))
    if not cond:
        FAILS.append(name)


async def main() -> None:
    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
    try:
        async with pool.acquire() as c:
            # ── #1 resource_pricing: 4 наценки ──
            print("1. resource_pricing — наценки per-resource:")
            expected = {"llm": Decimal("1.000"), "dadata": Decimal("3.000"),
                        "voice": Decimal("2.000"), "embedding": Decimal("3.000")}
            try:
                rows = await c.fetch("select resource, markup_multiplier from resource_pricing")
                got = {r["resource"]: r["markup_multiplier"] for r in rows}
            except asyncpg.UndefinedTableError:
                got = None
            check("таблица resource_pricing существует", got is not None)
            if got is not None:
                for res, mk in expected.items():
                    check(f"наценка {res} = {mk}", got.get(res) == mk, str(got.get(res)))
                check("LLM=1.000 (наценка вшита в курс, не в множитель)",
                      got.get("llm") == Decimal("1.000"))

            # ── #2 billing_token_rate: текущий курс ──
            print("2. billing_token_rate — курс продажи токена:")
            try:
                rate = await c.fetchval(
                    "select rate_microrub_per_1k from billing_token_rate "
                    "where effective_from <= now() order by effective_from desc limit 1")
            except asyncpg.UndefinedTableError:
                rate = None
            check("текущий курс = 1_500_000 µRUB/1k (0,0015 ₽/ток)", rate == 1_500_000, str(rate))

            # ── #3 usage_ledger: снимок курса ──
            print("3. usage_ledger — колонка снимка курса:")
            col = await c.fetchval(
                "select 1 from information_schema.columns "
                "where table_name='usage_ledger' and column_name='token_rate_microrub_per_1k'")
            check("колонка token_rate_microrub_per_1k присутствует", col == 1)

            # ── #4 гранты panel_rw ──
            print("4. гранты panel_rw:")
            grants: dict[str, set] = {"resource_pricing": set(), "billing_token_rate": set()}
            for r in await c.fetch(
                "select table_name, privilege_type from information_schema.role_table_grants "
                "where grantee='panel_rw' and table_name = any($1::text[])",
                list(grants.keys()),
            ):
                grants[r["table_name"]].add(r["privilege_type"])
            check("resource_pricing: select+insert+update",
                  {"SELECT", "INSERT", "UPDATE"} <= grants["resource_pricing"], str(grants["resource_pricing"]))
            check("billing_token_rate: select+insert",
                  {"SELECT", "INSERT"} <= grants["billing_token_rate"], str(grants["billing_token_rate"]))
            check("billing_token_rate append-only (без update/delete — курс новой строкой)",
                  not ({"UPDATE", "DELETE"} & grants["billing_token_rate"]), str(grants["billing_token_rate"]))

        print()
        if FAILS:
            print(f"❌ ПРОВАЛЫ ({len(FAILS)}): " + ", ".join(FAILS))
            sys.exit(1)
        print("✅ pricing smoke — все проверки зелёные (прайс-слой 1A)")
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
