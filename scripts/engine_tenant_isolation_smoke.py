"""Смоук изоляции tenant-scoped таблиц движка под ролью panel_rw (RLS-СУБЪЕКТ).
Под Вариантом A owner engine_rw обходит RLS на engine → изоляцию проверяет panel_rw.
Покрывает: grants-матрицу + RLS USING/WITH CHECK для engine.sources/search_profiles/
public.lead_feedback + append-only lead_feedback. Тенанты smoke-engine-a/b — сид roles_bootstrap.
Гард DSN: только эфемерный risuy_dev. Транзакция с ROLLBACK."""
import asyncio
import os

import asyncpg

DSN = os.environ.get("PANEL_RW_SMOKE_DSN")
if not DSN or "/risuy_dev" not in DSN.split("?")[0]:
    raise SystemExit("Задайте PANEL_RW_SMOKE_DSN на эфемерном risuy_dev (роль panel_rw).")

TA = "11111111-1111-1111-1111-111111111111"
TB = "22222222-2222-2222-2222-222222222222"


async def _rejected(conn, sql: str, *args) -> bool:
    # Вставка в сейвпоинте: отказ WITH CHECK откатывает только его, внешняя tx жива.
    try:
        async with conn.transaction():
            await conn.execute(sql, *args)
        return False
    except asyncpg.PostgresError:
        return True


async def main() -> None:
    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
    async with pool.acquire() as c:
        # ── grants-матрица (panel_rw проверяет СВОИ привилегии) ──
        # Нет доступа к сырью/пулу/графу/matching:
        for tbl in ("engine.raw_messages", "engine.accounts", "engine.identities",
                    "engine.identity_edges", "engine.matching"):
            has = await c.fetchval("select has_table_privilege($1, 'select')", tbl)
            assert has is False, f"panel_rw НЕ должен иметь select на {tbl}"
        # Есть CRUD к tenant-scoped:
        for tbl in ("engine.sources", "engine.search_profiles"):
            assert await c.fetchval("select has_table_privilege($1, 'insert')", tbl) is True, \
                f"panel_rw должен иметь insert на {tbl}"
        # lead_feedback: select+insert, НЕ update/delete (append-only):
        assert await c.fetchval("select has_table_privilege('public.lead_feedback','insert')") is True
        assert await c.fetchval("select has_table_privilege('public.lead_feedback','update')") is False, \
            "lead_feedback должен быть append-only (нет update у panel_rw)"
        assert await c.fetchval("select has_table_privilege('public.lead_feedback','delete')") is False, \
            "lead_feedback должен быть append-only (нет delete у panel_rw)"

        # ── RLS-изоляция под panel_rw для sources/search_profiles/lead_feedback ──
        tr = c.transaction()
        await tr.start()
        try:
            cases = [
                ("engine.sources",
                 "insert into engine.sources (tenant_id, source_kind, external_ref) values ($1,'telegram','chat-a')"),
                ("engine.search_profiles",
                 "insert into engine.search_profiles (tenant_id, name) values ($1,'проф-A')"),
                ("public.lead_feedback",
                 "insert into public.lead_feedback (tenant_id, verdict) values ($1,'junk')"),
            ]
            for tbl, ins in cases:
                # A вставляет свою строку и видит её.
                await c.execute("select set_config('app.tenant_id',$1,false)", TA)
                await c.execute(ins, TA)
                a_seen = await c.fetchval(f"select count(*) from {tbl} where tenant_id=$1", TA)
                assert a_seen >= 1, f"{tbl}: A должен видеть свою строку, видит {a_seen}"
                # B под своим контекстом НЕ видит строку A (USING).
                await c.execute("select set_config('app.tenant_id',$1,false)", TB)
                b_sees_a = await c.fetchval(f"select count(*) from {tbl} where tenant_id=$1", TA)
                assert b_sees_a == 0, f"{tbl}: B не должен видеть строки A, видит {b_sees_a}"
                # Пустой контекст → 0 строк (fail-closed на чтение).
                await c.execute("select set_config('app.tenant_id','',false)")
                none_ctx = await c.fetchval(f"select count(*) from {tbl}")
                assert none_ctx == 0, f"{tbl}: без контекста должно быть 0, видно {none_ctx}"
                # WITH CHECK: форж чужого tenant_id под ctx A отклоняется (в сейвпоинте).
                await c.execute("select set_config('app.tenant_id',$1,false)", TA)
                forged = await _rejected(c, ins, TB)
                assert forged, f"{tbl}: форж чужого tenant_id (TB под ctx TA) должен отклоняться"
        finally:
            await tr.rollback()
    print("engine_tenant_isolation_smoke: OK (grants-матрица + RLS USING/WITH CHECK + append-only)")


asyncio.run(main())
