#!/usr/bin/env python3
"""Смоук B5 (аудит платежей): panel-сторона оплаты заказов на risuy_dev. На прод НЕ запускать.

Проверяет:
  #31  mark_order_paid_by_payment: заказ→paid, лид→converted, «спасибо» в outbox В КАНАЛЕ лида
       (vk: messenger='vk', tg_user_id=NULL);
  #9   mark_order_paid_by_order_id: фолбэк-матч по id заказа + БЭКФИЛЛ provider_payment_id;
  #10  set_order_status_with_audit('paid'): ручной перевод ТОЖЕ конвертит лида;
  #31  create_invoice_order_with_audit/enqueue_invoice_message: канал-агностичны (vk-лид → счёт).

Запуск: B5_SMOKE_DSN="postgresql://<owner>@<host>:5432/risuy_dev?sslmode=require" \
        PYTHONPATH=admin-panel ./.venv-smoke/bin/python scripts/b5_payments_smoke.py
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

DSN = os.environ.get("B5_SMOKE_DSN")
if not DSN or "/risuy_dev" not in DSN.split("?")[0]:
    raise SystemExit("Задайте B5_SMOKE_DSN на risuy_dev.")

FAILS: list[str] = []
VK = 770330111


def check(name, cond, detail=""):
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail != "" else ""))
    if not cond:
        FAILS.append(name)


async def _mk_order(c, tid, lead, prod, pay_id):
    return await c.fetchval(
        "insert into orders (lead_id, product_id, amount, currency, status, source, created_by, "
        "                    tenant_id, provider_payment_id) "
        "values ($1,$2,100,'RUB','pending','yookassa','smoke',$3,$4) returning id",
        lead, prod, tid, pay_id)


async def _outbox_last(c, lead):
    return await c.fetchrow(
        "select messenger, tg_user_id, kind from outbox where lead_id = $1 order by id desc limit 1", lead)


async def main() -> None:
    db.pool = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    tid = None
    try:
        async with db.pool.acquire() as c:
            await c.execute("delete from leads where vk_user_id = $1", VK)
            tid = await c.fetchval("insert into tenants(slug,name,status) values('smoke-b5','B5','active') returning id")
            lead = await c.fetchval(
                "insert into leads(tenant_id, messenger, vk_user_id, source, consent, status) "
                "values ($1,'vk',$2,'vk_group',true,'new') returning id", tid, VK)
            prod = await c.fetchval(
                "insert into products(name, kind, price, currency, status, created_by, tenant_id) "
                "values ('P','main',100,'RUB','active','smoke',$1) returning id", tid)

        # ── #31 + by_payment ──
        print("1. mark_order_paid_by_payment (#31 канал-агностичное «спасибо»):")
        async with db.pool.acquire() as c:
            oid = await _mk_order(c, tid, lead, prod, "pay_a")
        res = await db.mark_order_paid_by_payment("pay_a")
        check("вернул заказ", res is not None)
        async with db.pool.acquire() as c:
            st = await c.fetchval("select status from orders where id=$1", oid)
            lst = await c.fetchval("select status from leads where id=$1", lead)
            ob = await _outbox_last(c, lead)
        check("заказ → paid", st == "paid", st)
        check("лид → converted", lst == "converted", lst)
        check("«спасибо» в outbox с messenger='vk'", ob and ob["messenger"] == "vk")
        check("«спасибо»: tg_user_id NULL (vk)", ob and ob["tg_user_id"] is None)

        # ── #9 фолбэк по order_id + бэкфилл ──
        print("2. mark_order_paid_by_order_id (#9 фолбэк + бэкфилл provider_payment_id):")
        async with db.pool.acquire() as c:
            await c.execute("update leads set status='new' where id=$1", lead)
            oid2 = await c.fetchval(
                "insert into orders (lead_id, product_id, amount, currency, status, source, created_by, tenant_id) "
                "values ($1,$2,100,'RUB','pending','yookassa','smoke',$3) returning id", lead, prod, tid)
        res2 = await db.mark_order_paid_by_order_id(oid2, "pay_b")
        async with db.pool.acquire() as c:
            ppid = await c.fetchval("select provider_payment_id from orders where id=$1", oid2)
            st2 = await c.fetchval("select status from orders where id=$1", oid2)
        check("фолбэк-заказ → paid", st2 == "paid", st2)
        check("provider_payment_id бэкфилл = pay_b", ppid == "pay_b", str(ppid))

        # ── #10 ручной статус конвертит лида ──
        print("3. set_order_status_with_audit('paid') (#10 ручной → конверсия):")
        async with db.pool.acquire() as c:
            await c.execute("update leads set status='new' where id=$1", lead)
            oid3 = await c.fetchval(
                "insert into orders (lead_id, product_id, amount, currency, status, source, created_by, tenant_id) "
                "values ($1,$2,100,'RUB','pending','manual','smoke',$3) returning id", lead, prod, tid)
        await db.set_order_status_with_audit(oid3, new_status="paid", actor="smoke", ip=None, user_agent=None)
        async with db.pool.acquire() as c:
            lst3 = await c.fetchval("select status from leads where id=$1", lead)
        check("ручной paid → лид converted", lst3 == "converted", lst3)

        # ── #31 счёт канал-агностичный ──
        print("4. create_invoice_order / enqueue_invoice_message (#31 счёт vk-лиду):")
        inv = await db.create_invoice_order_with_audit(lead, prod, 100, "RUB", actor="smoke", ip=None, user_agent=None)
        check("счёт создан для vk-лида (не None)", inv is not None)
        check("messenger='vk', addr=vk_user_id", inv and inv["messenger"] == "vk" and inv["addr"] == VK,
              str(inv and (inv["messenger"], inv["addr"])))
        await db.enqueue_invoice_message(lead, inv["messenger"], inv["addr"], "счёт", actor="smoke")
        async with db.pool.acquire() as c:
            ob4 = await _outbox_last(c, lead)
        check("сообщение-счёт в outbox с messenger='vk'", ob4 and ob4["messenger"] == "vk" and ob4["tg_user_id"] is None)

        print()
        if FAILS:
            print(f"❌ ПРОВАЛЫ ({len(FAILS)}): " + ", ".join(FAILS)); sys.exit(1)
        print("✅ b5_payments smoke — все проверки зелёные")
    finally:
        if tid is not None:
            async with db.pool.acquire() as c:
                await c.execute("delete from leads where vk_user_id = $1", VK)  # cascade: orders/outbox/messages
                await c.execute("delete from products where tenant_id = $1", tid)
                await c.execute("delete from orders where tenant_id = $1", tid)
                await c.execute("delete from tenants where id = $1", tid)
        await db.pool.close()


if __name__ == "__main__":
    asyncio.run(main())
