#!/usr/bin/env python3
"""Смоук Wave 6 — платформенная сводка дашборда (db.platform_summary) на risuy_dev.

Проверяет агрегат по тенантам ПОД РЕАЛЬНЫМ RLS, а не owner-обходом: создаёт временную
НЕ-owner роль smoke_ps_rls (как panel_rw → RLS применяется), сидит 2 тенанта с известными
кошельками/леджером/платежами (как owner, RLS обходит на вставке), затем гоняет
platform_summary ПОД ролью и проверяет per-tenant изоляцию + точные суммы. Все строки
smoke-ps-* и временная роль удаляются в конце. На прод НЕ запускать.

Если временную роль создать не вышло (нет CREATEROLE) — деградирует до owner-режима:
проверяет только shape + наличие тенантов (per-tenant суммы как owner недостоверны —
RLS обходится — поэтому пропускаются с предупреждением).

Запуск (DSN owner-DSN risuy_dev, не печатается):
  WAVE6_SMOKE_DSN="postgresql://<owner>:<pw>@<host>:5432/risuy_dev?sslmode=require" \
  PYTHONPATH=. python3 scripts/wave6_platform_smoke.py
"""
import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)                              # для пакета `shared`
sys.path.insert(0, os.path.join(ROOT, "admin-panel"))  # для config / db панели
# config панели требует эти env при импорте (значения для смоука неважны — пул ставим сами).
os.environ.setdefault("DATABASE_URL", "postgresql://x/y")
os.environ.setdefault("SESSION_SECRET", "smoke-secret-only-for-import-aaaaaaaa")
os.environ.setdefault("ADMIN_USERNAME", "smoke")
os.environ.setdefault("ADMIN_PASSWORD_HASH", "$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHQ$aGFzaHN0dWI")

import asyncpg  # noqa: E402
import db        # noqa: E402  (admin-panel/db.py)

DSN = os.environ.get("WAVE6_SMOKE_DSN")
if not DSN or "/risuy_dev" not in DSN.split("?")[0]:
    raise SystemExit("Задайте WAVE6_SMOKE_DSN на risuy_dev (делает delete тестовых строк + временную роль).")

FAILS: list[str] = []
ROLE = "smoke_ps_rls"
TABLES = ("tenants", "payments", "usage_ledger", "credit_wallets")


def check(name: str, cond: bool, detail="") -> None:
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail != "" else ""))
    if not cond:
        FAILS.append(name)


async def _cleanup_data(c) -> None:
    await c.execute("delete from payments where idempotence_key like 'smoke-ps:%'")
    await c.execute("delete from usage_ledger where idempotence_key like 'smoke-ps:%'")
    await c.execute("delete from credit_wallets where tenant_id in "
                    "(select id from tenants where slug like 'smoke-ps-%')")
    await c.execute("delete from tenants where slug like 'smoke-ps-%'")


async def _seed(c):
    await _cleanup_data(c)  # хвосты прошлых прогонов
    ta = await c.fetchval(
        "insert into tenants(slug,name,status) values('smoke-ps-a','PS A','active') returning id")
    tb = await c.fetchval(
        "insert into tenants(slug,name,status) values('smoke-ps-b','PS B','active') returning id")
    await c.execute("insert into credit_wallets(tenant_id,balance_microrub) values($1,$2)", ta, 5_000_000)
    await c.execute("insert into credit_wallets(tenant_id,balance_microrub) values($1,$2)", tb, -1_000_000)

    async def led(t, cost, charged, key):
        await c.execute(
            "insert into usage_ledger(tenant_id,kind,cost_microrub,multiplier,charged_microrub,"
            "balance_after_microrub,idempotence_key) values($1,'llm',$2,3.00,$3,0,$4)",
            t, cost, charged, key)
    await led(ta, 100_000, 300_000, "smoke-ps:a1")
    await led(ta, 200_000, 600_000, "smoke-ps:a2")
    await led(tb, 50_000, 150_000, "smoke-ps:b1")

    async def pay(t, typ, amt, status, key):
        await c.execute(
            "insert into payments(tenant_id,type,idempotence_key,amount_microrub,status) "
            "values($1,$2,$3,$4,$5)", t, typ, key, amt, status)
    await pay(ta, "subscription", 3_750_000, "succeeded", "smoke-ps:pa1")
    await pay(ta, "topup",        1_000_000, "succeeded", "smoke-ps:pa2")
    await pay(ta, "topup",        9_000_000, "pending",   "smoke-ps:pa3")  # не succeeded → не в выручке
    return ta, tb


async def main() -> None:
    owner = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    rls_ok = True
    async with owner.acquire() as c:
        try:
            await c.execute(
                f"do $$ begin if not exists (select 1 from pg_roles where rolname='{ROLE}') "
                f"then create role {ROLE} nologin; end if; end $$;")
            await c.execute(f"grant usage on schema public to {ROLE}")
            for t in TABLES:
                await c.execute(f"grant select on {t} to {ROLE}")
            await c.execute(f"grant {ROLE} to current_user")
        except Exception as e:  # noqa: BLE001
            rls_ok = False
            print(f"  WARN временную роль {ROLE} создать не вышло ({e!r}) — owner-режим (per-tenant суммы пропущены)")
        await _seed(c)

    async def _init(conn):
        if rls_ok:
            await conn.execute(f"set role {ROLE}")

    rls_pool = await asyncpg.create_pool(DSN, min_size=1, max_size=4, init=_init)
    db.pool = rls_pool
    try:
        res = await db.platform_summary()
    finally:
        await rls_pool.close()

    print("Платформенная сводка:")
    check("shape: ключи clients/tenants/totals", set(res) == {"clients", "tenants", "totals"}, str(set(res)))
    check("totals: 5 метрик", set(res["totals"]) == {"payments", "charged", "cost", "margin", "wallet"},
          str(set(res["totals"])))
    by_slug = {t["slug"]: t for t in res["tenants"]}
    check("оба тестовых клиента в сводке", "smoke-ps-a" in by_slug and "smoke-ps-b" in by_slug,
          str(list(by_slug)))
    check("clients >= 2", res["clients"] >= 2, str(res["clients"]))
    check("totals.margin == charged − cost", res["totals"]["margin"] == res["totals"]["charged"] - res["totals"]["cost"])

    if rls_ok and "smoke-ps-a" in by_slug and "smoke-ps-b" in by_slug:
        a, b = by_slug["smoke-ps-a"], by_slug["smoke-ps-b"]
        check("A.выручка == 4 750 000 (pending исключён)", a["payments"] == 4_750_000, a["payments"])
        check("A.начислено == 900 000", a["charged"] == 900_000, a["charged"])
        check("A.себестоимость == 300 000", a["cost"] == 300_000, a["cost"])
        check("A.маржа == 600 000", a["margin"] == 600_000, a["margin"])
        check("A.кошелёк == 5 000 000", a["wallet"] == 5_000_000, a["wallet"])
        check("B.выручка == 0 (RLS: B не видит платежи A)", b["payments"] == 0, b["payments"])
        check("B.начислено == 150 000 (RLS-изоляция)", b["charged"] == 150_000, b["charged"])
        check("B.себестоимость == 50 000", b["cost"] == 50_000, b["cost"])
        check("B.маржа == 100 000", b["margin"] == 100_000, b["margin"])
        check("B.кошелёк == -1 000 000 (postpaid минус)", b["wallet"] == -1_000_000, b["wallet"])
        check("totals >= A+B (вкл. прочие тенанты dev)",
              res["totals"]["charged"] >= 1_050_000 and res["totals"]["cost"] >= 350_000
              and res["totals"]["payments"] >= 4_750_000)
    else:
        print("  (per-tenant money asserts ПРОПУЩЕНЫ — нет RLS-роли)")

    async with owner.acquire() as c:
        await _cleanup_data(c)
        if rls_ok:
            try:
                for t in TABLES:
                    await c.execute(f"revoke select on {t} from {ROLE}")
                await c.execute(f"revoke usage on schema public from {ROLE}")
                await c.execute(f"revoke {ROLE} from current_user")
                await c.execute(f"drop role if exists {ROLE}")
            except Exception as e:  # noqa: BLE001 — роль NOLOGIN, остаток безвреден
                print(f"  WARN не удалось снять временную роль {ROLE}: {e!r}")
    await owner.close()

    print()
    if FAILS:
        print(f"❌ ПРОВАЛЫ ({len(FAILS)}): " + ", ".join(FAILS))
        sys.exit(1)
    print("✅ Wave 6 smoke — все проверки зелёные")


if __name__ == "__main__":
    asyncio.run(main())
