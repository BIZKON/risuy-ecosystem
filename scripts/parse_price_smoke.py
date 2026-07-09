#!/usr/bin/env python3
"""Смоук parse_price (shared/money.py): единая валидация цены для admin-panel/app.py
(форма product_save/payment_create) и admin-panel/brief_apply.py (число/строка из JSON
черновика). Без БД.
  PYTHONPATH=. ./.venv-smoke/bin/python scripts/parse_price_smoke.py
"""
import sys
from decimal import Decimal

from shared.money import parse_price

FAILS = []


def check(name, cond, detail=""):
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


check("None → (None, True)", parse_price(None) == (None, True))
check('"" → (None, True)', parse_price("") == (None, True))
check('"  " → (None, True)', parse_price("  ") == (None, True))
check('"0" → (Decimal("0.00"), True)', parse_price("0") == (Decimal("0.00"), True))
check("0 (int) → (Decimal(\"0.00\"), True)", parse_price(0) == (Decimal("0.00"), True))
check('"3000" → Decimal("3000.00")', parse_price("3000") == (Decimal("3000.00"), True))
check("3000.5 (float) → Decimal(\"3000.50\")", parse_price(3000.5) == (Decimal("3000.50"), True))

# те же пробельные символы, что и в money.py: обычный пробел ИЛИ NBSP как разделитель тысяч.
check('"1 000,50" (обычный пробел) → Decimal("1000.50")',
      parse_price("1" + " " + "000,50") == (Decimal("1000.50"), True))
check('"1\\xa0000,50" (NBSP) → Decimal("1000.50")',
      parse_price("1" + "\xa0" + "000,50") == (Decimal("1000.50"), True))

check('"-5" → (None, False)', parse_price("-5") == (None, False))
check('"abc" → (None, False)', parse_price("abc") == (None, False))
check('"99999999999" (≥10^10) → (None, False)', parse_price("99999999999") == (None, False))

print(("\nFAIL: " + ", ".join(FAILS)) if FAILS else "\n✅ parse_price_smoke OK")
sys.exit(1 if FAILS else 0)
