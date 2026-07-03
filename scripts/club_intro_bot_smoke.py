#!/usr/bin/env python3
"""Смоук 7b-БОТ-DB: бот-side зеркала знакомств (bot-telegram/db.py::club_intro_get/
club_member_by_lead/club_member_lead_channel/club_intro_decide/club_intro_reveal).
Зеркалит club_bot_db_smoke.py (3a — bot-telegram на PYTHONPATH, env-стабы,
db.current_tenant_id.set вместо параметра tenant_id — бот резолвит СВОЙ активный тенант
через tenant_id()) и покрытие club_intro_smoke.py (7a — панельный оригинал: create/decide/
reveal, взаимное согласие, идемпотентность decide, красная линия reveal). Гоняет
КОНТРОЛЛЕР (нужен TEAM_DSN — owner-DSN risuy_dev, доступа у автора хелперов нет). На прод
НЕ запускать.

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

    mid_a = mid_b = None
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

        # ── decide(accept=True) → accepted + 2 consent_events ────────────────
        decided = await db.club_intro_decide(intro_id, True, text_hash="abc")
        check("decide(accept=True) вернул True", decided is True)

        async with db.pool.acquire() as c:
            row = await c.fetchrow(
                "select status, decided_at from club_intros where id=$1", intro_id
            )
            n_consent = await c.fetchval(
                "select count(*) from consent_events "
                "where tenant_id=$1 and doc_type='intro_accept' and member_id = any($2::uuid[])",
                ta, [mid_a, mid_b],
            )
        check("status после accept = accepted", row is not None and row["status"] == "accepted")
        check("decided_at проставлен", row is not None and row["decided_at"] is not None)
        check("ровно 2 consent_events (from_member + to_member)", n_consent == 2, f"n={n_consent}")

        # ── 🔴 reveal ПОСЛЕ accept → контакты обоих ──────────────────────────
        reveal_after = await db.club_intro_reveal(intro_id)
        check(
            "reveal ПОСЛЕ accept возвращает контакты обоих участников",
            reveal_after is not None
            and reveal_after.get("from", {}).get("display_name") == "ООО Бот-Интро-А"
            and reveal_after.get("to", {}).get("display_name") == "ООО Бот-Интро-Б",
        )
        check(
            "reveal: контакт участника несёт имя/телефон лида (JOIN leads)",
            reveal_after is not None
            and reveal_after.get("to", {}).get("lead_phone") == "+79990000002",
        )

        # ── идемпотентность: повторный decide на решённый intro → False ──────
        decided_again = await db.club_intro_decide(intro_id, True, text_hash="xyz")
        check("повторный decide на уже решённый intro → False (ничего не меняется)",
              decided_again is False)
        async with db.pool.acquire() as c:
            n_consent_2 = await c.fetchval(
                "select count(*) from consent_events "
                "where tenant_id=$1 and doc_type='intro_accept' and member_id = any($2::uuid[])",
                ta, [mid_a, mid_b],
            )
        check("повторный decide НЕ добавил новых consent_events", n_consent_2 == 2,
              f"n={n_consent_2}")

        # ── decline-путь на отдельном intro ──────────────────────────────────
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

        declined = await db.club_intro_decide(intro_id_2, False)
        check("decide(accept=False) вернул True", declined is True)

        async with db.pool.acquire() as c:
            row2 = await c.fetchrow("select status from club_intros where id=$1", intro_id_2)
        check("status после decline = declined", row2 is not None and row2["status"] == "declined")

        reveal_declined = await db.club_intro_reveal(intro_id_2)
        check("reveal на declined intro → None", reveal_declined is None)

    finally:
        async with db.pool.acquire() as c:
            # Порядок FK: consent_events → club_intros → club_profiles → club_members →
            # leads → tenants.
            member_ids = [m for m in (mid_a, mid_b) if m]
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
