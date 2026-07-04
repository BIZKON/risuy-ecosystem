#!/usr/bin/env python3
"""Render-смоук: каталог «Клуб» на /club (club.html). Чистый Jinja-рендер (без БД/HTTP):
- участники активного тенанта рендерятся карточками (display_name/city/okved/статус);
- фильтр город/ОКВЭД присутствует в разметке (GET-форма /club);
- empty-state «Выберите клиента» без active_tenant, «Пока нет участников» без members;
- help_card «Зачем «Клуб»» с комплаенс-рамкой (только согласившиеся, взаимное согласие).
Запуск:
  PYTHONPATH=. ./.venv-smoke/bin/python scripts/club_catalog_ui_smoke.py
"""
import os
import sys

from jinja2 import ChainableUndefined, Environment, FileSystemLoader, select_autoescape

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TPL_DIR = os.path.join(ROOT, "admin-panel", "templates")

env = Environment(
    loader=FileSystemLoader(TPL_DIR),
    autoescape=select_autoescape(["html", "xml"]),
    undefined=ChainableUndefined,  # base.html может ссылаться на необязательные globals — не падаем
)
env.globals["asset_version"] = ""
env.globals["service_site_url"] = ""

FAILS: list[str] = []


def check(name, cond, detail=""):
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


MEMBERS = [
    {
        "display_name": "ИП Соколова Анна",
        "city": "Казань",
        "okved": "62.01",
        "status": "active",
        "inn": "165000000000",
        "prospect": None,
        "matches": [
            {"display_name": "ООО «Стройсервис»", "match_score": 85, "match_reason": "Он до вас в цепочке, тот же город"},
        ],
    },
    {
        "display_name": "ООО «Стройсервис»",
        "city": "Москва",
        "okved": "41.20",
        "status": "paused",
        "inn": "7700000000",
        "prospect": {
            "name_short": "ООО «Стройсервис»",
            "inn": "7700000000",
            "status": "ACTIVE",
            "subject_type": "legal",
            "okved": "41.20",
            "okved_name": "Строительство зданий",
        },
        "matches": [],
    },
]

BASE_CTX = dict(
    csrf_token="csrf",
    session={"is_platform": False, "active_tenant_name": "Тестовый клиент", "actor": "owner@example.com"},
    active="club",
    has_tenant=True,
    members=MEMBERS,
    cities=["Казань", "Москва"],
    okveds=["41.20", "62.01"],
    filter_city="",
    filter_okved="",
    filter_type="",
    filter_status="",
    types=["ИП", "ЮЛ", "Гос"],
    kpi={"total": 2, "active": 1, "paused": 1, "left": 0, "with_egrul": 1, "with_profile": 1, "cities": 2},
    support_url="",
    help_dismissed=False,
)


def render(**over):
    ctx = {**BASE_CTX, **over}
    return env.get_template("club.html").render(**ctx)


FILTER_FORM_MARK = 'action="/club"'
CITY_FILTER_MARK = 'name="city"'
OKVED_FILTER_MARK = 'name="okved"'

# 1. Каталог рендерит участников тенанта (карточки: имя, город, ОКВЭД, статус).
html_list = render()
check("участник 1 — display_name рендерится", "ИП Соколова Анна" in html_list)
check("участник 1 — город рендерится", "Казань" in html_list)
check("участник 1 — ОКВЭД рендерится", "62.01" in html_list)
check("участник 2 — статус «на паузе» рендерится", "на паузе" in html_list)

# 2. ЕГРЮЛ-обогащение: карточка prospect подмешивается (у участника 2 есть m.prospect).
check("ЕГРЮЛ-карточка участника 2 рендерится (company-card)", "company-card" in html_list)
check("ЕГРЮЛ-карточка несёт статус ACTIVE", "ACTIVE" in html_list)

# 2b. Рекомендации «Рекомендуем познакомиться» (Task 6, club_match.rank_matches):
# участник с matches рендерит секцию с причиной; участник без matches (paused, matches=[])
# секцию не рендерит.
check("секция «Рекомендуем познакомиться» рендерится", "Рекомендуем познакомиться" in html_list)
check("причина рекомендации рендерится", "Он до вас в цепочке, тот же город" in html_list)
html_no_matches = render(members=[{**MEMBERS[1], "matches": []}])
check(
    "участник без рекомендаций — секция не рендерится",
    "Рекомендуем познакомиться" not in html_no_matches,
)

# 3. Фильтр город/ОКВЭД присутствует в разметке (GET-форма на /club).
check("форма фильтра — action=/club", FILTER_FORM_MARK in html_list)
check("фильтр — поле город присутствует", CITY_FILTER_MARK in html_list)
check("фильтр — поле ОКВЭД присутствует", OKVED_FILTER_MARK in html_list)

# 4. Empty-state: без active_tenant → «Выберите клиента».
html_no_tenant = render(has_tenant=False, members=[])
check("без клиента — «Выберите клиента»", "Выберите клиента" in html_no_tenant)
check("без клиента — каталог/фильтр НЕ рендерится", FILTER_FORM_MARK not in html_no_tenant)

# 5. Empty-state: активный тенант, но без участников → «Пока нет участников».
html_empty = render(members=[])
check("тенант без участников — «Пока нет участников»", "Пока нет участников" in html_empty)
check("тенант без участников — фильтр всё равно виден", FILTER_FORM_MARK in html_empty)

# 6. help_card с комплаенс-рамкой (только согласившиеся, взаимное согласие) — видна, пока не скрыта.
html_help = render(help_dismissed=False)
check("help_card заголовок рендерится", "Зачем «Клуб»" in html_help)
check("help_card — только согласившиеся (opt-in)", "согласился" in html_help)
check("help_card — взаимное согласие на контакт", "взаимному согласию" in html_help)

html_help_dismissed = render(help_dismissed=True)
check("help_card скрыта при help_dismissed=True", "Зачем «Клуб»" not in html_help_dismissed)

# 7. KPI-полоска + фильтры тип/статус + ссылка на дашборд + выгрузка CSV (Task 3).
html = render()
check(
    "KPI-полоска: показан реальный total",
    str(BASE_CTX["kpi"]["total"]) in html and "club-kpi" in html,
)
check("фильтр по типу присутствует (name=type)", 'name="type"' in html)
check("фильтр по статусу присутствует (name=status)", 'name="status"' in html)
check("ссылка на дашборд есть", "/club/dashboard" in html)
check("кнопка выгрузки CSV (форма POST /club/export.csv)", "/club/export.csv" in html)


def _summary():
    print(f"\n{'ВСЕ ОК' if not FAILS else 'ПРОВАЛЫ: ' + ', '.join(FAILS)}")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    _summary()
