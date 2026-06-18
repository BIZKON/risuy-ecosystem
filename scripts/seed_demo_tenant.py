#!/usr/bin/env python3
"""Демо/sandbox-тенант для продаж (Founder's Playbook, GTM: «well-built demo environment closes deals
while you're in board meetings»). Сеет ИЗОЛИРОВАННЫЙ демо-тенант (slug='demo-sandbox') с включённой
Лией, парой товаров и наполненной воронкой лидов — чтобы лид/инвестор сразу видел работающий продукт.

🟥 БЕЗОПАСНОСТЬ:
  • По умолчанию работает ТОЛЬКО на risuy_dev. На боевой `risuy` — лишь при SEED_ALLOW_PROD=yes
    (двойное подтверждение). Трогает ИСКЛЮЧИТЕЛЬНО демо-тенант (slug='demo-sandbox') — другие тенанты
    не затрагиваются никогда.
  • Идемпотентно: пересоздаёт демо начисто (delete demo → insert). Всё в одной транзакции (rollback при сбое).
  • Реверсивно: `--teardown` удаляет демо-тенант целиком.

ЗАПУСК (dev-пруф):
  SEED_DSN="postgresql://gen_user:<pw>@81.31.246.136:5432/risuy_dev?sslmode=require" \
      ./.venv-smoke/bin/python scripts/seed_demo_tenant.py            # создать/пересоздать
  SEED_DSN="...risuy_dev..." ./.venv-smoke/bin/python scripts/seed_demo_tenant.py --teardown

ПРОД (только когда есть демо-бот-токен; токен кладётся в «Каналы»/«Ключи» отдельно, НЕ здесь):
  SEED_ALLOW_PROD=yes SEED_DSN="...risuy..." ./.venv-smoke/bin/python scripts/seed_demo_tenant.py
"""
import asyncio
import os
import sys

import asyncpg

SLUG = "demo-sandbox"
DSN = os.environ.get("SEED_DSN")
if not DSN:
    raise SystemExit("Задайте SEED_DSN.")
DBNAME = DSN.split("?")[0].rstrip("/").split("/")[-1]
TEARDOWN = "--teardown" in sys.argv

# Гард прода: боевая db называется ровно 'risuy'; dev — 'risuy_dev'.
if DBNAME == "risuy" and os.environ.get("SEED_ALLOW_PROD") != "yes":
    raise SystemExit("ОТКАЗ: это боевой risuy. Для прода явно: SEED_ALLOW_PROD=yes (нужен демо-бот-токен).")

DEMO_PROMPT = (
    "Ты — Лия, ИИ-ассистент по продажам «Демо-Академии» (онлайн-обучение запуску бизнеса). "
    "Веди диалог тепло и по делу: выяви задачу клиента, покажи ценность, мягко подведи к покупке "
    "курса или консультации. Не дави, не выдумывай фактов. Если клиент готов — предложи оформить оплату."
)
DEMO_FALLBACK = "Спасибо за сообщение! Я — демо-ассистент Лия. Чтобы посмотреть продукты, напишите «магазин»."

PRODUCTS = [
    ("Демо-курс «Запуск за 30 дней»", "main", 4900),
    ("Разбор-консультация 60 мин", "main", 2900),
]
# (tg_user_id, status, [(direction, text), ...])
LEADS = [
    (9000000001, "new",        [("in", "Здравствуйте, расскажите про курс")]),
    (9000000002, "nurturing",  [("in", "А есть рассрочка?"), ("out", "Да, можно частями — расскажу детали")]),
    (9000000003, "converted",  [("in", "Хочу купить курс"), ("out", "Отлично! Вот ссылка на оплату")]),
]


async def teardown(c, tid):
    if tid is None:
        return
    await c.execute("delete from leads where tenant_id = $1", tid)      # cascade: orders/outbox/messages
    await c.execute("delete from orders where tenant_id = $1", tid)     # на случай orphan
    await c.execute("delete from products where tenant_id = $1", tid)
    await c.execute("delete from tenant_settings where tenant_id = $1", tid)
    await c.execute("delete from tenants where id = $1", tid)


async def main():
    print(f"seed_demo_tenant · база={DBNAME} · slug={SLUG} · режим={'TEARDOWN' if TEARDOWN else 'CREATE'}")
    c = await asyncpg.connect(DSN)
    try:
        async with c.transaction():
            tid = await c.fetchval("select id from tenants where slug = $1", SLUG)
            await teardown(c, tid)  # всегда чистим демо начисто (или это и есть teardown)
            if TEARDOWN:
                print("✅ демо-тенант удалён" if tid else "демо-тенанта не было — нечего удалять")
                return

            tid = await c.fetchval(
                "insert into tenants(slug, name, status) values($1, $2, 'active') returning id",
                SLUG, "Демо — ИИ-Агент Про")
            # Лия включена со своим демо-промптом (бэкенд по умолчанию cloud_ai).
            for k, v in (("ai_enabled", "1"), ("ai_system_prompt", DEMO_PROMPT), ("ai_fallback_text", DEMO_FALLBACK)):
                await c.execute(
                    "insert into tenant_settings(tenant_id, key, value) values($1, $2, $3)", tid, k, v)
            prod_ids = []
            for name, kind, price in PRODUCTS:
                pid = await c.fetchval(
                    "insert into products(name, kind, price, currency, status, created_by, tenant_id) "
                    "values($1, $2, $3, 'RUB', 'active', 'demo-seed', $4) returning id",
                    name, kind, price, tid)
                prod_ids.append(pid)
            for tg, status, msgs in LEADS:
                lead_id = await c.fetchval(
                    "insert into leads(tenant_id, messenger, tg_user_id, source, consent, status) "
                    "values($1, 'tg', $2, 'demo', true, $3) returning id",
                    tid, tg, status)
                for direction, text in msgs:
                    await c.execute(
                        "insert into messages(lead_id, tg_user_id, messenger, direction, kind, text, source, tenant_id) "
                        "values($1, $2, 'tg', $3, 'text', $4, 'demo', $5)",
                        lead_id, tg, direction, text, tid)
                # для converted-лида — оплаченный заказ (наполняет воронку выручкой в демо)
                if status == "converted":
                    await c.execute(
                        "insert into orders(lead_id, product_id, amount, currency, status, source, created_by, tenant_id, paid_at) "
                        "values($1, $2, $3, 'RUB', 'paid', 'demo', 'demo-seed', $4, now())",
                        lead_id, prod_ids[0], PRODUCTS[0][2], tid)

            # верификация
            cnt = await c.fetchrow(
                "select (select count(*) from products where tenant_id=$1) p, "
                "       (select count(*) from leads where tenant_id=$1) l, "
                "       (select count(*) from orders where tenant_id=$1 and status='paid') o, "
                "       (select count(*) from tenant_settings where tenant_id=$1) s", tid)
            print(f"✅ демо-тенант создан id={tid}")
            print(f"   товаров={cnt['p']} · лидов={cnt['l']} · оплаченных заказов={cnt['o']} · настроек Лии={cnt['s']}")
            print("   Лия: ai_enabled=1 + демо-промпт. На прод оживёт после ввода демо-бот-токена в «Каналы».")
    finally:
        await c.close()


if __name__ == "__main__":
    asyncio.run(main())
