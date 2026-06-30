#!/usr/bin/env python3
"""Render-смоук онбординга: dashboard.html рендерит welcome-карточку + getting-started чеклист
(прогресс-классом, без inline-style), скрывает для платформы, вычёркивает выполненные шаги.
Чистый Jinja (без БД/HTTP).
  PYTHONPATH=. ./.venv-smoke/bin/python scripts/onboarding_ui_smoke.py
"""
import os
import sys

from jinja2 import ChainableUndefined, Environment, FileSystemLoader, select_autoescape

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
env = Environment(
    loader=FileSystemLoader(os.path.join(ROOT, "admin-panel", "templates")),
    autoescape=select_autoescape(["html", "xml"]),
    undefined=ChainableUndefined,
)
env.globals["asset_version"] = ""
env.globals["service_site_url"] = ""

FAILS = []
def check(n, c):  # noqa: E302
    print(f"  {'OK ' if c else 'FAIL'} {n}")
    if not c:
        FAILS.append(n)

_DEFS = [("bot", "Подключить бота", "/keys", "Подключить Telegram-бота"),
         ("team", "Собрать ИИ-команду", "/my-team", "Создать ИИ-сотрудника"),
         ("kb", "Загрузить базу знаний", "/knowledge", "Загрузить документ"),
         ("funnel", "Включить воронку", "/lead-magnet", "Настроить лид-магнит"),
         ("aha", "Проверить агента вживую", "/dialogs", "Написать боту тест")]


def state(done_keys):
    steps = [{"key": k, "label": l, "href": h, "cta": c, "done": k in done_keys} for k, l, h, c in _DEFS]
    done = len(done_keys)
    return {"steps": steps, "done_count": done, "total": 5, "pct": round(done / 5 * 100),
            "complete": done == 5}


BASE = dict(csrf_token="csrf", active="dashboard", counts={"total": 0}, conversion=0.0,
            by_source=[], platform=None, onboarding=None, status_labels={})


def render(**over):
    ctx = {**BASE, **over}
    return env.get_template("dashboard.html").render(**ctx)


TEN = {"is_platform": False, "active_tenant_name": "ООО Ромашка"}
PLAT = {"is_platform": True}

# 1. тенант, первый вход (welcome=True, 0/5): welcome + чеклист + прогресс-0 + dismiss + CTA
html = render(session=TEN, onboarding={"state": state(set()), "welcome": True})
check("welcome-карточка показана", "Добро пожаловать" in html)
check("чеклист «Настройка за 5 шагов»", "Настройка за 5 шагов" in html)
check("прогресс-бар класс onb__bar--0", "onb__bar--0" in html)
check("кнопка скрыть чеклист (POST /onboarding/dismiss)", 'action="/onboarding/dismiss"' in html)
check("CTA первого шага", "Подключить Telegram-бота" in html)

# 2. welcome уже просмотрен → без welcome, чеклист есть
html = render(session=TEN, onboarding={"state": state({"bot"}), "welcome": False})
check("welcome скрыт при welcome=False", "Добро пожаловать" not in html)
check("чеклист остаётся", 'class="onb"' in html)

# 3. всё выполнено (5/5): «завершена», прогресс-5, без CTA
html = render(session=TEN, onboarding={"state": state({"bot", "team", "kb", "funnel", "aha"}), "welcome": False})
check("complete → «Настройка завершена ✓»", "Настройка завершена ✓" in html)
check("прогресс-бар onb__bar--5", "onb__bar--5" in html)
check("у выполненных шагов нет CTA", "onb__cta" not in html)

# 4. частично 2/5: прогресс-2, есть is-done и CTA невыполненного
html = render(session=TEN, onboarding={"state": state({"bot", "team"}), "welcome": False})
check("прогресс-бар onb__bar--2", "onb__bar--2" in html)
check("выполненный шаг is-done", "onb__step is-done" in html)
check("CTA невыполненного (база знаний)", "Загрузить документ" in html)

# 5. платформа БЕЗ выбранного клиента (handler передаёт onboarding=None) → ни чеклиста, ни welcome
html = render(session=PLAT, onboarding=None)
check("платформа без клиента — без чеклиста", 'class="onb"' not in html)
check("платформа без клиента — без welcome", "Добро пожаловать" not in html)

# 6. платформа-ПОД-КЛИЕНТА (выбран клиент → handler передаёт onboarding активного тенанта):
#    онбординг ВИДЕН владельцу вместе с платформенной сводкой (фикс видимости, A1-паттерн).
html = render(session=PLAT, onboarding={"state": state({"bot"}), "welcome": True},
              platform={"clients": [], "revenue_rub": "0"})
check("платформа-с-клиентом — чеклист виден владельцу", 'class="onb"' in html)
check("платформа-с-клиентом — welcome виден владельцу", "Добро пожаловать" in html)
check("платформа-с-клиентом — платформенная сводка тоже на месте", "Платформа · клиенты" in html)

print("\n" + ("ВСЕ ОК" if not FAILS else "ПРОВАЛЫ: " + ", ".join(FAILS)))
sys.exit(1 if FAILS else 0)
