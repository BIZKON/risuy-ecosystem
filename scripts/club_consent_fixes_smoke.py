#!/usr/bin/env python3
"""Смоук: клуб-152-ФЗ фиксы СЛОЙ 2 (bot-telegram/db.py) — отзыв согласия члена клуба +
retention-обезличивание + ФЗ-38 offers opt-out + tenant-изоляция. Зеркалит club_bot_db_smoke.py
(бот на PYTHONPATH, env-стабы, db.current_tenant_id.set вместо параметра). Гоняет КОНТРОЛЛЕР
(нужен TEAM_DSN = owner-DSN risuy_dev). На прод НЕ запускать. Пишет/чистит свои временные тенанты.

Запуск:
  TEAM_DSN="postgresql://gen_user:...@81.31.246.136:5432/risuy_dev?sslmode=require" \
  PYTHONPATH=bot-telegram:. ./.venv-smoke/bin/python scripts/club_consent_fixes_smoke.py
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
import db       # noqa: E402  (bot-telegram/db.py)

DSN = os.environ.get("TEAM_DSN", "")
if "/risuy_dev" not in DSN.split("?")[0]:
    print("SKIP: нужен TEAM_DSN на risuy_dev")
    sys.exit(0)

FAILS: list[str] = []
SMOKE_ACTOR = "smoke-club-fixes"


def check(name, cond, detail=""):
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


async def _mk_tenant(c) -> str:
    return await c.fetchval(
        "insert into tenants (slug, name, status) values "
        "('smoke-club-fix-'||substr(md5(random()::text),1,8),'SMOKE CLUB FIX','active') returning id"
    )


async def main() -> None:
    db.pool = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    async with db.pool.acquire() as c:
        ta = await _mk_tenant(c)
        tb = await _mk_tenant(c)
    tg = 77_000_001  # один и тот же внешний tg-id в ОБОИХ тенантах (для теста изоляции)
    try:
        # ── 1. club_member_create пишет tg_user_id + offers_opt_in default true ─────────
        db.current_tenant_id.set(ta)
        mid = await db.club_member_create(display_name="ООО Отзыв", city="Казань", okved="62.01",
                                          tg_user_id=tg, messenger="tg")
        async with db.pool.acquire() as c:
            row = await c.fetchrow("select tg_user_id, offers_opt_in, erase_requested_at "
                                   "from club_members where id = $1::uuid", mid)
        check("club_member_create записал tg_user_id", row and row["tg_user_id"] == tg, repr(row and row["tg_user_id"]))
        check("offers_opt_in по умолчанию true", row and row["offers_opt_in"] is True)
        check("erase_requested_at изначально NULL", row and row["erase_requested_at"] is None)
        await db.club_profile_upsert(mid, offering="разработка", seeking="дизайн",
                                     chain_position="before", okved_seek="")

        # Тот же tg в тенанте B — для проверки изоляции отзыва.
        db.current_tenant_id.set(tb)
        mid_b = await db.club_member_create(display_name="ООО Другой", city="Уфа", okved="10.71",
                                            tg_user_id=tg, messenger="tg")

        # ── 2. club_revoke_member: чистый член без lead отзывается + пишет revoked ───────
        db.current_tenant_id.set(ta)
        n = await db.club_revoke_member(tg, channel="tg")
        check("club_revoke_member вернул 1 (член найден)", n == 1, f"n={n}")
        async with db.pool.acquire() as c:
            era = await c.fetchval("select erase_requested_at from club_members where id=$1::uuid", mid)
            rev = await c.fetchval("select count(*) from consent_events where member_id=$1::uuid "
                                   "and doc_type='club_join' and action='revoked'", mid)
            era_b = await c.fetchval("select erase_requested_at from club_members where id=$1::uuid", mid_b)
        check("erase_requested_at проставлен члену A", era is not None)
        check("consent_events revoked записан (append-only реестр)", rev == 1, f"rev={rev}")
        check("🔒 ИЗОЛЯЦИЯ: член B с тем же tg НЕ затронут", era_b is None)

        # ── 3. is_erase_requested учитывает членство (по тенанту) ───────────────────────
        db.current_tenant_id.set(ta)
        check("is_erase_requested(A)=True после отзыва", await db.is_erase_requested(tg) is True)
        db.current_tenant_id.set(tb)
        check("🔒 is_erase_requested(B)=False (изоляция)", await db.is_erase_requested(tg) is False)

        # ── 4. ФЗ-38 offers opt-out без выхода из клуба ─────────────────────────────────
        db.current_tenant_id.set(tb)
        n_off = await db.club_set_offers_opt_in(tg, False)
        async with db.pool.acquire() as c:
            oo = await c.fetchval("select offers_opt_in from club_members where id=$1::uuid", mid_b)
            st = await c.fetchval("select status from club_members where id=$1::uuid", mid_b)
        check("club_set_offers_opt_in выключил предложения", n_off == 1 and oo is False)
        check("членство сохранено (status='active')", st == "active")

        # ── 5. retention: club_due_for_erase ловит члена A, club_erase_member обезличивает ─
        db.current_tenant_id.set(ta)
        async with db.pool.acquire() as c:  # состарим отзыв на 40 дней
            await c.execute("update club_members set erase_requested_at = now() - interval '40 days' "
                            "where id=$1::uuid", mid)
        due = await db.club_due_for_erase(30)
        check("club_due_for_erase(30) содержит члена A (lead_id=NULL покрыт)", mid in due, f"due={due}")
        db.current_tenant_id.set(tb)
        due_b = await db.club_due_for_erase(30)
        check("🔒 club_due_for_erase(B) НЕ содержит члена A", mid not in due_b)

        db.current_tenant_id.set(ta)
        await db.club_erase_member(mid, actor=SMOKE_ACTOR)
        async with db.pool.acquire() as c:
            m = await c.fetchrow("select display_name, city, okved, inn, tg_user_id, status "
                                 "from club_members where id=$1::uuid", mid)
            p = await c.fetchrow("select offering, seeking, description, chain_position "
                                 "from club_profiles where member_id=$1::uuid", mid)
            aud = await c.fetchval("select count(*) from admin_audit where actor=$1 "
                                   "and action='club_member_erased'", SMOKE_ACTOR)
        check("club_members обезличен (display_name='удалено', PII null)",
              m and m["display_name"] == "удалено" and m["city"] is None and m["inn"] is None
              and m["tg_user_id"] is None)
        check("status='left' после обезличивания", m and m["status"] == "left")
        check("club_profiles обезличен (offering/seeking/description null)",
              p and p["offering"] is None and p["seeking"] is None and p["description"] is None)
        check("admin_audit club_member_erased записан (срок для РКН)", aud == 1, f"aud={aud}")
        due_after = await db.club_due_for_erase(30)
        check("после обезличивания член A НЕ повторяется в выборке (status=left)", mid not in due_after)

        # ── 6. erase_lead расширен: промотированный член (lead_id) обезличивается синхронно ─
        db.current_tenant_id.set(ta)
        async with db.pool.acquire() as c:
            lead = await c.fetchval(
                "insert into leads (tg_user_id, messenger, source, status, tenant_id, name, phone) "
                "values ($1,'tg','smoke','new',$2::uuid,'Иван','+79990000000') returning id",
                77_000_002, ta,
            )
        mid2 = await db.club_member_create(display_name="ООО Промо", city="Сочи", okved="55.10",
                                           lead_id=str(lead), tg_user_id=77_000_002)
        await db.club_profile_upsert(mid2, offering="отели", seeking="трафик",
                                     chain_position="after", okved_seek="")
        await db.erase_lead(str(lead), actor=SMOKE_ACTOR)
        async with db.pool.acquire() as c:
            m2 = await c.fetchrow("select display_name, city, inn, status from club_members where id=$1::uuid", mid2)
            p2 = await c.fetchrow("select offering, seeking from club_profiles where member_id=$1::uuid", mid2)
        check("erase_lead обезличил club-карточку промотированного лида",
              m2 and m2["display_name"] == "удалено" and m2["city"] is None and m2["status"] == "left")
        check("erase_lead обезличил club_profiles промотированного",
              p2 and p2["offering"] is None and p2["seeking"] is None)

    finally:
        async with db.pool.acquire() as c:
            for t in (ta, tb):
                await c.execute("delete from consent_events where tenant_id = $1::uuid", t)
                await c.execute("delete from club_profiles where tenant_id = $1::uuid", t)
                await c.execute("delete from club_members where tenant_id = $1::uuid", t)
                await c.execute("delete from leads where tenant_id = $1::uuid", t)
                await c.execute("delete from tenants where id = $1::uuid", t)
            await c.execute("delete from admin_audit where actor = $1", SMOKE_ACTOR)
        await db.pool.close()

    print()
    if FAILS:
        print(f"❌ ПРОВАЛЫ ({len(FAILS)}): " + ", ".join(FAILS))
        sys.exit(1)
    print("✅ club_consent_fixes_smoke — все проверки зелёные")


if __name__ == "__main__":
    asyncio.run(main())
