#!/usr/bin/env python3
"""БД-смоук обезличенной выгрузки на risuy_dev: stream_leads_anon (нет прямых идентификаторов,
has_notes), stream_leads_map (соответствие; отзыв → ПДн обнулены), RLS-изоляция (FORCE RLS),
guard при пустом тенанте, round-trip subject_code. Гонится как owner; на время — FORCE RLS на leads.

Запуск:
  ANON_SMOKE_DSN="postgresql://gen_user:<pw>@81.31.246.136:5432/risuy_dev?sslmode=require" \
  PYTHONPATH=admin-panel:. ./.venv-smoke/bin/python scripts/pii_anon_db_smoke.py

Требует роль-ВЛАДЕЛЬЦА (gen_user): seeding + ALTER TABLE ... FORCE ROW LEVEL SECURITY. Под panel_rw
(least-privilege) не пройдёт (нет ALTER; INSERT leads под RLS требует ctx). Поведение эквивалентно
проверено под panel_rw отдельным behavioural-прогоном + live schema-check (см. handoff).
"""
import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "admin-panel"))
os.environ.setdefault("DATABASE_URL", os.environ.get("ANON_SMOKE_DSN", "postgresql://x/y"))
os.environ.setdefault("SESSION_SECRET", "smoke-session-secret-padding-0123456789-abcdef")
os.environ.setdefault("ADMIN_USERNAME", "smoke")
os.environ.setdefault("ADMIN_PASSWORD_HASH",
                      "$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHQ$aGFzaHN0dWI")

import asyncpg  # noqa: E402
import db        # noqa: E402  (admin-panel/db.py)
from shared import anon  # noqa: E402

DSN = os.environ.get("ANON_SMOKE_DSN") or os.environ.get("DATABASE_URL")
if not DSN or "/risuy_dev" not in DSN.split("?")[0]:
    raise SystemExit("Задайте ANON_SMOKE_DSN на risuy_dev (FORCE RLS временно + delete тестовых строк).")

FAILS: list[str] = []
SLUG_A, SLUG_B = "smoke-anon-a", "smoke-anon-b"


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


async def _drop(c) -> None:
    await c.execute("delete from leads where tenant_id in (select id from tenants where slug = any($1))",
                    [SLUG_A, SLUG_B])
    await c.execute("delete from tenants where slug = any($1)", [SLUG_A, SLUG_B])


async def main() -> None:
    db.pool = await asyncpg.create_pool(DSN, min_size=1, max_size=4, setup=db._apply_tenant_guc)
    forced = False
    try:
        db.set_active_tenant(None)
        async with db.pool.acquire() as c:
            await _drop(c)
            ta = await c.fetchval("insert into tenants(slug,name,status) values($1,'A','active') returning id", SLUG_A)
            tb = await c.fetchval("insert into tenants(slug,name,status) values($1,'B','active') returning id", SLUG_B)
            # Тенант A: обычный лид (с notes-ФИО + телефон) и лид с отзывом согласия.
            la1 = await c.fetchval(
                "insert into leads(tenant_id,messenger,source,status,name,phone,tg_user_id,notes) "
                "values($1,'tg','vk','new','Иван Петров','+79001112233',990777001,"
                "'живёт на Тверской, муж Пётр') returning id", ta)
            la2 = await c.fetchval(
                "insert into leads(tenant_id,messenger,source,status,name,phone,tg_user_id,erase_requested_at) "
                "values($1,'tg','other','lost','Анна','+79005556677',990777002, now()) returning id", ta)
            # Тенант B: один лид (не должен видеться под ctx A).
            await c.execute(
                "insert into leads(tenant_id,messenger,source,status,name,tg_user_id) "
                "values($1,'tg','vk','new','Чужой',990777003)", tb)
            await c.execute("alter table leads force row level security")
            forced = True

        # ── ctx A: anon-стрим ──
        db.set_active_tenant(str(ta))
        anon_recs = [r async for r in db.stream_leads_anon(row_cap=10000)]
        check("anon: видны только 2 лида тенанта A", len(anon_recs) == 2, str(len(anon_recs)))
        keys = set(anon_recs[0].keys()) if anon_recs else set()
        for forbidden in ("name", "phone", "phone_hash", "tg_user_id", "vk_user_id",
                          "max_user_id", "max_chat_id", "web_session_id", "notes"):
            check(f"anon: нет колонки {forbidden}", forbidden not in keys)
        check("anon: есть has_notes", "has_notes" in keys)
        by_id = {str(r["id"]): r for r in anon_recs}
        check("anon: has_notes=True у лида с заметкой", by_id[str(la1)]["has_notes"] is True)

        # ── ctx A: map-стрим (соответствие + обработка отзыва) ──
        map_recs = {str(r["id"]): r for r in [m async for m in db.stream_leads_map(row_cap=10000)]}
        check("map: 2 лида A", len(map_recs) == 2, str(len(map_recs)))
        row_ok = anon.map_row(dict(map_recs[str(la1)]))
        check("map: обычный лид — имя на месте", row_ok[anon.MAP_HEADER.index("name")] == "Иван Петров")
        check("map: обычный лид — телефон на месте",
              row_ok[anon.MAP_HEADER.index("phone")] == "+79001112233")
        row_er = anon.map_row(dict(map_recs[str(la2)]))
        check("map: отзыв — имя обнулено", row_er[anon.MAP_HEADER.index("name")] == "")
        check("map: отзыв — телефон обнулён", row_er[anon.MAP_HEADER.index("phone")] == "")
        check("map: отзыв — флаг", row_er[anon.MAP_HEADER.index("erase_status")]
              == "отзыв — обезличивание в процессе")

        # ── round-trip subject_code (anon ↔ map один лид → один код) ──
        check("round-trip subject_code la1",
              anon.subject_code(la1) == row_ok[0]
              == anon.anon_row(dict(by_id[str(la1)]), set())[0])

        # ── guard: пустой тенант → стрим падает (не отдаём неопределённый набор) ──
        db.set_active_tenant(None)
        raised = False
        try:
            _ = [r async for r in db.stream_leads_anon(row_cap=10)]
        except RuntimeError:
            raised = True
        check("guard: пустой тенант → RuntimeError", raised)

    finally:
        try:
            db.set_active_tenant(None)
            async with db.pool.acquire() as c:
                if forced:
                    await c.execute("alter table leads no force row level security")
                await _drop(c)
        finally:
            await db.pool.close()

    if FAILS:
        print("\n".join("❌ " + f for f in FAILS))
        raise SystemExit(1)
    print("🟢 pii_anon_db_smoke зелёный")


if __name__ == "__main__":
    asyncio.run(main())
