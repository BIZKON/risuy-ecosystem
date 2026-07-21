#!/usr/bin/env python3
"""Смоук ядра метеринга (charge_usage) на DEV-базе — приёмка ТЗ §8.2–8.4 + T-1A-2.

Гоняет РЕАЛЬНЫЕ транзакции против risuy_dev (никаких моков леджера):
  1. LLM tokens×курс (T-1A-2): charged == tokens × billing_token_rate/1000; снимок курса
     token_rate_microrub_per_1k записан; множитель=1.00 (наценка вшита в курс); маржа ≥ 76,5%;
  2. идемпотентность:  один idempotence_key дважды → ОДНА строка, ОДНО списание;
  3. DaData cost×наценка_ресурса: charged == ceil_mul(cost, 3.00) == 22,5₽; token_rate NULL; маржа ≥ 66,7%;
  4. Voice ×2: наценка ЧИТАЕТСЯ из resource_pricing (2.00), НЕ из плана (3.00) — доказывает per-resource;
  5. гонка/FOR UPDATE: 5 параллельных списаний dadata при кошельке на 2 → ровно 2 успеха, 3 отказа;
  6. per_message:      план econom → charged == цене сообщения плана (7,5 ₽);
  7. постфактум-минус: allow_negative=True + неизвестный ресурс → множитель плана, кошелёк в минус.

Тест-тенанты smoke-* создаются и УДАЛЯЮТСЯ в конце. На прод не запускать (чистка делает delete в леджере).

Запуск (DSN не печатается; пароль owner — из Timeweb API через PGPASSWORD):
  METERING_SMOKE_DSN="postgresql://<owner>@<host>:5432/risuy_dev?sslmode=require" \
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


def margin_pct(charged: int, cost: int) -> float:
    return round((charged - cost) / charged * 100, 1) if charged else 0.0


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


async def set_wallet(conn: asyncpg.Connection, tid: uuid.UUID, micro: int) -> None:
    # T-1B-2: средства тенанта живут в кошельке-авансе (topup); пул included=0.
    # balance_microrub держим зеркалом суммы бакетов (депрекейт в T-1C).
    await conn.execute(
        "insert into credit_wallets (tenant_id, topup_microrub, balance_microrub) values ($1, $2, $2) "
        "on conflict (tenant_id) do update set topup_microrub = $2, balance_microrub = $2, "
        "included_microrub = 0, included_period_end = null",
        tid, micro,
    )


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
        t_price = await make_tenant(conn, "smoke-price", "custom")   # cost_multiplier ×3, prepaid
        t_msg = await make_tenant(conn, "smoke-permsg", "econom")    # per_message 7,5 ₽
        t_legacy = await make_tenant(conn, "smoke-legacy", None)     # без плана (переходник Школы)

        # ── 1. LLM tokens×курс + снимок курса + маржа (T-1A-2) ───────────────
        print("1. LLM tokens×курс (5000 ток × 1500 µRUB/1k = 7,5₽):")
        await set_wallet(conn, t_price, 10_000_000)
        row = await charge_usage(
            conn, t_price, 1_761_750,  # себест. блендед 352 350 µRUB/1k × 5
            {"kind": "llm", "provider": "smoke", "model": "m", "units": {"tokens_total": 5000}},
            "smoke:llm:1", allow_negative=False,
        )
        check("charged == 7_500_000 (tokens×курс)", row["charged_microrub"] == 7_500_000,
              f"факт {row['charged_microrub']}")
        check("снимок курса token_rate == 1_500_000", row["token_rate_microrub_per_1k"] == 1_500_000,
              f"факт {row['token_rate_microrub_per_1k']}")
        check("множитель снимок == 1.00 (наценка в курсе)", float(row["multiplier"]) == 1.00,
              f"факт {row['multiplier']}")
        check("balance_after == 2_500_000", row["balance_after_microrub"] == 2_500_000)
        check("маржа LLM ≥ 76,5%", margin_pct(row["charged_microrub"], row["cost_microrub"]) >= 76.5,
              f"{margin_pct(row['charged_microrub'], row['cost_microrub'])}%")

        # ── 2. Идемпотентность (§8.3) ────────────────────────────────────────
        print("2. Идемпотентность:")
        again = await charge_usage(
            conn, t_price, 1_761_750, {"kind": "llm", "units": {"tokens_total": 5000}},
            "smoke:llm:1", allow_negative=False,
        )
        n_rows = await conn.fetchval(
            "select count(*) from usage_ledger where idempotence_key = 'smoke:llm:1'")
        bal = await conn.fetchval(
            "select balance_microrub from credit_wallets where tenant_id = $1", t_price)
        check("повтор ключа → та же строка", again["id"] == row["id"])
        check("строка в леджере одна", n_rows == 1)
        check("повторного списания нет (баланс 2_500_000)", bal == 2_500_000)

        # ── 3. DaData cost×наценка_ресурса + token_rate NULL + маржа ─────────
        print("3. DaData cost×3 (7,5₽ себест. → 22,5₽):")
        await set_wallet(conn, t_price, 30_000_000)
        row = await charge_usage(
            conn, t_price, 7_500_000,
            {"kind": "other", "resource": "dadata", "provider": "dadata", "units": {"requests": 1}},
            "smoke:dadata:1", allow_negative=False,
        )
        check("charged == 22_500_000 (cost×3)", row["charged_microrub"] == 22_500_000,
              f"факт {row['charged_microrub']}")
        check("token_rate NULL (не-LLM)", row["token_rate_microrub_per_1k"] is None)
        check("множитель снимок == 3.00", float(row["multiplier"]) == 3.00, f"факт {row['multiplier']}")
        check("маржа DaData ≥ 66,7%", margin_pct(row["charged_microrub"], row["cost_microrub"]) >= 66.7,
              f"{margin_pct(row['charged_microrub'], row['cost_microrub'])}%")

        # ── 4. Voice ×2: наценка из resource_pricing, НЕ из плана (доказательство) ──
        print("4. Voice ×2 (наценка ресурса 2.00, а не плана 3.00):")
        await set_wallet(conn, t_price, 5_000_000)
        row = await charge_usage(
            conn, t_price, 1_000_000,
            {"kind": "other", "resource": "voice", "units": {"minutes": 1}},
            "smoke:voice:1", allow_negative=False,
        )
        check("charged == 2_000_000 (cost×2, ресурсная наценка)", row["charged_microrub"] == 2_000_000,
              f"факт {row['charged_microrub']} (если 3_000_000 — читается план, не resource_pricing)")
        check("множитель снимок == 2.00", float(row["multiplier"]) == 2.00, f"факт {row['multiplier']}")
        check("token_rate NULL (не-LLM)", row["token_rate_microrub_per_1k"] is None)

        # ── 5. Гонка: 5 списаний dadata при кошельке на 2 (§8.4) ─────────────
        print("5. Гонка (5 параллельных dadata, хватает на 2):")
        await set_wallet(conn, t_price, 650)

        async def one(i: int):
            async with pool.acquire() as c:
                try:
                    await charge_usage(
                        c, t_price, 100,
                        {"kind": "other", "resource": "dadata"}, f"smoke:race:{i}",
                        allow_negative=False,
                    )
                    return "ok"
                except InsufficientCreditsError:
                    return "denied"

        results = await asyncio.gather(*[one(i) for i in range(5)])
        bal = await conn.fetchval(
            "select balance_microrub from credit_wallets where tenant_id = $1", t_price)
        n_race = await conn.fetchval(
            "select count(*) from usage_ledger where idempotence_key like 'smoke:race:%'")
        check("ровно 2 успеха", results.count("ok") == 2, f"факт {results}")
        check("ровно 3 отказа", results.count("denied") == 3)
        check("ровно 2 строки леджера", n_race == 2)
        check("баланс == 650 − 2×300 == 50 (не ниже пола)", bal == 50, f"факт {bal}")

        # ── 6. per_message: charged == цене сообщения плана ──────────────────
        print("6. per_message (econom, 7,5 ₽/сообщение):")
        await set_wallet(conn, t_msg, 10_000_000)
        row = await charge_usage(
            conn, t_msg, 0,
            {"kind": "message", "provider": "smoke", "units": {"messages": 1}},
            "smoke:msg:1", allow_negative=False,
        )
        check("charged == 7_500_000 µRUB", row["charged_microrub"] == 7_500_000,
              f"факт {row['charged_microrub']}")
        check("balance_after == 2_500_000", row["balance_after_microrub"] == 2_500_000)

        # ── 7. Постфактум-минус: неизвестный ресурс → множитель плана, минус ──
        print("7. Тенант без плана, неизвестный ресурс, allow_negative=True:")
        row = await charge_usage(
            conn, t_legacy, 100_000,
            {"kind": "other", "units": {"n": 1}},
            "smoke:legacy:1", allow_negative=True,
        )
        check("множитель дефолтный 3.00 → charged 300000", row["charged_microrub"] == 300_000,
              f"факт {row['charged_microrub']}")
        check("кошелёк ушёл в минус (−300000)", row["balance_after_microrub"] == -300_000)

        await cleanup(conn)
        print("Чистка smoke-тенантов выполнена.")

    await pool.close()
    if FAILS:
        raise SystemExit(f"ПРОВАЛ: {len(FAILS)} проверок: {FAILS}")
    print("ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ.")


if __name__ == "__main__":
    asyncio.run(main())
