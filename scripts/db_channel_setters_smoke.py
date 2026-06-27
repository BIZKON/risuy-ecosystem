#!/usr/bin/env python3
"""Smoke: funnel-сеттеры канал-агностичны (messenger через _user_col). TG-регрессия (tg_user_id)
+ VK/MAX (vk_user_id/max_user_id). risuy_dev, throwaway-тенант, чистка каскадом.

Запуск:
  FUNNEL_SMOKE_DSN="postgresql://gen_user:<pw>@HOST:5432/risuy_dev?sslmode=require" \
  PYTHONPATH=bot-telegram BOT_TOKEN=x DATABASE_URL="$FUNNEL_SMOKE_DSN" CHANNEL_ID=-100 \
  CHANNEL_URL=https://t.me/x GUIDE_URL=https://x ./.venv-smoke/bin/python scripts/db_channel_setters_smoke.py
"""
import asyncio, os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import db  # noqa: E402

DSN = os.environ.get("FUNNEL_SMOKE_DSN") or os.environ.get("DATABASE_URL")
if not DSN or "risuy_dev" not in DSN:
    raise SystemExit("Задайте DSN на risuy_dev (защита от прода).")
SLUG = "smoke-chan-setters"


async def main() -> None:
    await db.init()
    fails: list[str] = []
    async with db.pool.acquire() as c:
        async def drop():
            await c.execute("delete from consent_events where tenant_id in (select id from tenants where slug=$1)", SLUG)
            await c.execute("delete from leads where tenant_id in (select id from tenants where slug=$1)", SLUG)
            await c.execute("delete from tenants where slug=$1", SLUG)

        async def under(tid, f):
            tok = db.current_tenant_id.set(tid)
            try:
                return await f()
            finally:
                db.current_tenant_id.reset(tok)

        await drop()
        tid = await c.fetchval("insert into tenants (slug,name,status) values ($1,'SMOKE setters','active') returning id", SLUG)
        # VK-лид (идентичность vk_user_id) + MAX-лид (max_user_id)
        await c.execute("insert into leads (tenant_id,messenger,source,vk_user_id,status) values ($1,'vk','vk',$2,'new')", tid, 555001)
        await c.execute("insert into leads (tenant_id,messenger,source,max_user_id,status) values ($1,'max','max',$2,'new')", tid, 555002)
        try:
            await under(tid, lambda: db.set_consent(555001, True, consent_text="VK-СОГЛАСИЕ", channel="vk", messenger="vk"))
            await under(tid, lambda: db.set_phone(555001, "+7 999 000-11-22", "deadbeef", messenger="vk"))
            st = await under(tid, lambda: db.get_lead_status(555001, messenger="vk"))
            if st is None:
                fails.append("get_lead_status(vk) вернул None — не нашёл vk-лида по vk_user_id")
            ev = await c.fetchval("select count(*) from consent_events where tenant_id=$1 and channel='vk' and action='granted'", tid)
            if ev != 1:
                fails.append(f"ожидалась 1 granted(vk), получено {ev}")
            await under(tid, lambda: db.mark_guide_sent(555002, messenger="max"))
            st2 = await c.fetchval("select status from leads where tenant_id=$1 and max_user_id=$2", tid, 555002)
            if st2 != "guide_sent":
                fails.append(f"mark_guide_sent(max) не выставил status (got {st2})")
        finally:
            await drop()
    if fails:
        print("\n".join("❌ " + f for f in fails)); raise SystemExit(1)
    print("🟢 db_channel_setters_smoke зелёный")

if __name__ == "__main__":
    asyncio.run(main())
