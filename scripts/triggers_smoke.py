#!/usr/bin/env python3
"""Смоук Слоя B движка триггеров (bot-telegram/triggers.py + db) на risuy_dev.

Чистая логика (без aiogram): match_stopwords, format_trigger_card.
БД (risuy_dev, создаёт тестовый тенант/лид/триггеры, чистит): get_active_triggers (фильтр по
типам, исключение disabled, tenant-изоляция), count_inbound_messages, pause_lead. На прод НЕ запускать.

Запуск: TRIG_SMOKE_DSN="postgresql://<owner>@<host>:5432/risuy_dev?sslmode=require" \
        PYTHONPATH=. ./.venv-smoke/bin/python scripts/triggers_smoke.py
"""
import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "bot-telegram"))
os.environ.setdefault("BOT_TOKEN", "0:smoke")
os.environ.setdefault("DATABASE_URL", "postgresql://x/y")
os.environ.setdefault("CHANNEL_ID", "-1001234567890")
os.environ.setdefault("CHANNEL_URL", "https://t.me/smoke")
os.environ.setdefault("GUIDE_URL", "https://example.org/guide")

import asyncpg    # noqa: E402
import db         # noqa: E402  (bot-telegram/db.py)
import triggers   # noqa: E402  (bot-telegram/triggers.py)

DSN = os.environ.get("TRIG_SMOKE_DSN")
if not DSN or "/risuy_dev" not in DSN.split("?")[0]:
    raise SystemExit("Задайте TRIG_SMOKE_DSN на risuy_dev.")

FAILS: list[str] = []
TG = 990777055


def check(name, cond, detail=""):
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail != "" else ""))
    if not cond:
        FAILS.append(name)


async def main() -> None:
    # ── 1. match_stopwords (чистая логика) ──
    print("1. match_stopwords:")
    check("регистронезависимо (ОТМЕНА=отмена)", triggers.match_stopwords("Прошу ОТМЕНА брони", ["отмена"]) == "отмена")
    check("точное слово найдено", triggers.match_stopwords("прошу отмена брони", ["отмена"]) == "отмена")
    check("словоформа: 'отмену' НЕ матчит 'отмена' (клиент добавляет формы)",
          triggers.match_stopwords("прошу отмену", ["отмена"]) is None)
    check("фраза найдена", triggers.match_stopwords("а когда менеджер свяжется со мной?", ["когда менеджер свяжется"]) == "когда менеджер свяжется")
    check("граница слова: 'кредит' НЕ ловится в 'дискредитировать'",
          triggers.match_stopwords("вы меня дискредитировали", ["кредит"]) is None)
    check("первое из нескольких", triggers.match_stopwords("нужен договор и оферта", ["оферта", "договор"]) in ("оферта", "договор"))
    check("нет совпадения → None", triggers.match_stopwords("обычный вопрос", ["отмена"]) is None)
    check("пустые слова игнор", triggers.match_stopwords("текст", ["", "  "]) is None)

    # ── 2. format_trigger_card (чистая логика) ──
    print("2. format_trigger_card:")
    card = triggers.format_trigger_card(
        {"type": "stopwords"}, tg_user_id=TG, reason="стоп-слово «оферта»",
        snippet="дайте\nоферту пожалуйста", lead_id="lead-9", panel_base="https://panel.example")
    check("карточка: тип по-русски", "стоп-слово в сообщении" in card)
    check("карточка: причина", "стоп-слово «оферта»" in card)
    check("карточка: сниппет нормализован (без \\n)", "дайте оферту пожалуйста" in card and "\nдайте" not in card)
    check("карточка: ссылка на диалог", "https://panel.example/dialogs/lead-9" in card)
    check("карточка: tg-ссылка", f"tg://user?id={TG}" in card)
    check("карточка plain (без тегов)", "<" not in card)

    # ── 2b. intent: parse_trigger_markers / build_intent_addendum (чистая логика) ──
    print("2b. intent (маркеры + аддендум):")
    c0, i0 = triggers.parse_trigger_markers("обычный ответ без меток")
    check("нет метки → (текст, [])", c0 == "обычный ответ без меток" and i0 == [])
    c1, i1 = triggers.parse_trigger_markers("Спасибо! Передам менеджеру.[[TRIGGER:1]]")
    check("метка вырезана, индекс [1]", "[[TRIGGER" not in c1 and c1 == "Спасибо! Передам менеджеру." and i1 == [1], repr((c1, i1)))
    c2, i2 = triggers.parse_trigger_markers("ответ [[TRIGGER:1]][[trigger:3]] хвост")
    check("несколько меток (рег.незав.) → [1,3]", "trigger" not in c2.lower() and i2 == [1, 3], repr((c2, i2)))
    c3, i3 = triggers.parse_trigger_markers("x [[TRIGGER:2]][[TRIGGER:2]]")
    check("дубликаты схлопнуты → [2]", i3 == [2], repr(i3))
    c4, i4 = triggers.parse_trigger_markers("Готово.[[TRIGGER:1")  # усечённая метка (нет ]])
    check("усечённая метка вырезана (без утечки), индекс не сработал", "[[TRIGGER" not in c4 and c4 == "Готово." and i4 == [], repr((c4, i4)))

    add = triggers.build_intent_addendum([
        {"intent_desc": "просит оплату", "action": "notify_reply_continue", "reply_text": "вот ссылка"},
        {"intent_desc": "негатив", "action": "notify_only", "reply_text": "не показывать"},
    ])
    check("аддендум: инструкция про [[TRIGGER:N]]", "[[TRIGGER:N]]" in add)
    check("аддендум: условие 1 пронумеровано", "1. просит оплату" in add)
    check("аддендум: подсказка ответа для reply-действия", "вот ссылка" in add)
    check("аддендум: notify_only без подсказки ответа", "не показывать" not in add)

    # ── 3. БД: get_active_triggers / count / pause (risuy_dev) ──
    print("3. БД движка (risuy_dev):")
    db.pool = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    ta = tb = None
    try:
        async with db.pool.acquire() as c:
            await c.execute("delete from leads where tg_user_id = $1", TG)
            ta = await c.fetchval("insert into tenants(slug,name,status) values('smoke-trig-a','A','active') returning id")
            tb = await c.fetchval("insert into tenants(slug,name,status) values('smoke-trig-b','B','active') returning id")
            await c.execute(
                "insert into leads(tg_user_id,messenger,source,status,name,tenant_id) "
                "values($1,'tg','x','new','Тест',$2)", TG, ta)
            # триггеры тенанта A: 2 активных (stopwords, message_count) + 1 disabled + 1 тип documents
            await c.execute(
                "insert into tenant_triggers(tenant_id,type,action,stopwords,reply_text,notify_chat_id,enabled,position) "
                "values($1,'stopwords','notify_reply_continue',$2,'ответ','-100777',true,1)",
                ta, ["отмена", "перенести"])
            await c.execute(
                "insert into tenant_triggers(tenant_id,type,action,msg_count,notify_chat_id,enabled,position) "
                "values($1,'message_count','notify_only',5,'-100777',true,2)", ta)
            await c.execute(
                "insert into tenant_triggers(tenant_id,type,action,stopwords,enabled,position) "
                "values($1,'stopwords','notify_only',$2,false,3)", ta, ["выключен"])
            await c.execute(
                "insert into tenant_triggers(tenant_id,type,action,reply_text,notify_chat_id,enabled,position) "
                "values($1,'documents','notify_reply_continue','принято','-100777',true,4)", ta)
            # триггер ЧУЖОГО тенанта B (изоляция)
            await c.execute(
                "insert into tenant_triggers(tenant_id,type,action,stopwords,enabled) "
                "values($1,'stopwords','notify_only',$2,true)", tb, ["чужой"])

        db.current_tenant_id.set(ta)
        all_a = await db.get_active_triggers(ta)
        check("активных триггеров A = 3 (disabled исключён)", len(all_a) == 3, f"факт {len(all_a)}")
        text_a = await db.get_active_triggers(ta, types=("stopwords", "message_count"))
        check("фильтр по типам (stopwords+count) = 2", len(text_a) == 2, f"факт {len(text_a)}")
        docs_a = await db.get_active_triggers(ta, types=("documents",))
        check("фильтр documents = 1", len(docs_a) == 1)
        check("порядок по position (stopwords первым)", text_a[0]["type"] == "stopwords")
        check("stopwords массив прочитан", list(text_a[0]["stopwords"]) == ["отмена", "перенести"])
        # tenant-изоляция (owner фильтрует tenant_id явно): A не видит триггер B
        b_from_a_ctx = [t for t in all_a if "чужой" in (list(t.get("stopwords") or []))]
        check("триггер тенанта B НЕ попал в выборку A", b_from_a_ctx == [])
        check("get_active_triggers(None) → []", (await db.get_active_triggers(None)) == [])

        # count_inbound_messages
        n0 = await db.count_inbound_messages(TG)
        async with db.pool.acquire() as c:
            await c.execute(
                "insert into messages(lead_id,tg_user_id,direction,kind,text,tenant_id) "
                "select id,$1,'in','text','m',$2 from leads where tg_user_id=$1 and tenant_id=$2", TG, ta)
        n1 = await db.count_inbound_messages(TG)
        check("count_inbound_messages растёт", n1 == n0 + 1, f"{n0}→{n1}")

        # pause_lead
        await db.pause_lead(TG)
        check("pause_lead → is_bot_paused True", (await db.is_bot_paused(TG)) is True)
    finally:
        async with db.pool.acquire() as c:
            await c.execute("delete from messages where tg_user_id = $1", TG)
            await c.execute("delete from leads where tg_user_id = $1", TG)
            for t in (ta, tb):
                if t:
                    await c.execute("delete from tenants where id = $1", t)  # cascade чистит tenant_triggers
        await db.pool.close()

    print()
    if FAILS:
        print(f"❌ ПРОВАЛЫ ({len(FAILS)}): " + ", ".join(FAILS)); sys.exit(1)
    print("✅ triggers smoke — все проверки зелёные")


if __name__ == "__main__":
    asyncio.run(main())
