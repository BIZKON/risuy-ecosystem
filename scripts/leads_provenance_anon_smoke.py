"""Смоук SL: outbound_signal исключён из РКН anon-выгрузки (152-ФЗ C1).
Проверяет реальную обвязку db.stream_leads_anon / stream_leads_map / count_leads_anon.
Гонится как owner на эфемерном risuy_dev. Гард DSN: только risuy_dev. Самоочистка."""
import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "admin-panel"))
os.environ.setdefault("DATABASE_URL", "postgresql://x/y")
os.environ.setdefault("SESSION_SECRET", "smoke-secret-only-for-import-aaaaaaaa")
os.environ.setdefault("ADMIN_USERNAME", "smoke")
os.environ.setdefault("ADMIN_PASSWORD_HASH", "$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHQ$aGFzaHN0dWI")

import asyncpg  # noqa: E402
import db        # noqa: E402  (admin-panel/db.py)

DSN = os.environ.get("PROV_SMOKE_DSN")
if not DSN or "/risuy_dev" not in DSN.split("?")[0]:
    raise SystemExit("Задайте PROV_SMOKE_DSN на эфемерном risuy_dev (owner).")

TA = "11111111-1111-1111-1111-111111111111"
IB, OB = 777001, 777002  # tg_user_id инбаунд/аутбаунд тест-лидов


async def main() -> None:
    db.pool = await asyncpg.create_pool(DSN, min_size=1, max_size=4, setup=db._apply_tenant_guc)
    try:
        db.set_active_tenant(None)
        async with db.pool.acquire() as c:
            await c.execute("delete from leads where tg_user_id = any($1::bigint[])", [IB, OB])
            await c.execute(
                "insert into leads (tenant_id, messenger, source, status, tg_user_id, provenance) "
                "values ($1,'tg','other','new',$2,'inbound_optin')", TA, IB)
            await c.execute(
                "insert into leads (tenant_id, messenger, source, status, tg_user_id, provenance, consent, source_url) "
                "values ($1,'tg','other','new',$2,'outbound_signal',false,'https://t.me/x/1')", TA, OB)
            ib_id = await c.fetchval("select id from leads where tg_user_id=$1", IB)
            ob_id = await c.fetchval("select id from leads where tg_user_id=$1", OB)

        db.set_active_tenant(TA)
        anon_ids = [r["id"] async for r in db.stream_leads_anon(row_cap=1000)]
        map_ids = [r["id"] async for r in db.stream_leads_map(row_cap=1000)]
        cnt = await db.count_leads_anon()
        async with db.pool.acquire() as c:
            inbound_total = await c.fetchval("select count(*) from leads where provenance='inbound_optin'")
            all_total = await c.fetchval("select count(*) from leads")

        assert ib_id in anon_ids, "инбаунд ДОЛЖЕН быть в anon-выгрузке"
        assert ob_id not in anon_ids, "outbound НЕ должен быть в anon-выгрузке (C1)"
        assert ob_id not in map_ids, "outbound НЕ должен быть в map-выгрузке (C1)"
        # robust к предсуществующим строкам: считает ровно inbound и исключает наш outbound-сид.
        assert cnt == inbound_total, f"count_leads_anon != число inbound ({cnt} vs {inbound_total})"
        assert cnt < all_total, "count_leads_anon должен исключать outbound (< всех лидов)"
    finally:
        db.set_active_tenant(None)
        async with db.pool.acquire() as c:
            await c.execute("delete from leads where tg_user_id = any($1::bigint[])", [IB, OB])
        await db.pool.close()
    print("leads_provenance_anon_smoke: OK (outbound исключён из anon/map + count_leads_anon)")


asyncio.run(main())
