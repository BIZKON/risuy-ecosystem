#!/usr/bin/env python3
"""DB-смоук create_tenant_admin: платформа заводит ПУСТОЙ тенант-кабинет (tenants-строка active +
аудит, БЕЗ admin_user/membership). Проверяет вставку, поля, аудит; чистит за собой. ТОЛЬКО risuy_dev.

Запуск: TENANT_CREATE_SMOKE_DSN="postgresql://<owner>@<host>:5432/risuy_dev?sslmode=require" \\
        PYTHONPATH=admin-panel:. ./.venv-smoke/bin/python scripts/tenant_create_db_smoke.py
"""
import asyncio
import os

os.environ.setdefault("DATABASE_URL", "postgresql://x/y")
os.environ.setdefault("SESSION_SECRET", "x" * 40)
os.environ.setdefault("ADMIN_USERNAME", "admin")
os.environ.setdefault("ADMIN_PASSWORD_HASH",
                      "$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHQ$aGFzaHN0dWI")

import asyncpg  # noqa: E402
import db  # noqa: E402  (admin-panel/db.py)

DSN = os.environ.get("TENANT_CREATE_SMOKE_DSN")
if not DSN:
    raise SystemExit("Задайте TENANT_CREATE_SMOKE_DSN на risuy_dev (throwaway-вставка + delete).")

FAILS = []
def check(n, c):  # noqa: E302
    print(f"  {'OK ' if c else 'FAIL'} {n}")
    if not c:
        FAILS.append(n)


async def main():
    db.pool = await asyncpg.create_pool(DSN, min_size=1, max_size=3)
    try:
        async with db.pool.acquire() as c:
            dbname = await c.fetchval("select current_database()")
        if dbname != "risuy_dev":
            raise SystemExit(f"ОТКАЗ: смоук только на risuy_dev, а DSN указывает на '{dbname}'.")

        slug, tid = await db.create_tenant_admin(
            "Смоук-кабинет владельца", actor="smoke-admin", ip="127.0.0.1", user_agent="smoke")
        check("create_tenant_admin вернул slug client-*", slug.startswith("client-"))
        check("вернул валидный tenant_id", bool(tid) and len(tid) == 36)

        async with db.pool.acquire() as c:
            row = await c.fetchrow("select slug, name, status from tenants where id = $1", tid)
            audit = await c.fetchval(
                "select count(*) from admin_audit where action = 'tenant_create_admin' "
                "and detail->>'tenant_id' = $1", tid)
        check("tenants-строка создана", row is not None)
        check("status = active", row and row["status"] == "active")
        check("name сохранён", row and row["name"] == "Смоук-кабинет владельца")
        check("slug в БД совпадает", row and row["slug"] == slug)
        check("аудит tenant_create_admin записан", audit == 1)

        # чистка throwaway
        async with db.pool.acquire() as c:
            async with c.transaction():
                await c.execute("delete from admin_audit where action='tenant_create_admin' and detail->>'tenant_id'=$1", tid)
                await c.execute("delete from tenants where id = $1", tid)
            gone = await c.fetchval("select count(*) from tenants where id = $1", tid)
        check("тестовый тенант удалён (чистка)", gone == 0)
    finally:
        await db.pool.close()

    print("\n" + ("ВСЕ ОК" if not FAILS else "ПРОВАЛЫ: " + ", ".join(FAILS)))
    raise SystemExit(1 if FAILS else 0)


asyncio.run(main())
