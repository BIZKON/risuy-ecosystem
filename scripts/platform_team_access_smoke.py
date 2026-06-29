#!/usr/bin/env python3
"""Render-смоук A1: «ИИ-команда клиента» в панели владельца-платформы.
Чистый Jinja-рендер шаблонов admin-panel (без БД/HTTP): nav-пункт платформы,
ролевое «клиент не выбран» (CTA «Клиенты» vs «поддержка»), бейдж активного клиента.
Запуск:
  PYTHONPATH=. ./.venv-smoke/bin/python scripts/platform_team_access_smoke.py
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


BASE_CTX = dict(
    csrf_token="csrf", saved="", err="", active="my_team",
    agents=[], channel_map={}, messengers=[], presets=[],
    prompt_max=4000, support_url="https://t.me/support",
)


def render(**over):
    ctx = {**BASE_CTX, **over}
    return env.get_template("my_team.html").render(**ctx)


# Точные nav-лейблы (макрос nav_item: <span class="snav__label">{{ label }}</span>) —
# проверяем именно nav-пункт, а не <h1>/<title>/action= форм (robust к удалению nav-строки).
PLATFORM_NAV = '<span class="snav__label">ИИ-команда клиента</span>'
TENANT_NAV = '<span class="snav__label">ИИ-команда</span>'

# 1. nav: платформа видит «ИИ-команда клиента» → /my-team
html_p = render(session={"is_platform": True}, has_tenant=False)
check("nav: платформа — пункт «ИИ-команда клиента» на /my-team",
      PLATFORM_NAV in html_p and 'href="/my-team"' in html_p)

# 2. nav: тенант ПОЛОЖИТЕЛЬНО видит «ИИ-команда» и НЕ видит платформенную метку
html_t = render(session={"is_platform": False}, has_tenant=True)
check("nav: тенант — пункт «ИИ-команда» рендерится, без платформенной метки",
      TENANT_NAV in html_t and PLATFORM_NAV not in html_t)

# 3. no-tenant + платформа → CTA на «Клиенты», без текста «поддержка»
html = render(session={"is_platform": True}, has_tenant=False)
check("no-tenant платформа — CTA «Клиенты» (/tenants), без «поддержки»",
      ("Клиент не выбран" in html) and ("/tenants" in html) and ("напишите в поддержку" not in html))

# 4. no-tenant + тенант → «напишите в поддержку» (без регрессии)
html = render(session={"is_platform": False}, has_tenant=False)
check("no-tenant тенант — «напишите в поддержку»", "напишите в поддержку" in html)

# 5. бейдж активного клиента: платформа + активный тенант
html = render(session={"is_platform": True, "active_tenant_name": "ООО Ромашка"}, has_tenant=True)
check("бейдж — «Клиент: ООО Ромашка»", "Клиент: ООО Ромашка" in html)

# 6. у тенанта бейджа «Клиент:» нет
html = render(session={"is_platform": False, "active_tenant_name": "X"}, has_tenant=True)
check("тенант — без бейджа «Клиент:»", "Клиент:" not in html)

# 7. UX-фикс роли: дропдаун-пресет назван «Шаблон роли» (необязат.), роль задаётся в «Инструкциях»
html = render(session={"is_platform": False}, has_tenant=True)  # форма «Добавить агента» рендерится
check("UX: «Шаблон роли» вместо «Должность (роль)» + подсказка у «Инструкций»",
      ("Шаблон роли" in html) and ("Должность (роль)" not in html)
      and ("роль и поведение агента задаются здесь" in html))

# 8. СП-2a: тумблер «База знаний» на агента в форме добавления (checked по умолчанию)
html = render(session={"is_platform": False}, has_tenant=True)
check("СП-2a: тумблер «База знаний» в /my-team (checked по умолчанию)",
      ('name="kb_enabled"' in html) and ("База знаний компании" in html))


def _summary():
    print(f"\n{'ВСЕ ОК' if not FAILS else 'ПРОВАЛЫ: ' + ', '.join(FAILS)}")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    _summary()
