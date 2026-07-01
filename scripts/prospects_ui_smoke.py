#!/usr/bin/env python3
"""Render-смоук шаблонов раздела «Компании» (чистый Jinja, без БД/HTTP).
  PYTHONPATH=. ./.venv-smoke/bin/python scripts/prospects_ui_smoke.py"""
import os, sys
from jinja2 import ChainableUndefined, Environment, FileSystemLoader, select_autoescape

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
env = Environment(
    loader=FileSystemLoader(os.path.join(ROOT, "admin-panel", "templates")),
    autoescape=select_autoescape(["html", "xml"]), undefined=ChainableUndefined)

def render(name, **ctx):
    return env.get_template(name).render(**ctx)

FAILS = []
def check(name, cond):
    print(f"  {'OK ' if cond else 'FAIL'} {name}")
    if not cond:
        FAILS.append(name)

# провайдер подключён, есть сохранённые карточки
html = render("companies.html", csrf_token="TESTCSRF", active="companies", has_tenant=True,
              provider_on=True, prospects=[{"id": "1", "inn": "7707083893", "subject_type": "legal",
              "name_short": "ООО А", "okved_name": "Разработка ПО", "city": "Москва", "status": "ACTIVE",
              "lead_id": None, "archived": False, "fetched": "2026-07-01 10:00"}],
              suggestions=[], search_q="", err="", saved=0, support_url="https://t.me/x", help_dismissed=True)
check("форма поиска по ИНН → /companies/lookup", 'action="/companies/lookup"' in html and 'name="inn"' in html)
check("форма поиска по названию → /companies/search", 'action="/companies/search"' in html)
check("CSRF-поле есть", 'name="csrf_token"' in html and "TESTCSRF" in html)
check("карточка сохранённой компании видна", "ООО А" in html and "7707083893" in html)
check("нет телефона в разметке", "phone" not in html.lower() and "телефон" not in html.lower())

# провайдер не подключён → плашка
html2 = render("companies.html", csrf_token="T", active="companies", has_tenant=True, provider_on=False,
               prospects=[], suggestions=[], search_q="", err="", saved=0, support_url="", help_dismissed=True)
check("provider_off → плашка «источник не подключён»", "не подключён" in html2)

# без тенанта → подсказка выбрать клиента
html3 = render("companies.html", csrf_token="T", active="companies", has_tenant=False, provider_on=True,
               prospects=[], suggestions=[], search_q="", err="", saved=0, support_url="", help_dismissed=True)
check("без тенанта → раздел не даёт поиск", "выберите клиента" in html3.lower() or 'name="inn"' not in html3)

# подсказки поиска по названию (server-round-trip)
htmls = render("companies.html", csrf_token="T", active="companies", has_tenant=True, provider_on=True,
               prospects=[], search_q="рога",
               suggestions=[{"inn": "7707083893", "name": "ООО РОГА", "city": "Москва", "status": "ACTIVE"}],
               err="", saved=0, support_url="", help_dismissed=True)
check("подсказки: строка с кнопкой «Подтянуть»", "ООО РОГА" in htmls and "Подтянуть" in htmls)

# партиал карточки
card = render("_company_card.html", p={"inn": "7707083893", "subject_type": "legal", "name_short": "ООО А",
              "name_full": "ОБЩЕСТВО", "opf": "ООО", "okved": "62.01", "okved_name": "Разработка ПО",
              "address": "г Москва", "city": "Москва", "status": "ACTIVE", "registration_date": "2003-03-03",
              "management": {"name": "Иванов И.И.", "post": "Директор"}}, csrf_token="T", back="/companies")
check("партиал: реквизиты ЮЛ", "7707083893" in card and "62.01" in card and "ACTIVE" in card)

print(("\nFAIL: " + ", ".join(FAILS)) if FAILS else "\nВсе render-проверки OK")
sys.exit(1 if FAILS else 0)
