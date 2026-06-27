#!/usr/bin/env python3
"""Smoke: отзыв согласия из бота (152-ФЗ ст.9 ч.2) на risuy_dev. request_erase ставит
erase_requested_at+unsubscribed_at+consent_events('revoked'); is_erase_requested=True (бот молчит);
повторное согласие ВОЗВРАЩАЕТ (снимает erase/unsub). Throwaway-тенант+лид, чистка каскадом по порядку.

Запуск:
  FUNNEL_SMOKE_DSN="postgresql://gen_user:<pw>@HOST:5432/risuy_dev?sslmode=require" \
  PYTHONPATH=bot-telegram BOT_TOKEN=x DATABASE_URL="$FUNNEL_SMOKE_DSN" CHANNEL_ID=-100 \
  CHANNEL_URL=https://t.me/x GUIDE_URL=https://x ./.venv-smoke/bin/python scripts/consent_revoke_smoke.py
"""
import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import db  # noqa: E402

DSN = os.environ.get("FUNNEL_SMOKE_DSN") or os.environ.get("DATABASE_URL")
if not DSN or "risuy_dev" not in DSN:
    raise SystemExit("Задайте DSN на risuy_dev (защита от прода).")

SLUG = "smoke-revoke"
TG = 990002223


async def main() -> None:
    await db.init()
    fails: list[str] = []
    async with db.pool.acquire() as c:
        async def drop() -> None:
            await c.execute("delete from consent_events where tenant_id in (select id from tenants where slug=$1)", SLUG)
            await c.execute("delete from leads where tenant_id in (select id from tenants where slug=$1)", SLUG)
            await c.execute("delete from tenants where slug=$1", SLUG)

        async def under(tid, coro_factory):
            tok = db.current_tenant_id.set(tid)
            try:
                return await coro_factory()
            finally:
                db.current_tenant_id.reset(tok)

        await drop()
        tid = await c.fetchval("insert into tenants (slug,name,status) values ($1,'SMOKE revoke','active') returning id", SLUG)
        await c.execute("insert into leads (tenant_id,messenger,source,tg_user_id,status) values ($1,'tg','other',$2,'new')", tid, TG)
        try:
            # согласие
            await under(tid, lambda: db.set_consent(TG, True, consent_text="ТЕСТ", channel="tg"))
            # отзыв
            await under(tid, lambda: db.request_erase(TG, channel="tg"))
            row = await c.fetchrow("select consent, erase_requested_at, unsubscribed_at from leads where tenant_id=$1 and tg_user_id=$2", tid, TG)
            if row["erase_requested_at"] is None:
                fails.append("erase_requested_at не выставлен после отзыва")
            if row["unsubscribed_at"] is None:
                fails.append("unsubscribed_at не выставлен после отзыва")
            rev = await c.fetchval("select count(*) from consent_events where tenant_id=$1 and action='revoked'", tid)
            if rev != 1:
                fails.append(f"ожидалась 1 запись revoked, получено {rev}")
            silenced = await under(tid, lambda: db.is_erase_requested(TG))
            if silenced is not True:
                fails.append("is_erase_requested должен быть True после отзыва (бот молчит)")

            # повторное согласие → ВОЗВРАТ (erase/unsub сняты)
            await under(tid, lambda: db.set_consent(TG, True, consent_text="ТЕСТ", channel="tg"))
            row2 = await c.fetchrow("select erase_requested_at, unsubscribed_at from leads where tenant_id=$1 and tg_user_id=$2", tid, TG)
            if row2["erase_requested_at"] is not None:
                fails.append("повторное согласие НЕ сняло erase_requested_at (вернувшегося обезличит cron)")
            if row2["unsubscribed_at"] is not None:
                fails.append("повторное согласие НЕ сняло unsubscribed_at")
            if await under(tid, lambda: db.is_erase_requested(TG)) is not False:
                fails.append("после возврата is_erase_requested должен быть False")
            granted = await c.fetchval("select count(*) from consent_events where tenant_id=$1 and action='granted'", tid)
            if granted != 2:
                fails.append(f"ожидалось 2 granted (история), получено {granted}")
        finally:
            await drop()

    if fails:
        print("\n".join("❌ " + f for f in fails))
        raise SystemExit(1)
    print("🟢 consent_revoke_smoke зелёный")


if __name__ == "__main__":
    asyncio.run(main())
