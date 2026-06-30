#!/usr/bin/env python3
"""DB-смоук СП-2b: RLS-изоляция базы знаний В ПАНЕЛИ (роль panel_rw). Клиент B не видит и не
удаляет документ клиента A. Прямой SQL под `set role panel_rw` (owner обошёл бы RLS).
  TEAM_DSN="postgresql://gen_user:<pw>@<host>:5432/risuy_dev?sslmode=require" \
  ./.venv-smoke/bin/python scripts/kb_panel_isolation_smoke.py
"""
import asyncio
import os
import sys

import asyncpg

DSN = os.environ.get("TEAM_DSN", "")
assert DSN and "/risuy_dev" in DSN.split("?")[0], "только risuy_dev (owner-DSN от владельца)"

FAILS: list[str] = []


def check(name, cond):
    print(f"  {'OK ' if cond else 'FAIL'} {name}")
    if not cond:
        FAILS.append(name)


VEC = "[" + ",".join("0.01" for _ in range(768)) + "]"


async def main():
    conn = await asyncpg.connect(DSN)
    try:
        # setup как owner
        ta = await conn.fetchval("insert into tenants(slug,name,status) values('kbui-a','A','active') returning id")
        tb = await conn.fetchval("insert into tenants(slug,name,status) values('kbui-b','B','active') returning id")
        try:
            await conn.execute("set role panel_rw")
            # A пишет свой документ (WITH CHECK совпадает с app.tenant_id)
            await conn.execute("select set_config('app.tenant_id', $1, false)", str(ta))
            da = await conn.fetchval(
                "insert into kb_documents(tenant_id,title,content) values($1,'A-doc','a') returning id", ta)
            await conn.execute(
                "insert into kb_chunks(tenant_id,document_id,chunk_index,content,embedding,metadata) "
                "values($1,$2,0,'ФАКТ-A',$3::vector,'{\"role_tag\":\"\"}'::jsonb)", ta, da, VEC)
            a_titles = [r["title"] for r in await conn.fetch("select title from kb_documents")]
            check("A видит свой документ", "A-doc" in a_titles)

            # B переключается — НЕ видит документ A
            await conn.execute("select set_config('app.tenant_id', $1, false)", str(tb))
            b_titles = [r["title"] for r in await conn.fetch("select title from kb_documents")]
            check("B НЕ видит документ A (RLS list)", "A-doc" not in b_titles)
            b_chunks = [r["content"] for r in await conn.fetch("select content from kb_chunks")]
            check("B НЕ видит чанк A (RLS chunks)", "ФАКТ-A" not in b_chunks)

            # B пытается удалить документ A по id — 0 строк (RLS)
            res = await conn.execute("delete from kb_documents where id=$1", da)
            check("B НЕ удаляет документ A (RLS delete = 0 rows)", res.endswith(" 0"))

            await conn.execute("reset role")
            # документ A всё ещё на месте (owner-проверка)
            still = await conn.fetchval("select count(*) from kb_documents where id=$1", da)
            check("документ A пережил попытку удаления B", still == 1)
        finally:
            await conn.execute("reset role")
    finally:
        await conn.execute("delete from tenants where slug in ('kbui-a','kbui-b')")  # cascade чистит kb_*
        await conn.close()
    print("\n" + ("ВСЕ ОК" if not FAILS else "ПРОВАЛЫ: " + ", ".join(FAILS)))
    sys.exit(1 if FAILS else 0)


asyncio.run(main())
