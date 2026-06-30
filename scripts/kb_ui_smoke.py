#!/usr/bin/env python3
"""Render-смоук СП-2b: /knowledge обоим контурам (платформа-под-клиента + тенант self-serve).
Чистый Jinja-рендер knowledge.html + base.html (без БД/HTTP): has_tenant-гейт, бейдж клиента,
дропдаун отдела из kb_roles, nav-пункт в обеих ветках, гейт глобального тумблера.
  PYTHONPATH=. ./.venv-smoke/bin/python scripts/kb_ui_smoke.py
"""
import os
import sys

from jinja2 import ChainableUndefined, Environment, FileSystemLoader, select_autoescape

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TPL_DIR = os.path.join(ROOT, "admin-panel", "templates")

env = Environment(
    loader=FileSystemLoader(TPL_DIR),
    autoescape=select_autoescape(["html", "xml"]),
    undefined=ChainableUndefined,
)
env.globals["asset_version"] = ""
env.globals["service_site_url"] = ""

FAILS: list[str] = []


def check(name, cond, detail=""):
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


BASE_CTX = dict(
    csrf_token="csrf", err="", kb_saved=0, active="knowledge",
    has_tenant=True, kb_docs=[], kb_enabled=False, show_global_toggle=False,
    embedder_enabled=True, kb_roles={}, kb_max_mb=10,
    support_url="https://t.me/support", help_dismissed=True,
)


def render(**over):
    ctx = {**BASE_CTX, **over}
    return env.get_template("knowledge.html").render(**ctx)


KNOW_NAV = '<span class="snav__label">Базы знаний</span>'

# 1. платформа без клиента → CTA «Клиенты», без «поддержки»
html = render(session={"is_platform": True}, has_tenant=False)
check("платформа без клиента — «Клиент не выбран» + /tenants, без «поддержки»",
      ("Клиент не выбран" in html) and ("/tenants" in html) and ("напишите в поддержку" not in html))

# 2. тенант без привязки → «напишите в поддержку»
html = render(session={"is_platform": False}, has_tenant=False)
check("тенант без кабинета — «напишите в поддержку»", "напишите в поддержку" in html)

# 3. тенант с кабинетом → форма загрузки + дропдаун с НАЗВАНИЯМИ отделов из kb_roles
html = render(
    session={"is_platform": False},
    has_tenant=True,
    kb_roles={"sales": {"name": "Отдел продаж", "role": ""}, "support": {"name": "Поддержка", "role": ""}},
)
check("тенант — форма загрузки и дропдаун отделов (названия)",
      ('action="/knowledge/upload"' in html) and ("Отдел продаж" in html) and ("Поддержка" in html))
check("тенант — есть опция общей справки", "Для всех ролей" in html or "общая справка" in html.lower())

# 4. платформа с клиентом → бейдж «Клиент: …»
html = render(session={"is_platform": True, "active_tenant_name": "ООО Ромашка"}, has_tenant=True)
check("платформа — бейдж «Клиент: ООО Ромашка»", "Клиент: ООО Ромашка" in html)

# 5. тенант → бейджа «Клиент:» нет
html = render(session={"is_platform": False, "active_tenant_name": "X"}, has_tenant=True)
check("тенант — без бейджа «Клиент:»", "Клиент:" not in html)

# 6. глобальный тумблер показывается только при show_global_toggle (School/платформа)
html_off = render(session={"is_platform": False}, has_tenant=True, show_global_toggle=False)
html_on = render(session={"is_platform": True}, has_tenant=True, show_global_toggle=True)
check("тумблер скрыт у тенанта", 'action="/knowledge/toggle"' not in html_off)
check("тумблер виден при show_global_toggle", 'action="/knowledge/toggle"' in html_on)

# 7. nav: пункт «Базы знаний» рендерится В ОБЕИХ ветках (платформа и тенант)
check("nav: платформа видит «Базы знаний»", KNOW_NAV in render(session={"is_platform": True}, has_tenant=True))
check("nav: тенант видит «Базы знаний»", KNOW_NAV in render(session={"is_platform": False}, has_tenant=True))

# 8. help_card «Зачем база знаний»: показан при help_dismissed=False, скрыт при True
html = render(session={"is_platform": False}, has_tenant=True, help_dismissed=False)
check("help_card показан (help_dismissed=False)",
      ("Зачем база знаний" in html) and ('action="/onboarding/dismiss-help"' in html))
html = render(session={"is_platform": False}, has_tenant=True, help_dismissed=True)
check("help_card скрыт (help_dismissed=True)", "Зачем база знаний" not in html)


def _summary():
    print(f"\n{'ВСЕ ОК' if not FAILS else 'ПРОВАЛЫ: ' + ', '.join(FAILS)}")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    _summary()
