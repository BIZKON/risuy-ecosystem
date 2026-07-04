#!/usr/bin/env python3
"""Юнит-смоук чистого модуля club_analytics (без БД/сети):
entity_type (ИП/ЮЛ/Гос/не указан), normalize_city (алиасы/префиксы), summarize
(KPI/распределения/чек None-safe), csv_business_rows (колонки + ОТСУТСТВИЕ контактов),
filter_members (город/тип). Formula-guard проверяем через anon.csv_safe.
  PYTHONPATH=. ./.venv-smoke/bin/python scripts/club_analytics_smoke.py
"""
import sys

from shared import club_analytics as ca
from shared.anon import csv_safe

FAILS = []


def check(name, cond, detail=""):
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


# ── entity_type ───────────────────────────────────────────────────────────────
check("ИНН 12 → ИП", ca.entity_type("165000000000", None) == "ИП")
check("ИНН 10 + ООО → ЮЛ", ca.entity_type("7700000000", "ООО") == "ЮЛ")
check("ИНН 10 без ОПФ → ЮЛ", ca.entity_type("7700000000", None) == "ЮЛ")
check("ИНН 10 + ГБУ → Гос", ca.entity_type("7700000000", "ГБУ") == "Гос")
check("ИНН 10 + 'муниципальное учреждение' → Гос",
      ca.entity_type("7700000000", "муниципальное автономное учреждение") == "Гос")
check("пустой ИНН → не указан", ca.entity_type("", "ООО") == "не указан")
check("мусорный ИНН → не указан", ca.entity_type("123", None) == "не указан")
check("ИНН с дефисами 10 цифр → ЮЛ", ca.entity_type("77-000-000-00", "АО") == "ЮЛ")

# ── normalize_city ────────────────────────────────────────────────────────────
check("мск → Москва", ca.normalize_city("мск") == "Москва")
check("'г. Москва' → Москва", ca.normalize_city("г. Москва") == "Москва")
check("СПб → Санкт-Петербург", ca.normalize_city("СПб") == "Санкт-Петербург")
check("пусто → Не указан", ca.normalize_city("") == "Не указан")
check("None → Не указан", ca.normalize_city(None) == "Не указан")
check("'старый оскол' → Старый Оскол", ca.normalize_city("старый оскол") == "Старый Оскол")
check("'КАЗАНЬ' → Казань", ca.normalize_city("КАЗАНЬ") == "Казань")

# ── summarize ─────────────────────────────────────────────────────────────────
ROWS = [
    {"status": "active", "city": "мск", "okved": "62.01", "inn": "165000000000",
     "chain_position": "before", "avg_check": 100, "offering": "x", "prospect": None},
    {"status": "active", "city": "Москва", "okved": "62.01", "inn": "7700000000",
     "chain_position": "after", "avg_check": 300, "prospect": {"opf": "ООО", "name_short": "ООО Ромашка"}},
    {"status": "paused", "city": "Казань", "okved": "41.20", "inn": "7800000000",
     "chain_position": None, "avg_check": None, "prospect": {"opf": "ГБУ"}},
]
s = ca.summarize(ROWS)
check("summarize.kpi.total == 3", s["kpi"]["total"] == 3)
check("summarize.kpi.active == 2", s["kpi"]["active"] == 2)
check("summarize.kpi.paused == 1", s["kpi"]["paused"] == 1)
check("summarize.kpi.with_egrul == 2", s["kpi"]["with_egrul"] == 2)
check("summarize.by_type ИП=1 ЮЛ=1 Гос=1",
      s["by_type"]["ИП"] == 1 and s["by_type"]["ЮЛ"] == 1 and s["by_type"]["Гос"] == 1)
check("summarize.chain before=1 after=1 'нет профиля'=1",
      s["chain"]["before"] == 1 and s["chain"]["after"] == 1 and s["chain"]["нет профиля"] == 1)
check("summarize город 'мск' и 'Москва' схлопнулись в Москва=2",
      dict(s["by_city"]).get("Москва") == 2)
check("summarize.avg_check.count == 2 (None пропущен)", s["avg_check"]["count"] == 2)
check("summarize.avg_check.median == 200", s["avg_check"]["median"] == 200)
empty = ca.summarize([])
check("summarize([]) не падает, total=0", empty["kpi"]["total"] == 0)
check("summarize([]).avg_check.median == 0 (None-safe)", empty["avg_check"]["median"] == 0)

# ── filter_members ────────────────────────────────────────────────────────────
check("filter_members city='Москва' ловит и 'мск' → 2",
      len(ca.filter_members(ROWS, city="Москва")) == 2)
check("filter_members etype='Гос' → 1", len(ca.filter_members(ROWS, etype="Гос")) == 1)
check("filter_members без фильтров → все", len(ca.filter_members(ROWS)) == 3)

# ── csv_business_rows: колонки, отсутствие контактов, formula-guard ───────────
check("CSV_HEADERS = 13 колонок", len(ca.CSV_HEADERS) == 13)
_hdr_join = " ".join(ca.CSV_HEADERS).lower()
for banned in ("телефон", "phone", "tg", "vk", "email", "контакт", "руковод", "адрес"):
    check(f"в заголовках CSV нет '{banned}'", banned not in _hdr_join)
data = list(ca.csv_business_rows(ROWS))
check("csv_business_rows отдаёт только data (3 строки, без заголовка)", len(data) == 3)
check("каждая CSV-строка = 13 полей", all(len(r) == 13 for r in data))
# formula-guard применяется на слое _csv_line (anon.csv_safe) — проверяем сам guard:
check("anon.csv_safe нейтрализует ведущий '='", csv_safe("=SUM(A1)").startswith("'"))

print(("\nFAIL: " + ", ".join(FAILS)) if FAILS else "\nOK: club_analytics_smoke")
sys.exit(1 if FAILS else 0)
