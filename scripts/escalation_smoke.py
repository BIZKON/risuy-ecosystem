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
                                   "reason": "qualified", "intent": "enroll"}, tg_user_id=TG)
    check("карточка содержит имя/телефон/курс/tg_id", all(s in card for s in ("Анна", "89211234567", "В Академию Репина", str(TG))))
    check("enum-коды переведены на русский (qualified/enroll)",
          "квалифицирован" in card and "запись на курс" in card and "qualified" not in card and "enroll" not in card, repr(card))
    check("карточка plain (без HTML-тегов)", "<" not in card)
    card_empty = escalation.format_card({}, tg_user_id=TG, raw="raw-signal")
    check("пустой payload → сырой сигнал + tg_id", "raw-signal" in card_empty and str(TG) in card_empty)

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
        c1 = await db.claim_lead_escalation(TG)
        c2 = await db.claim_lead_escalation(TG)
        check("первый claim = True (застолбил)", c1 is True)
        check("второй claim = False (дедуп)", c2 is False)
        await db.release_lead_escalation(TG)
        c3 = await db.claim_lead_escalation(TG)
        check("после release claim снова True (ретрай при сбое отправки)", c3 is True)
        check("claim несуществующего лида = False", (await db.claim_lead_escalation(999000111)) is False)
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
