#!/usr/bin/env python3
"""Unit-смоук провайдера dadata.py: парсинг ЮЛ/ИП + вырезание телефонов/email.
Без сети (проверяет чистые _parse_party/_sanitize на образце ответа DaData).
  PYTHONPATH=. ./.venv-smoke/bin/python scripts/dadata_smoke.py"""
import os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "admin-panel"))

# Заглушки обязательных env (unit-тест без БД; реальные значения не нужны)
os.environ.setdefault("DATABASE_URL", "postgresql://x/y")
os.environ.setdefault("SESSION_SECRET", "smoke-secret-only-for-import-aaaaaaaa")
os.environ.setdefault("ADMIN_USERNAME", "smoke")
os.environ.setdefault("ADMIN_PASSWORD_HASH", "$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHQ$aGFzaHN0dWI")

import dadata

FAILS = []
def check(name, cond, detail=""):
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)

# Образец ответа find-party для ЮЛ (с телефонами/email — должны быть вырезаны)
LEGAL = {
    "type": "LEGAL", "inn": "7707083893", "kpp": "770701001", "ogrn": "1027700132195",
    "name": {"short_with_opf": "ООО «РОГА»", "full_with_opf": "ОБЩЕСТВО ... «РОГА»", "short": "РОГА"},
    "opf": {"short": "ООО"}, "okved": "62.01",
    "okveds": [{"main": True, "code": "62.01", "name": "Разработка ПО"}],
    "address": {"value": "г Москва, ул Тверская, 1", "data": {"region": "Москва", "city": "Москва"}},
    "state": {"status": "ACTIVE", "registration_date": 1046649600000, "liquidation_date": None},
    "management": {"name": "Иванов Иван Иванович", "post": "ГЕНДИРЕКТОР"},
    "phones": [{"value": "+7 495 1234567"}], "emails": [{"value": "info@roga.ru"}],
}
# Образец для ИП (ФИО = ПДн; адрес — только город)
INDIVID = {
    "type": "INDIVIDUAL", "inn": "500100732259", "ogrn": "304500116000157",
    "name": {"full": "ПЕТРОВ ПЁТР ПЕТРОВИЧ"}, "fio": {"surname": "Петров", "name": "Пётр", "patronymic": "Петрович"},
    "okved": "47.91", "okveds": [{"main": True, "code": "47.91", "name": "Розница"}],
    "address": {"value": "г Казань", "data": {"region": "Татарстан", "city": "Казань"}},
    "state": {"status": "ACTIVE", "registration_date": 1046649600000},
}

leg = dadata._parse_party(LEGAL)
check("ЮЛ: subject_type=legal", leg.subject_type == "legal")
check("ЮЛ: inn/ogrn/kpp", leg.inn == "7707083893" and leg.ogrn == "1027700132195" and leg.kpp == "770701001")
check("ЮЛ: имя/ОПФ/ОКВЭД", leg.name_short == "ООО «РОГА»" and leg.opf == "ООО" and leg.okved == "62.01")
check("ЮЛ: okved_name основной", leg.okved_name == "Разработка ПО")
check("ЮЛ: город/регион/статус", leg.city == "Москва" and leg.region == "Москва" and leg.status == "ACTIVE")
check("ЮЛ: дата регистрации ISO", leg.registration_date == "2003-03-03")
check("ЮЛ: руководитель сохранён", (leg.management or {}).get("name") == "Иванов Иван Иванович")
check("ЮЛ: телефоны ВЫРЕЗАНЫ из raw", "phones" not in leg.raw)
check("ЮЛ: email ВЫРЕЗАНЫ из raw", "emails" not in leg.raw)
check("ЮЛ: в карточке нет полей-контактов", not hasattr(leg, "phones") and not hasattr(leg, "emails"))

ind = dadata._parse_party(INDIVID)
check("ИП: subject_type=individual", ind.subject_type == "individual")
check("ИП: management НЕ ставим (ПДн)", ind.management is None)
check("ИП: имя из ЕГРИП сохранено", ind.name_full == "ПЕТРОВ ПЁТР ПЕТРОВИЧ")
check("ИП: город есть, полного адреса места жительства нет", ind.city == "Казань")

print(("\nFAIL: " + ", ".join(FAILS)) if FAILS else "\nВсе проверки dadata OK")
sys.exit(1 if FAILS else 0)
