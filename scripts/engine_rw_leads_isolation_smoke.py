"""Смоук S0M: роль engine_rw (не-owner) подчиняется RLS на public.leads.
Инвариант B-FWD: движок ВИДИТ и ПИШЕТ ТОЛЬКО строки своего app.tenant_id.
Проверяет обе половины: read-изоляцию (USING) и write-форж (WITH CHECK).
Тенанты smoke-a/smoke-b сидит roles_bootstrap.sql (FK leads.tenant_id→tenants).
Весь тест — в транзакции с ROLLBACK: самоочистка + идемпотентность.
Гард DSN: только эфемерный risuy_dev (как rls_leads_messages_smoke)."""
import asyncio
import os

import asyncpg

DSN = os.environ.get("ENGINE_RW_SMOKE_DSN")
if not DSN or "/risuy_dev" not in DSN.split("?")[0]:
    raise SystemExit("Задайте ENGINE_RW_SMOKE_DSN на эфемерном risuy_dev (роль engine_rw).")

TA = "11111111-1111-1111-1111-111111111111"
TB = "22222222-2222-2222-2222-222222222222"
INS = "insert into leads (tenant_id, messenger, source, status) values ($1,'tg','other','new')"


async def _rejected(c, tenant_id: str) -> bool:
    # Вставка в сейвпоинте: WITH CHECK-отказ откатывает только его, внешняя tx жива.
    try:
        async with c.transaction():
            await c.execute(INS, tenant_id)
        return False
    except asyncpg.PostgresError:
        return True


async def main() -> None:
    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
    async with pool.acquire() as c:
        tr = c.transaction()
        await tr.start()
        try:
            # A вставляет лид под своим app.tenant_id и видит его.
            await c.execute("select set_config('app.tenant_id',$1,false)", TA)
            await c.execute(INS, TA)
            a_seen = await c.fetchval("select count(*) from leads where tenant_id=$1", TA)
            assert a_seen >= 1, f"A должен видеть свою строку, видит {a_seen}"

            # READ-изоляция: B под своим контекстом НЕ видит строки A (USING).
            await c.execute("select set_config('app.tenant_id',$1,false)", TB)
            b_sees_a = await c.fetchval("select count(*) from leads where tenant_id=$1", TA)
            assert b_sees_a == 0, f"B не должен видеть строки A, видит {b_sees_a}"

            # Пустой контекст — 0 строк на чтение (fail-closed).
            await c.execute("select set_config('app.tenant_id','',false)")
            none_ctx = await c.fetchval("select count(*) from leads")
            assert none_ctx == 0, f"без тенант-контекста должно быть 0, видно {none_ctx}"

            # WRITE-форж: под ctx TA вставить чужой tenant_id TB → отказ WITH CHECK.
            await c.execute("select set_config('app.tenant_id',$1,false)", TA)
            assert await _rejected(c, TB), "форж чужого tenant_id (TB под ctx TA) должен отклоняться"

            # WRITE при пустом контексте → отказ WITH CHECK (nullif→NULL, fail-closed).
            await c.execute("select set_config('app.tenant_id','',false)")
            assert await _rejected(c, TA), "запись при пустом app.tenant_id должна отклоняться"
        finally:
            await tr.rollback()
    print("engine_rw_leads_isolation_smoke: OK (read-изоляция USING + write-форж WITH CHECK)")


asyncio.run(main())
