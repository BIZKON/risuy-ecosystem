#!/usr/bin/env python3
"""Smoke: set_consent пишет реестр согласий consent_events (152-ФЗ ст.9) АТОМАРНО с leads.consent.
Throwaway-тенант+лид на risuy_dev; чистка (consent_events → leads → tenant; FK без cascade у leads).

Запуск:
  FUNNEL_SMOKE_DSN="postgresql://gen_user:<pw>@HOST:5432/risuy_dev?sslmode=require" \
  PYTHONPATH=bot-telegram BOT_TOKEN=x DATABASE_URL="$FUNNEL_SMOKE_DSN" CHANNEL_ID=-100 \
  CHANNEL_URL=https://t.me/x GUIDE_URL=https://x ./.venv-smoke/bin/python scripts/consent_events_smoke.py
"""
import asyncio
import hashlib
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import db  # noqa: E402

DSN = os.environ.get("FUNNEL_SMOKE_DSN") or os.environ.get("DATABASE_URL")
if not DSN or "risuy_dev" not in DSN:
    raise SystemExit("Задайте DSN на risuy_dev (защита от прода).")

SLUG = "smoke-consent-evt"
TG = 990001112
TEXT = "ТЕСТ: согласие на обработку персональных данных"


async def main() -> None:
    await db.init()
    fails: list[str] = []
    async with db.pool.acquire() as c:
        async def drop() -> None:
            await c.execute("delete from consent_events where tenant_id in "
                            "(select id from tenants where slug=$1)", SLUG)
            await c.execute("delete from leads where tenant_id in "
                            "(select id from tenants where slug=$1)", SLUG)
            await c.execute("delete from tenants where slug=$1", SLUG)

        async def consent_under(tid) -> None:
            tok = db.current_tenant_id.set(tid)
            try:
                await db.set_consent(TG, True, consent_text=TEXT, channel="tg")
            finally:
                db.current_tenant_id.reset(tok)

        await drop()
        tid = await c.fetchval(
            "insert into tenants (slug,name,status) values ($1,'SMOKE consent','active') returning id", SLUG)
        await c.execute(
            "insert into leads (tenant_id,messenger,source,tg_user_id,status) "
            "values ($1,'tg','other',$2,'new')", tid, TG)
        try:
            await consent_under(tid)

            cons = await c.fetchval("select consent from leads where tenant_id=$1 and tg_user_id=$2", tid, TG)
            if cons is not True:
                fails.append(f"leads.consent не true: {cons}")

            rows = await c.fetch(
                "select action, doc_version, text_hash, channel, lead_id from consent_events where tenant_id=$1", tid)
            if len(rows) != 1:
                fails.append(f"ожидалась 1 запись реестра, получено {len(rows)}")
            else:
                r = rows[0]
                if r["action"] != "granted":
                    fails.append(f"action: {r['action']}")
                if r["doc_version"] != 1:
                    fails.append(f"doc_version: {r['doc_version']}")
                if r["channel"] != "tg":
                    fails.append(f"channel: {r['channel']}")
                if r["text_hash"] != hashlib.sha256(TEXT.encode("utf-8")).hexdigest():
                    fails.append("text_hash не совпал с sha256(текста)")
                if r["lead_id"] is None:
                    fails.append("lead_id null (должен указывать на лида)")

            # Повторное согласие → ВТОРАЯ granted-запись (история append-only, не перезапись)
            await consent_under(tid)
            n = await c.fetchval("select count(*) from consent_events where tenant_id=$1", tid)
            if n != 2:
                fails.append(f"повторное согласие: ожидалось 2 записи (история), получено {n}")
        finally:
            await drop()

    if fails:
        print("\n".join("❌ " + f for f in fails))
        raise SystemExit(1)
    print("🟢 consent_events_smoke зелёный")


if __name__ == "__main__":
    asyncio.run(main())
