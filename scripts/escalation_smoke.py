#!/usr/bin/env python3
"""Смоук A3: эскалация лида менеджерам (bot-telegram/escalation.py + db.claim/release).

Парсер/карточка — чистая логика (без aiogram). Дедуп claim/release — на risuy_dev (создаёт
тестовый тенант+лид, чистит). На прод НЕ запускать.

Запуск: ESC_SMOKE_DSN="postgresql://<owner>@<host>:5432/risuy_dev?sslmode=require" \
        PYTHONPATH=. ./.venv-smoke/bin/python scripts/escalation_smoke.py
"""
import asyncio
import os
import sys
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "bot-telegram"))
os.environ.setdefault("BOT_TOKEN", "0:smoke")
os.environ.setdefault("DATABASE_URL", "postgresql://x/y")
os.environ.setdefault("CHANNEL_ID", "-1001234567890")
os.environ.setdefault("CHANNEL_URL", "https://t.me/smoke")
os.environ.setdefault("GUIDE_URL", "https://example.org/guide")

import asyncpg          # noqa: E402
import db               # noqa: E402  (bot-telegram/db.py)
import escalation       # noqa: E402  (bot-telegram/escalation.py)

DSN = os.environ.get("ESC_SMOKE_DSN")
if not DSN or "/risuy_dev" not in DSN.split("?")[0]:
    raise SystemExit("Задайте ESC_SMOKE_DSN на risuy_dev.")

FAILS: list[str] = []
TG = 990777001


def check(name, cond, detail=""):
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail != "" else ""))
    if not cond:
        FAILS.append(name)


async def main() -> None:
    # ── 1. parse_escalation (чистая логика) ──
    print("1. parse_escalation:")
    t1, p1 = escalation.parse_escalation('Спасибо! Менеджер свяжется.\n[[ESCALATE]]{"name":"Анна","phone":"89211234567","reason":"qualified"}[[/ESCALATE]]')
    check("маркер вырезан из текста клиента", "[[ESCALATE]]" not in t1 and t1 == "Спасибо! Менеджер свяжется.", repr(t1))
    check("payload распарсен", isinstance(p1, dict) and p1.get("name") == "Анна" and p1.get("phone") == "89211234567")
    t2, p2 = escalation.parse_escalation("Обычный ответ без маркера")
    check("нет маркера → (текст, None)", t2 == "Обычный ответ без маркера" and p2 is None)
    t3, p3 = escalation.parse_escalation("Текст [[ESCALATE]]{битый json,,}[[/ESCALATE]] ещё")
    check("битый JSON → маркер вырезан, payload={}", "[[ESCALATE]]" not in t3 and p3 == {}, repr((t3, p3)))
    t4, p4 = escalation.parse_escalation("a [[escalate]]{\n  \"reason\": \"client_request\"\n}[[/escalate]] b")
    check("регистр/многострочный JSON распарсен", "escalate" not in t4.lower() and p4.get("reason") == "client_request", repr((t4, p4)))
    # Ревью A3 (high): усечённый/непарный маркер (LLM оборвал на открытом) — маркер ВСЁ РАВНО
    # вырезан, ПДн клиента не утекают, payload={} (сигнал, не «тихая потеря»).
    t5, p5 = escalation.parse_escalation('Спасибо!\n[[ESCALATE]]{"name":"Иван","phone":"+79991234567"')
    check("усечённый открытый маркер вырезан (без утечки ПДн)", "[[ESCALATE]]" not in t5 and "79991234567" not in t5 and t5 == "Спасибо!", repr(t5))
    check("усечённый маркер → payload={} (не None, не тихая потеря)", p5 == {}, repr(p5))
    t6, p6 = escalation.parse_escalation("Ответ клиенту.[[/ESCALATE]] хвост")
    check("осиротевший закрывающий маркер вырезан", "ESCALATE" not in t6.upper(), repr(t6))
    t7, p7 = escalation.parse_escalation('x [[ESCALATE]]{"name":"Аня"} y')  # только открытый (нет закрывающего)
    check("открытый без пары: маркер+хвост вырезаны", "ESCALATE" not in t7.upper() and "Аня" not in t7, repr(t7))
    # Ревью A3 round 2 (high): усечение на САМОМ опенере — ДО его ']]'. Раньше утекало (якорь на ']]').
    t8, p8 = escalation.parse_escalation('Здравствуйте! [[ESCALATE')
    check("усечённый опенер [[ESCALATE (без ]]) вырезан", "ESCALATE" not in t8.upper() and t8 == "Здравствуйте!", repr(t8))
    check("усечённый опенер → payload={} (сигнал)", p8 == {}, repr(p8))
    t8b, p8b = escalation.parse_escalation('Привет [[ESCALATE]{"name":"Х"')  # опенер с одной ] + обрыв
    check("опенер с одной скобкой [[ESCALATE] вырезан", "ESCALATE" not in t8b.upper() and "Х" not in t8b, repr(t8b))
    # Ревью A3 round 2 (low): голый 'ESCALATE]]' БЕЗ '[[' — НЕ маркер → НЕ ложная эскалация.
    t9, p9 = escalation.parse_escalation('Команда: ESCALATE]] это просто текст')
    check("'ESCALATE]]' без '[[' → текст не тронут", t9 == "Команда: ESCALATE]] это просто текст", repr(t9))
    check("'ESCALATE]]' без '[[' → payload=None (нет ложной эскалации)", p9 is None, repr(p9))

    # ── 2. format_card ──
    print("2. format_card:")
    card = escalation.format_card({"name": "Анна", "phone": "89211234567", "product": "В Академию Репина",
                                   "reason": "qualified", "intent": "enroll", "summary": "Хочет курс, готова платить."},
                                  tg_user_id=TG, lead_id="abc-123", panel_base="https://panel.example")
    check("карточка содержит имя/телефон/курс", all(s in card for s in ("Анна", "89211234567", "В Академию Репина")))
    check("enum-коды переведены на русский (qualified/enroll)",
          "квалифицирован" in card and "запись на курс" in card and "qualified" not in card and "enroll" not in card, repr(card))
    check("ссылка на диалог в панели", "https://panel.example/dialogs/abc-123" in card)
    check("прямой ЧС tg://user?id", f"tg://user?id={TG}" in card)
    check("«Сводка диалога» как подпись", "Сводка диалога:" in card)
    check("карточка plain (без HTML-тегов)", "<" not in card)
    card_nopanel = escalation.format_card({"name": "Б"}, tg_user_id=TG)
    check("без panel_base — только tg-ссылка, без /dialogs/", f"tg://user?id={TG}" in card_nopanel and "/dialogs/" not in card_nopanel)
    card_empty = escalation.format_card({}, tg_user_id=TG, raw="raw-signal")
    check("пустой payload → сырой сигнал + tg-ссылка", "raw-signal" in card_empty and f"tg://user?id={TG}" in card_empty)
    # Слой C: client_link для VK (карточка ведёт на vk.com, а не tg://)
    import vk_driver  # noqa: E402
    card_vk = escalation.format_card({"name": "Вика"}, tg_user_id=778899, client_link=vk_driver.vk_client_link(778899))
    check("VK-карточка: ссылка vk.com/id (НЕ tg://)", "https://vk.com/id778899" in card_vk and "tg://" not in card_vk, repr(card_vk))
    check("VK-карточка: подпись про ВКонтакте", "ВКонтакте" in card_vk)
    check("_client_link('vk') → vk-url", escalation._client_link("vk", 778899) == ("https://vk.com/id778899", "Написать клиенту в ВКонтакте"))
    check("_client_link('tg') → None (дефолт tg://)", escalation._client_link("tg", 778899) is None)
    # MAX: карточка без url (нет публичного профиля) → строка с подписью, без 'tg://'
    card_max = escalation.format_card({"name": "Макс"}, tg_user_id=910921, client_link=escalation._client_link("max", 910921))
    check("MAX-карточка: подпись про MAX, без tg:// и без двоеточия-url", "Клиент в MAX" in card_max and "tg://" not in card_max, repr(card_max))

    # ── 3. claim/release дедуп (risuy_dev) ──
    print("3. claim/release дедуп:")
    db.pool = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    tid = None
    try:
        async with db.pool.acquire() as c:
            await c.execute("delete from leads where tg_user_id = $1", TG)
            tid = await c.fetchval("insert into tenants(slug,name,status) values('smoke-esc','ESC','active') returning id")
            await c.execute(
                "insert into leads(tg_user_id,messenger,source,status,name,tenant_id) "
                "values($1,'tg','x','new','Тест',$2)", TG, tid)
        db.current_tenant_id.set(tid)  # активный тенант = тестовый
        check("get_lead_id вернул id лида (для ссылки на диалог)", (await db.get_lead_id(TG)) is not None)
        c1 = await db.claim_lead_escalation(TG)
        c2 = await db.claim_lead_escalation(TG)
        check("первый claim = True (застолбил)", c1 is True)
        check("второй claim = False (дедуп)", c2 is False)
        await db.release_lead_escalation(TG)
        c3 = await db.claim_lead_escalation(TG)
        check("после release claim снова True (ретрай при сбое отправки)", c3 is True)
        check("claim несуществующего лида = False", (await db.claim_lead_escalation(999000111)) is False)

        # ── 4. resolve_escalation_target: per-tenant адрес + env-фолбэк только для Школы ──
        print("4. resolve_escalation_target (Слой A):")
        import config as botcfg
        db.current_tenant_id.set(tid)
        db._default_tenant_id = None                      # тестовый тенант — НЕ Школа
        check("тенант без адреса (не Школа) → None", (await escalation.resolve_escalation_target(tid)) is None)
        async with db.pool.acquire() as c:
            await c.execute(
                "insert into tenant_settings(tenant_id,key,value) values "
                "($1,'escalation_enabled','1'),($1,'escalation_chat_id','-1009998887'),($1,'escalation_topic_id','42') "
                "on conflict (tenant_id,key) do update set value=excluded.value", tid)
        check("тенант с включённым адресом → (chat_id, topic)",
              (await escalation.resolve_escalation_target(tid)) == (-1009998887, 42))
        async with db.pool.acquire() as c:
            await c.execute("update tenant_settings set value='' where tenant_id=$1 and key='escalation_enabled'", tid)
        check("тенант с выключенным тумблером → None", (await escalation.resolve_escalation_target(tid)) is None)
        # env-фолбэк ТОЛЬКО для дефолт-тенанта (Школа): делаем tid дефолтным + задаём env-группу
        botcfg.MANAGER_GROUP_ID, botcfg.MANAGER_TOPIC_ID = -100123, 7
        db._default_tenant_id = tid
        check("дефолт-тенант (Школа) без своего адреса → env-фолбэк",
              (await escalation.resolve_escalation_target(tid)) == (-100123, 7))
        db._default_tenant_id = None
    finally:
        async with db.pool.acquire() as c:
            await c.execute("delete from leads where tg_user_id = $1", TG)
            if tid:
                await c.execute("delete from tenants where id = $1", tid)
        await db.pool.close()

    print()
    if FAILS:
        print(f"❌ ПРОВАЛЫ ({len(FAILS)}): " + ", ".join(FAILS)); sys.exit(1)
    print("✅ escalation smoke — все проверки зелёные")


if __name__ == "__main__":
    asyncio.run(main())
