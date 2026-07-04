#!/usr/bin/env python3
"""Смоук CSV-экспорта клуба: csv_business_rows отдаёт ровно бизнес-колонки (CSV_HEADERS),
НЕ содержит контактных/ПДн-полей, formula-guard нейтрализует инъекцию. Без БД/HTTP.
  PYTHONPATH=. ./.venv-smoke/bin/python scripts/club_export_csv_smoke.py
"""
import sys

from shared import club_analytics as ca
from shared.anon import csv_safe

FAILS = []


def check(name, cond, detail=""):
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


ROWS = [
    {"display_name": "ИП Соколова", "city": "мск", "inn": "165000000000", "okved": "62.01",
     "offering": "разработка", "seeking": "дизайн", "chain_position": "before",
     "avg_check": 150, "status": "active", "created_at": None,
     # контактные/ПДн поля НАМЕРЕННО присутствуют во входе — не должны попасть в CSV:
     "tg_user_id": 12345, "prospect": {"opf": "ООО", "name_short": "ООО Р", "okved_name": "ПО",
                                       "management": "Иванов Иван Иванович", "address": "секрет"}},
]
rows = list(ca.csv_business_rows(ROWS))
check("одна data-строка", len(rows) == 1)
check("строка = 13 полей (= CSV_HEADERS)", len(rows[0]) == len(ca.CSV_HEADERS) == 13)
flat = " | ".join(rows[0])
check("НЕТ tg_user_id в строке", "12345" not in flat)
check("НЕТ ФИО руководителя (management)", "Иванов" not in flat)
check("НЕТ адреса ИП", "секрет" not in flat)
check("город нормализован (мск→Москва)", "Москва" in flat)
check("тип ИП определён", "ИП" in rows[0])
check("ЕГРЮЛ name_short попал", "ООО Р" in flat)

# formula-guard на слое _csv_line (anon.csv_safe):
mal = list(ca.csv_business_rows([{"display_name": "=cmd()", "inn": "7700000000", "prospect": {"opf": "ООО"}}]))
guarded = [csv_safe(v) for v in mal[0]]
check("formula-guard нейтрализует '=cmd()' в названии", guarded[0].startswith("'"))

print(("\nFAIL: " + ", ".join(FAILS)) if FAILS else "\nOK: club_export_csv_smoke")
sys.exit(1 if FAILS else 0)
