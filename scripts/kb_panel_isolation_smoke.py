#!/usr/bin/env python3
"""DB-смоук СП-2b: изоляция базы знаний В ПАНЕЛИ через РЕАЛЬНЫЕ функции (kb_insert/list/delete).
Клиент B не видит и не удаляет документ клиента A. Тестирует in-query tenant-backstop (явный
where tenant_id) ПЛЮС цепочку set_active_tenant→pool-хук _apply_tenant_guc. Под gen_user (owner
обходит RLS) → изоляцию здесь держит именно backstop из кода: его поломка/удаление = провал теста.
RLS — дополнительный первый слой для panel_rw (подтверждён ревью migrate_rls_orders_kb_broadcasts.sql).

  TEAM_DSN="postgresql://gen_user:<pw>@<host>:5432/risuy_dev?sslmode=require" \
  PYTHONPATH=admin-panel:. ./.venv-smoke/bin/python scripts/kb_panel_isolation_smoke.py
"""
import asyncio
import os
import secrets
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)                       # пакет shared (импортится из db)
sys.path.insert(0, os.path.join(ROOT, "admin-panel"))

DSN = os.environ.get("TEAM_DSN") or os.environ.get("DATABASE_URL", "")
assert DSN and "/risuy_dev" in DSN.split("?")[0], "только risuy_dev (owner-DSN от владельца)"

# Заглушки config панели (как в funnel_panel_smoke) — до импорта db.
os.environ.setdefault("DATABASE_URL", DSN)
os.environ.setdefault("SESSION_SECRET", secrets.token_urlsafe(48))
os.environ.setdefault("ADMIN_USERNAME", "smoke")
os.environ.setdefault(
    "ADMIN_PASSWORD_HASH",
    "$argon2id$v=19$m=65536,t=3,p=4$c21va2VzbW9rZQ$c21va2VzbW9rZXNtb2tl",
)

import db  # noqa: E402  (admin-panel на PYTHONPATH)

FAILS: list[str] = []


def check(name, cond):
    print(f"  {'OK ' if cond else 'FAIL'} {name}")
    if not cond:
        FAILS.append(name)


EMB = [0.01] * 768


def _titles(rows):
    return [r["title"] for r in rows]


async def main():
    await db.init()
    async with db.pool.acquire() as c:
        ta = await c.fetchval("insert into tenants(slug,name,status) values('kbui-a','A','active') returning id")
        tb = await c.fetchval("insert into tenants(slug,name,status) values('kbui-b','B','active') returning id")
    try:
        # A пишет свой документ под своим scope
        db.set_active_tenant(str(ta))
        await db.kb_insert_document(
            title="A-doc", source="a.txt", role_tag="", content="факт A",
            chunks=["ФАКТ-A"], embeddings=[EMB], tenant_id=ta,
            actor="smoke", ip=None, user_agent=None)
        # B пишет свой документ под своим scope
        db.set_active_tenant(str(tb))
        await db.kb_insert_document(
            title="B-doc", source="b.txt", role_tag="", content="факт B",
            chunks=["ФАКТ-B"], embeddings=[EMB], tenant_id=tb,
            actor="smoke", ip=None, user_agent=None)

        # A видит только A-doc
        db.set_active_tenant(str(ta))
        a_rows = await db.kb_list_documents()
        check("A видит свой A-doc", "A-doc" in _titles(a_rows))
        check("A НЕ видит B-doc (backstop list)", "B-doc" not in _titles(a_rows))
        a_id = next((r["id"] for r in a_rows if r["title"] == "A-doc"), None)
        check("A-doc id получен", a_id is not None)

        # B видит только B-doc
        db.set_active_tenant(str(tb))
        b_rows = await db.kb_list_documents()
        check("B видит свой B-doc", "B-doc" in _titles(b_rows))
        check("B НЕ видит A-doc (backstop list)", "A-doc" not in _titles(b_rows))

        # B пытается удалить документ A по id — отказ (backstop delete)
        deleted = await db.kb_delete_document(str(a_id), actor="smoke", ip=None, user_agent=None)
        check("B НЕ удаляет A-doc (kb_delete_document → False)", deleted is False)

        # A-doc пережил попытку удаления B
        db.set_active_tenant(str(ta))
        check("A-doc пережил удаление B", "A-doc" in _titles(await db.kb_list_documents()))
    finally:
        db.set_active_tenant(None)
        async with db.pool.acquire() as c:
            await c.execute("delete from tenants where slug in ('kbui-a','kbui-b')")  # cascade чистит kb_*
    print("\n" + ("ВСЕ ОК" if not FAILS else "ПРОВАЛЫ: " + ", ".join(FAILS)))
    sys.exit(1 if FAILS else 0)


asyncio.run(main())
