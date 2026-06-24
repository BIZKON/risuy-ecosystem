#!/usr/bin/env python3
"""Смоук персистенции веб-чата (Демо-монитор) на risuy_dev: веб-лид по session_id ложится как
лид demo-тенанта (messenger='web'), переписка пишется (tg_user_id=NULL), upsert идемпотентен.

Запуск:  WEB_SMOKE_DSN="postgresql://gen_user:<pw>@.../risuy_dev?sslmode=require" \
         PYTHONPATH=bot-telegram BOT_TOKEN=x DATABASE_URL="$WEB_SMOKE_DSN" CHANNEL_ID=-100 \
         CHANNEL_URL=https://t.me/x GUIDE_URL=https://x ./.venv-smoke/bin/python scripts/web_chat_persist_smoke.py
"""
import asyncio
import os

import db

DSN = os.environ.get("WEB_SMOKE_DSN") or os.environ.get("DATABASE_URL")
if not DSN or "risuy_dev" not in DSN:
    raise SystemExit("Задайте WEB_SMOKE_DSN на risuy_dev (защита от прода).")

SID = "web-smoke-test-0001"


async def main():
    await db.init()
    ok = True
    async with db.pool.acquire() as c:
        tid = (await c.fetchval("select id from tenants where slug='demo-sandbox'")
               or await c.fetchval("select id from tenants where slug='lesov-school'"))
        assert tid is not None, "нет тенанта"

        async def clean():
            await c.execute("delete from messages where lead_id in "
                            "(select id from leads where web_session_id=$1 and tenant_id=$2)", SID, tid)
            await c.execute("delete from leads where web_session_id=$1 and tenant_id=$2", SID, tid)

        await clean()
        tok = db.current_tenant_id.set(tid)
        try:
            await db.upsert_start(SID, "web", messenger="web")
            await db.upsert_start(SID, "web", messenger="web")  # повтор → тот же лид
            lid = await db.get_lead_id(SID, messenger="web")
            assert lid, "веб-лид не создан/не найден"
            cnt = await c.fetchval("select count(*) from leads where web_session_id=$1 and tenant_id=$2", SID, tid)
            assert cnt == 1, f"ожидался 1 лид на session_id, получено {cnt}"
            print("✅ upsert веб-лида идемпотентен (1 лид на session_id)")

            await db.log_message(lead_id=lid, tg_user_id=0, messenger="web", direction="in", text="вопрос с сайта")
            await db.log_message(lead_id=lid, tg_user_id=0, messenger="web", direction="out",
                                 text="ответ Лии", source="liya")
            rows = await c.fetch("select direction, messenger, tg_user_id, text from messages "
                                 "where lead_id=$1 order by created_at", lid)
            assert len(rows) == 2, f"ожидалось 2 сообщения, получено {len(rows)}"
            assert all(r["messenger"] == "web" and r["tg_user_id"] is None for r in rows), \
                "messenger должен быть 'web', tg_user_id — NULL"
            assert [r["direction"] for r in rows] == ["in", "out"], "порядок in→out"
            print("✅ переписка записана: messenger='web', tg_user_id=NULL, in+out")

            lead = await c.fetchrow("select messenger, source, status from leads where id=$1", lid)
            assert lead["messenger"] == "web" and lead["source"] == "web", dict(lead)
            print(f"✅ лид: messenger={lead['messenger']}, source={lead['source']}, status={lead['status']}")

            # get_due_tenant_followups НЕ должен цеплять веб-лида (дожим — только messenger='tg')
            due = await db.get_due_tenant_followups(tid, "follow_up_1_at", 1)
            assert SID not in [str(x) for x in due], "веб-лид не должен попадать в TG-дожим"
            print("✅ дожим (TG-only) не трогает веб-лида")
        except AssertionError as e:
            ok = False
            print("❌", e)
        finally:
            db.current_tenant_id.reset(tok)
            await clean()
    await db.close()
    print("\n" + ("✅ ВСЕ ПРОВЕРКИ ЗЕЛЁНЫЕ" if ok else "❌ ЕСТЬ ПАДЕНИЯ"))
    if not ok:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
