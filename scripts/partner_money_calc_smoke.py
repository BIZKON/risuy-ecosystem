#!/usr/bin/env python3
"""Смоук чистых расчётов партнёрской программы — БЕЗ базы:
  PYTHONPATH=admin-panel:. \
    /Users/konstantin/Downloads/risuy-ecosystem/.venv-smoke/bin/python \
    scripts/partner_money_calc_smoke.py
"""
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "admin-panel"))
os.environ.setdefault("DATABASE_URL", "postgresql://x/y")
os.environ.setdefault("SESSION_SECRET", "smoke-secret-padding-0123456789abcdef")
os.environ.setdefault("ADMIN_USERNAME", "smoke")
os.environ.setdefault("ADMIN_PASSWORD_HASH", "$argon2id$v=19$m=65536,t=3,p=4$c21va2U$c21va2U")

import partner_money as pm  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


NOW = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)


def node(pid, parent=None, joined=NOW, rate="20"):
    return pm.PartnerNode(id=pid, parent_id=parent, joined_at=joined, rate_percent=Decimal(rate))


print("1. Продавец получает 20% от платежа:")
seller = node("s1")
rows = pm.accruals_for_payment(amount_rub=Decimal("7500"), seller=seller, mentor=None, at=NOW)
check("одна строка", len(rows) == 1, f"строк={len(rows)}")
check("1500.00 продавцу", rows[0].amount_rub == Decimal("1500.00"), str(rows[0].amount_rub))
check("уровень 0", rows[0].level == 0)
check("причина sale", rows[0].reason == "sale")

print("2. Ставка берётся из партнёра, а не из конфига:")
rows = pm.accruals_for_payment(amount_rub=Decimal("7500"), seller=node("s1", rate="30"),
                               mentor=None, at=NOW)
check("2250.00 при ставке 30", rows[0].amount_rub == Decimal("2250.00"), str(rows[0].amount_rub))
check("ставка записана в строку", rows[0].rate_percent == Decimal("30"))

print("3. Наставник получает 5% СВЕРХ, продавец не теряет:")
seller = node("s1", parent="m1")
mentor = node("m1")
rows = pm.accruals_for_payment(amount_rub=Decimal("7500"), seller=seller, mentor=mentor, at=NOW)
check("две строки", len(rows) == 2, f"строк={len(rows)}")
check("продавцу по-прежнему 1500.00", rows[0].amount_rub == Decimal("1500.00"))
check("наставнику 375.00", rows[1].amount_rub == Decimal("375.00"), str(rows[1].amount_rub))
check("уровень 1", rows[1].level == 1)
check("причина mentor", rows[1].reason == "mentor")

print("4. Наставник наставника не получает ничего:")
rows = pm.accruals_for_payment(amount_rub=Decimal("7500"), seller=node("s1", parent="m1"),
                               mentor=node("m2"), at=NOW)
check("чужой наставник не начисляется", len(rows) == 1, f"строк={len(rows)}")

print("5. Срок наставнических — 12 месяцев от регистрации подопечного:")
joined = datetime(2025, 8, 16, 12, 0, tzinfo=timezone.utc)
check("день в день ещё идёт", pm.mentor_still_earns(joined, NOW))
check("на следующий день прекращается",
      not pm.mentor_still_earns(joined, NOW + timedelta(days=1)))
check("за месяц до — идёт", pm.mentor_still_earns(joined, NOW - timedelta(days=30)))
rows = pm.accruals_for_payment(amount_rub=Decimal("7500"),
                               seller=node("s1", parent="m1", joined=joined),
                               mentor=node("m1"), at=NOW + timedelta(days=1))
check("после срока строки наставника нет", len(rows) == 1, f"строк={len(rows)}")

print("6. В тенантском контуре наставнических нет вовсе:")
rows = pm.accruals_for_payment(amount_rub=Decimal("7500"), seller=node("s1", parent="m1"),
                               mentor=node("m1"), at=NOW, mentors_enabled=False)
check("только продавец", len(rows) == 1, f"строк={len(rows)}")

print("7. Баланс = начислено − выплачено, сторно уменьшает начисленное:")
accrued, paid, due = pm.partner_balance(
    [Decimal("1500.00"), Decimal("375.00"), Decimal("-1500.00")],
    [Decimal("200.00"), Decimal("100.00")])
check("начислено 375.00", accrued == Decimal("375.00"), str(accrued))
check("выплачено 300.00", paid == Decimal("300.00"), str(paid))
check("к выплате 75.00", due == Decimal("75.00"), str(due))

print("8. Цикл в дереве наставников отбивается:")
parent_of = {"a": None, "b": "a"}
check("A не может стать подопечным B", pm.would_create_cycle("a", "b", parent_of))
check("сам себе наставник запрещён", pm.would_create_cycle("a", "a", parent_of))
check("нормальная привязка проходит", not pm.would_create_cycle("c", "a", parent_of))
check("без наставника цикла нет", not pm.would_create_cycle("c", None, parent_of))

print("9. Копейки округляются вверх по половине:")
rows = pm.accruals_for_payment(amount_rub=Decimal("3750"), seller=node("s1"), mentor=None, at=NOW)
check("750.00 с Эконома", rows[0].amount_rub == Decimal("750.00"), str(rows[0].amount_rub))
check("round_rub(0.125) = 0.13", pm.round_rub(Decimal("0.125")) == Decimal("0.13"))

print()
if FAILS:
    print(f"ПРОВАЛЕНО: {len(FAILS)} — {FAILS}")
    sys.exit(1)
print("ВСЁ ЗЕЛЁНОЕ")
