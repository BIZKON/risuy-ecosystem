#!/usr/bin/env python3
"""Смоук db-ядра знакомств на risuy_dev: create / ДВУСТОРОННИЙ accept_side / decline /
reveal. Красная линия (152-ФЗ): каждая сторона принимает СВОИМ действием, контакты
раскрываются ТОЛЬКО когда ОБЕ приняли (status='accepted'). Покрыто: accept одной стороны
(status ЕЩЁ requested, reveal None), accept второй (both=True, 2 consent, reveal обоих),
идемпотентность (already, дубль consent не пишется), not_party (не участник), decline
(один decline убивает intro).
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

    mid_a = mid_b = mid_c = mid_np = None
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

        # ── 4a. accept_side(from) → status ЕЩЁ requested, 1 consent, reveal None ─
        r_from = await db.club_intro_accept_side(intro_id, mid_a, ta, text_hash="hash-from")
        check("accept_side(from) → ok/both=False/side=from",
              r_from == {"ok": True, "both": False, "side": "from"}, f"got={r_from}")

        async with db.pool.acquire() as c:
            row_a = await c.fetchrow(
                "select status, from_accepted_at, to_accepted_at from club_intros where id=$1",
                intro_id,
            )
            n_from = await c.fetchval(
                "select count(*) from consent_events "
                "where tenant_id=$1 and doc_type='intro_accept' and member_id=$2",
                ta, mid_a,
            )
        check("после accept(from): status ЕЩЁ requested",
              row_a is not None and row_a["status"] == "requested")
        check("после accept(from): from_accepted_at проставлен",
              row_a is not None and row_a["from_accepted_at"] is not None)
        check("после accept(from): to_accepted_at ещё NULL",
              row_a is not None and row_a["to_accepted_at"] is None)
        check("после accept(from): ровно 1 consent_event (member=from)", n_from == 1, f"n={n_from}")

        reveal_mid = await db.club_intro_reveal(intro_id, ta)
        check("reveal при одном accept → None (оба ещё не приняли!)", reveal_mid is None)

        # ── 4b. accept_side(to) → both=True, status accepted, 2 consent ──────
        r_to = await db.club_intro_accept_side(intro_id, mid_b, ta, text_hash="hash-to")
        check("accept_side(to) → ok/both=True/side=to",
              r_to == {"ok": True, "both": True, "side": "to"}, f"got={r_to}")

        async with db.pool.acquire() as c:
            row2 = await c.fetchrow(
                "select status, decided_at, to_accepted_at from club_intros where id=$1", intro_id
            )
            n_consent = await c.fetchval(
                "select count(*) from consent_events "
                "where tenant_id=$1 and doc_type='intro_accept' and member_id = any($2::uuid[])",
                ta, [mid_a, mid_b],
            )
        check("после accept(to): status = accepted", row2 is not None and row2["status"] == "accepted")
        check("после accept(to): decided_at проставлен", row2 is not None and row2["decided_at"] is not None)
        check("после accept(to): to_accepted_at проставлен",
              row2 is not None and row2["to_accepted_at"] is not None)
        check("ровно 2 consent_events (from + to)", n_consent == 2, f"n={n_consent}")

        # ── 5. reveal ПОСЛЕ обоих accept → непустые контакты обоих участников ──
        reveal_after = await db.club_intro_reveal(intro_id, ta)
        check(
            "reveal ПОСЛЕ обоих accept возвращает контакты обоих участников",
            reveal_after is not None
            and reveal_after.get("from", {}).get("display_name") == "ООО Тест-А"
            and reveal_after.get("to", {}).get("display_name") == "ООО Тест-Б",
        )

        # ── 6. идемпотентность: повторный accept_side(from) → both=True, без дубля ─
        r_from_again = await db.club_intro_accept_side(intro_id, mid_a, ta, text_hash="hash-dup")
        check("повторный accept_side(from) на accepted intro → not_open (уже решён)",
              r_from_again.get("ok") is False and r_from_again.get("reason") == "not_open",
              f"got={r_from_again}")
        async with db.pool.acquire() as c:
            n_consent_2 = await c.fetchval(
                "select count(*) from consent_events "
                "where tenant_id=$1 and doc_type='intro_accept' and member_id = any($2::uuid[])",
                ta, [mid_a, mid_b],
            )
        check("повторный accept НЕ добавил новых consent_events", n_consent_2 == 2, f"n={n_consent_2}")

        # ── 6b. accept_side чужого члена (не from/to) → not_party ─────────────
        intro_np = await db.club_intro_create(ta, mid_a, mid_b, message="Для проверки not_party")
        # mid_c — участник ДРУГОГО тенанта; для чистоты проверяем на не-участнике этого intro.
        # Возьмём mid_b как to и mid_a как from — сторонним будет любой третий член того же
        # тенанта; создадим его.
        mid_np = await db.club_member_create(ta, display_name="ООО Тест-НП", city="Тула", okved="70.22")
        r_np = await db.club_intro_accept_side(intro_np, mid_np, ta, text_hash="hash-np")
        check("accept_side(не участник) → not_party",
              r_np.get("ok") is False and r_np.get("reason") == "not_party", f"got={r_np}")
        await db.club_intro_decline(intro_np, mid_a, ta)  # прибираем этот intro

        # ── 7. идемпотентность одной стороны на ОТКРЫТОМ intro (already) ──────
        intro_idem = await db.club_intro_create(ta, mid_a, mid_b, message="Idem-проверка")
        await db.club_intro_accept_side(intro_idem, mid_a, ta, text_hash="idem-1")
        r_idem = await db.club_intro_accept_side(intro_idem, mid_a, ta, text_hash="idem-2")
        check("повторный accept той же стороны на открытом intro → already, both=False",
              r_idem == {"ok": True, "both": False, "side": "from", "already": True},
              f"got={r_idem}")
        async with db.pool.acquire() as c:
            n_idem = await c.fetchval(
                "select count(*) from consent_events "
                "where tenant_id=$1 and doc_type='intro_accept' and member_id=$2 "
                "and text_hash like 'idem-%'",
                ta, mid_a,
            )
        check("idem-accept: ровно 1 consent (дубль не записан)", n_idem == 1, f"n={n_idem}")
        await db.club_intro_decline(intro_idem, mid_b, ta)  # прибираем

        # ── 8. decline-путь: accept(from) → decline(to) → declined, reveal None ─
        intro_id_2 = await db.club_intro_create(ta, mid_b, mid_a, message="Второе знакомство")
        check("создан второй intro для decline-пути", intro_id_2 is not None)

        r2_from = await db.club_intro_accept_side(intro_id_2, mid_b, ta, text_hash="d-from")
        check("accept_side(from) на decline-intro → ok/both=False",
              r2_from == {"ok": True, "both": False, "side": "from"}, f"got={r2_from}")

        declined = await db.club_intro_decline(intro_id_2, mid_a, ta)
        check("decline(to) вернул True", declined is True)

        async with db.pool.acquire() as c:
            row3 = await c.fetchrow("select status from club_intros where id=$1", intro_id_2)
        check("status после decline = declined", row3 is not None and row3["status"] == "declined")

        reveal_declined = await db.club_intro_reveal(intro_id_2, ta)
        check("reveal на declined intro (был 1 accept) → None", reveal_declined is None)

        # ── список ──────────────────────────────────────────────────────────
        intros = await db.club_intro_list(ta)
        check("club_intro_list содержит оба intro тенанта",
              {str(i["id"]) for i in intros} >= {intro_id, intro_id_2})

    finally:
        async with db.pool.acquire() as c:
            # Порядок FK: consent_events (по member_id/tenant) → club_intros →
            # club_profiles → club_members → leads → tenants.
            member_ids = [m for m in (mid_a, mid_b, mid_c, mid_np) if m]
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
