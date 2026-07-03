#!/usr/bin/env python3
"""Смоук db-ядра знакомств (Task 7a) на risuy_dev: create/decide/reveal, взаимное
согласие, tenant-изоляция, идемпотентность decide, красная линия reveal (контакты
ТОЛЬКО при status='accepted').
Гоняет КОНТРОЛЛЕР (нужен TEAM_DSN — owner-DSN risuy_dev, доступа у автора хелперов нет).

  TEAM_DSN="postgresql://gen_user:...@81.31.246.136:5432/risuy_dev?sslmode=require" \\
  PYTHONPATH=admin-panel:. ./.venv-smoke/bin/python scripts/club_intro_smoke.py

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
            "('smoke-intro-a-'||substr(md5(random()::text),1,8),'SMOKE INTRO A','active') returning id"
        )
        tc = await c.fetchval(
            "insert into tenants (slug, name, status) values "
            "('smoke-intro-c-'||substr(md5(random()::text),1,8),'SMOKE INTRO C','active') returning id"
        )

    mid_a = mid_b = mid_c = None
    try:
        # ── тенант A: два участника (A, B) с профилями, для непустых контактов в reveal ──
        db.set_active_tenant(ta)
        mid_a = await db.club_member_create(
            ta, display_name="ООО Тест-А", city="Москва", okved="62.01", inn="7701234567"
        )
        mid_b = await db.club_member_create(
            ta, display_name="ООО Тест-Б", city="Санкт-Петербург", okved="62.02", inn="7809876543"
        )
        await db.club_profile_upsert(
            mid_a, ta, offering="разработка", seeking="дизайн",
            chain_position="before", okved_seek="74.10",
        )
        await db.club_profile_upsert(
            mid_b, ta, offering="дизайн", seeking="разработка",
            chain_position="after", okved_seek="62.01",
        )

        # участник другого тенанта (для проверки cross-tenant изоляции create)
        db.set_active_tenant(tc)
        mid_c = await db.club_member_create(
            tc, display_name="ООО Тест-C", city="Казань", okved="70.22"
        )
        db.set_active_tenant(ta)

        # ── 1. create → requested ────────────────────────────────────────────
        intro_id = await db.club_intro_create(ta, mid_a, mid_b, message="Познакомим?")
        check("club_intro_create вернул id", intro_id is not None)

        async with db.pool.acquire() as c:
            row = await c.fetchrow(
                "select status, to_tenant_id from club_intros where id=$1", intro_id
            )
        check("status после create = requested", row is not None and row["status"] == "requested")
        check("to_tenant_id = tenant_id (Уровень 1)",
              row is not None and str(row["to_tenant_id"]) == str(ta))

        # ── 2. 🔴 КРАСНАЯ ЛИНИЯ: reveal ДО accept → None ─────────────────────
        reveal_before = await db.club_intro_reveal(intro_id, ta)
        check("reveal ДО accept возвращает None (контакты НЕ раскрыты)", reveal_before is None)

        # ── 3. tenant-изоляция create: cross-tenant to_member → None ─────────
        cross_intro = await db.club_intro_create(ta, mid_a, mid_c, message="Чужой тенант")
        check("create с чужим (тенант C) to_member → None (intro НЕ создан)", cross_intro is None)
        async with db.pool.acquire() as c:
            n_cross = await c.fetchval(
                "select count(*) from club_intros where tenant_id=$1 and from_member=$2 and to_member=$3",
                ta, mid_a, mid_c,
            )
        check("cross-tenant intro отсутствует в БД", n_cross == 0, f"n={n_cross}")

        # ── 4. decide(accept=True) → accepted + 2 consent_events ─────────────
        decided = await db.club_intro_decide(intro_id, ta, True, text_hash="abc")
        check("decide(accept=True) вернул True", decided is True)

        async with db.pool.acquire() as c:
            row2 = await c.fetchrow(
                "select status, decided_at from club_intros where id=$1", intro_id
            )
            n_consent = await c.fetchval(
                "select count(*) from consent_events "
                "where tenant_id=$1 and doc_type='intro_accept' and member_id = any($2::uuid[])",
                ta, [mid_a, mid_b],
            )
        check("status после accept = accepted", row2 is not None and row2["status"] == "accepted")
        check("decided_at проставлен", row2 is not None and row2["decided_at"] is not None)
        check("ровно 2 consent_events (from_member + to_member)", n_consent == 2, f"n={n_consent}")

        # ── 5. reveal ПОСЛЕ accept → непустые контакты обоих участников ──────
        reveal_after = await db.club_intro_reveal(intro_id, ta)
        check(
            "reveal ПОСЛЕ accept возвращает контакты обоих участников",
            reveal_after is not None
            and reveal_after.get("from", {}).get("display_name") == "ООО Тест-А"
            and reveal_after.get("to", {}).get("display_name") == "ООО Тест-Б",
        )

        # ── 6. идемпотентность: повторный decide на решённый intro → False ───
        decided_again = await db.club_intro_decide(intro_id, ta, True, text_hash="xyz")
        check("повторный decide на уже решённый intro → False (ничего не меняется)",
              decided_again is False)
        async with db.pool.acquire() as c:
            n_consent_2 = await c.fetchval(
                "select count(*) from consent_events "
                "where tenant_id=$1 and doc_type='intro_accept' and member_id = any($2::uuid[])",
                ta, [mid_a, mid_b],
            )
        check("повторный decide НЕ добавил новых consent_events", n_consent_2 == 2, f"n={n_consent_2}")

        # ── 7. decline-путь на отдельном intro ────────────────────────────────
        intro_id_2 = await db.club_intro_create(ta, mid_b, mid_a, message="Второе знакомство")
        check("создан второй intro для decline-пути", intro_id_2 is not None)

        declined = await db.club_intro_decide(intro_id_2, ta, False)
        check("decide(accept=False) вернул True", declined is True)

        async with db.pool.acquire() as c:
            row3 = await c.fetchrow("select status from club_intros where id=$1", intro_id_2)
        check("status после decline = declined", row3 is not None and row3["status"] == "declined")

        reveal_declined = await db.club_intro_reveal(intro_id_2, ta)
        check("reveal на declined intro → None", reveal_declined is None)

        # ── список ──────────────────────────────────────────────────────────
        intros = await db.club_intro_list(ta)
        check("club_intro_list содержит оба intro тенанта",
              {str(i["id"]) for i in intros} >= {intro_id, intro_id_2})

    finally:
        async with db.pool.acquire() as c:
            # Порядок FK: consent_events (по member_id/tenant) → club_intros →
            # club_profiles → club_members → leads → tenants.
            member_ids = [m for m in (mid_a, mid_b, mid_c) if m]
            if member_ids:
                await c.execute(
                    "delete from consent_events where member_id = any($1::uuid[])", member_ids
                )
            await c.execute(
                "delete from consent_events where tenant_id = any($1::uuid[])", [ta, tc]
            )
            await c.execute(
                "delete from club_intros where tenant_id = any($1::uuid[])", [ta, tc]
            )
            await c.execute(
                "delete from club_profiles where tenant_id = any($1::uuid[])", [ta, tc]
            )
            await c.execute(
                "delete from club_members where tenant_id = any($1::uuid[])", [ta, tc]
            )
            await c.execute("delete from leads where tenant_id = any($1::uuid[])", [ta, tc])
            await c.execute("delete from tenants where id = any($1::uuid[])", [ta, tc])
        await db.pool.close()

    print(("\nFAIL: " + ", ".join(FAILS)) if FAILS else "\nOK: club_intro_smoke")
    sys.exit(1 if FAILS else 0)


asyncio.run(main())
