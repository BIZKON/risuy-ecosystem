#!/usr/bin/env python3
"""Смоук Слоя C C0: обобщение идентичности лида по каналам (bot-telegram/db.py) на risuy_dev.

Проверяет, что per-lead функции работают для tg (как было) И для vk/max (новые колонки):
  1. upsert_start/get_lead_id: tg → tg_user_id, vk → vk_user_id (разные лиды);
  2. ОДИН И ТОТ ЖЕ числовой id в tg и vk → ДВА РАЗНЫХ лида (гибрид-модель, нет коллизии);
  3. log_message: vk-сообщение пишется (tg_user_id=NULL, messenger='vk', lead_id связан) — блокер №1 снят;
  4. get_ai_history/count_inbound_messages по каналу (vk — через lead_id);
  5. claim/release_lead_escalation, pause_lead/is_bot_paused — по колонке канала;
  6. кросс-канал: get_lead_id(vk_id, messenger='tg') → None (id в чужой колонке не виден).

Требует применённой migrate_layer_c_identity.sql (vk_user_id, messages.messenger). На прод НЕ запускать.

Запуск: C0_SMOKE_DSN="postgresql://<owner>@<host>:5432/risuy_dev?sslmode=require" \
        PYTHONPATH=. ./.venv-smoke/bin/python scripts/c0_identity_smoke.py
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

import asyncpg  # noqa: E402
import db        # noqa: E402  (bot-telegram/db.py)

DSN = os.environ.get("C0_SMOKE_DSN")
if not DSN or "/risuy_dev" not in DSN.split("?")[0]:
    raise SystemExit("Задайте C0_SMOKE_DSN на risuy_dev.")

FAILS: list[str] = []
UID = 990777088          # один и тот же числовой id в tg И vk → должны быть РАЗНЫЕ лиды


def check(name, cond, detail=""):
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail != "" else ""))
    if not cond:
        FAILS.append(name)


async def main() -> None:
    db.pool = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    tid = None
    try:
        async with db.pool.acquire() as c:
            await c.execute("delete from leads where tg_user_id = $1 or vk_user_id = $1", UID)
            tid = await c.fetchval("insert into tenants(slug,name,status) values('smoke-c0','C0','active') returning id")
        db.current_tenant_id.set(tid)

        # ── 1. upsert_start tg + vk (разные колонки) ──
        print("1. upsert_start tg + vk:")
        await db.upsert_start(UID, "reels", messenger="tg")
        await db.upsert_start(UID, "vk_group", messenger="vk")
        tg_lead = await db.get_lead_id(UID, messenger="tg")
        vk_lead = await db.get_lead_id(UID, messenger="vk")
        check("tg-лид создан (get_lead_id tg)", tg_lead is not None)
        check("vk-лид создан (get_lead_id vk)", vk_lead is not None)

        # ── 2. ГИБРИД: один числовой id в tg и vk = ДВА РАЗНЫХ лида ──
        print("2. гибрид — один id в двух каналах = два лида:")
        check("tg-лид ≠ vk-лид (нет коллизии id между каналами)", tg_lead != vk_lead, f"{tg_lead} vs {vk_lead}")
        async with db.pool.acquire() as c:
            n = await c.fetchval("select count(*) from leads where tenant_id=$1 and (tg_user_id=$2 or vk_user_id=$2)", tid, UID)
            tg_col = await c.fetchval("select tg_user_id from leads where id=$1", tg_lead)
            vk_tgcol = await c.fetchval("select tg_user_id from leads where id=$1", vk_lead)
            vk_vkcol = await c.fetchval("select vk_user_id from leads where id=$1", vk_lead)
        check("в БД два отдельных лида", int(n) == 2, f"факт {n}")
        check("у vk-лида tg_user_id=NULL, vk_user_id=UID", vk_tgcol is None and vk_vkcol == UID)
        check("у tg-лида tg_user_id=UID", tg_col == UID)

        # ── 3. log_message vk пишется (блокер №1 снят) ──
        print("3. log_message vk (tg_user_id=NULL, messenger='vk'):")
        await db.log_message(tg_user_id=UID, messenger="vk", direction="in", text="привет из вк", tg_message_id=111)
        await db.log_message(tg_user_id=UID, messenger="tg", direction="in", text="привет из тг", tg_message_id=222)
        async with db.pool.acquire() as c:
            vkmsg = await c.fetchrow("select tg_user_id, messenger, lead_id from messages where text='привет из вк'")
        check("vk-сообщение записано (раньше падало на NOT NULL)", vkmsg is not None)
        check("vk-сообщение: tg_user_id=NULL, messenger='vk', lead_id=vk-лид",
              vkmsg and vkmsg["tg_user_id"] is None and vkmsg["messenger"] == "vk" and str(vkmsg["lead_id"]) == vk_lead)

        # ── 4. count / history по каналу ──
        print("4. count_inbound / get_ai_history по каналу:")
        check("count vk = 1", (await db.count_inbound_messages(UID, messenger="vk")) == 1)
        check("count tg = 1", (await db.count_inbound_messages(UID, messenger="tg")) == 1)
        h_vk = await db.get_ai_history(UID, messenger="vk")
        h_tg = await db.get_ai_history(UID, messenger="tg")
        check("history vk видит ТОЛЬКО вк-сообщение", len(h_vk) == 1 and h_vk[0]["content"] == "привет из вк", repr(h_vk))
        check("history tg видит ТОЛЬКО тг-сообщение", len(h_tg) == 1 and h_tg[0]["content"] == "привет из тг", repr(h_tg))

        # ── 5. escalation claim / pause по каналу ──
        print("5. claim/pause по каналу:")
        check("claim vk = True", (await db.claim_lead_escalation(UID, messenger="vk")) is True)
        check("claim vk повторно = False (дедуп)", (await db.claim_lead_escalation(UID, messenger="vk")) is False)
        check("claim tg = True (другой лид, независимо)", (await db.claim_lead_escalation(UID, messenger="tg")) is True)
        await db.pause_lead(UID, messenger="vk")
        check("pause vk → is_bot_paused vk True", (await db.is_bot_paused(UID, messenger="vk")) is True)
        check("tg-лид НЕ на паузе (изоляция каналов)", (await db.is_bot_paused(UID, messenger="tg")) is False)

        # ── 6. кросс-канал ──
        print("6. кросс-канал:")
        check("get_lead_id(UID, messenger='max') → None (нет max-лида)", (await db.get_lead_id(UID, messenger="max")) is None)

    finally:
        async with db.pool.acquire() as c:
            await c.execute("delete from messages where tg_user_id = $1 or lead_id in (select id from leads where tenant_id=$2)", UID, tid)
            await c.execute("delete from leads where tenant_id = $1", tid)
            if tid:
                await c.execute("delete from tenants where id = $1", tid)
        await db.pool.close()

    print()
    if FAILS:
        print(f"❌ ПРОВАЛЫ ({len(FAILS)}): " + ", ".join(FAILS)); sys.exit(1)
    print("✅ C0 identity smoke — все проверки зелёные")


if __name__ == "__main__":
    asyncio.run(main())
