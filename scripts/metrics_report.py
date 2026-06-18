#!/usr/bin/env python3
"""Measurement-framework risuy (Founder's Playbook, Launch-стадия): read-only снимок ключевых метрик
платформы и продукта. НИЧЕГО НЕ ПИШЕТ — только SELECT, плюс пояс безопасности
`default_transaction_read_only=on` (любая случайная запись → ошибка). Объекты в БД НЕ создаёт.

Метрики (определения и «почему» — в docs/metrics-framework.md):
  ПЛАТФОРМА  — привлечение/активация/удержание тенантов + выручка (service_invoices/subscriptions/usage).
  ПРОДУКТ    — воронка лидов (new→guide_sent→nurturing→escalated→converted), конверсия в оплату, вовлечённость.
  🚩 TRIPWIRES — сигналы «ложного PMF» по playbook (подписались-но-не-активировались, активны-но-уснули,
                лиды-без-вовлечения, выручка-без-удержания).

Запуск (read-only owner-DSN; ПРОД — там реальные данные):
  METRICS_DSN="postgresql://gen_user:<pw>@81.31.246.136:5432/risuy?sslmode=require" \
      ./.venv-smoke/bin/python scripts/metrics_report.py
"""
import asyncio
import os
import sys

import asyncpg  # есть в .venv-smoke

DSN = os.environ.get("METRICS_DSN")
if not DSN:
    raise SystemExit("Задайте METRICS_DSN (read-only owner-DSN на risuy или risuy_dev).")

# Активным считаем лид с ≥1 ВХОДЯЩИМ сообщением (реальное использование, канал-агностично).
ACTIVE_LEAD = "exists (select 1 from messages m where m.lead_id = l.id and m.direction = 'in')"


def h(title):
    print(f"\n{'═'*64}\n{title}\n{'═'*64}")


def row(label, value, hint=""):
    val = "—" if value is None else value
    print(f"  {label:<46} {str(val):>10}" + (f"   {hint}" if hint else ""))


async def scalar(c, sql, *a):
    try:
        return await c.fetchval(sql, *a)
    except Exception as e:  # noqa: BLE001
        return f"ERR:{type(e).__name__}"


async def main():
    db = os.path.basename(DSN.split("?")[0].split("/")[-1])
    print(f"METRICS SNAPSHOT · база={db} · режим=READ-ONLY")
    c = await asyncpg.connect(DSN)
    try:
        await c.execute("set default_transaction_read_only = on")  # пояс безопасности

        # ── ПЛАТФОРМА: тенанты ───────────────────────────────────────────────
        h("ПЛАТФОРМА · ТЕНАНТЫ (привлечение / активация / удержание)")
        row("Всего тенантов", await scalar(c, "select count(*) from tenants"))
        row("  active", await scalar(c, "select count(*) from tenants where status='active'"))
        row("Новых за 7 дней", await scalar(c, "select count(*) from tenants where created_at >= now()-interval '7 days'"))
        row("Новых за 30 дней", await scalar(c, "select count(*) from tenants where created_at >= now()-interval '30 days'"))
        activated = await scalar(c, f"select count(distinct l.tenant_id) from leads l where {ACTIVE_LEAD}")
        row("Активированных (есть лид с входящим)", activated, "← реальное использование")
        row("Удержание: активность за 30д (есть msg)",
            await scalar(c, "select count(distinct tenant_id) from messages where created_at >= now()-interval '30 days'"))

        # ── ПЛАТФОРМА: выручка ───────────────────────────────────────────────
        h("ПЛАТФОРМА · ВЫРУЧКА (подписки тенантов на саму платформу)")
        row("service_invoices: оплачено (шт)", await scalar(c, "select count(*) from service_invoices where status='paid'"))
        row("service_invoices: сумма оплат, ₽", await scalar(c, "select coalesce(sum(amount),0) from service_invoices where status='paid'"))
        row("Подписок active", await scalar(c, "select count(*) from subscriptions where status='active'"))
        row("usage_ledger: списано всего, ₽",
            await scalar(c, "select round(coalesce(sum(charged_microrub),0)/1e6,2) from usage_ledger"))

        # ── ПРОДУКТ: воронка лидов ───────────────────────────────────────────
        h("ПРОДУКТ · ВОРОНКА ЛИДОВ (ценность, которую Лия приносит тенанту)")
        total_leads = await scalar(c, "select count(*) from leads")
        row("Всего лидов", total_leads)
        for st in ("new", "guide_sent", "nurturing", "converted"):
            row(f"  status = {st}", await scalar(c, "select count(*) from leads where status=$1", st))
        row("  эскалировано (escalated_at)", await scalar(c, "select count(*) from leads where escalated_at is not null"))
        row("  отписалось (unsubscribed_at)", await scalar(c, "select count(*) from leads where unsubscribed_at is not null"))
        h2 = "select messenger, count(*) from leads group by messenger order by 2 desc"
        try:
            print("  по каналам:")
            for r in await c.fetch(h2):
                print(f"     {r['messenger'] or '—':<10} {r['count']:>6}")
        except Exception:  # noqa: BLE001
            pass

        # ── ПРОДУКТ: конверсия и вовлечённость ───────────────────────────────
        h("ПРОДУКТ · КОНВЕРСИЯ И ВОВЛЕЧЁННОСТЬ")
        paid = await scalar(c, "select count(*) from orders where status='paid'")
        row("Заказов оплачено", paid)
        row("Выручка с заказов (paid), ₽", await scalar(c, "select coalesce(sum(amount),0) from orders where status='paid'"))
        row("Средний чек (paid), ₽", await scalar(c, "select round(avg(amount),2) from orders where status='paid'"))
        if isinstance(total_leads, int) and total_leads > 0 and isinstance(paid, int):
            row("Конверсия лид→оплата, %", round(100.0 * paid / total_leads, 1))
        row("Активных лидов (входящее за 7д)",
            await scalar(c, "select count(distinct lead_id) from messages where direction='in' and created_at >= now()-interval '7 days'"))
        row("Сообщений всего", await scalar(c, "select count(*) from messages"))
        row("  входящих", await scalar(c, "select count(*) from messages where direction='in'"))

        # ── 🚩 TRIPWIRES: сигналы ложного PMF ────────────────────────────────
        h("🚩 TRIPWIRES · сигналы ложного PMF (playbook)")
        row("Тенантов подписались-но-НЕ-активировались",
            await scalar(c, f"select count(*) from tenants t where not exists "
                            f"(select 1 from leads l where l.tenant_id=t.id and {ACTIVE_LEAD})"),
            "← нет ни одного лида с входящим")
        row("Тенантов активны-но-уснули (нет msg 30д)",
            await scalar(c, "select count(*) from tenants t where exists (select 1 from messages m where m.tenant_id=t.id) "
                            "and not exists (select 1 from messages m where m.tenant_id=t.id and m.created_at >= now()-interval '30 days')"))
        row("Лидов БЕЗ вовлечения (нет входящих)",
            await scalar(c, "select count(*) from leads l where not exists (select 1 from messages m where m.lead_id=l.id and m.direction='in')"))
        if isinstance(total_leads, int) and total_leads > 0:
            no_eng = await scalar(c, "select count(*) from leads l where not exists (select 1 from messages m where m.lead_id=l.id and m.direction='in')")
            if isinstance(no_eng, int):
                row("  доля лидов без вовлечения, %", round(100.0 * no_eng / total_leads, 1))

        print("\n" + "─" * 64)
        print("Примечание: пустые/единичные значения = платформа в pre-PMF (мало реального")
        print("использования). Главный приоритет по playbook — привлечение/валидация, а не стройка.")
        print("Цели/бенчмарки и определения метрик — docs/metrics-framework.md")
    finally:
        await c.close()


if __name__ == "__main__":
    asyncio.run(main())
