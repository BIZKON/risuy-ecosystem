#!/usr/bin/env python3
"""Смоук Слоя C3: канал-агностичная ИСХОДЯЩАЯ доставка (bot-telegram/db.py) на risuy_dev.

Проверяет бот-сторону доставки ответов оператора и рассылок в VK/MAX:
  1. _audience_where('tg') ПОБАЙТОВО == прежней константе (TG-инвариант); vk/max — адрес-колонка канала;
  2. claim_outbox (TG) НЕ берёт vk/max-строки (messenger<>'tg' изолирован);
  3. claim_outbox_channels резолвит адрес ответа: vk → vk_user_id, max → max_chat_id (из leads);
  4. note_max_chat_id пишет адрес ответа MAX;
  5. materialize_recipients для vk-рассылки кладёт messenger='vk' + reply_address=vk_user_id;
  6. claim_broadcast_recipients возвращает reply_address/messenger.

Требует применённой migrate_c3_channel_outbound.sql. На прод НЕ запускать.

Запуск: C3_SMOKE_DSN="postgresql://<owner>@<host>:5432/risuy_dev?sslmode=require" \
        PYTHONPATH=. ./.venv-smoke/bin/python scripts/c3_channel_outbound_smoke.py
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

DSN = os.environ.get("C3_SMOKE_DSN")
if not DSN or "/risuy_dev" not in DSN.split("?")[0]:
    raise SystemExit("Задайте C3_SMOKE_DSN на risuy_dev.")

FAILS: list[str] = []
VK_UID = 880655111       # vk_user_id (= адрес ответа VK)
MAX_UID = 880655222      # max_user_id (идентичность MAX)
MAX_CHAT = 770655999     # max_chat_id (адрес ответа MAX, ≠ user_id)


def check(name, cond, detail=""):
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail != "" else ""))
    if not cond:
        FAILS.append(name)


async def main() -> None:
    db.pool = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    tid = None
    try:
        # ── 0. TG-инвариант аудитории (чистая проверка, без БД) ──
        print("0. _audience_where:")
        expected_tg = ("messenger = 'tg' and tg_user_id is not null and consent = true "
                       "and unsubscribed_at is null and erase_requested_at is null and bot_paused = false")
        check("tg байт-в-байт прежняя строка", db._audience_where("tg") == expected_tg, db._audience_where("tg"))
        check("vk → vk_user_id is not null", "vk_user_id is not null" in db._audience_where("vk"))
        check("max → max_chat_id is not null (адрес ответа)", "max_chat_id is not null" in db._audience_where("max"))

        async with db.pool.acquire() as c:
            await c.execute("delete from leads where vk_user_id = $1 or max_user_id = $2", VK_UID, MAX_UID)
            tid = await c.fetchval(
                "insert into tenants(slug,name,status) values('smoke-c3','C3','active') returning id")
        db.current_tenant_id.set(tid)

        # ── лиды vk/max с согласием ──
        await db.upsert_start(VK_UID, "vk_group", messenger="vk")
        await db.upsert_start(MAX_UID, "max", messenger="max")
        vk_lead = await db.get_lead_id(VK_UID, messenger="vk")
        max_lead = await db.get_lead_id(MAX_UID, messenger="max")
        async with db.pool.acquire() as c:
            await c.execute("update leads set consent = true where id = any($1::uuid[])", [vk_lead, max_lead])

        # ── 4. note_max_chat_id ──
        print("4. note_max_chat_id:")
        await db.note_max_chat_id(MAX_UID, MAX_CHAT)
        async with db.pool.acquire() as c:
            got = await c.fetchval("select max_chat_id from leads where id = $1", max_lead)
        check("max_chat_id записан", got == MAX_CHAT, str(got))

        # ── outbox vk/max + изоляция от TG-claim ──
        print("2-3. outbox каналов:")
        async with db.pool.acquire() as c:
            await c.execute(
                "insert into outbox (lead_id, messenger, kind, text, status, created_by, tenant_id) "
                "values ($1,'vk','text','привет vk','queued','smoke',$2),"
                "       ($3,'max','text','привет max','queued','smoke',$2)",
                vk_lead, tid, max_lead)
        tg_claim = await db.claim_outbox(50)
        check("claim_outbox (TG) НЕ берёт vk/max", all(r.get("messenger", "tg") == "tg" for r in tg_claim)
              and not any(r["lead_id"] in (vk_lead, max_lead) for r in tg_claim))
        ch = await db.claim_outbox_channels(50)
        by_lead = {str(r["lead_id"]): r for r in ch}  # ключи → str (get_lead_id отдаёт str, claim — UUID)
        check("vk-строка: reply_address = vk_user_id", by_lead.get(vk_lead, {}).get("reply_address") == VK_UID,
              str(by_lead.get(vk_lead, {}).get("reply_address")))
        check("max-строка: reply_address = max_chat_id", by_lead.get(max_lead, {}).get("reply_address") == MAX_CHAT,
              str(by_lead.get(max_lead, {}).get("reply_address")))

        # ── 5-6. рассылка vk: материализация + claim ──
        print("5-6. рассылка vk:")
        async with db.pool.acquire() as c:
            bid = await c.fetchval(
                "insert into broadcasts (title, messenger, kind, body_template, status, "
                "created_by, tenant_id) "
                "values ('c3','vk','text','тест','draft','smoke',$1) returning id", tid)
        n = await db.materialize_recipients(bid)
        check("материализован ≥1 получатель", n >= 1, str(n))
        async with db.pool.acquire() as c:
            row = await c.fetchrow(
                "select messenger, reply_address, tg_user_id from broadcast_recipients "
                "where broadcast_id = $1 and lead_id = $2", bid, vk_lead)
        check("получатель vk: messenger='vk'", row and row["messenger"] == "vk")
        check("получатель vk: reply_address=vk_user_id", row and row["reply_address"] == VK_UID, str(row and row["reply_address"]))
        check("получатель vk: tg_user_id NULL", row and row["tg_user_id"] is None)
        claimed = await db.claim_broadcast_recipients(bid, 10)
        check("claim_broadcast_recipients отдаёт reply_address", claimed and claimed[0]["reply_address"] == VK_UID)

        print()
        if FAILS:
            print(f"❌ ПРОВАЛЫ ({len(FAILS)}): " + ", ".join(FAILS)); sys.exit(1)
        print("✅ c3_channel_outbound smoke — все проверки зелёные")
    finally:
        if tid is not None:
            async with db.pool.acquire() as c:
                await c.execute("delete from leads where vk_user_id = $1 or max_user_id = $2", VK_UID, MAX_UID)
                await c.execute("delete from broadcasts where tenant_id = $1", tid)
                await c.execute("delete from tenants where id = $1", tid)
        await db.pool.close()


if __name__ == "__main__":
    asyncio.run(main())
