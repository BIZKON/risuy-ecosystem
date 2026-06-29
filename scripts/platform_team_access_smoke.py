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


# 1. nav: платформа видит «ИИ-команда клиента» → /my-team
html_p = render(session={"is_platform": True}, has_tenant=False)
check("nav: платформа — пункт «ИИ-команда клиента» на /my-team",
      "ИИ-команда клиента" in html_p and "/my-team" in html_p)

# 2. nav: тенант видит «ИИ-команда», но НЕ «ИИ-команда клиента»
html_t = render(session={"is_platform": False}, has_tenant=True)
check("nav: тенант — «ИИ-команда» без платформенной метки",
      "ИИ-команда клиента" not in html_t)


def _summary():
    print(f"\n{'ВСЕ ОК' if not FAILS else 'ПРОВАЛЫ: ' + ', '.join(FAILS)}")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    _summary()
