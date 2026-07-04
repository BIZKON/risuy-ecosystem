#!/usr/bin/env python3
"""db-смоук новых клуб-аналитических хелперов на risuy_dev: club_member_list_enriched
(ЕГРЮЛ-join по inn + фильтры status/okved), club_growth (week/month), club_intro_funnel.
Гоняет КОНТРОЛЛЕР (нужен TEAM_DSN — owner-DSN risuy_dev). Пишет/чистит свои тенанты.
  TEAM_DSN="postgresql://gen_user:...@81.31.246.136:5432/risuy_dev?sslmode=require" \\
  PYTHONPATH=admin-panel:. ./.venv-smoke/bin/python scripts/club_dashboard_db_smoke.py
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
    db.pool = await asyncpg.create_pool(DSN, min_size=1, max_size=4, setup=db._apply_tenant_guc)
    ta = tb = None
    try:
        async with db.pool.acquire() as c:
            ta = await c.fetchval(
                "insert into tenants (slug, name, status) values "
                "('smoke-dash-a-'||substr(md5(random()::text),1,8),'SMOKE DASH A','active') returning id")
            tb = await c.fetchval(
                "insert into tenants (slug, name, status) values "
                "('smoke-dash-b-'||substr(md5(random()::text),1,8),'SMOKE DASH B','active') returning id")
            # члены A: ИП (Казань), ЮЛ с ЕГРЮЛ (Москва), ещё один ЮЛ (Москва, okved 41.20)
            m1 = await c.fetchval(
                "insert into club_members (tenant_id, display_name, city, okved, inn, status, created_at) "
                "values ($1,'ИП Соколова','Казань','62.01','165000000000','active', now() - interval '40 days') returning id", ta)
            m2 = await c.fetchval(
                "insert into club_members (tenant_id, display_name, city, okved, inn, status, created_at) "
                "values ($1,'ООО Ромашка','Москва','62.01','7700000001','active', now()) returning id", ta)
            m3 = await c.fetchval(
                "insert into club_members (tenant_id, display_name, city, okved, inn, status) "
                "values ($1,'ООО Строй','Москва','41.20','7700000002','paused') returning id", ta)
            await c.execute(
                "insert into club_profiles (member_id, tenant_id, offering, seeking, chain_position, avg_check) "
                "values ($1,$2,'разработка','дизайн','before',300)", m2, ta)
            # ЕГРЮЛ-карточка для m2 (join по inn)
            await c.execute(
                "insert into prospects (tenant_id, inn, subject_type, name_short, opf, okved, okved_name) "
                "values ($1,'7700000001','legal','ООО Ромашка','ООО','62.01','Разработка ПО')", ta)
            # знакомства: одно accepted-обоюдное, одно requested, одно declined
            i1 = await c.fetchval(
                "insert into club_intros (tenant_id, from_member, to_member, status, from_accepted_at, to_accepted_at) "
                "values ($1,$2,$3,'accepted', now(), now()) returning id", ta, m2, m3)
            await c.execute(
                "insert into club_intros (tenant_id, from_member, to_member, status) values ($1,$2,$3,'requested')", ta, m2, m1)
            await c.execute(
                "insert into club_intros (tenant_id, from_member, to_member, status) values ($1,$2,$3,'declined')", ta, m3, m1)

        db.set_active_tenant(ta)

        # ── enriched: все члены A, ЕГРЮЛ подмешан для m2 ──────────────────────
        rows = await db.club_member_list_enriched(ta)
        check("enriched(A) вернул 3 членов", len(rows) == 3, f"n={len(rows)}")
        by_name = {r["display_name"]: r for r in rows}
        check("enriched: у ООО Ромашка подмешан ЕГРЮЛ-opf=ООО",
              by_name["ООО Ромашка"]["prospect_opf"] == "ООО")
        check("enriched: у ООО Ромашка profile avg_check=300",
              by_name["ООО Ромашка"]["avg_check"] == 300)
        check("enriched: у ИП Соколова ЕГРЮЛ отсутствует (prospect_opf=NULL)",
              by_name["ИП Соколова"]["prospect_opf"] is None)

        # ── enriched: фильтры status/okved в SQL ──────────────────────────────
        act = await db.club_member_list_enriched(ta, status="active")
        check("enriched(status=active) → 2", len(act) == 2, f"n={len(act)}")
        ok = await db.club_member_list_enriched(ta, okved="41.20")
        check("enriched(okved=41.20) → 1 (ООО Строй)",
              len(ok) == 1 and ok[0]["display_name"] == "ООО Строй")

        # ── изоляция: B не видит членов A ────────────────────────────────────
        db.set_active_tenant(tb)
        rows_b = await db.club_member_list_enriched(tb)
        check("изоляция: enriched(B) пуст (нет членов B)", len(rows_b) == 0)

        # ── рост: месяц/неделя ────────────────────────────────────────────────
        db.set_active_tenant(ta)
        gm = await db.club_growth(ta, "month")
        check("growth(month): ≥2 бакета (40 дней назад + сейчас)", len(gm) >= 2, f"buckets={len(gm)}")
        check("growth(month): сумма count == 3", sum(b["count"] for b in gm) == 3)
        check("growth: bucket — строка YYYY-MM-DD", all(isinstance(b["bucket"], str) for b in gm))
        gw = await db.club_growth(ta, "week")
        check("growth(week): сумма count == 3", sum(b["count"] for b in gw) == 3)

        # ── воронка знакомств ─────────────────────────────────────────────────
        f = await db.club_intro_funnel(ta)
        check("funnel.accepted == 1", f["accepted"] == 1, f"f={f}")
        check("funnel.requested == 1", f["requested"] == 1)
        check("funnel.declined == 1", f["declined"] == 1)
        check("funnel.both_accepted == 1", f["both_accepted"] == 1)
        check("funnel.total == 3", f["total"] == 3)

        # ── пустой тенант: воронка/рост не падают ─────────────────────────────
        db.set_active_tenant(tb)
        fb = await db.club_intro_funnel(tb)
        check("funnel(пустой) total=0 both_accepted=0", fb["total"] == 0 and fb["both_accepted"] == 0)
        check("growth(пустой) == []", await db.club_growth(tb, "month") == [])

    finally:
        async with db.pool.acquire() as c:
            for t in (ta, tb):
                if not t:
                    continue
                await c.execute("delete from club_intros where tenant_id=$1", t)
                await c.execute("delete from club_profiles where tenant_id=$1", t)
                await c.execute("delete from prospects where tenant_id=$1", t)
                await c.execute("delete from club_members where tenant_id=$1", t)
                await c.execute("delete from tenants where id=$1", t)
        await db.pool.close()

    print(("\nFAIL: " + ", ".join(FAILS)) if FAILS else "\nOK: club_dashboard_db_smoke")
    sys.exit(1 if FAILS else 0)


asyncio.run(main())
