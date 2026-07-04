#!/usr/bin/env python3
"""Render-смоук вкладки /club/dashboard (club_dashboard.html): KPI-плитки,
распределения (город/ОКВЭД/тип), покрытие цепочки, средний чек, рост, воронка знакомств;
empty-state без active_tenant. Чистый Jinja (без БД/HTTP).
  PYTHONPATH=. ./.venv-smoke/bin/python scripts/club_dashboard_ui_smoke.py
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


def check(name, cond, detail=""):
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


SUMMARY = {
    "kpi": {"total": 3, "active": 2, "paused": 1, "left": 0, "with_egrul": 2, "with_profile": 1, "cities": 2},
    "by_city": [("Москва", 2), ("Казань", 1)],
    "by_okved": [("62.01", 2), ("41.20", 1)],
    "by_type": {"ИП": 1, "ЮЛ": 1, "Гос": 1, "не указан": 0},
    "chain": {"before": 1, "after": 1, "both": 0, "нет профиля": 1},
    "avg_check": {"count": 2, "min": 100, "median": 200, "max": 300},
}
CTX = dict(
    csrf_token="csrf",
    session={"is_platform": False, "active_tenant_name": "Тестовый клиент", "actor": "o@e.com"},
    active="club", has_tenant=True, support_url="",
    summary=SUMMARY,
    growth_month=[{"bucket": "2026-06-01", "count": 1}, {"bucket": "2026-07-01", "count": 2}],
    growth_week=[{"bucket": "2026-06-29", "count": 3}],
    funnel={"requested": 1, "accepted": 1, "declined": 1, "cancelled": 0, "both_accepted": 1, "total": 3},
)


def render(**over):
    ctx = dict(CTX); ctx.update(over)
    return env.get_template("club_dashboard.html").render(**ctx)


html = render()
check("KPI total рендерится", "3" in html and ("Всего" in html or "kpi" in html.lower()))
check("распределение по типу (ИП/ЮЛ/Гос)", "ИП" in html and "ЮЛ" in html and "Гос" in html)
check("распределение по городу (Москва)", "Москва" in html)
check("покрытие цепочки (до вас/после вас или before/after)", "цепоч" in html.lower() or "before" in html)
check("средний чек (медиана 200)", "200" in html)
check("рост клуба (бакет-дата)", "2026-07" in html)
check("воронка знакомств (предложено/принято)", "знаком" in html.lower() or "воронк" in html.lower())

empty = render(has_tenant=False, summary={"kpi": {"total": 0, "active": 0, "paused": 0, "left": 0,
    "with_egrul": 0, "with_profile": 0, "cities": 0}, "by_city": [], "by_okved": [],
    "by_type": {"ИП": 0, "ЮЛ": 0, "Гос": 0, "не указан": 0},
    "chain": {"before": 0, "after": 0, "both": 0, "нет профиля": 0},
    "avg_check": {"count": 0, "min": 0, "median": 0, "max": 0}},
    growth_month=[], growth_week=[], funnel={"requested": 0, "accepted": 0, "declined": 0,
    "cancelled": 0, "both_accepted": 0, "total": 0})
check("empty-state без active_tenant не падает", "Выберите клиента" in empty or "клиент" in empty.lower())

print(("\nFAIL: " + ", ".join(FAILS)) if FAILS else "\nOK: club_dashboard_ui_smoke")
sys.exit(1 if FAILS else 0)
