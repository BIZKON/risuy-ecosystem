#!/usr/bin/env python3
"""DB-смоук tenant_brief (гонит КОНТРОЛЛЕР на risuy_dev).
Проверяет жизненный цикл: create → get_by_token → submit → set_proposal →
mark_applied, и что чужой токен не резолвится. Панельные функции (создание/центр
управления брифом) — из admin-panel/db.py; get_brief_by_token/submit_brief живут
ТОЛЬКО в bot-telegram/db.py (публичную форму /brief/{token} обслуживает бот), поэтому
их берём из бот-модуля, загруженного отдельно с общим пулом. Обе роли — owner-DSN.

Запуск:
  BRIEF_SMOKE_DSN="postgresql://gen_user:...@81.31.246.136:5432/risuy_dev?sslmode=require" \
  PYTHONPATH=admin-panel:. ./.venv-smoke/bin/python scripts/brief_db_smoke.py
"""
import asyncio
import importlib.util
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "admin-panel"))
os.environ.setdefault("DATABASE_URL", "postgresql://x/y")
os.environ.setdefault("SESSION_SECRET", "smoke-secret-padding-0123456789abcdef")
os.environ.setdefault("ADMIN_USERNAME", "smoke")
os.environ.setdefault("ADMIN_PASSWORD_HASH", "$argon2id$v=19$m=65536,t=3,p=4$c21va2U$c21va2U")
# bot-telegram/config.py требует эти env при импорте (get_brief_by_token/submit_brief — ботовые).
for _k, _v in (("BOT_TOKEN", "smoke:token"), ("CHANNEL_ID", "-100"),
               ("CHANNEL_URL", "https://t.me/smoke"), ("GUIDE_URL", "https://example.com")):
    os.environ.setdefault(_k, _v)

import asyncpg  # noqa: E402
import db  # noqa: E402  (admin-panel/db.py)


def _load_botdb():
    """Загружает bot-telegram/db.py как отдельный модуль `botdb` (имя db уже занято панелью)."""
    bt = os.path.join(ROOT, "bot-telegram")
    if bt not in sys.path:
        sys.path.insert(0, bt)  # для его `import config`/`import shared`
    spec = importlib.util.spec_from_file_location("botdb", os.path.join(bt, "db.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["botdb"] = mod
    spec.loader.exec_module(mod)
    return mod


botdb = _load_botdb()

DSN = os.environ.get("BRIEF_SMOKE_DSN")
if not DSN or "/risuy_dev" not in DSN.split("?")[0]:
    raise SystemExit("Задайте BRIEF_SMOKE_DSN на risuy_dev")

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


async def _cleanup(c) -> None:
    await c.execute("delete from tenant_brief where tenant_id in "
                    "(select id from tenants where slug like 'smoke-brief-%')")
    await c.execute("delete from tenants where slug like 'smoke-brief-%'")


async def main() -> None:
    db.pool = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    botdb.pool = db.pool  # общий пул: ботовые get_brief_by_token/submit_brief на том же owner-DSN
    try:
        db.set_active_tenant(None)
        async with db.pool.acquire() as c:
            await _cleanup(c)
            ta = await c.fetchval(
                "insert into tenants(slug,name,status) values('smoke-brief-a','Клиент А','active') returning id")

        print("1. create_tenant_brief:")
        brief_id, token = await db.create_tenant_brief(
            ta, actor="smoke", ip=None, user_agent=None, ttl_days=30)
        check("вернул id и token", bool(brief_id) and len(token) >= 16)

        print("2. get_brief_by_token резолвит (ботовая функция):")
        got = await botdb.get_brief_by_token(token)
        check("токен резолвится в тенанта А", got is not None and str(got["tenant_id"]) == str(ta))
        check("статус pending", got and got["status"] == "pending")

        print("3. get_brief_by_token на мусорный токен → None:")
        check("чужой токен None", await botdb.get_brief_by_token("нет-такого-токена") is None)

        print("4. submit_brief (ботовая функция):")
        res = await botdb.submit_brief(token, {"version": 1, "business": {"company_name": "А"}})
        check("submit ok", res == "ok")
        again = await botdb.submit_brief(token, {"version": 1})
        check("повторный submit → already", again == "already")
        got2 = await db.get_tenant_brief(brief_id)
        check("статус submitted", got2 and got2["status"] == "submitted")
        check("answers сохранены", got2 and got2["answers"].get("business", {}).get("company_name") == "А")

        print("5. set_brief_proposal → proposed:")
        await db.set_brief_proposal(brief_id, {"settings": {}, "products": [],
                                               "recommendations": [], "gaps": []})
        got3 = await db.get_tenant_brief(brief_id)
        check("статус proposed", got3 and got3["status"] == "proposed")

        print("6. mark_brief_applied → applied:")
        await db.mark_brief_applied(brief_id, {"sections": ["funnel"]},
                                    actor="smoke", ip=None, user_agent=None)
        got4 = await db.get_tenant_brief(brief_id)
        check("статус applied", got4 and got4["status"] == "applied")

        print("7. list_tenant_briefs содержит наш бриф:")
        lst = await db.list_tenant_briefs()
        check("бриф в списке", any(str(b["id"]) == str(brief_id) for b in lst))

    finally:
        async with db.pool.acquire() as c:
            await _cleanup(c)
        await db.pool.close()

    print()
    if FAILS:
        print("❌ ПРОВАЛЫ: " + ", ".join(FAILS))
        sys.exit(1)
    print("✅ brief_db smoke — OK")


if __name__ == "__main__":
    asyncio.run(main())
