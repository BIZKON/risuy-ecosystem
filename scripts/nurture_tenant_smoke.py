#!/usr/bin/env python3
"""Смоук tenant-aware дожима (item B) на risuy_dev. Проверяет:
  • get_tenant_nurture — парсинг конфига (enabled/steps, битый JSON, выключено);
  • get_due_tenant_followups — ВАЛИДНОСТЬ SQL + логику якоря/сброса/стоп-условий на синтетике;
  • mark_tenant_followup_sent.
Создаёт временный лид+сообщения под демо-тенантом с КОНТРОЛИРУЕМЫМИ временами, проверяет, чистит за собой.

Запуск:  NURTURE_SMOKE_DSN="postgresql://gen_user:<pw>@.../risuy_dev?sslmode=require" \
         PYTHONPATH=bot-telegram BOT_TOKEN=x DATABASE_URL="$NURTURE_SMOKE_DSN" CHANNEL_ID=-100 \
         CHANNEL_URL=https://t.me/x GUIDE_URL=https://x ./.venv-smoke/bin/python scripts/nurture_tenant_smoke.py
"""
import asyncio
import json
import os

import db

DSN = os.environ.get("NURTURE_SMOKE_DSN") or os.environ.get("DATABASE_URL")
if not DSN or "risuy_dev" not in DSN:
    raise SystemExit("Задайте NURTURE_SMOKE_DSN на risuy_dev (защита от прода).")

TG = 9123456780      # временный tg_user_id лида-пустышки
VK_UID = 9123450555  # временный vk_user_id лида-пустышки
MAX_UID = 9123450777   # MAX-лид С chat_id (достижим дожимом)
MAX_CHAT = 9123450888  # max_chat_id (адрес ответа MAX ≠ user_id)
MAX_UID2 = 9123450999  # MAX-лид БЕЗ chat_id (недостижим → исключается из дожима)


async def _cleanup(c, tid):
    # лиды-пустышки всех каналов + их сообщения (vk/max: tg_user_id=NULL → чистим по lead_id) + конфиг
    await c.execute(
        "delete from messages where tenant_id=$1 and lead_id in "
        "(select id from leads where tenant_id=$1 and "
        " (tg_user_id=$2 or vk_user_id=$3 or max_user_id = any($4::bigint[])))",
        tid, TG, VK_UID, [MAX_UID, MAX_UID2])
    await c.execute(
        "delete from leads where tenant_id=$1 and "
        "(tg_user_id=$2 or vk_user_id=$3 or max_user_id = any($4::bigint[]))",
        tid, TG, VK_UID, [MAX_UID, MAX_UID2])
    for k in ("nurture_enabled", "nurture_steps"):
        await c.execute("delete from tenant_settings where tenant_id=$1 and key=$2", tid, k)


async def main():
    await db.init()
    ok = True
    async with db.pool.acquire() as c:
        tid = await c.fetchval("select id from tenants where slug='demo-sandbox'")
        if tid is None:
            tid = await c.fetchval("select id from tenants where slug='lesov-school'")
        assert tid is not None, "нет тенанта для теста"
        await _cleanup(c, tid)
        try:
            # ── 1) Парсинг конфига ───────────────────────────────────────────────
            async def setcfg(enabled, steps_json):
                await _cleanup(c, tid)
                if enabled is not None:
                    await c.execute("insert into tenant_settings(tenant_id,key,value) values($1,'nurture_enabled',$2)", tid, enabled)
                if steps_json is not None:
                    await c.execute("insert into tenant_settings(tenant_id,key,value) values($1,'nurture_steps',$2)", tid, steps_json)

            await setcfg("1", json.dumps([{"delay_seconds": 7200, "text": "к1"}, {"delay_seconds": 86400, "text": "к2"}]))
            cfg = await db.get_tenant_nurture(tid)
            assert cfg["enabled"] and len(cfg["steps"]) == 2 and cfg["steps"][0]["text"] == "к1", cfg
            print("✅ парсинг: 2 валидных шага")

            await setcfg("", json.dumps([{"delay_seconds": 7200, "text": "к1"}]))
            assert (await db.get_tenant_nurture(tid))["enabled"] is False, "выключено должно быть disabled"
            print("✅ парсинг: nurture_enabled='' → disabled")

            await setcfg("1", "не json")
            assert (await db.get_tenant_nurture(tid))["enabled"] is False, "битый JSON → disabled"
            print("✅ парсинг: битый JSON → disabled")

            await setcfg("1", json.dumps([{"delay_seconds": 0, "text": "пусто-задержка"}, {"delay_seconds": 100, "text": ""}]))
            assert (await db.get_tenant_nurture(tid))["steps"] == [], "невалидные шаги отфильтрованы"
            print("✅ парсинг: шаги с delay<=0 / пустым текстом отброшены")

            # ── 2) get_due_tenant_followups: SQL + логика якоря/сброса/стопов ─────
            lead_id = await c.fetchval(
                "insert into leads(tenant_id,messenger,tg_user_id,source,consent,status) "
                "values($1,'tg',$2,'demo',true,'new') returning id", tid, TG)

            async def add_in(ago_sql):  # входящее сообщение N времени назад
                await c.execute(
                    "insert into messages(lead_id,tg_user_id,messenger,direction,kind,text,source,tenant_id,created_at) "
                    f"values($1,$2,'tg','in','text','q','demo',$3, now() - interval '{ago_sql}')",
                    lead_id, TG, tid)

            async def due(col, delay, prev=None, messenger="tg"):
                rows = await db.get_due_tenant_followups(tid, col, delay, prev_col=prev, messenger=messenger)
                return [r["addr"] for r in rows]  # адрес ответа канала (tg→tg_user_id, vk→vk_user_id, max→max_chat_id)

            COL = "follow_up_1_at"
            # последний входящий 4ч назад, касание не слали, delay 2ч → ДОЛЖЕН быть due
            await add_in("4 hours")
            assert TG in await due(COL, 7200), "должен быть due (молчит 4ч > 2ч)"
            print("✅ due: лид молчит дольше задержки → касание положено")

            # пометили отправленным → НЕ due (касание свежее последнего входящего)
            await db.mark_tenant_followup_sent(tid, COL, lead_id)
            assert TG not in await due(COL, 7200), "после отправки касания не должен повторяться"
            print("✅ стоп-повтор: касание отправлено для этой активности → больше не due")

            # лид ОТВЕТИЛ ПОСЛЕ касания → серия сброшена → снова due. Касание ставим 3ч назад,
            # ответ — 1ч назад (позже касания), delay 30мин → касание «протухло» относит. ответа.
            await c.execute("update leads set follow_up_1_at = now() - interval '3 hours' where id=$1", lead_id)
            await add_in("1 hour")
            assert TG in await due(COL, 1800), "после ответа (позже касания) серия должна перезапуститься"
            print("✅ сброс: ответ ПОСЛЕ касания → касание снова положено")

            # стоп-условия: отписка / пауза / эскалация / конверсия — каждое глушит
            for sql, label in (
                ("update leads set unsubscribed_at=now() where id=$1", "отписка"),
                ("update leads set unsubscribed_at=null, bot_paused=true where id=$1", "ручная пауза"),
                ("update leads set bot_paused=false, escalated_at=now() where id=$1", "эскалация"),
                ("update leads set escalated_at=null, status='converted' where id=$1", "конверсия"),
            ):
                await c.execute(sql, lead_id)
                assert TG not in await due(COL, 1800), f"{label} должна глушить дожим"
            print("✅ стопы: отписка / пауза / эскалация / конверсия — глушат")

            # ── 3) ЦЕПОЧКА ШАГОВ: анти-залп (ревью) ──────────────────────────────
            # Лид молчит ДОЛЬШЕ ВСЕХ задержек (накопленная база) — НЕ должны уйти все 3 разом.
            await c.execute("delete from messages where tg_user_id=$1 and tenant_id=$2", TG, tid)
            await c.execute(
                "update leads set follow_up_1_at=null, follow_up_2_at=null, follow_up_3_at=null, "
                "unsubscribed_at=null, bot_paused=false, escalated_at=null, status='new' where id=$1", lead_id)
            await add_in("5 days")
            C1, C2, C3 = "follow_up_1_at", "follow_up_2_at", "follow_up_3_at"
            assert TG in await due(C1, 7200, prev=None), "шаг1 должен быть due"
            assert TG not in await due(C2, 86400, prev=C1), "шаг2 НЕ due пока не отправлен шаг1"
            assert TG not in await due(C3, 259200, prev=C2), "шаг3 НЕ due пока не отправлен шаг2"
            print("✅ анти-залп: молчит 5 дней — due только шаг1 (не все 3 разом)")
            # шаг1 отправлен 2 дня назад → шаг2 (пауза 1д от шага1) теперь due, шаг3 ещё ждёт
            await c.execute("update leads set follow_up_1_at = now() - interval '2 days' where id=$1", lead_id)
            assert TG in await due(C2, 86400, prev=C1), "шаг2 due через 1д после шага1"
            assert TG not in await due(C3, 259200, prev=C2), "шаг3 ждёт отправки шага2"
            print("✅ цепочка: шаг2 due только ПОСЛЕ шага1 (кумулятивная пауза, порядок гарантирован)")

            # ── 4) VK/MAX: дожим по каналам (ключ lead_id, адрес = vk_user_id / max_chat_id) ─────
            # VK-лид молчит 4ч, delay 2ч → due; адрес для отправки = vk_user_id
            vk_lead = await c.fetchval(
                "insert into leads(tenant_id,messenger,vk_user_id,source,consent,status) "
                "values($1,'vk',$2,'demo',true,'new') returning id", tid, VK_UID)
            await c.execute(
                "insert into messages(lead_id,messenger,direction,kind,text,source,tenant_id,created_at) "
                "values($1,'vk','in','text','q','demo',$2, now() - interval '4 hours')", vk_lead, tid)
            assert VK_UID in await due(C1, 7200, messenger="vk"), "VK-лид должен быть due (адрес=vk_user_id)"
            await db.mark_tenant_followup_sent(tid, C1, vk_lead)
            assert VK_UID not in await due(C1, 7200, messenger="vk"), "после касания VK не повторяется"
            print("✅ VK: лид due по vk_user_id; mark по lead_id → не повторяется")

            # MAX-лид С chat_id → due, адрес ответа = max_chat_id (≠ max_user_id в личке)
            max_lead = await c.fetchval(
                "insert into leads(tenant_id,messenger,max_user_id,max_chat_id,source,consent,status) "
                "values($1,'max',$2,$3,'demo',true,'new') returning id", tid, MAX_UID, MAX_CHAT)
            await c.execute(
                "insert into messages(lead_id,messenger,direction,kind,text,source,tenant_id,created_at) "
                "values($1,'max','in','text','q','demo',$2, now() - interval '4 hours')", max_lead, tid)
            # MAX-лид БЕЗ chat_id (не писал в личку) → должен быть ИСКЛЮЧЁН (нет адреса ответа)
            max_lead2 = await c.fetchval(
                "insert into leads(tenant_id,messenger,max_user_id,source,consent,status) "
                "values($1,'max',$2,'demo',true,'new') returning id", tid, MAX_UID2)
            await c.execute(
                "insert into messages(lead_id,messenger,direction,kind,text,source,tenant_id,created_at) "
                "values($1,'max','in','text','q','demo',$2, now() - interval '4 hours')", max_lead2, tid)
            max_due = await due(C1, 7200, messenger="max")
            assert MAX_CHAT in max_due, "MAX-лид с chat_id due (адрес=max_chat_id)"
            assert None not in max_due, "MAX-лид без chat_id исключён (адрес ответа NULL)"
            print("✅ MAX: due по max_chat_id (≠ user_id); лид без chat_id исключён")
        except AssertionError as e:
            ok = False
            print("❌", e)
        finally:
            await _cleanup(c, tid)
    await db.close()
    print("\n" + ("✅ ВСЕ ПРОВЕРКИ ЗЕЛЁНЫЕ" if ok else "❌ ЕСТЬ ПАДЕНИЯ"))
    if not ok:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
