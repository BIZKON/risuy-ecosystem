#!/usr/bin/env python3
"""Смоук 3a: бот-side club-хелперы (bot-telegram/db.py::club_member_create/club_profile_upsert/
club_consent_record). Зеркалит escalation_smoke.py (bot-telegram на PYTHONPATH, env-стабы,
db.current_tenant_id.set вместо параметра tenant_id — бот резолвит СВОЙ активный тенант через
tenant_id(), в отличие от панели). Гоняет КОНТРОЛЛЕР (нужен TEAM_DSN — owner-DSN risuy_dev,
доступа у автора хелперов нет). На прод НЕ запускать.

Запуск:
  TEAM_DSN="postgresql://gen_user:...@81.31.246.136:5432/risuy_dev?sslmode=require" \
  PYTHONPATH=bot-telegram:. ./.venv-smoke/bin/python scripts/club_bot_db_smoke.py

Пишет/чистит собственный временный тенант — на существующие данные risuy_dev не полагается.
"""
import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)  # пакет shared (ленивые импорты внутри bot-telegram/db.py)
sys.path.insert(0, os.path.join(ROOT, "bot-telegram"))
os.environ.setdefault("BOT_TOKEN", "0:smoke")
os.environ.setdefault("DATABASE_URL", "postgresql://x/y")
os.environ.setdefault("CHANNEL_ID", "-1001234567890")
os.environ.setdefault("CHANNEL_URL", "https://t.me/smoke")
os.environ.setdefault("GUIDE_URL", "https://example.org/guide")

import asyncpg  # noqa: E402
import db       # noqa: E402  (bot-telegram/db.py)

DSN = os.environ.get("TEAM_DSN", "")
if "/risuy_dev" not in DSN.split("?")[0]:
    print("SKIP: нужен TEAM_DSN на risuy_dev")
    sys.exit(0)

FAILS: list[str] = []


def check(name, cond, detail=""):
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


async def main() -> None:
    db.pool = await asyncpg.create_pool(DSN, min_size=1, max_size=4)

    async with db.pool.acquire() as c:
        ta = await c.fetchval(
            "insert into tenants (slug, name, status) values "
            "('smoke-club-bot-'||substr(md5(random()::text),1,8),'SMOKE CLUB BOT','active') returning id"
        )

    mid = None
    try:
        # Бот резолвит СВОЙ единственный активный тенант через tenant_id() — контекст
        # выставляем так же, как это делает мультиплекс-мидлварь (_TenantContextMiddleware).
        db.current_tenant_id.set(ta)

        # ── club_member_create: без lead_id ──────────────────────────────────
        mid = await db.club_member_create(display_name="ООО Бот-Тест", city="Новосибирск", okved="62.01")
        check("club_member_create вернул id (str)", isinstance(mid, str) and len(mid) > 0, repr(mid))

        async with db.pool.acquire() as c:
            row = await c.fetchrow("select * from club_members where id = $1::uuid", mid)
        check("участник создан с tenant_id() активного тенанта",
              row is not None and str(row["tenant_id"]) == str(ta))
        check("lead_id не задан → NULL", row is not None and row["lead_id"] is None)

        # ── club_profile_upsert ───────────────────────────────────────────────
        await db.club_profile_upsert(
            mid, offering="разработка", seeking="дизайн",
            chain_position="before", okved_seek="74.10",
        )
        async with db.pool.acquire() as c:
            prow = await c.fetchrow("select * from club_profiles where member_id = $1::uuid", mid)
        check("профиль создан (offering/seeking записаны)",
              prow is not None and prow["offering"] == "разработка" and prow["seeking"] == "дизайн")

        # upsert повторно — обновление, не дубликат
        await db.club_profile_upsert(
            mid, offering="разработка 2.0", seeking="маркетинг",
            chain_position="both", okved_seek="74.10",
        )
        async with db.pool.acquire() as c:
            prow2 = await c.fetchrow("select * from club_profiles where member_id = $1::uuid", mid)
            cnt = await c.fetchval("select count(*) from club_profiles where member_id = $1::uuid", mid)
        check("повторный upsert обновил запись (offering='разработка 2.0')",
              prow2 is not None and prow2["offering"] == "разработка 2.0")
        check("повторный upsert НЕ создал дубликат (count=1)", cnt == 1, f"cnt={cnt}")

        # ── club_consent_record ───────────────────────────────────────────────
        await db.club_consent_record(doc_type="club_join", member_id=mid, text_hash="abc123", channel="tg")
        async with db.pool.acquire() as c:
            ccount = await c.fetchval(
                "select count(*) from consent_events where member_id = $1::uuid and doc_type = 'club_join'",
                mid,
            )
        check("consent_events: club_join записан ровно один раз", ccount == 1, f"ccount={ccount}")

    finally:
        async with db.pool.acquire() as c:
            # Порядок FK: consent_events → club_profiles → club_members → leads → tenants.
            await c.execute("delete from consent_events where tenant_id = $1::uuid", ta)
            await c.execute("delete from club_profiles where tenant_id = $1::uuid", ta)
            await c.execute("delete from club_members where tenant_id = $1::uuid", ta)
            await c.execute("delete from leads where tenant_id = $1::uuid", ta)
            await c.execute("delete from tenants where id = $1::uuid", ta)
        await db.pool.close()

    print()
    if FAILS:
        print(f"❌ ПРОВАЛЫ ({len(FAILS)}): " + ", ".join(FAILS))
        sys.exit(1)
    print("✅ club_bot_db_smoke — все проверки зелёные")


if __name__ == "__main__":
    asyncio.run(main())
