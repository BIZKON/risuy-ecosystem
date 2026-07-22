#!/usr/bin/env python3
"""Смоук тарификации DaData (T-1D-3, план 2026-07-20-token-billing-stage1.md) на risuy_dev.

DaData (проверка ИНН/ОГРН, companies_lookup/companies_search в app.py) до этого этапа
использовалась БЕСПЛАТНО для тенанта — квота dadata_quota_take лимитирует запросы к
провайдеру, но НЕ списывает деньги тенанта (утечка §7.5). Проверяет обёртку
db.charge_dadata (admin-panel/db.py), которая зовёт ЕДИНУЮ точку списания charge_usage
(shared/metering.py) с resource='dadata':

  1. db.charge_dadata(tenant, key) пишет usage_ledger: kind='other', provider='dadata',
     cost_microrub=7_500_000 (7,5₽ себестоимость), charged_microrub=22_500_000
     (×3.00 наценка resource_pricing.dadata — сеяно в 1A), token_rate IS NULL
     (снимок курса — только для kind='llm'), units={"requests":1};
  2. per-tenant: второй тенант получает СВОЮ строку леджера; под RLS (симулируем
     panel_rw через FORCE ROW LEVEL SECURITY, owner иначе RLS обходит) тенант A не
     видит строку тенанта B и наоборот — то же RLS-разграничение, что использует
     require_session+_apply_tenant_guc в проде;
  3. идемпотентность: idempotence_key = 'dadata:{tenant}:{key}:{дата UTC}' — повтор
     ТОГО ЖЕ (тенант, key) в те же сутки НЕ создаёт вторую строку и не списывает повторно;
  4. dadata_quota_take (глобальный суточный rate-limit провайдера, app_settings) остаётся
     НЕЗАВИСИМЫМ путём: charge_dadata его не трогает, dadata_quota_take по-прежнему
     инкрементирует счётчик — биллинг НЕ подменяет и не ломает существующий rate-limit.
  5. Фикс Important-финдинга ревью T-1D-3 (фантомное списание): прямой вызов роута
     POST /companies/search (admin-panel/app.py: companies_search) с q ДЛИННЕЕ
     dadata.MAX_QUERY_LEN — suggest_party НЕ вызывается (провайдеру нечего слать), поэтому
     ни квота (dadata_quota_take), ни списание (charge_dadata/usage_ledger) НЕ должны
     срабатывать. Контроль-регресс: тот же роут с валидным q (≤ лимита) по-прежнему
     тарифицирует (квота+usage_ledger как раньше).

Тестовые tenants 'smoke-dd-%' и их usage_ledger/credit_wallets удаляются в конце.
app_settings-ключ квоты DaData за сегодня возвращается к состоянию ДО теста (не
затирается целиком — ключ общий для всего dev, не тенант-скоуплен). На прод НЕ запускать.

Запуск:
  METERING_SMOKE_DSN="postgresql://<owner>@<host>:5432/risuy_dev?sslmode=require" \
  PYTHONPATH=. python3 scripts/dadata_metering_smoke.py
"""
import asyncio
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "admin-panel"))

os.environ.setdefault("DATABASE_URL", "postgresql://x/y")
os.environ.setdefault("SESSION_SECRET", "smoke-secret-only-for-import-aaaaaaaa")
os.environ.setdefault("ADMIN_USERNAME", "smoke")
os.environ.setdefault("ADMIN_PASSWORD_HASH", "$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHQ$aGFzaHN0dWI")

import asyncpg  # noqa: E402
import db        # noqa: E402  (admin-panel/db.py)
import auth      # noqa: E402  (admin-panel/auth.py — auth.Session для прямого вызова роута)

# app.py (FastAPI) грузится с cwd=admin-panel/ — при импорте он монтирует StaticFiles("static")
# и Jinja2Templates("templates") ОТНОСИТЕЛЬНЫМ путём (jinja2.FileSystemLoader НЕ резолвит его
# в абсолютный при конструировании — см. billing_tenant_smoke.py, тот же паттерн). Сразу
# возвращаем cwd назад, чтобы относительные пути DSN/скрипта ниже не сбились; ПЕРЕД любым
# вызовом роута, который рендерит Jinja2-шаблон (companies_search рендерит companies.html),
# нужно временно chdir В admin-panel/ — иначе TemplateNotFound (проверено эмпирически).
_cwd_before_app_import = os.getcwd()
os.chdir(os.path.join(ROOT, "admin-panel"))
import app as admin_app  # noqa: E402  (T-1D-3 fix: прямой вызов companies_search)
os.chdir(_cwd_before_app_import)


class _FakeHeaders:
    """Пустые заголовки запроса — _same_origin(request) при отсутствии host доверяет
    CSRF-токену (см. app.py), как в billing_tenant_smoke.py."""

    def get(self, key, default=None):  # noqa: ARG002 — сигнатура Headers.get
        return default


class _FakeRequest:
    """Минимальная замена fastapi.Request для прямого (не через ASGI) вызова
    companies_search — тестируем гейт квоты/списания, не HTTP-транспорт."""

    def __init__(self):
        self.headers = _FakeHeaders()
        self.client = None
        self.cookies = {}

DSN = os.environ.get("METERING_SMOKE_DSN") or os.environ.get("BILLING_SMOKE_DSN")
if not DSN or "/risuy_dev" not in DSN.split("?")[0]:
    raise SystemExit(
        "Задайте METERING_SMOKE_DSN (или BILLING_SMOKE_DSN) на risuy_dev "
        "(FORCE RLS временно + delete тестовых строк)."
    )

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


async def _cleanup(c) -> None:
    sub = "(select id from tenants where slug like 'smoke-dd-%')"
    for tbl in ("usage_ledger", "credit_wallets"):
        await c.execute(f"delete from {tbl} where tenant_id in {sub}")
    await c.execute("delete from tenants where slug like 'smoke-dd-%'")


async def main() -> None:
    db.pool = await asyncpg.create_pool(DSN, min_size=1, max_size=4, setup=db._apply_tenant_guc)
    forced = False
    quota_key = "dadata_quota__" + datetime.now(timezone.utc).date().isoformat()
    quota_before = None
    ta = tb = tc = None
    try:
        db.set_active_tenant(None)
        async with db.pool.acquire() as c:
            await _cleanup(c)
            quota_before = await c.fetchval(
                "select value from app_settings where key = $1", quota_key
            )
            ta = await c.fetchval(
                "insert into tenants(slug,name,status) values('smoke-dd-a','A','active') returning id"
            )
            tb = await c.fetchval(
                "insert into tenants(slug,name,status) values('smoke-dd-b','B','active') returning id"
            )
            tc = await c.fetchval(
                "insert into tenants(slug,name,status) values('smoke-dd-c','C','active') returning id"
            )
            # Симулируем ограниченную роль panel_rw — owner (gen_user) иначе обходит RLS
            # и «прошло бы» даже при сломанной политике (см. billing_tenant_smoke.py).
            await c.execute("alter table credit_wallets force row level security")
            await c.execute("alter table usage_ledger   force row level security")
            forced = True

            markup = await c.fetchval(
                "select markup_multiplier from resource_pricing where resource = 'dadata'"
            )
        check(
            "resource_pricing.dadata сеяно (=3.000, 1A)",
            markup is not None and Decimal(markup) == Decimal("3.000"),
            f"факт {markup}",
        )

        # ── 1. charge_dadata пишет корректную строку леджера ──
        print("1. charge_dadata → usage_ledger (kind=other, provider=dadata, ×3):")
        db.set_active_tenant(ta)
        await db.charge_dadata(ta, "7707083893")
        async with db.pool.acquire() as c:
            row = await c.fetchrow(
                "select * from usage_ledger where tenant_id = $1", ta
            )
        check("строка леджера создана", row is not None)
        if row is not None:
            check("kind == 'other'", row["kind"] == "other", repr(row["kind"]))
            check("provider == 'dadata'", row["provider"] == "dadata", repr(row["provider"]))
            check("cost_microrub == 7_500_000 (себестоимость)", row["cost_microrub"] == 7_500_000,
                  str(row["cost_microrub"]))
            check("multiplier == 3.00", Decimal(row["multiplier"]) == Decimal("3.00"),
                  str(row["multiplier"]))
            check("charged_microrub == 22_500_000 (7,5₽×3=22,5₽)", row["charged_microrub"] == 22_500_000,
                  str(row["charged_microrub"]))
            check("token_rate_microrub_per_1k IS NULL (не LLM)", row["token_rate_microrub_per_1k"] is None,
                  str(row["token_rate_microrub_per_1k"]))

        # ── 2. per-tenant + RLS-изоляция ──
        print("2. per-tenant: тенант B получает свою строку, RLS не даёт видеть чужую:")
        db.set_active_tenant(tb)
        await db.charge_dadata(tb, "7707083893")  # тот же key_part — другой тенант, свой idem-ключ
        async with db.pool.acquire() as c:
            b_own = await c.fetch("select * from usage_ledger where tenant_id = $1", tb)
        db.set_active_tenant(ta)
        async with db.pool.acquire() as c:
            a_sees_b = await c.fetchval(
                "select count(*) from usage_ledger where tenant_id = $1", tb
            )
            a_own = await c.fetchval(
                "select count(*) from usage_ledger where tenant_id = $1", ta
            )
        check("тенант B: своя строка (1)", len(b_own) == 1, f"факт {len(b_own)}")
        check("тенант A под RLS НЕ видит строку B (0)", a_sees_b == 0, f"факт {a_sees_b}")
        check("тенант A по-прежнему видит свою (1)", a_own == 1, f"факт {a_own}")

        # ── 3. идемпотентность: повтор (тенант, key) в те же сутки — без нового списания ──
        print("3. идемпотентность (повтор того же ИНН в те же сутки):")
        db.set_active_tenant(ta)
        await db.charge_dadata(ta, "7707083893")
        async with db.pool.acquire() as c:
            cnt = await c.fetchval("select count(*) from usage_ledger where tenant_id = $1", ta)
            bal = await c.fetchrow(
                "select topup_microrub from credit_wallets where tenant_id = $1", ta
            )
        check("повтор НЕ создал вторую строку (всё ещё 1)", cnt == 1, f"факт {cnt}")
        check("повтор НЕ списал повторно (аванс == -22_500_000, ровно одно списание)",
              bal is not None and bal["topup_microrub"] == -22_500_000, repr(dict(bal) if bal else None))

        # ── 4. dadata_quota_take — независимый путь (глобальный rate-limit, НЕ биллинг) ──
        print("4. dadata_quota_take остаётся независимым от charge_dadata:")
        async with db.pool.acquire() as c:
            before_quota = await c.fetchval(
                "select value::int from app_settings where key = $1", quota_key
            ) or 0
        ok_quota = await db.dadata_quota_take(999999)
        async with db.pool.acquire() as c:
            after_quota_take = await c.fetchval(
                "select value::int from app_settings where key = $1", quota_key
            )
        check("dadata_quota_take вернул True (в пределах лимита)", ok_quota is True)
        check("dadata_quota_take инкрементировал счётчик (+1)",
              after_quota_take == before_quota + 1, f"{before_quota} → {after_quota_take}")

        db.set_active_tenant(ta)
        await db.charge_dadata(ta, "novyi-zapros-b")  # новый key_part → новое списание
        async with db.pool.acquire() as c:
            after_charge = await c.fetchval(
                "select value::int from app_settings where key = $1", quota_key
            )
            cnt2 = await c.fetchval("select count(*) from usage_ledger where tenant_id = $1", ta)
        check("charge_dadata НЕ трогает квоту (счётчик не изменился)",
              after_charge == after_quota_take, f"{after_quota_take} → {after_charge}")
        check("новый key_part → вторая строка леджера тенанта A (2)", cnt2 == 2, f"факт {cnt2}")

        # ── 5. Фикс Important-финдинга ревью T-1D-3: гейт длины q в companies_search ──
        print("5. companies_search: фантом-сценарий (q > MAX_QUERY_LEN) НЕ квотирует и НЕ списывает:")
        db.set_active_tenant(tc)
        sess = auth.Session(sid="smoke-sid-dd-c", actor="smoke-dd", csrf_token="smoke-csrf-dd-c",
                             active_tenant_id=tc)

        # Провайдер «включён» + suggest_party замокан (без реального HTTP к DaData) —
        # проверяем ТОЛЬКО гейт квоты/списания в companies_search, не сеть.
        orig_is_configured = admin_app.dadata.is_configured
        orig_suggest_party = admin_app.dadata.suggest_party
        suggest_calls: list[str] = []

        admin_app.dadata.is_configured = lambda: True

        async def _fake_suggest_party(q, count=7):  # noqa: ARG001 — сигнатура suggest_party
            suggest_calls.append(q)
            return [{"inn": "7707083893", "name": "ООО Рога и Копыта", "city": "", "status": ""}]

        admin_app.dadata.suggest_party = _fake_suggest_party

        long_q = "Ф" * (admin_app.dadata.MAX_QUERY_LEN + 1)  # 301 символ — длиннее лимита
        valid_q = "Рога и копыта"  # в пределах лимита

        async def _call_companies_search(q: str):
            # companies_search рендерит Jinja2-шаблон относительным путём ("templates") —
            # нужен cwd=admin-panel/ на момент вызова (см. комментарий у import app выше).
            cwd0 = os.getcwd()
            os.chdir(os.path.join(ROOT, "admin-panel"))
            try:
                return await admin_app.companies_search(
                    request=_FakeRequest(), session=sess, q=q, csrf_token="smoke-csrf-dd-c")
            finally:
                os.chdir(cwd0)

        try:
            async with db.pool.acquire() as c:
                quota_before_c = await c.fetchval(
                    "select value::int from app_settings where key = $1", quota_key) or 0
                ledger_before_c = await c.fetchval(
                    "select count(*) from usage_ledger where tenant_id = $1", tc)

            # RED (до фикса): суggest_party() внутри ВСЁ РАВНО отдал бы [] по длине, но
            # квота+списание уже произошли бы выше по коду. GREEN (после фикса): гейт
            # dadata.would_query(q) в companies_search не даёт дойти ни до квоты, ни до вызова.
            resp_long = await _call_companies_search(long_q)

            async with db.pool.acquire() as c:
                quota_after_long = await c.fetchval(
                    "select value::int from app_settings where key = $1", quota_key) or 0
                ledger_after_long = await c.fetchval(
                    "select count(*) from usage_ledger where tenant_id = $1", tc)

            check("q > MAX_QUERY_LEN: роут отработал (200)",
                  getattr(resp_long, "status_code", None) == 200,
                  str(getattr(resp_long, "status_code", None)))
            check("q > MAX_QUERY_LEN: suggest_party НЕ вызван (провайдеру нечего слать)",
                  len(suggest_calls) == 0, f"вызовов: {len(suggest_calls)}")
            check("q > MAX_QUERY_LEN: квота НЕ инкрементирована",
                  quota_after_long == quota_before_c,
                  f"{quota_before_c} → {quota_after_long}")
            check("q > MAX_QUERY_LEN: usage_ledger БЕЗ новой строки (списания не было)",
                  ledger_after_long == ledger_before_c == 0,
                  f"before={ledger_before_c} after={ledger_after_long}")

            # ── регресс: валидный q (≤ лимита) по-прежнему тарифицируется ──
            resp_valid = await _call_companies_search(valid_q)

            async with db.pool.acquire() as c:
                quota_after_valid = await c.fetchval(
                    "select value::int from app_settings where key = $1", quota_key) or 0
                ledger_row_valid = await c.fetchrow(
                    "select charged_microrub from usage_ledger where tenant_id = $1", tc)

            check("регресс: валидный q → роут отработал (200)",
                  getattr(resp_valid, "status_code", None) == 200,
                  str(getattr(resp_valid, "status_code", None)))
            check("регресс: валидный q → suggest_party ВЫЗВАН (1 раз)",
                  len(suggest_calls) == 1, f"вызовов: {len(suggest_calls)}")
            check("регресс: валидный q → квота инкрементирована (+1)",
                  quota_after_valid == quota_after_long + 1,
                  f"{quota_after_long} → {quota_after_valid}")
            check("регресс: валидный q → usage_ledger получил строку (списание есть)",
                  ledger_row_valid is not None,
                  "строка есть" if ledger_row_valid is not None else "строки нет")
            check("регресс: charged_microrub == 22_500_000 (7,5₽×3=22,5₽)",
                  ledger_row_valid is not None and ledger_row_valid["charged_microrub"] == 22_500_000,
                  str(ledger_row_valid["charged_microrub"]) if ledger_row_valid else "нет строки")
        finally:
            admin_app.dadata.is_configured = orig_is_configured
            admin_app.dadata.suggest_party = orig_suggest_party

    finally:
        async with db.pool.acquire() as c:
            if forced:
                await c.execute("alter table credit_wallets no force row level security")
                await c.execute("alter table usage_ledger   no force row level security")
            db.set_active_tenant(None)
            await _cleanup(c)
            if quota_before is None:
                await c.execute("delete from app_settings where key = $1", quota_key)
            else:
                await c.execute(
                    "update app_settings set value = $2 where key = $1", quota_key, quota_before
                )
        await db.pool.close()

    print()
    if FAILS:
        print(f"❌ ПРОВАЛЫ ({len(FAILS)}): " + ", ".join(FAILS))
        sys.exit(1)
    print("✅ dadata metering smoke — все проверки зелёные")


if __name__ == "__main__":
    asyncio.run(main())
