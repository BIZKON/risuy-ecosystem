#!/usr/bin/env python3
"""Смоук ядра метеринга (charge_usage) на DEV-базе — частичная приёмка ТЗ §8.2–8.4.

Гоняет РЕАЛЬНЫЕ транзакции против risuy_dev (никаких моков леджера):
  1. точность ×3:      charged == ceil_mul(cost, 3.00), balance_after == prev − charged;
  2. идемпотентность:  один idempotence_key дважды → ОДНА строка, ОДНО списание;
  3. гонка:            5 параллельных списаний при кошельке на 2 → ровно 2 успеха,
                       3 × InsufficientCreditsError, баланс не ниже пола;
  4. per_message:      план econom → charged == цене сообщения плана (7,5 ₽);
  5. постфактум-минус: allow_negative=True уводит кошелёк в минус (переходник Школы).

Тестовые тенанты smoke-* создаются и УДАЛЯЮТСЯ в конце (вместе со строками
леджера/кошельков/подписок). На прод не запускать: чистка делает delete в леджере.

Запуск (DSN не печатается и не хардкодится; пароль owner — из Timeweb API):
  METERING_SMOKE_DSN="postgresql://<owner>:<pw>@<host>:5432/risuy_dev?sslmode=require" \
  PYTHONPATH=. python3 scripts/metering_smoke.py
"""
import asyncio
import os
import sys
import uuid

import asyncpg

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared.metering import InsufficientCreditsError, charge_usage  # noqa: E402

DSN = os.environ.get("METERING_SMOKE_DSN")
if not DSN:
    raise SystemExit("Задайте METERING_SMOKE_DSN (owner-DSN базы risuy_dev).")
if "/risuy_dev" not in DSN.split("?")[0]:
    raise SystemExit("Смоук гоняется ТОЛЬКО на risuy_dev (чистка делает delete в леджере).")

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


async def make_tenant(conn: asyncpg.Connection, slug: str, plan_code: str | None) -> uuid.UUID:
    tid = await conn.fetchval(
        "insert into tenants (slug, name, status) values ($1, $2, 'active') "
        "on conflict (slug) do update set status = 'active' returning id",
        slug, f"Смоук {slug}",
    )
    if plan_code:
        await conn.execute(
            """
            insert into subscriptions (tenant_id, plan_id, status,
                                       current_period_start, current_period_end)
            select $1, p.id, 'active', now(), now() + interval '30 days'
            from plans p where p.code = $2
            """,
            tid, plan_code,
        )
    return tid


async def cleanup(conn: asyncpg.Connection) -> None:
    await conn.execute(
        """
        do $$
        declare t uuid;
        begin
            for t in select id from tenants where slug like 'smoke-%' loop
                delete from usage_ledger    where tenant_id = t;
                delete from credit_wallets  where tenant_id = t;
                delete from subscriptions   where tenant_id = t;
                delete from tenant_settings where tenant_id = t;
                delete from tenants         where id = t;
            end loop;
        end $$;
        """
    )


async def main() -> None:
    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=6)
    async with pool.acquire() as conn:
        await cleanup(conn)  # хвосты прошлых прогонов
        t_mult = await make_tenant(conn, "smoke-mult", "custom")     # cost_multiplier ×3, prepaid
        t_msg = await make_tenant(conn, "smoke-permsg", "econom")    # per_message 7,5 ₽
        t_legacy = await make_tenant(conn, "smoke-legacy", None)     # без плана (переходник Школы)

        # ── 1. Точность ×3 (§8.2) ────────────────────────────────────────────
        print("1. Точность списания ×3:")
        await conn.execute(
            "insert into credit_wallets (tenant_id, balance_microrub) values ($1, $2) "
            "on conflict (tenant_id) do update set balance_microrub = $2",
            t_mult, 1_000_000,
        )
        row = await charge_usage(
            conn, t_mult, 333_333,
            {"kind": "llm", "provider": "smoke", "model": "m", "units": {"tokens_total": 1}},
            "smoke:precision:1", allow_negative=False,
        )
        check("charged == ceil(333333 × 3.00) == 999999", row["charged_microrub"] == 999_999,
              f"факт {row['charged_microrub']}")
        check("balance_after == 1000000 − 999999 == 1", row["balance_after_microrub"] == 1)

        # ── 2. Идемпотентность (§8.3) ────────────────────────────────────────
        print("2. Идемпотентность:")
        again = await charge_usage(
            conn, t_mult, 333_333, {"kind": "llm"}, "smoke:precision:1", allow_negative=False,
        )
        n_rows = await conn.fetchval(
            "select count(*) from usage_ledger where idempotence_key = 'smoke:precision:1'")
        bal = await conn.fetchval(
            "select balance_microrub from credit_wallets where tenant_id = $1", t_mult)
        check("повтор ключа → та же строка", again["id"] == row["id"])
        check("строка в леджере одна", n_rows == 1)
        check("повторного списания нет (баланс 1)", bal == 1)

        # ── 3. Гонка: 5 списаний при кошельке на 2 (§8.4) ────────────────────
        print("3. Гонка (5 параллельных, хватает на 2):")
        await conn.execute(
            "update credit_wallets set balance_microrub = $2 where tenant_id = $1",
            t_mult, 650,
        )

        async def one(i: int):
            async with pool.acquire() as c:
                try:
                    await charge_usage(
                        c, t_mult, 100, {"kind": "llm"}, f"smoke:race:{i}",
                        allow_negative=False,
                    )
                    return "ok"
                except InsufficientCreditsError:
                    return "denied"

        results = await asyncio.gather(*[one(i) for i in range(5)])
        bal = await conn.fetchval(
            "select balance_microrub from credit_wallets where tenant_id = $1", t_mult)
        n_race = await conn.fetchval(
            "select count(*) from usage_ledger where idempotence_key like 'smoke:race:%'")
        check("ровно 2 успеха", results.count("ok") == 2, f"факт {results}")
        check("ровно 3 отказа", results.count("denied") == 3)
        check("ровно 2 строки леджера", n_race == 2)
        check("баланс == 650 − 2×300 == 50 (не ниже пола)", bal == 50, f"факт {bal}")

        # ── 4. per_message: charged == цене сообщения плана ──────────────────
        print("4. per_message (econom, 7,5 ₽/сообщение):")
        await conn.execute(
            "insert into credit_wallets (tenant_id, balance_microrub) values ($1, $2) "
            "on conflict (tenant_id) do update set balance_microrub = $2",
            t_msg, 10_000_000,
        )
        row = await charge_usage(
            conn, t_msg, 0,
            {"kind": "message", "provider": "smoke", "units": {"messages": 1}},
            "smoke:msg:1", allow_negative=False,
        )
        check("charged == 7_500_000 µRUB", row["charged_microrub"] == 7_500_000,
              f"факт {row['charged_microrub']}")
        check("balance_after == 2_500_000", row["balance_after_microrub"] == 2_500_000)

        # ── 5. Постфактум-минус (переходник Школы, allow_negative=True) ─────
        print("5. Тенант без плана, пустой кошелёк, allow_negative=True:")
        row = await charge_usage(
            conn, t_legacy, 100_000,
            {"kind": "llm", "units": {"tokens_total": 760}},
            "smoke:legacy:1", allow_negative=True,
        )
        check("множитель дефолтный 3.00 → charged 300000", row["charged_microrub"] == 300_000)
        check("кошелёк ушёл в минус (−300000)", row["balance_after_microrub"] == -300_000)

        await cleanup(conn)
        print("Чистка smoke-тенантов выполнена.")

    await pool.close()
    if FAILS:
        raise SystemExit(f"ПРОВАЛ: {len(FAILS)} проверок: {FAILS}")
    print("ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ.")


if __name__ == "__main__":
    asyncio.run(main())
