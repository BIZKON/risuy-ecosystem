#!/usr/bin/env python3
"""DB-смоук применения черновика (контроллер, risuy_dev): apply_proposal пишет в
tenant_settings/products через существующие сеттеры, скоуплено на тенанта.

Запуск:
  BRIEF_APPLY_SMOKE_DSN="postgresql://gen_user:...@81.31.246.136:5432/risuy_dev?sslmode=require" \
  PYTHONPATH=admin-panel:. ./.venv-smoke/bin/python scripts/brief_apply_smoke.py
"""
import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "admin-panel"))
os.environ.setdefault("DATABASE_URL", "postgresql://x/y")
os.environ.setdefault("SESSION_SECRET", "smoke-secret")
os.environ.setdefault("ADMIN_USERNAME", "smoke")
os.environ.setdefault("ADMIN_PASSWORD_HASH", "$argon2id$v=19$m=65536,t=3,p=4$c21va2U$c21va2U")

import asyncpg  # noqa: E402
import db  # noqa: E402  (admin-panel/db.py)
import brief_apply  # noqa: E402  (admin-panel/brief_apply.py)

DSN = os.environ.get("BRIEF_APPLY_SMOKE_DSN")
if not DSN or "/risuy_dev" not in DSN.split("?")[0]:
    raise SystemExit("Задайте BRIEF_APPLY_SMOKE_DSN на risuy_dev")

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


async def _cleanup(c) -> None:
    await c.execute("delete from products where tenant_id in "
                    "(select id from tenants where slug like 'smoke-apply-%')")
    await c.execute("delete from tenant_settings where tenant_id in "
                    "(select id from tenants where slug like 'smoke-apply-%')")
    await c.execute("delete from tenant_triggers where tenant_id in "
                    "(select id from tenants where slug like 'smoke-apply-%')")
    await c.execute("delete from tenants where slug like 'smoke-apply-%'")


async def main() -> None:
    db.pool = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    try:
        db.set_active_tenant(None)
        async with db.pool.acquire() as c:
            await _cleanup(c)
            ta = await c.fetchval(
                "insert into tenants(slug,name,status) values('smoke-apply-a','A','active') returning id")
            tb = await c.fetchval(
                "insert into tenants(slug,name,status) values('smoke-apply-b','B','active') returning id")

        proposal = {
            "settings": {"funnel": {"company_name": "A-Компания", "operator_name": "ИП Тест",
                                    "operator_inn": "770000000000", "operator_email": "a@example.com"},
                         "persona": {"behavior_prompt": "Общение на «вы»"},
                         "triggers": [{"kind": "stopword", "value": "конкурент"}],
                         "channels": {"telegram": "a-agent"}},
            "products": [{"name": "Абонемент", "price": 3000, "currency": "RUB",
                          "caption": "30 чашек", "kind": "main"}],
            "recommendations": [{"title": "Проверьте черновик", "why": "собрано автоматически"}],
            "gaps": []}

        print("1. apply секций funnel+products+triggers+channels:")
        res = await brief_apply.apply_proposal(
            ta, proposal, ["funnel", "products", "triggers", "channels"],
            actor="smoke", ip=None, user_agent=None)
        check("нет ошибок применения", not res.get("errors"), str(res.get("errors")))
        check("секции отмечены", set(res["sections"]) >= {"funnel", "products", "triggers", "channels"},
              str(res.get("sections")))

        print("2. funnel/products/triggers/channels записаны в тенанта А:")
        db.set_active_tenant(ta)
        async with db.pool.acquire() as c:
            await c.execute("select set_config('app.tenant_id', $1, true)", str(ta))
            cn = await c.fetchval(
                "select value from tenant_settings where tenant_id=$1 and key='company_name'", ta)
            nprod = await c.fetchval("select count(*) from products where tenant_id=$1", ta)
            ntrig = await c.fetchval("select count(*) from tenant_triggers where tenant_id=$1", ta)
            chan = await c.fetchval(
                "select value from tenant_settings where tenant_id=$1 and key='agent_for_channel__telegram'", ta)
        check("company_name сохранён", cn == "A-Компания", f"cn={cn}")
        check("продукт создан", nprod == 1, f"n={nprod}")
        check("триггер создан", ntrig == 1, f"n={ntrig}")
        check("канал привязан", chan == "a-agent", f"chan={chan}")

        print("3. персона НЕ применяется автоматически (нет секции в apply):")
        res2 = await brief_apply.apply_proposal(
            ta, proposal, ["persona"], actor="smoke", ip=None, user_agent=None)
        check("секция persona не поддерживается — ничего не применено",
              res2["sections"] == [] and res2["errors"] == [], str(res2))

        print("4. скоуп на тенанта — применение к Б не трогает А:")
        proposal_b = {"settings": {"funnel": {"company_name": "B-Компания",
                                              "operator_name": "ИП Б", "operator_inn": "770000000001",
                                              "operator_email": "b@example.com"},
                                    "persona": {}, "triggers": [], "channels": {}},
                     "products": [], "recommendations": [], "gaps": []}
        await brief_apply.apply_proposal(tb, proposal_b, ["funnel"], actor="smoke", ip=None, user_agent=None)
        db.set_active_tenant(ta)
        async with db.pool.acquire() as c:
            await c.execute("select set_config('app.tenant_id', $1, true)", str(ta))
            cn_a_after = await c.fetchval(
                "select value from tenant_settings where tenant_id=$1 and key='company_name'", ta)
        check("тенант А не изменился после apply на Б", cn_a_after == "A-Компания", f"cn={cn_a_after}")

        print("5. ошибка одной секции не рушит остальные (funnel_enabled=1 + невалидный ИНН):")
        bad_proposal = {"settings": {"funnel": {"funnel_enabled": "1", "operator_inn": "не-инн"},
                                     "persona": {}, "triggers": [], "channels": {}},
                        "products": [{"name": "Продукт-2", "price": 500, "currency": "RUB",
                                     "caption": "", "kind": "main"}],
                        "recommendations": [], "gaps": []}
        res3 = await brief_apply.apply_proposal(
            ta, bad_proposal, ["funnel", "products"], actor="smoke", ip=None, user_agent=None)
        check("funnel вернул ошибку (невалидный ИНН)", any("funnel" in e for e in res3["errors"]),
              str(res3))
        check("products применились несмотря на ошибку funnel", "products" in res3["sections"],
              str(res3))
        async with db.pool.acquire() as c:
            await c.execute("select set_config('app.tenant_id', $1, true)", str(ta))
            nprod2 = await c.fetchval("select count(*) from products where tenant_id=$1", ta)
        check("второй продукт создан (products не зависит от funnel)", nprod2 == 2, f"n={nprod2}")

    finally:
        db.set_active_tenant(None)
        async with db.pool.acquire() as c:
            await _cleanup(c)
        await db.pool.close()

    print()
    if FAILS:
        print("❌ ПРОВАЛЫ: " + ", ".join(FAILS))
        sys.exit(1)
    print("✅ brief_apply smoke — OK")


if __name__ == "__main__":
    asyncio.run(main())
