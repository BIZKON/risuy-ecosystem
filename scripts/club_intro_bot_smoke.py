#!/usr/bin/env python3
"""Смоук 7b-БОТ-DB: бот-side зеркала знакомств (bot-telegram/db.py::club_intro_get/
club_member_by_lead/club_member_lead_channel/club_intro_accept_side/club_intro_decline/
club_intro_reveal). Зеркалит club_bot_db_smoke.py (3a — bot-telegram на PYTHONPATH,
env-стабы, db.current_tenant_id.set вместо параметра tenant_id — бот резолвит СВОЙ активный
тенант через tenant_id()) и покрытие club_intro_smoke.py (панельный оригинал). ДВУСТОРОННИЙ
accept: каждая сторона принимает своим действием, контакты раскрываются только когда ОБЕ
приняли; покрыто accept(from)→reveal None, accept(to)→both/reveal обоих, идемпотентность,
not_party, decline. Гоняет КОНТРОЛЛЕР (нужен TEAM_DSN — owner-DSN risuy_dev, доступа у
автора хелперов нет). На прод НЕ запускать.

Запуск:
  TEAM_DSN="postgresql://gen_user:...@81.31.246.136:5432/risuy_dev?sslmode=require" \
  PYTHONPATH=bot-telegram:. ./.venv-smoke/bin/python scripts/club_intro_bot_smoke.py

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
            "('smoke-intro-bot-'||substr(md5(random()::text),1,8),'SMOKE INTRO BOT','active') "
            "returning id"
        )

    mid_a = mid_b = mid_np = None
    lead_a = lead_b = None
    intro_id = intro_id_2 = None
    try:
        # Бот резолвит СВОЙ единственный активный тенант через tenant_id() — контекст
        # выставляем так же, как это делает мультиплекс-мидлварь (_TenantContextMiddleware).
        db.current_tenant_id.set(ta)

        # ── подготовка: 2 лида (с tg_user_id — для каналов доставки) + 2 члена клуба ──
        async with db.pool.acquire() as c:
            lead_a = await c.fetchval(
                "insert into leads (tenant_id, tg_user_id, source, name, phone) "
                "values ($1, 900001, 'smoke', 'Лид А', '+79990000001') returning id",
                ta,
            )
            lead_b = await c.fetchval(
                "insert into leads (tenant_id, tg_user_id, source, name, phone) "
                "values ($1, 900002, 'smoke', 'Лид Б', '+79990000002') returning id",
                ta,
            )

        mid_a = await db.club_member_create(
            display_name="ООО Бот-Интро-А", city="Москва", okved="62.01", lead_id=lead_a,
        )
        mid_b = await db.club_member_create(
            display_name="ООО Бот-Интро-Б", city="Казань", okved="62.02", lead_id=lead_b,
        )

        # intro вставляем напрямую (raw SQL) — club_intro_create живёт только в панели (7a),
        # бот-side зеркало по задаче 7b-БОТ-DB создание не покрывает.
        async with db.pool.acquire() as c:
            intro_id = await c.fetchval(
                """
                insert into club_intros (tenant_id, from_member, to_member, to_tenant_id,
                    status, message)
                values ($1, $2, $3, $1, 'requested', 'Познакомим?')
                returning id
                """,
                ta, mid_a, mid_b,
            )

        # ── club_intro_get ────────────────────────────────────────────────────
        got = await db.club_intro_get(intro_id)
        check("club_intro_get вернул строку", got is not None)
        check("club_intro_get: status='requested'", got is not None and got["status"] == "requested")
        check("club_intro_get: to_member=B", got is not None and str(got["to_member"]) == str(mid_b))
        check("club_intro_get: from_accepted_at NULL", got is not None and got["from_accepted_at"] is None)
        check("club_intro_get: to_accepted_at NULL", got is not None and got["to_accepted_at"] is None)

        # ── club_member_by_lead ──────────────────────────────────────────────
        member_by_lead = await db.club_member_by_lead(lead_b)
        check("club_member_by_lead(lead_b) вернул member B",
              member_by_lead is not None and str(member_by_lead["id"]) == str(mid_b))

        # ── club_member_lead_channel ─────────────────────────────────────────
        channel_b = await db.club_member_lead_channel(mid_b)
        check("club_member_lead_channel(B) вернул tg_user_id лида B", channel_b == 900002,
              f"got={channel_b}")

        # ── 🔴 reveal ДО accept → None ────────────────────────────────────────
        reveal_before = await db.club_intro_reveal(intro_id)
        check("reveal ДО accept возвращает None (контакты НЕ раскрыты)", reveal_before is None)

        # ── accept_side(from) → status ЕЩЁ requested, 1 consent, reveal None ──
        r_from = await db.club_intro_accept_side(intro_id, mid_a, text_hash="hash-from")
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

        reveal_mid = await db.club_intro_reveal(intro_id)
        check("reveal при одном accept → None (оба ещё не приняли!)", reveal_mid is None)

        # ── accept_side(to) → both=True, status accepted, 2 consent ──────────
        r_to = await db.club_intro_accept_side(intro_id, mid_b, text_hash="hash-to")
        check("accept_side(to) → ok/both=True/side=to",
              r_to == {"ok": True, "both": True, "side": "to"}, f"got={r_to}")

        async with db.pool.acquire() as c:
            row = await c.fetchrow(
                "select status, decided_at, to_accepted_at from club_intros where id=$1", intro_id
            )
            n_consent = await c.fetchval(
                "select count(*) from consent_events "
                "where tenant_id=$1 and doc_type='intro_accept' and member_id = any($2::uuid[])",
                ta, [mid_a, mid_b],
            )
        check("после accept(to): status = accepted", row is not None and row["status"] == "accepted")
        check("после accept(to): decided_at проставлен", row is not None and row["decided_at"] is not None)
        check("после accept(to): to_accepted_at проставлен",
              row is not None and row["to_accepted_at"] is not None)
        check("ровно 2 consent_events (from + to)", n_consent == 2, f"n={n_consent}")

        # ── 🔴 reveal ПОСЛЕ обоих accept → контакты обоих ────────────────────
        reveal_after = await db.club_intro_reveal(intro_id)
        check(
            "reveal ПОСЛЕ обоих accept возвращает контакты обоих участников",
            reveal_after is not None
            and reveal_after.get("from", {}).get("display_name") == "ООО Бот-Интро-А"
            and reveal_after.get("to", {}).get("display_name") == "ООО Бот-Интро-Б",
        )
        check(
            "reveal: контакт участника несёт имя/телефон лида (JOIN leads)",
            reveal_after is not None
            and reveal_after.get("to", {}).get("lead_phone") == "+79990000002",
        )

        # ── идемпотентность: повторный accept_side на решённый intro → not_open ─
        r_again = await db.club_intro_accept_side(intro_id, mid_a, text_hash="xyz")
        check("повторный accept_side на accepted intro → not_open",
              r_again.get("ok") is False and r_again.get("reason") == "not_open", f"got={r_again}")
        async with db.pool.acquire() as c:
            n_consent_2 = await c.fetchval(
                "select count(*) from consent_events "
                "where tenant_id=$1 and doc_type='intro_accept' and member_id = any($2::uuid[])",
                ta, [mid_a, mid_b],
            )
        check("повторный accept НЕ добавил новых consent_events", n_consent_2 == 2,
              f"n={n_consent_2}")

        # ── accept_side чужого члена (не from/to) → not_party ────────────────
        async with db.pool.acquire() as c:
            intro_np = await c.fetchval(
                """
                insert into club_intros (tenant_id, from_member, to_member, to_tenant_id,
                    status, message)
                values ($1, $2, $3, $1, 'requested', 'not_party')
                returning id
                """,
                ta, mid_a, mid_b,
            )
        # mid_np — третий член того же тенанта, не участник intro_np
        mid_np = await db.club_member_create(
            display_name="ООО Бот-Интро-НП", city="Тула", okved="70.22",
        )
        r_np = await db.club_intro_accept_side(intro_np, mid_np, text_hash="np")
        check("accept_side(не участник) → not_party",
              r_np.get("ok") is False and r_np.get("reason") == "not_party", f"got={r_np}")

        # ── идемпотентность одной стороны на ОТКРЫТОМ intro (already) ─────────
        async with db.pool.acquire() as c:
            intro_idem = await c.fetchval(
                """
                insert into club_intros (tenant_id, from_member, to_member, to_tenant_id,
                    status, message)
                values ($1, $2, $3, $1, 'requested', 'idem')
                returning id
                """,
                ta, mid_a, mid_b,
            )
        await db.club_intro_accept_side(intro_idem, mid_a, text_hash="idem-1")
        r_idem = await db.club_intro_accept_side(intro_idem, mid_a, text_hash="idem-2")
        check("повторный accept той же стороны на открытом intro → already, both=False",
              r_idem == {"ok": True, "both": False, "side": "from", "already": True},
              f"got={r_idem}")

        # ── decline-путь: accept(from) → decline(to) → declined, reveal None ─
        async with db.pool.acquire() as c:
            intro_id_2 = await c.fetchval(
                """
                insert into club_intros (tenant_id, from_member, to_member, to_tenant_id,
                    status, message)
                values ($1, $2, $3, $1, 'requested', 'Второе знакомство')
                returning id
                """,
                ta, mid_b, mid_a,
            )

        r2_from = await db.club_intro_accept_side(intro_id_2, mid_b, text_hash="d-from")
        check("accept_side(from) на decline-intro → ok/both=False",
              r2_from == {"ok": True, "both": False, "side": "from"}, f"got={r2_from}")

        declined = await db.club_intro_decline(intro_id_2, mid_a)
        check("decline(to) вернул True", declined is True)

        async with db.pool.acquire() as c:
            row2 = await c.fetchrow("select status from club_intros where id=$1", intro_id_2)
        check("status после decline = declined", row2 is not None and row2["status"] == "declined")

        reveal_declined = await db.club_intro_reveal(intro_id_2)
        check("reveal на declined intro (был 1 accept) → None", reveal_declined is None)

    finally:
        async with db.pool.acquire() as c:
            # Порядок FK: consent_events → club_intros → club_profiles → club_members →
            # leads → tenants.
            member_ids = [m for m in (mid_a, mid_b, mid_np) if m]
            if member_ids:
                await c.execute(
                    "delete from consent_events where member_id = any($1::uuid[])", member_ids
                )
            await c.execute("delete from consent_events where tenant_id = $1::uuid", ta)
            await c.execute("delete from club_intros where tenant_id = $1::uuid", ta)
            await c.execute("delete from club_profiles where tenant_id = $1::uuid", ta)
            await c.execute("delete from club_members where tenant_id = $1::uuid", ta)
            await c.execute("delete from leads where tenant_id = $1::uuid", ta)
            await c.execute("delete from tenants where id = $1::uuid", ta)
        await db.pool.close()

    print()
    if FAILS:
        print(f"❌ ПРОВАЛЫ ({len(FAILS)}): " + ", ".join(FAILS))
        sys.exit(1)
    print("✅ club_intro_bot_smoke — все проверки зелёные")


if __name__ == "__main__":
    asyncio.run(main())
