#!/usr/bin/env python3
"""Смоук B7 (аудит #5): RLS на orders НЕ ломает вебхук ЮKassa. risuy_dev. На прод НЕ запускать.

ГЛАВНАЯ ПРОВЕРКА: вебхук работает в процессе панели под ролью panel_rw (НЕ owner → подчиняется RLS) и
читает orders СЕССИОННО-БЕЗ app.tenant_id. Тест вызывает РЕАЛЬНУЮ db.mark_order_paid_by_payment под
пулом panel_rw при включённом RLS — если заказ помечается paid (SECURITY DEFINER-discovery обходит RLS,
затем set_config открывает RLS-скоуп), значит прод не сломается. Контроль: прямой select orders под
panel_rw БЕЗ app.tenant_id возвращает 0 (RLS реально включён, не «дыра»).

Setup/verify/cleanup — под OWNER (gen_user, обходит RLS). Требует применённых migrate_rls_discovery_fns +
migrate_rls_orders_kb_broadcasts.

Запуск: OWNER_DSN=... PANEL_RW_DSN=... PYTHONPATH=admin-panel ./.venv-smoke/bin/python scripts/rls_orders_smoke.py
"""
import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "admin-panel"))
os.environ.setdefault("DATABASE_URL", "postgresql://x/y")
os.environ.setdefault("SESSION_SECRET", "x" * 40)
os.environ.setdefault("ADMIN_USERNAME", "admin")
os.environ.setdefault("ADMIN_PASSWORD_HASH",
                      "$argon2id$v=19$m=65536,t=3,p=4$c2FsdHNhbHRzYWx0$aGFzaGhhc2hoYXNoaGFzaGhhc2g")

import asyncpg  # noqa: E402
import db        # noqa: E402  (admin-panel/db.py)

OWNER = os.environ.get("OWNER_DSN")
PANEL = os.environ.get("PANEL_RW_DSN")
for name, dsn in (("OWNER_DSN", OWNER), ("PANEL_RW_DSN", PANEL)):
    if not dsn or "/risuy_dev" not in dsn.split("?")[0]:
        raise SystemExit(f"Задайте {name} на risuy_dev.")

FAILS: list[str] = []
VK = 779900111


def check(name, cond, detail=""):
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail != "" else ""))
    if not cond:
        FAILS.append(name)


async def main() -> None:
    owner = await asyncpg.create_pool(OWNER, min_size=1, max_size=3)
    panel = await asyncpg.create_pool(PANEL, min_size=1, max_size=3)
    tid = None
    try:
        # ── setup (owner, обходит RLS) ──
        async with owner.acquire() as c:
            await c.execute("delete from leads where vk_user_id = $1", VK)
            tid = await c.fetchval("insert into tenants(slug,name,status) values('smoke-b7','B7','active') returning id")
            lead = await c.fetchval(
                "insert into leads(tenant_id,messenger,vk_user_id,source,consent,status) "
                "values ($1,'vk',$2,'vk_group',true,'new') returning id", tid, VK)
            prod = await c.fetchval(
                "insert into products(name,kind,price,currency,status,created_by,tenant_id) "
                "values ('P','main',100,'RUB','active','smoke',$1) returning id", tid)
            oid = await c.fetchval(
                "insert into orders(lead_id,product_id,amount,currency,status,source,created_by,"
                "tenant_id,provider_payment_id) values ($1,$2,100,'RUB','pending','yookassa','smoke',$3,'b7pay') "
                "returning id", lead, prod, tid)

        # ── контроль: под panel_rw БЕЗ app.tenant_id RLS реально режет ──
        print("1. RLS реально включён (panel_rw без app.tenant_id):")
        async with panel.acquire() as c:
            n_noctx = await c.fetchval("select count(*) from orders where provider_payment_id='b7pay'")
            sd = await c.fetchval("select order_tenant_for_payment('b7pay')")
        check("прямой select orders → 0 (RLS режет)", n_noctx == 0, str(n_noctx))
        check("SECURITY DEFINER discovery → tenant (обход RLS)", str(sd) == str(tid), str(sd))

        # ── ГЛАВНОЕ: реальная mark_order_paid_by_payment под пулом panel_rw при RLS ──
        print("2. db.mark_order_paid_by_payment под panel_rw (как вебхук) при включённом RLS:")
        db.pool = panel
        res = await db.mark_order_paid_by_payment("b7pay")
        check("вебхук пометил заказ (не None — RLS НЕ сломал)", res is not None)

        # ── verify (owner) ──
        db.pool = owner
        async with owner.acquire() as c:
            st = await c.fetchval("select status from orders where id=$1", oid)
            lst = await c.fetchval("select status from leads where id=$1", lead)
            ob = await c.fetchrow("select messenger, tg_user_id from outbox where lead_id=$1 order by id desc limit 1", lead)
        check("заказ → paid", st == "paid", st)
        check("лид → converted", lst == "converted", lst)
        check("«спасибо» в канал vk (messenger='vk', tg_user_id NULL)", ob and ob["messenger"] == "vk" and ob["tg_user_id"] is None)

        print("3. Фолбэк #9 (by_order_id) под panel_rw при RLS:")
        async with owner.acquire() as c:
            await c.execute("update leads set status='new' where id=$1", lead)
            oid2 = await c.fetchval(
                "insert into orders(lead_id,product_id,amount,currency,status,source,created_by,tenant_id) "
                "values ($1,$2,100,'RUB','pending','yookassa','smoke',$3) returning id", lead, prod, tid)
        db.pool = panel
        res2 = await db.mark_order_paid_by_order_id(oid2, "b7pay2")
        db.pool = owner
        async with owner.acquire() as c:
            st2 = await c.fetchrow("select status, provider_payment_id from orders where id=$1", oid2)
        check("фолбэк под panel_rw пометил paid + бэкфилл", res2 is not None and st2 and st2["status"] == "paid"
              and st2["provider_payment_id"] == "b7pay2")

        print()
        if FAILS:
            print(f"❌ ПРОВАЛЫ ({len(FAILS)}): " + ", ".join(FAILS)); sys.exit(1)
        print("✅ rls_orders smoke — RLS включён И вебхук (panel_rw) работает: прод не сломается")
    finally:
        db.pool = owner
        if tid is not None:
            async with owner.acquire() as c:
                await c.execute("delete from leads where vk_user_id = $1", VK)
                await c.execute("delete from products where tenant_id = $1", tid)
                await c.execute("delete from orders where tenant_id = $1", tid)
                await c.execute("delete from tenants where id = $1", tid)
        await owner.close()
        await panel.close()


if __name__ == "__main__":
    asyncio.run(main())
