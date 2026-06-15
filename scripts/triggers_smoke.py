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
        {"type": "stopwords"}, external_id=TG, reason="стоп-слово «оферта»",
        snippet="дайте\nоферту пожалуйста", lead_id="lead-9", panel_base="https://panel.example")
    check("карточка: тип по-русски", "стоп-слово в сообщении" in card)
    check("карточка: причина", "стоп-слово «оферта»" in card)
    check("карточка: сниппет нормализован (без \\n)", "дайте оферту пожалуйста" in card and "\nдайте" not in card)
    check("карточка: ссылка на диалог", "https://panel.example/dialogs/lead-9" in card)
    check("карточка: tg-ссылка по умолчанию (client_link=None)", f"tg://user?id={TG}" in card)
    check("карточка plain (без тегов)", "<" not in card)
    # Слой C: канальная ссылка на клиента (client_link) вместо tg:// по умолчанию.
    card_vk = triggers.format_trigger_card(
        {"type": "stopwords"}, external_id=778899, reason="r", snippet="s", lead_id=None,
        panel_base=None, client_link=("https://vk.com/id778899", "Написать клиенту в ВКонтакте"))
    check("VK-карточка: ссылка vk.com/id (НЕ tg://)", "https://vk.com/id778899" in card_vk and "tg://" not in card_vk, repr(card_vk))
    card_max = triggers.format_trigger_card(
        {"type": "stopwords"}, external_id=910921, reason="r", snippet="s", lead_id=None,
        panel_base=None, client_link=("", "Клиент в MAX (id 910921) — ответьте через панель"))
    check("MAX-карточка: подпись про MAX, без url и без tg://", "Клиент в MAX" in card_max and "tg://" not in card_max, repr(card_max))

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
    ta = tb = tc = None
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

        # ── 3b. Слой C: канал-агностичность (vk/max идентичность + handle_text через ctx) ──
        print("3b. Слой C — каналы (risuy_dev):")
        VK_ID, MAX_ID = 555000111, 777000222
        async with db.pool.acquire() as c:
            tc = await c.fetchval("insert into tenants(slug,name,status) values('smoke-trig-c','C','active') returning id")
            # vk/max лиды тенанта C — идентичность в своих колонках (db._user_col)
            await c.execute("insert into leads(vk_user_id,messenger,source,status,tenant_id) values($1,'vk','vk','new',$2)", VK_ID, tc)
            await c.execute("insert into leads(max_user_id,messenger,source,status,tenant_id) values($1,'max','max','new',$2)", MAX_ID, tc)
            # триггеры C с notify_chat_id=NULL → _notify выходит ДО import messaging (тест без aiogram)
            await c.execute(
                "insert into tenant_triggers(tenant_id,type,action,stopwords,reply_text,enabled,position) "
                "values($1,'stopwords','notify_reply_continue',$2,'Передаю менеджеру',true,1)", tc, ["отмена"])
            await c.execute(
                "insert into tenant_triggers(tenant_id,type,action,msg_count,enabled,position) "
                "values($1,'message_count','notify_reply_pause',1,true,2)", tc)
        db.current_tenant_id.set(tc)

        # handle_text end-to-end через ctx (VK): стоп-слово → canned-ответ клиенту + return True, БЕЗ aiogram
        captured: list[str] = []

        async def _reply(body: str) -> None:
            captured.append(body)

        ctx_vk = triggers.TriggerCtx(messenger="vk", external_id=VK_ID, text="прошу отмена брони",
                                     reply=_reply, notifier_fallback_bot=None)
        check("VK handle_text: стоп-слово → True", (await triggers.handle_text(ctx_vk)) is True)
        check("VK handle_text: canned-ответ ушёл клиенту через ctx.reply", captured == ["Передаю менеджеру"], repr(captured))

        # count_inbound_messages по каналу vk (messages.tg_user_id=NULL, матч по lead_id)
        cvk0 = await db.count_inbound_messages(VK_ID, messenger="vk")
        async with db.pool.acquire() as c:
            await c.execute(
                "insert into messages(lead_id,direction,kind,text,tenant_id,messenger) "
                "select id,'in','text','m',$2,'vk' from leads where vk_user_id=$1 and tenant_id=$2", VK_ID, tc)
        cvk1 = await db.count_inbound_messages(VK_ID, messenger="vk")
        check("count_inbound_messages(vk) растёт (по lead_id, tg_user_id NULL)", cvk1 == cvk0 + 1, f"{cvk0}→{cvk1}")

        # message_count на vk: текст без стоп-слова, count==1 → fire (action pause) → канал-пауза
        captured.clear()
        ctx_vk2 = triggers.TriggerCtx(messenger="vk", external_id=VK_ID, text="когда занятие?",
                                      reply=_reply, notifier_fallback_bot=None)
        check("VK handle_text: message_count==1 → True", (await triggers.handle_text(ctx_vk2)) is True)
        check("VK action=pause → is_bot_paused(vk) True", (await db.is_bot_paused(VK_ID, messenger="vk")) is True)
        check("VK message_count(pause) без reply_text: ответ клиенту НЕ слался", captured == [], repr(captured))

        # max идентичность: count + pause по каналу max
        async with db.pool.acquire() as c:
            await c.execute(
                "insert into messages(lead_id,direction,kind,text,tenant_id,messenger) "
                "select id,'in','text','m',$2,'max' from leads where max_user_id=$1 and tenant_id=$2", MAX_ID, tc)
        check("count_inbound_messages(max) == 1", (await db.count_inbound_messages(MAX_ID, messenger="max")) == 1)
        await db.pause_lead(MAX_ID, messenger="max")
        check("pause_lead(max) → is_bot_paused(max) True", (await db.is_bot_paused(MAX_ID, messenger="max")) is True)
        # изоляция колонок: пауза vk-лида не «прокрашивает» несуществующего tg-лида того же тенанта
        check("is_bot_paused(tg) у отсутствующего лида → False", (await db.is_bot_paused(999999999, messenger="tg")) is False)

        # handle_document(ctx) + fire_intent(ctx) end-to-end через ctx (notify_chat_id=NULL → без aiogram)
        VK_DOC, VK_INTENT = 555000333, 555000444
        async with db.pool.acquire() as c:
            await c.execute("insert into leads(vk_user_id,messenger,source,status,tenant_id) values($1,'vk','vk','new',$2)", VK_DOC, tc)
            await c.execute("insert into leads(vk_user_id,messenger,source,status,tenant_id) values($1,'vk','vk','new',$2)", VK_INTENT, tc)
            await c.execute(
                "insert into tenant_triggers(tenant_id,type,action,reply_text,enabled,position) "
                "values($1,'documents','notify_reply_continue','Документ принят',true,3)", tc)
            await c.execute(
                "insert into tenant_triggers(tenant_id,type,action,intent_desc,enabled,position) "
                "values($1,'intent','notify_reply_pause','клиент просит счёт',true,4)", tc)
        captured.clear()
        ctx_doc = triggers.TriggerCtx(messenger="vk", external_id=VK_DOC, text="вот документ",
                                      reply=_reply, notifier_fallback_bot=None)
        check("VK handle_document → True (documents-триггер)", (await triggers.handle_document(ctx_doc)) is True)
        check("VK handle_document: canned-ответ ушёл через ctx.reply", captured == ["Документ принят"], repr(captured))

        intent_trigs = await db.get_active_triggers(tc, types=("intent",))
        check("intent-триггеров у C = 1", len(intent_trigs) == 1, f"факт {len(intent_trigs)}")
        ctx_int = triggers.TriggerCtx(messenger="vk", external_id=VK_INTENT, text="а счёт пришлёте?",
                                      reply=_reply, notifier_fallback_bot=None)
        await triggers.fire_intent(ctx_int, intent_trigs, [1])   # индекс 1 → notify_reply_pause
        check("VK fire_intent(pause) → is_bot_paused(vk) True", (await db.is_bot_paused(VK_INTENT, messenger="vk")) is True)
        await triggers.fire_intent(ctx_int, intent_trigs, [99])  # невалидный индекс игнорируется (без падения)
        check("fire_intent: невалидный индекс не падает", True)
    finally:
        async with db.pool.acquire() as c:
            await c.execute("delete from leads where tg_user_id = $1", TG)  # на случай прежнего пол-рана
            for t in (ta, tb, tc):
                if t:
                    # удаляем по tenant_id: vk/max-сообщения имеют tg_user_id=NULL (по нему не вычистить)
                    await c.execute("delete from messages where tenant_id = $1", t)
                    await c.execute("delete from leads where tenant_id = $1", t)
                    await c.execute("delete from tenants where id = $1", t)  # cascade чистит tenant_triggers
        await db.pool.close()

    print()
    if FAILS:
        print(f"❌ ПРОВАЛЫ ({len(FAILS)}): " + ", ".join(FAILS)); sys.exit(1)
    print("✅ triggers smoke — все проверки зелёные")


if __name__ == "__main__":
    asyncio.run(main())
