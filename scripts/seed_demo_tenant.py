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
    "Ты — Лия, ИИ-агент продаж сервиса «ИИ-Агент Про» — платформы, которая даёт бизнесу собственного "
    "ИИ-агента продаж в Telegram, ВКонтакте и MAX. Агент 24/7 отвечает лидам, ведёт их по воронке, "
    "передаёт горячих менеджеру и принимает оплату. Главное: в этом диалоге ты НА СОБСТВЕННОМ ПРИМЕРЕ "
    "показываешь, как это работает — ты сама и есть такой агент. Веди тепло и по делу: выясни, какой у "
    "клиента бизнес и где он теряет лиды (медленные ответы, ночью некому отвечать, менеджеры выгорают), "
    "покажи ценность (мгновенные ответы без выходных, рост конверсии, экономия на операторах), мягко "
    "подведи к подключению. Не дави и не выдумывай фактов. Если клиент заинтересован — предложи "
    "оформить подключение тарифа (напиши «магазин» — покажу тарифы)."
)
DEMO_FALLBACK = (
    "Я — Лия, демо ИИ-агента продаж сервиса «ИИ-Агент Про». Прямо сейчас я работаю так же, как будет "
    "работать агент у вас. Напишите «магазин» — покажу тарифы подключения."
)

# Демо-тарифы (цифры иллюстративные — заменить на реальные тарифы).
PRODUCTS = [
    ("Подключение ИИ-агента «Старт» — 1 канал", "main", 9900),
    ("Тариф «Бизнес» — Telegram+VK+MAX, триггеры, касса", "main", 24900),
]
# (tg_user_id, status, [(direction, text), ...]) — наполняют воронку в панели «Диалоги»
LEADS = [
    (9000000001, "new",        [("in", "Здравствуйте, как работает ИИ-агент?")]),
    (9000000002, "nurturing",  [("in", "А он сможет отвечать в нашем ВК?"),
                                ("out", "Да — и в ВКонтакте, и в MAX, и в Telegram. Расскажу, как подключить")]),
    (9000000003, "converted",  [("in", "Хотим подключить агента"),
                                ("out", "Отлично! Оформляю подключение тарифа")]),
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
