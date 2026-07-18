"""Смоук схемы engine (S1-RAW) под ролью engine_rw (owner): ownership + дедуп + pgvector.
Гард DSN: только эфемерный risuy_dev. Транзакция с ROLLBACK (самоочистка)."""
import asyncio
import os

import asyncpg

DSN = os.environ.get("ENGINE_SCHEMA_SMOKE_DSN")
if not DSN or "/risuy_dev" not in DSN.split("?")[0]:
    raise SystemExit("Задайте ENGINE_SCHEMA_SMOKE_DSN на эфемерном risuy_dev (роль engine_rw).")

ENGINE_TABLES = ("raw_messages", "accounts", "matching", "identities", "identity_edges",
                 "sources", "search_profiles")


async def main() -> None:
    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
    async with pool.acquire() as c:
        # ── ownership: все engine-таблицы принадлежат engine_rw (Вариант A) ──
        rows = await c.fetch(
            "select relname, pg_get_userbyid(relowner) as owner from pg_class "
            "where relnamespace = 'engine'::regnamespace and relkind = 'r'")
        owners = {r["relname"]: r["owner"] for r in rows}
        for t in ENGINE_TABLES:
            assert owners.get(t) == "engine_rw", f"{t} owner={owners.get(t)} != engine_rw"

        tr = c.transaction()
        await tr.start()
        try:
            # ── дедуп raw_messages по (source_kind, external_id) ──
            for _ in range(2):
                await c.execute(
                    "insert into engine.raw_messages (source_kind, external_id, body) "
                    "values ('telegram','dedup-1','x') on conflict (source_kind, external_id) do nothing")
            n = await c.fetchval("select count(*) from engine.raw_messages where external_id='dedup-1'")
            assert n == 1, f"дедуп: ожидалось 1, факт {n}"

            # ── pgvector: вставка vector(768) + косинус-поиск возвращает её ──
            vec = "[" + ",".join(["0.01"] * 768) + "]"
            await c.execute(
                "insert into engine.raw_messages (source_kind, external_id, body, embedding) "
                "values ('telegram','vec-1','y',$1::vector)", vec)
            got = await c.fetchval(
                "select external_id from engine.raw_messages where embedding is not null "
                "order by embedding <=> $1::vector limit 1", vec)
            assert got == "vec-1", f"pgvector-поиск вернул {got}"
        finally:
            await tr.rollback()
    print("engine_schema_smoke: OK (ownership engine_rw + дедуп + pgvector)")


asyncio.run(main())
