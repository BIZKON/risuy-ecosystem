#!/usr/bin/env python3
"""Smoke панели: реестр согласий — list_lead_consent_events (карточка лида) + stream_consent_events
(CSV-экспорт) на risuy_dev. Throwaway-тенант+лид+2 события, чистка по порядку (consent_events→leads→tenant).

Запуск:
  FUNNEL_SMOKE_DSN="postgresql://gen_user:<pw>@HOST:5432/risuy_dev?sslmode=require" \
  DATABASE_URL="$FUNNEL_SMOKE_DSN" SESSION_SECRET="$(python3 -c 'import secrets;print(secrets.token_urlsafe(48))')" \
  ADMIN_USERNAME=smoke ADMIN_PASSWORD_HASH='$argon2id$v=19$m=65536,t=3,p=4$c21va2VzbW9rZQ$c21va2VzbW9rZXNtb2tl' \
  PYTHONPATH=admin-panel ./.venv-smoke/bin/python scripts/consent_registry_smoke.py
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

SLUG = "smoke-consent-reg"


async def main() -> None:
    await db.init()
    fails: list[str] = []
    async with db.pool.acquire() as c:
        async def drop() -> None:
            await c.execute("delete from consent_events where tenant_id in (select id from tenants where slug=$1)", SLUG)
            await c.execute("delete from leads where tenant_id in (select id from tenants where slug=$1)", SLUG)
            await c.execute("delete from tenants where slug=$1", SLUG)

        await drop()
        tid = await c.fetchval("insert into tenants (slug,name,status) values ($1,'SMOKE reg','active') returning id", SLUG)
        lid = await c.fetchval(
            "insert into leads (tenant_id,messenger,source,tg_user_id,status) "
            "values ($1,'tg','other',990003334,'new') returning id", tid)
        await c.execute(
            "insert into consent_events (tenant_id,lead_id,doc_type,doc_version,text_hash,action,channel) "
            "values ($1,$2,'consent',1,'abc','granted','tg')", tid, lid)
        await c.execute(
            "insert into consent_events (tenant_id,lead_id,doc_type,action,channel) "
            "values ($1,$2,'consent','revoked','tg')", tid, lid)
        try:
            evs = await db.list_lead_consent_events(lid)
            if len(evs) != 2:
                fails.append(f"list_lead_consent_events: ожидалось 2, получено {len(evs)}")
            elif evs[0]["action"] != "revoked":
                fails.append(f"порядок (свежие сверху): первый должен быть revoked, получено {evs[0]['action']}")

            seen = 0
            async for r in db.stream_consent_events(row_cap=20000):
                if str(r["lead_id"]) == str(lid):
                    seen += 1
            if seen != 2:
                fails.append(f"stream_consent_events: для лида ожидалось 2, получено {seen}")
        finally:
            await drop()

    if fails:
        print("\n".join("❌ " + f for f in fails))
        raise SystemExit(1)
    print("🟢 consent_registry_smoke зелёный")


if __name__ == "__main__":
    asyncio.run(main())
