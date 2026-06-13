#!/usr/bin/env python3
"""Смоук логики снапшот-воркера (Wave 3) на DEV-базе — проверка фиксов ревью №1/№2/№4.

Гоняет РЕАЛЬНЫЕ транзакции _process_agent_delta / _scan_tenant_messages против
risuy_dev (никаких моков леджера/снапшотов):
  A. baseline агента:    первый снапшот, БЕЗ списания;
  B. дельта→списание:    used растёт → одна строка леджера, charged по смешанной цене;
  C. glitch used=0:      при prev>0 НЕ сбрасывает baseline и НЕ списывает (финдинг №1);
  D. recovery:           следующая дельта считается от СОХРАНЁННОГО baseline (не от 0);
  E. реальный сброс:     ненулевое уменьшение → новый baseline, без списания;
  F. per_message baseline: первый скан = hwm=max(id) истории Лии БЕЗ списания (финдинг №2);
  G. per_message charge:   новое сообщение после baseline списывается по цене плана.

Тестовые сущности smoke-* создаются и УДАЛЯЮТСЯ в конце. На прод не запускать.

  METERING_SMOKE_DSN="postgresql://<owner>:<pw>@<host>:5432/risuy_dev?sslmode=require" \
  BOT_TOKEN=x CHANNEL_ID=1 CHANNEL_URL=https://t.me/x GUIDE_URL=https://x \
  PYTHONPATH=bot-telegram:. python3 scripts/metering_worker_smoke.py
"""
import asyncio
import os
import sys

import asyncpg

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot-telegram"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DSN = os.environ.get("METERING_SMOKE_DSN")
if not DSN or "/risuy_dev" not in DSN.split("?")[0]:
    raise SystemExit("METERING_SMOKE_DSN обязателен и ТОЛЬКО risuy_dev.")
os.environ.setdefault("DATABASE_URL", DSN)
os.environ.setdefault("BOT_TOKEN", "x")
os.environ.setdefault("CHANNEL_ID", "1")
os.environ.setdefault("CHANNEL_URL", "https://t.me/x")
os.environ.setdefault("GUIDE_URL", "https://x")

import db  # noqa: E402
import metering_worker as mw  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


async def cleanup(c: asyncpg.Connection) -> None:
    await c.execute(
        """
        do $$ declare t uuid;
        begin
            for t in select id from tenants where slug like 'smoke-w%' loop
                delete from usage_ledger          where tenant_id = t;
                delete from agent_token_snapshots where tenant_id = t;
                delete from credit_wallets        where tenant_id = t;
                delete from subscriptions         where tenant_id = t;
                delete from tenant_settings       where tenant_id = t;
                delete from messages              where tenant_id = t;
                delete from tenant_agents         where tenant_id = t;
                delete from leads                 where tenant_id = t;
                delete from tenants               where id = t;
            end loop;
        end $$;
        """
    )


async def ledger_count(c, tenant) -> int:
    return int(await c.fetchval(
        "select count(*) from usage_ledger where tenant_id = $1", tenant))


async def last_snapshot(c, agent_id) -> int | None:
    return await c.fetchval(
        "select used_tokens from agent_token_snapshots where agent_id = $1 "
        "order by taken_at desc limit 1", agent_id)


async def main() -> None:
    await db.init()  # резолвит default tenant (lesov-school должен быть в dev)
    pool = db.pool
    AGENT = 999999001
    async with pool.acquire() as c:
        await cleanup(c)
        t_mult = await c.fetchval(
            "insert into tenants (slug, name, status) values "
            "('smoke-w-mult', 'Смоук агент', 'active') returning id")
        await c.execute(
            "insert into subscriptions (tenant_id, plan_id, status, "
            "current_period_start, current_period_end) "
            "select $1, p.id, 'active', now(), now()+interval '30 days' "
            "from plans p where p.code='custom'", t_mult)  # cost_multiplier ×3
        await c.execute(
            "insert into tenant_agents (agent_id, tenant_id) values ($1,$2)", AGENT, t_mult)
        await c.execute(
            "insert into model_prices (provider, model, price_in_microrub_per_1k, "
            "price_out_microrub_per_1k) values ('timeweb-cloud-ai','smoke-model',234900,469800)")
        await c.execute(
            "insert into credit_wallets (tenant_id, balance_microrub) values ($1, 100000000) "
            "on conflict (tenant_id) do update set balance_microrub=100000000", t_mult)

    agent = {"id": AGENT, "model_id": 0, "used_tokens": 0}

    # A. baseline
    print("A. baseline агента (без списания):")
    await mw._process_agent_delta(AGENT, t_mult, 1000, "smoke-model")
    async with pool.acquire() as c:
        check("снапшот = 1000", await last_snapshot(c, AGENT) == 1000)
        check("леджер пуст", await ledger_count(c, t_mult) == 0)

    # B. дельта → списание. Себестоимость blended @0.5: (234900+469800)/2/1000 =
    # 352.35 µRUB/ткн → cost(1000)=352350; charged = cost × markup 3.00 = 1057050.
    print("B. дельта 1000 токенов → списание (cost×3):")
    await mw._process_agent_delta(AGENT, t_mult, 2000, "smoke-model")
    async with pool.acquire() as c:
        row = await c.fetchrow(
            "select cost_microrub, charged_microrub from usage_ledger where tenant_id=$1", t_mult)
        check("одна строка леджера", await ledger_count(c, t_mult) == 1)
        check("cost == 352350 (себестоимость)", row and row["cost_microrub"] == 352350,
              f"факт {row['cost_microrub'] if row else None}")
        check("charged == 352350×3 == 1057050", row and row["charged_microrub"] == 1057050,
              f"факт {row['charged_microrub'] if row else None}")
        check("снапшот = 2000", await last_snapshot(c, AGENT) == 2000)

    # C. glitch used=0 при prev>0 → НЕ baseline, НЕ списание (финдинг №1)
    print("C. глитч used_tokens=0 (финдинг №1):")
    await mw._process_agent_delta(AGENT, t_mult, 0, "smoke-model")
    async with pool.acquire() as c:
        check("снапшот всё ещё 2000 (baseline НЕ сброшен)", await last_snapshot(c, AGENT) == 2000)
        check("леджер не вырос (1 строка)", await ledger_count(c, t_mult) == 1)

    # D. recovery: дельта считается от сохранённого 2000, не от 0
    print("D. восстановление после глитча:")
    await mw._process_agent_delta(AGENT, t_mult, 2500, "smoke-model")
    async with pool.acquire() as c:
        check("вторая строка леджера (delta 500, не 2500)", await ledger_count(c, t_mult) == 2)
        row = await c.fetchrow(
            "select charged_microrub from usage_ledger where tenant_id=$1 "
            "order by id desc limit 1", t_mult)
        check("charged == ceil(500×352.35)×3 == 528525", row["charged_microrub"] == 528525,
              f"факт {row['charged_microrub']}")

    # E. реальный сброс (ненулевое уменьшение) → новый baseline
    print("E. реальный сброс счётчика (2500 → 100):")
    await mw._process_agent_delta(AGENT, t_mult, 100, "smoke-model")
    async with pool.acquire() as c:
        check("снапшот = 100 (новый baseline)", await last_snapshot(c, AGENT) == 100)
        check("леджер не вырос (2 строки)", await ledger_count(c, t_mult) == 2)

    # F/G. per_message baseline + charge
    print("F. per_message baseline (финдинг №2):")
    async with pool.acquire() as c:
        t_msg = await c.fetchval(
            "insert into tenants (slug, name, status) values "
            "('smoke-w-msg', 'Смоук сообщения', 'active') returning id")
        await c.execute(
            "insert into subscriptions (tenant_id, plan_id, status, "
            "current_period_start, current_period_end) "
            "select $1, p.id, 'active', now(), now()+interval '30 days' "
            "from plans p where p.code='econom'", t_msg)  # per_message 7,5 ₽
        await c.execute(
            "insert into credit_wallets (tenant_id, balance_microrub) values ($1, 100000000)", t_msg)
        # 3 ИСТОРИЧЕСКИХ сообщения Лии (created_at старше grace) — НЕ должны списаться
        for i in range(3):
            await c.execute(
                "insert into messages (tg_user_id, direction, kind, source, tenant_id, created_at) "
                "values ($1, 'out', 'text', 'liya', $2, now()-interval '10 min')", 5000+i, t_msg)
    await mw._scan_tenant_messages(t_msg)
    async with pool.acquire() as c:
        hwm = await c.fetchval(
            "select value from tenant_settings where tenant_id=$1 and key='metering_msg_hwm'", t_msg)
        check("baseline hwm установлен", hwm is not None)
        check("история НЕ списана (леджер пуст)", await ledger_count(c, t_msg) == 0)

    print("G. per_message: новое сообщение после baseline списывается:")
    async with pool.acquire() as c:
        await c.execute(
            "insert into messages (tg_user_id, direction, kind, source, tenant_id, created_at) "
            "values ($1, 'out', 'text', 'liya', $2, now()-interval '5 min')", 6000, t_msg)
    await mw._scan_tenant_messages(t_msg)
    async with pool.acquire() as c:
        row = await c.fetchrow(
            "select charged_microrub from usage_ledger where tenant_id=$1", t_msg)
        check("одно списание", await ledger_count(c, t_msg) == 1)
        check("charged == 7_500_000 (цена econom)", row and row["charged_microrub"] == 7_500_000,
              f"факт {row['charged_microrub'] if row else None}")

    async with pool.acquire() as c:
        await c.execute("delete from model_prices where model='smoke-model'")
        await cleanup(c)
    await db.close()
    print("Чистка выполнена.")
    if FAILS:
        raise SystemExit(f"ПРОВАЛ: {FAILS}")
    print("ВСЕ ПРОВЕРКИ ВОРКЕРА ПРОЙДЕНЫ.")


if __name__ == "__main__":
    asyncio.run(main())
