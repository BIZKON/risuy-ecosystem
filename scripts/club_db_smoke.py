#!/usr/bin/env python3
"""RLS-смоук домена клуба на risuy_dev: изоляция A≠B (club_member_get/list через явный
tenant-фильтр), cross-tenant lead → NULL, запись согласия в consent_events.
Гоняет КОНТРОЛЛЕР (нужен TEAM_DSN — owner-DSN risuy_dev, доступа у автора хелперов нет).

  TEAM_DSN="postgresql://gen_user:...@81.31.246.136:5432/risuy_dev?sslmode=require" \\
  PYTHONPATH=admin-panel:. ./.venv-smoke/bin/python scripts/club_db_smoke.py

Пишет/чистит собственные временные тенанты — на существующие данные risuy_dev не полагается.
"""
import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "admin-panel"))
os.environ.setdefault("DATABASE_URL", "postgresql://x/y")
os.environ.setdefault("SESSION_SECRET", "smoke-secret-only-for-import-aaaaaaaa")
os.environ.setdefault("ADMIN_USERNAME", "smoke")
os.environ.setdefault("ADMIN_PASSWORD_HASH", "$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHQ$aGFzaHN0dWI")

import asyncpg  # noqa: E402
import db  # admin-panel/db.py  # noqa: E402

DSN = os.environ.get("TEAM_DSN", "")
if "/risuy_dev" not in DSN.split("?")[0]:
    print("SKIP: нужен TEAM_DSN на risuy_dev")
    sys.exit(0)

FAILS = []


def check(name, cond, detail=""):
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


async def main():
    # ⚠️ setup=db._apply_tenant_guc — GUC ставится централизованно на каждом чек-ауте
    # пула из contextvar (set_active_tenant), а НЕ отдельным "set local" в acquire()
    # (в autocommit-режиме asyncpg такой execute — no-op между разными acquire()).
    db.pool = await asyncpg.create_pool(DSN, min_size=1, max_size=4, setup=db._apply_tenant_guc)

    async with db.pool.acquire() as c:
        ta = await c.fetchval(
            "insert into tenants (slug, name, status) values "
            "('smoke-club-a-'||substr(md5(random()::text),1,8),'SMOKE CLUB A','active') returning id"
        )
        tb = await c.fetchval(
            "insert into tenants (slug, name, status) values "
            "('smoke-club-b-'||substr(md5(random()::text),1,8),'SMOKE CLUB B','active') returning id"
        )
        lead_b = await c.fetchval(
            "insert into leads (tenant_id, name, consent) values ($1,'Лид B',true) returning id", tb
        )

    mid = None
    try:
        # ── тенант A: создание участника + профиль ──────────────────────────
        db.set_active_tenant(ta)
        mid = await db.club_member_create(
            ta, display_name="ООО Тест-А", city="Москва", okved="62.01"
        )
        await db.club_profile_upsert(
            mid, ta, offering="разработка", seeking="дизайн",
            chain_position="before", okved_seek="74.10",
        )

        # cross-tenant lead: чужой (B) лид не привязывается к участнику тенанта A
        mid_x = await db.club_member_create(
            ta, display_name="ООО Тест-X", city="Казань", okved="62.02", lead_id=lead_b,
        )
        got_x = await db.club_member_get(mid_x, ta)
        check("чужой лид (тенант B) НЕ привязан к участнику тенанта A (lead_id=NULL)",
              got_x is not None and got_x["lead_id"] is None)

        # ── RLS-изоляция явным tenant-фильтром: A видит, B — нет ────────────
        db.set_active_tenant(ta)
        got_a = await db.club_member_get(mid, ta)
        check("A видит своего club_member", got_a is not None)

        db.set_active_tenant(tb)
        got_b = await db.club_member_get(mid, tb)
        check("RLS: B НЕ видит club_member тенанта A", got_b is None)

        db.set_active_tenant(ta)
        list_a = await db.club_member_list(ta)
        check("list(A) содержит mid", any(m["id"] == mid for m in list_a))

        db.set_active_tenant(tb)
        list_b = await db.club_member_list(tb)
        check("RLS: list(B) НЕ содержит mid", all(m["id"] != mid for m in list_b))

        # ── согласие пишется в consent_events ────────────────────────────────
        db.set_active_tenant(ta)
        await db.club_consent_record(
            ta, doc_type="club_join", member_id=mid, text_hash="abc", channel="web"
        )
        # gen_user (owner-DSN) видит строку без app.tenant_id — прямой count без GUC.
        async with db.pool.acquire() as c:
            n = await c.fetchval(
                "select count(*) from consent_events where member_id=$1 and doc_type='club_join'",
                mid,
            )
        check("согласие club_join записано ровно один раз", n == 1, f"n={n}")

    finally:
        async with db.pool.acquire() as c:
            # Порядок FK: consent_events → club_profiles/club_intros → club_members → leads → tenants.
            await c.execute(
                "delete from consent_events where tenant_id = any($1::uuid[])", [ta, tb]
            )
            await c.execute(
                "delete from club_profiles where tenant_id = any($1::uuid[])", [ta, tb]
            )
            await c.execute(
                "delete from club_intros where tenant_id = any($1::uuid[])", [ta, tb]
            )
            await c.execute(
                "delete from club_members where tenant_id = any($1::uuid[])", [ta, tb]
            )
            await c.execute("delete from leads where tenant_id = any($1::uuid[])", [ta, tb])
            await c.execute("delete from tenants where id = any($1::uuid[])", [ta, tb])
        await db.pool.close()

    print(("\nFAIL: " + ", ".join(FAILS)) if FAILS else "\nOK: club_db_smoke")
    sys.exit(1 if FAILS else 0)


asyncio.run(main())
