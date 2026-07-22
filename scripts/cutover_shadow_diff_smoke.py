#!/usr/bin/env python3
"""Смоук cutover-разреза счётчиков + shadow-diff реконсиляции (T-1C-3) на risuy_dev.

Доказывает на СИНТЕТИЧЕСКИХ тенантах (0 живых подписок на dev/проде), что переход
с per_message-учёта (счёт по сообщениям Лии) на токен-пул (usage_ledger/credit_wallets)
НЕ теряет и НЕ дублирует ни одного сообщения — через shadow-diff инвариант:

  OLD_expected(окно)  = count_ai_messages(окно) × per_message_microrub
  LEDGER_actual(окно) = Σ usage_ledger.charged_microrub где kind='message'
                        и idempotence_key LIKE 'msg:%' за окно тенанта
  shadow_diff := OLD_expected − LEDGER_actual   (==0 ⟺ ничего не потеряно/не сдвоено)

Секции:
  S0 сид синтетики (тенант smoke-cut-a: оплаченный период с quota, N сообщений Лии,
     N строк леджера kind='message' charged=pmm, кошелёк в минусе, metering_msg_hwm);
  S1 GREEN-реконсиляция: shadow_diff == 0 (ядро приёмки);
  S2 корректность cut_over_tenant: маркер, hwm не тронут, финальный overage-счёт,
     перенос теневого минуса в аванс, пул выставлен;
  S3 идемпотентность: повтор → noop, счёт/пул неизменны;
  S4 RED-детектор ПОТЕРИ: одна строка charged=0 → shadow_diff == pmm (≠0);
  S5 RED-детектор ДУБЛЯ: лишняя msg:%-строка → shadow_diff < 0;
  S6 запрет пересечения окон: пул уже выдан (период в будущем) без маркера → skipped/overlap;
  S7 Школа (§8.7): cut_over_tenant(default) → skipped, кошелёк не тронут.

RLS-тонкость: count_ai_messages/get_latest_paid_invoice скоупятся ТОЛЬКО через RLS-GUC
(db.set_active_tenant). gen_user (owner) обходит ENABLE-RLS → на время смоука FORCE RLS
на messages + service_invoices (owner становится субъектом политики, имитируем panel_rw);
в finally — снять FORCE и почистить. Тестовые smoke-cut-* удаляются. На прод НЕ запускать.

Запуск: METERING_SMOKE_DSN="postgresql://<owner>@<host>:5432/risuy_dev?sslmode=require" \
        PYTHONPATH=. ./venv/bin/python scripts/cutover_shadow_diff_smoke.py
"""
import asyncio
import os
import sys
from datetime import date, datetime, timezone
from decimal import Decimal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "admin-panel"))
# config admin-панели валидирует env на импорте — стабы только для загрузки модуля.
os.environ.setdefault("DATABASE_URL", "postgresql://x/y")
os.environ.setdefault("SESSION_SECRET", "smoke-secret-only-for-import-aaaaaaaa")
os.environ.setdefault("ADMIN_USERNAME", "smoke")
os.environ.setdefault("ADMIN_PASSWORD_HASH", "$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHQ$aGFzaHN0dWI")

import asyncpg          # noqa: E402
import db               # noqa: E402  (admin-panel/db.py)
import cut_over_metering as cut  # noqa: E402  (scripts/cut_over_metering.py)

DSN = os.environ.get("METERING_SMOKE_DSN")
if not DSN or "/risuy_dev" not in DSN.split("?")[0]:
    raise SystemExit("METERING_SMOKE_DSN обязателен и ТОЛЬКО risuy_dev.")

PMM = 7_500_000            # per_message_microrub эконома (7,5 ₽) — единица shadow-diff
N = 5                      # сообщений Лии в окне
QUOTA = 2                  # снимок квоты в оплаченном счёте → overage = N−QUOTA = 3
OVER_PRICE = Decimal("7.5")
PERIOD_START = date(2026, 6, 1)
PERIOD_END = date(2026, 7, 1)
MSG_AT = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)   # внутри окна

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


async def _cleanup(c: asyncpg.Connection) -> None:
    sub = "(select id from tenants where slug like 'smoke-cut-%')"
    for tbl in ("service_invoices", "usage_ledger", "credit_wallets", "payments",
                "subscriptions", "tenant_settings", "messages", "agent_token_snapshots",
                "tenant_agents", "leads"):
        await c.execute(f"delete from {tbl} where tenant_id in {sub}")
    await c.execute("delete from tenants where slug like 'smoke-cut-%'")


async def _make_tenant(c, slug: str):
    return await c.fetchval(
        "insert into tenants (slug, name, status) values ($1, $2, 'active') returning id",
        slug, f"Смоук cutover {slug}")


async def _seed_sub_econom(c, tenant) -> None:
    """Подписка econom (после T-1C-1 — cost_multiplier, included_credits эконома)."""
    await c.execute(
        "insert into subscriptions (tenant_id, plan_id, status, "
        "current_period_start, current_period_end) "
        "select $1, p.id, 'active', now(), now()+interval '30 days' "
        "from plans p where p.code='econom'", tenant)


async def _seed_paid_invoice(c, tenant) -> None:
    """Прошлый ОПЛАЧЕННЫЙ период со снимком quota (для get_latest_paid_invoice/overage)."""
    await c.execute(
        "insert into service_invoices (tenant_id, period_start, period_end, plan_key, plan_name, "
        "quota, plan_amount, overage_count, overage_amount, amount, currency, status, paid_at, created_by) "
        "values ($1,$2,$3,'econom','Эконом',$4,3750,0,0,3750,'RUB','paid',now(),'smoke-cut')",
        tenant, PERIOD_START, PERIOD_END, QUOTA)


async def _seed_messages(c, tenant, n: int) -> int:
    """n исходящих сообщений Лии в окне; возвращает max(id) (будущий metering_msg_hwm)."""
    max_id = 0
    for i in range(n):
        mid = await c.fetchval(
            "insert into messages (tg_user_id, direction, kind, source, tenant_id, created_at) "
            "values ($1,'out','text','liya',$2,$3) returning id", 7000 + i, tenant, MSG_AT)
        max_id = max(max_id, int(mid))
    return max_id


async def _seed_ledger_msg(c, tenant, key: str, charged: int) -> None:
    """Строка usage_ledger kind='message' idempotence_key='msg:...' (эмуляция _scan_tenant_messages)."""
    await c.execute(
        "insert into usage_ledger (tenant_id, kind, provider, model, units, cost_microrub, "
        "multiplier, charged_microrub, balance_after_microrub, request_id, idempotence_key) "
        "values ($1,'message',null,null,'{\"messages\":1}'::jsonb,0,1.00,$2,0,$3,$4)",
        tenant, charged, key, key)


async def _shadow_diff(tenant) -> tuple[int, int, int]:
    """(OLD_expected, LEDGER_actual, shadow_diff) для тенанта в окне PERIOD_START..PERIOD_END.

    OLD_expected — count_ai_messages(окно) под RLS активного тенанта × PMM.
    LEDGER_actual — Σ charged по msg:%-строкам тенанта (явный tenant_id-фильтр: не зависит от RLS)."""
    db.set_active_tenant(tenant)
    count = await db.count_ai_messages(PERIOD_START, PERIOD_END)
    async with db.pool.acquire() as c:
        actual = int(await c.fetchval(
            "select coalesce(sum(charged_microrub), 0) from usage_ledger "
            "where tenant_id = $1 and kind = 'message' and idempotence_key like 'msg:%'", tenant))
    old_expected = int(count) * PMM
    return old_expected, actual, old_expected - actual


async def _wallet(c, tenant):
    return await c.fetchrow(
        "select included_microrub, included_period_end, topup_microrub, balance_microrub "
        "from credit_wallets where tenant_id = $1", tenant)


async def main() -> None:
    db.pool = await asyncpg.create_pool(DSN, min_size=1, max_size=5, setup=db._apply_tenant_guc)
    forced = False
    try:
        # ── стартовая чистка + FORCE RLS (owner становится субъектом политики) ──
        db.set_active_tenant(None)
        async with db.pool.acquire() as c:
            await _cleanup(c)
            await c.execute("alter table messages force row level security")
            await c.execute("alter table service_invoices force row level security")
            forced = True
            plan_included = int(await c.fetchval(
                "select included_credits_microrub from plans where code = 'econom'"))

        # ── S0. сид синтетики (тенант smoke-cut-a) ──
        print("S0. сид синтетики smoke-cut-a:")
        db.set_active_tenant(None)
        async with db.pool.acquire() as c:
            ta = await _make_tenant(c, "smoke-cut-a")
        db.set_active_tenant(ta)
        async with db.pool.acquire() as c:
            await _seed_sub_econom(c, ta)
            await _seed_paid_invoice(c, ta)
            hwm = await _seed_messages(c, ta, N)
            for i in range(N):
                await _seed_ledger_msg(c, ta, f"msg:a{i}", PMM)
            # кошелёк в минусе (теневой долг per_message в авансе); пул пуст, период не выставлен
            await c.execute(
                "insert into credit_wallets (tenant_id, included_microrub, included_period_end, "
                "topup_microrub, balance_microrub) values ($1, 0, null, -1000000, -1000000)", ta)
            await c.execute(
                "insert into tenant_settings (tenant_id, key, value) "
                "values ($1, 'metering_msg_hwm', $2)", ta, str(hwm))
        check("сид: hwm выставлен", hwm > 0, f"hwm={hwm}")

        # ── S1. GREEN-реконсиляция: shadow_diff == 0 (ядро приёмки) ──
        print("S1. shadow_diff == 0 (реконсиляция):")
        old_exp, actual, diff = await _shadow_diff(ta)
        check("OLD_expected == N×pmm", old_exp == N * PMM, f"{old_exp}")
        check("LEDGER_actual == N×pmm", actual == N * PMM, f"{actual}")
        check("shadow_diff == 0", diff == 0, f"diff={diff}")

        # ── S2. корректность cut_over_tenant(a) ──
        print("S2. cut_over_tenant(a) — корректность разреза:")
        async with db.pool.acquire() as c:
            w_before = await _wallet(c, ta)
        db.set_active_tenant(ta)
        async with db.pool.acquire() as conn:
            res = await cut.cut_over_tenant(conn, ta)
        check("status == done", res.get("status") == "done", repr(res))
        async with db.pool.acquire() as c:
            marker = await c.fetchval(
                "select value from tenant_settings where tenant_id=$1 and key='billing_cutover_done'", ta)
            hwm_after = await c.fetchval(
                "select value from tenant_settings where tenant_id=$1 and key='metering_msg_hwm'", ta)
            w_after = await _wallet(c, ta)
        async with db.pool.acquire() as c:
            over_inv = await c.fetchrow(
                "select overage_count, overage_amount, plan_amount, amount, status "
                "from service_invoices where tenant_id=$1 and status='pending' "
                "order by created_at desc limit 1", ta)
        check("маркер billing_cutover_done выставлен", marker is not None)
        check("metering_msg_hwm НЕ изменён", hwm_after == str(hwm), f"{hwm_after} vs {hwm}")
        check("финальный overage-счёт создан (pending)", over_inv is not None,
              repr(over_inv))
        check("overage_count == N−quota == 3",
              over_inv is not None and over_inv["overage_count"] == N - QUOTA,
              str(over_inv["overage_count"]) if over_inv else "нет счёта")
        check("overage_amount == 3×7,5 == 22.50",
              over_inv is not None and Decimal(str(over_inv["overage_amount"])) == OVER_PRICE * (N - QUOTA),
              str(over_inv["overage_amount"]) if over_inv else "нет счёта")
        check("плата settle-only: amount == overage_amount (plan_amount 0)",
              over_inv is not None and Decimal(str(over_inv["amount"])) == Decimal(str(over_inv["overage_amount"]))
              and Decimal(str(over_inv["plan_amount"])) == 0,
              f"amount={over_inv['amount']} plan={over_inv['plan_amount']}" if over_inv else "нет счёта")
        check("аванс сохранён: topup_after == topup_before (минус в авансе, carried=0)",
              w_after["topup_microrub"] == w_before["topup_microrub"],
              f"{w_after['topup_microrub']} vs {w_before['topup_microrub']}")
        check("пул выставлен: included == econom.included",
              w_after["included_microrub"] == plan_included,
              f"{w_after['included_microrub']} vs {plan_included}")
        check("included_period_end в будущем",
              w_after["included_period_end"] is not None
              and w_after["included_period_end"] > datetime.now(timezone.utc))
        check("balance == included + topup",
              w_after["balance_microrub"] == plan_included + w_before["topup_microrub"],
              str(w_after["balance_microrub"]))

        # ── S3. идемпотентность: повтор → noop ──
        print("S3. идемпотентность повтора:")
        async with db.pool.acquire() as c:
            inv_before = await c.fetchval(
                "select count(*) from service_invoices where tenant_id=$1 and status='pending'", ta)
        db.set_active_tenant(ta)
        async with db.pool.acquire() as conn:
            res2 = await cut.cut_over_tenant(conn, ta)
        async with db.pool.acquire() as c:
            inv_after = await c.fetchval(
                "select count(*) from service_invoices where tenant_id=$1 and status='pending'", ta)
            w_re = await _wallet(c, ta)
            markers = await c.fetchval(
                "select count(*) from tenant_settings where tenant_id=$1 and key='billing_cutover_done'", ta)
        check("повтор → status == noop", res2.get("status") == "noop", repr(res2))
        check("счёт не задвоен", inv_after == inv_before, f"{inv_after} vs {inv_before}")
        check("пул/аванс неизменны", w_re["included_microrub"] == plan_included
              and w_re["topup_microrub"] == w_before["topup_microrub"])
        check("маркер один", markers == 1, f"факт {markers}")

        # ── S4. RED-детектор ПОТЕРИ (одна строка charged=0) ──
        print("S4. RED-детектор потери (charged=0):")
        db.set_active_tenant(None)
        async with db.pool.acquire() as c:
            tl = await _make_tenant(c, "smoke-cut-loss")
        db.set_active_tenant(tl)
        async with db.pool.acquire() as c:
            await _seed_messages(c, tl, N)
            for i in range(N - 1):
                await _seed_ledger_msg(c, tl, f"msg:l{i}", PMM)
            await _seed_ledger_msg(c, tl, f"msg:l{N-1}", 0)   # ПОТЕРЯ: списание 0
        _, _, diff_loss = await _shadow_diff(tl)
        check("shadow_diff == pmm (потеря обнаружена)", diff_loss == PMM, f"diff={diff_loss}")
        check("shadow_diff != 0", diff_loss != 0)

        # ── S5. RED-детектор ДУБЛЯ (лишняя msg:%-строка) ──
        print("S5. RED-детектор дубля (лишняя строка):")
        db.set_active_tenant(None)
        async with db.pool.acquire() as c:
            td = await _make_tenant(c, "smoke-cut-dup")
        db.set_active_tenant(td)
        async with db.pool.acquire() as c:
            await _seed_messages(c, td, N)
            for i in range(N):
                await _seed_ledger_msg(c, td, f"msg:d{i}", PMM)
            await _seed_ledger_msg(c, td, "msg:d-dup", PMM)   # ДУБЛЬ: лишнее списание
        _, _, diff_dup = await _shadow_diff(td)
        check("shadow_diff < 0 (дубль обнаружен)", diff_dup < 0, f"diff={diff_dup}")
        check("shadow_diff == -pmm", diff_dup == -PMM, f"diff={diff_dup}")

        # ── S6. запрет пересечения окон (пул уже выдан, маркера нет) ──
        print("S6. запрет пересечения окон:")
        db.set_active_tenant(None)
        async with db.pool.acquire() as c:
            to = await _make_tenant(c, "smoke-cut-overlap")
        db.set_active_tenant(to)
        async with db.pool.acquire() as c:
            await _seed_sub_econom(c, to)
            await c.execute(
                "insert into credit_wallets (tenant_id, included_microrub, included_period_end, "
                "topup_microrub, balance_microrub) "
                "values ($1, 100, now()+interval '30 days', 0, 100)", to)  # период В БУДУЩЕМ
            await c.execute(
                "insert into tenant_settings (tenant_id, key, value) "
                "values ($1, 'metering_msg_hwm', '10')", to)
        db.set_active_tenant(to)
        async with db.pool.acquire() as conn:
            res_ov = await cut.cut_over_tenant(conn, to)
        async with db.pool.acquire() as c:
            w_ov = await _wallet(c, to)
            marker_ov = await c.fetchval(
                "select count(*) from tenant_settings where tenant_id=$1 and key='billing_cutover_done'", to)
        check("status == skipped", res_ov.get("status") == "skipped", repr(res_ov))
        check("reason == overlap", res_ov.get("reason") == "overlap", repr(res_ov))
        check("кошелёк не тронут (included 100)", w_ov["included_microrub"] == 100)
        check("маркер НЕ выставлен", marker_ov == 0)

        # ── S7. Школа (§8.7): default → skipped, кошелёк не тронут ──
        print("S7. Школа (default) → skipped:")
        async with db.pool.acquire() as c:
            default_tid = await c.fetchval(
                "select id from tenants where slug = 'lesov-school'")
        if default_tid is None:
            check("дефолт-тенант lesov-school найден", False, "нет на dev")
        else:
            db.set_active_tenant(default_tid)
            async with db.pool.acquire() as c:
                w_school_before = await _wallet(c, default_tid)
            db.set_active_tenant(default_tid)
            async with db.pool.acquire() as conn:
                res_sc = await cut.cut_over_tenant(conn, default_tid)
            async with db.pool.acquire() as c:
                w_school_after = await _wallet(c, default_tid)
                sc_marker = await c.fetchval(
                    "select count(*) from tenant_settings where tenant_id=$1 and key='billing_cutover_done'",
                    default_tid)
            check("status == skipped", res_sc.get("status") == "skipped", repr(res_sc))
            check("reason == school", res_sc.get("reason") == "school", repr(res_sc))
            check("кошелёк Школы не тронут",
                  (w_school_before is None and w_school_after is None)
                  or (w_school_before is not None and w_school_after is not None
                      and w_school_before["included_microrub"] == w_school_after["included_microrub"]
                      and w_school_before["topup_microrub"] == w_school_after["topup_microrub"]))
            check("маркер Школе НЕ выставлен", sc_marker == 0)

    finally:
        async with db.pool.acquire() as c:
            if forced:
                await c.execute("alter table service_invoices no force row level security")
                await c.execute("alter table messages no force row level security")
            db.set_active_tenant(None)
            await _cleanup(c)
        await db.pool.close()

    print()
    if FAILS:
        print(f"❌ ПРОВАЛЫ ({len(FAILS)}): " + ", ".join(FAILS))
        sys.exit(1)
    print("✅ cutover shadow-diff smoke — все проверки зелёные")


if __name__ == "__main__":
    asyncio.run(main())
