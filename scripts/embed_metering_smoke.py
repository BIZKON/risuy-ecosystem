#!/usr/bin/env python3
"""Смоук T-1D-2: тарификация эмбеддингов (shared/embed_metering.py) на risuy_dev.

  1. ОЦЕНКА ТОКЕНОВ: ceil(len/3) с клампом 512 (TEI --auto-truncate режет молча — за хвост
     не платим); пустой текст → 0.
  2. ИНЕРТНОСТЬ БЕЗ ЦЕНЫ: строки model_prices эмбеддера нет → charge_embedding вернул False,
     в usage_ledger НИ ОДНОЙ строки (charge_usage не звался).
  3. 🔴 КЛЮЧ НЕ СОЖЖЁН (главный регресс): после инертного прогона вписываем цену и повторяем
     ТОТ ЖЕ контент → списание ПРОХОДИТ (если бы инертный путь позвал charge_usage с cost=0,
     unique(idempotence_key) навсегда заблокировал бы корректное списание).
  4. СПИСАНИЕ С ЦЕНОЙ: kind='embedding', charged = ceil(tokens×price/1000) × наценка 3.000,
     units.tokens_est/texts/chars корректны.
  5. ИДЕМПОТЕНТНОСТЬ: тот же контент/scope повторно → второй строки НЕТ.
  6. РАЗНЫЕ scope одного контента → РАЗНЫЕ ключи (2 строки).

Гонится как gen_user (owner). Тестовая строка model_prices и smoke-tf1d-* удаляются в finally.
На ПРОД не запускать (гард /risuy_dev).
Запуск: BILLING_SMOKE_DSN=…/risuy_dev PGPASSWORD=… PYTHONPATH=. <venv>/bin/python scripts/embed_metering_smoke.py
"""
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
from shared import embed_metering as em  # noqa: E402

DSN = os.environ.get("BILLING_SMOKE_DSN")
if not DSN or "/risuy_dev" not in DSN.split("?")[0]:
    raise SystemExit("Задайте BILLING_SMOKE_DSN на risuy_dev.")

FAILS: list[str] = []
PRICE_IN = 45_000          # тестовая себестоимость µRUB/1k (45 ₽/млн) — ТОЛЬКО для смоука
MARKUP = 3                 # resource_pricing['embedding'] = 3.000


def check(name, cond, detail=""):
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail != "" else ""))
    if not cond:
        FAILS.append(name)


async def _ledger(c, tid, scope=None):
    q = ("select kind, charged_microrub, units, idempotence_key from usage_ledger "
         "where tenant_id = $1" + (" and idempotence_key like $2" if scope else "") +
         " order by id")
    return await (c.fetch(q, tid, f"emb:{tid}:{scope}:%") if scope else c.fetch(q, tid))


_saved_price_rows: list = []      # снимок РЕАЛЬНЫХ строк цены эмбеддера (смоук их временно удаляет)


async def _del_price(c):
    await c.execute("delete from model_prices where provider = $1 and model = $2",
                    em.EMBED_PROVIDER, em.EMBED_MODEL)


async def _save_price_rows(c):
    """Снять снимок боевых строк цены эмбеддера ДО прогона: смоук проверяет инертность,
    поэтому обязан их удалить — но не имеет права оставить dev без тарификации."""
    global _saved_price_rows
    _saved_price_rows = await c.fetch(
        "select price_in_microrub_per_1k pin, price_out_microrub_per_1k pout, effective_from ef "
        "from model_prices where provider = $1 and model = $2",
        em.EMBED_PROVIDER, em.EMBED_MODEL)


async def _restore_price_rows(c):
    """Вернуть снятые строки (иначе прогон смоука молча отключил бы тарификацию эмбеддингов)."""
    for r in _saved_price_rows:
        await c.execute(
            "insert into model_prices(provider, model, price_in_microrub_per_1k, "
            "price_out_microrub_per_1k, effective_from) values($1,$2,$3,$4,$5) "
            "on conflict (provider, model, effective_from) do nothing",
            em.EMBED_PROVIDER, em.EMBED_MODEL, r["pin"], r["pout"], r["ef"])


async def _cleanup(c):
    await _del_price(c)
    sub = "(select id from tenants where slug like 'smoke-tf1d-%')"
    for t in ("usage_ledger", "credit_wallets", "payments", "subscriptions", "tenant_settings"):
        await c.execute(f"delete from {t} where tenant_id in {sub}")
    await c.execute("delete from tenants where slug like 'smoke-tf1d-%'")


async def main() -> None:
    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    try:
        async with pool.acquire() as c:
            await _save_price_rows(c)      # боевые строки цены вернём в finally
            await _cleanup(c)
            econ = await c.fetchval("select id from plans where code='econom'")
            tid = await c.fetchval(
                "insert into tenants(slug,name,status,plan_id) "
                "values('smoke-tf1d-a','TF1D','active',$1) returning id", econ)
            await c.execute(
                "insert into credit_wallets(tenant_id, included_microrub, included_period_end, "
                "topup_microrub, balance_microrub, updated_at) "
                "values($1, 100000000000, now()+interval '30 days', 0, 100000000000, now())", tid)

        # ── 1. оценка токенов ──
        print("1. оценка токенов (ceil(len/3), кламп 512):")
        check("пустой → 0", em.est_tokens("") == 0)
        check("len=3 → 1", em.est_tokens("абв") == 1, str(em.est_tokens("абв")))
        check("len=4 → 2 (ceil)", em.est_tokens("абвг") == 2, str(em.est_tokens("абвг")))
        check("длинный → кламп 512", em.est_tokens("я" * 100000) == 512, str(em.est_tokens("я" * 100000)))

        # ── 2. инертность без цены ──
        print("2. инертность без строки model_prices:")
        texts = ["Как оформить возврат товара по закону?"]
        async with pool.acquire() as c:
            await _del_price(c)
            em.reset_price_cache()          # цена меняется по ходу прогона → кэш сбрасываем явно
            ok_inert = await em.charge_embedding(c, tid, texts, scope="query")
            rows = await _ledger(c, tid)
        check("charge_embedding → False (инертно)", ok_inert is False, repr(ok_inert))
        check("в usage_ledger НИ ОДНОЙ строки", len(rows) == 0, f"факт {len(rows)}")

        # ── 3. 🔴 ключ НЕ сожжён: вписываем цену → тот же контент СПИСЫВАЕТСЯ ──
        print("3. ключ НЕ сожжён (цена появилась → списание проходит):")
        async with pool.acquire() as c:
            await c.execute(
                "insert into model_prices(provider, model, price_in_microrub_per_1k, "
                "price_out_microrub_per_1k) values($1,$2,$3,0)",
                em.EMBED_PROVIDER, em.EMBED_MODEL, PRICE_IN)
            em.reset_price_cache()          # цена появилась → сбрасываем кэш (в проде — TTL 60с)
            ok_now = await em.charge_embedding(c, tid, texts, scope="query")
            rows = await _ledger(c, tid, "query")
        tokens = em.est_tokens(texts[0])
        expect_cost = -(-tokens * PRICE_IN // 1000)          # ceil
        expect_charged = expect_cost * MARKUP
        check("списание прошло (True)", ok_now is True, repr(ok_now))
        check("ровно ОДНА строка леджера", len(rows) == 1, f"факт {len(rows)}")
        if rows:
            r = rows[0]
            import json as _j
            u = r["units"] if isinstance(r["units"], dict) else _j.loads(r["units"])
            check("kind='embedding'", r["kind"] == "embedding", r["kind"])
            check("charged = ceil(tokens×цена/1000) × 3", int(r["charged_microrub"]) == expect_charged,
                  f"{r['charged_microrub']} vs {expect_charged} (tokens={tokens})")
            check("units.tokens_est корректен", int(u.get("tokens_est", 0)) == tokens, str(u))
            check("units.texts/chars корректны",
                  int(u.get("texts", 0)) == 1 and int(u.get("chars", 0)) == len(texts[0]), str(u))

        # ── 4. идемпотентность ──
        print("4. идемпотентность (тот же контент+scope):")
        async with pool.acquire() as c:
            await em.charge_embedding(c, tid, texts, scope="query")
            rows2 = await _ledger(c, tid, "query")
        check("второй строки НЕТ", len(rows2) == 1, f"факт {len(rows2)}")

        # ── 5. другой scope → другой ключ ──
        print("5. другой scope того же контента → отдельное списание:")
        async with pool.acquire() as c:
            await em.charge_embedding(c, tid, texts, scope="memory")
            rows3 = await _ledger(c, tid)
        check("всего 2 строки (query+memory)", len(rows3) == 2, f"факт {len(rows3)}")

        async with pool.acquire() as c:
            await _cleanup(c)
    finally:
        # Вернуть боевые строки цены ДАЖЕ при падении смоука — иначе прогон молча
        # отключил бы тарификацию эмбеддингов на этом кластере.
        try:
            async with pool.acquire() as c:
                await _restore_price_rows(c)
        except Exception:  # noqa: BLE001
            print("  ⚠️ НЕ УДАЛОСЬ вернуть строки model_prices эмбеддера — проверьте вручную!")
        await pool.close()

    print()
    if FAILS:
        print(f"❌ ПРОВАЛЫ ({len(FAILS)}): " + "; ".join(FAILS)); sys.exit(1)
    print("✅ embed_metering_smoke: все проверки зелёные")


if __name__ == "__main__":
    asyncio.run(main())
