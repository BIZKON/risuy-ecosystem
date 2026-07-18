"""Смоук S0M: роль engine_rw (не-owner) подчиняется RLS на public.leads.
Инвариант B-FWD: движок пишет/видит ТОЛЬКО строки своего app.tenant_id.
Гард DSN: только эфемерный risuy_dev (как rls_leads_messages_smoke)."""
import asyncio
import os

import asyncpg

DSN = os.environ.get("ENGINE_RW_SMOKE_DSN")
if not DSN or "/risuy_dev" not in DSN.split("?")[0]:
    raise SystemExit("Задайте ENGINE_RW_SMOKE_DSN на эфемерном risuy_dev (роль engine_rw).")

TA = "11111111-1111-1111-1111-111111111111"
TB = "22222222-2222-2222-2222-222222222222"


async def main() -> None:
    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
    async with pool.acquire() as c:
        # Тенант A вставляет лид под своим app.tenant_id.
        await c.execute("select set_config('app.tenant_id',$1,false)", TA)
        await c.execute(
            "insert into leads (tenant_id, messenger, source, status) values ($1,'tg','other','new')",
            TA)
        a_rows = await c.fetchval("select count(*) from leads")
        assert a_rows >= 1, f"A должен видеть свою строку, видит {a_rows}"

        # Тенант B под своим app.tenant_id НЕ видит строки A (RLS).
        await c.execute("select set_config('app.tenant_id',$1,false)", TB)
        b_sees_a = await c.fetchval("select count(*) from leads where tenant_id=$1", TA)
        assert b_sees_a == 0, f"B не должен видеть строки A, видит {b_sees_a}"

        # Без app.tenant_id — 0 строк (fail-closed).
        await c.execute("select set_config('app.tenant_id','',false)")
        none_ctx = await c.fetchval("select count(*) from leads")
        assert none_ctx == 0, f"без тенант-контекста должно быть 0, видно {none_ctx}"
    print("engine_rw_leads_isolation_smoke: OK (engine_rw подчиняется RLS)")


asyncio.run(main())
