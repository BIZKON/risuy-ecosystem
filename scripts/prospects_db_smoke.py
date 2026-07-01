#!/usr/bin/env python3
"""RLS-смоук prospects на risuy_dev: изоляция A≠B, unique(tenant_id,inn),
lead_id→set null, отсутствие полей-контактов. Пишет/чистит тестовые строки.
  PROSPECTS_SMOKE_DSN="postgresql://gen_user:...@81.31.246.136:5432/risuy_dev?sslmode=require" \
  PYTHONPATH=. ./.venv-smoke/bin/python scripts/prospects_db_smoke.py"""
import asyncio, os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "admin-panel"))
os.environ.setdefault("DATABASE_URL", "postgresql://x/y")
os.environ.setdefault("SESSION_SECRET", "smoke-secret-only-for-import-aaaaaaaa")
os.environ.setdefault("ADMIN_USERNAME", "smoke")
os.environ.setdefault("ADMIN_PASSWORD_HASH", "$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHQ$aGFzaHN0dWI")

import asyncpg
import db
import dadata

DSN = os.environ.get("PROSPECTS_SMOKE_DSN")
if not DSN or "/risuy_dev" not in DSN.split("?")[0]:
    raise SystemExit("Задайте PROSPECTS_SMOKE_DSN на risuy_dev (тест пишет/чистит строки).")

FAILS = []
def check(name, cond, detail=""):
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)

CARD_A = dadata.ProspectCard(inn="7707083893", subject_type="legal", name_short="ООО А",
                             city="Москва", status="ACTIVE", raw={"inn": "7707083893"})
CARD_B = dadata.ProspectCard(inn="7707083893", subject_type="legal", name_short="ООО Б (тенант B)",
                             city="Казань", status="ACTIVE", raw={"inn": "7707083893"})


async def main():
    db.pool = await asyncpg.create_pool(DSN, min_size=1, max_size=4, setup=db._apply_tenant_guc)
    async with db.pool.acquire() as c:
        ta = await c.fetchval("insert into tenants (slug, name, status) values "
                              "('smoke-prospect-a-'||substr(md5(random()::text),1,8),'SMOKE A','active') returning id")
        tb = await c.fetchval("insert into tenants (slug, name, status) values "
                              "('smoke-prospect-b-'||substr(md5(random()::text),1,8),'SMOKE B','active') returning id")
        lead_a = await c.fetchval("insert into leads (tenant_id, name, consent) values ($1,'Лид A',true) returning id", ta)
        lead_b = await c.fetchval("insert into leads (tenant_id, name, consent) values ($1,'Лид B',true) returning id", tb)
    try:
        db.set_active_tenant(ta)
        pid_a = await db.prospect_upsert(card=CARD_A, tenant_id=ta, actor="smoke", ip=None,
                                         user_agent=None, lead_id=lead_a)
        rows_a = await db.prospect_list()
        check("A видит свою карточку", len(rows_a) == 1 and rows_a[0]["inn"] == "7707083893")
        p_for_lead = await db.prospect_for_lead(lead_a)
        check("A: карточка привязана к лиду", p_for_lead is not None and str(p_for_lead["id"]) == pid_a)

        db.set_active_tenant(tb)
        pid_b = await db.prospect_upsert(card=CARD_B, tenant_id=tb, actor="smoke", ip=None, user_agent=None)
        rows_b = await db.prospect_list()
        check("B видит ТОЛЬКО свою карточку", len(rows_b) == 1 and rows_b[0]["name_short"] == "ООО Б (тенант B)")
        check("A≠B: разные id для того же ИНН", pid_a != pid_b)

        db.set_active_tenant(ta)
        got_b = await db.prospect_get(pid_b)
        check("A НЕ видит карточку B (RLS)", got_b is None)

        # CRITICAL-2 фикс: чужой лид (тенант B) НЕ привязывается к карточке тенанта A (tenant-scoped подзапрос → NULL)
        pid_x = await db.prospect_upsert(
            card=dadata.ProspectCard(inn="7800000000", subject_type="legal", name_short="ООО X",
                                     raw={"inn": "7800000000"}),
            tenant_id=ta, actor="smoke", ip=None, user_agent=None, lead_id=lead_b)
        px = await db.prospect_get(pid_x)
        check("чужой лид тенанта B НЕ привязан к карточке тенанта A (lead_id=NULL)",
              px is not None and px["lead_id"] is None)

        async with db.pool.acquire() as c:
            await c.execute("delete from leads where id=$1", lead_a)
        again = await db.prospect_get(pid_a)
        check("удаление лида → prospect.lead_id = NULL (карточка жива)", again is not None and again["lead_id"] is None)

        async with db.pool.acquire() as c:
            cols = {r["column_name"] for r in await c.fetch(
                "select column_name from information_schema.columns where table_name='prospects'")}
        check("нет колонок под телефоны/email/паспорт", not (cols & {"phones", "emails", "phone", "passport"}))
    finally:
        async with db.pool.acquire() as c:
            await c.execute("delete from prospects where tenant_id = any($1::uuid[])", [ta, tb])
            await c.execute("delete from tenants where id = any($1::uuid[])", [ta, tb])
        await db.pool.close()
    print(("\nFAIL: " + ", ".join(FAILS)) if FAILS else "\nВсе проверки prospects OK")
    sys.exit(1 if FAILS else 0)

asyncio.run(main())
