#!/usr/bin/env python3
"""Render-смоук: чекбокс платформы «Инференс ИИ в РФ» на /agents (agents.html).
Чистый Jinja-рендер (без БД/HTTP): чекбокс/форма видны платформе (is_platform=True),
скрыты тенанту (is_platform=False); отражает текущее значение ai_inference_rf (checked/unchecked).
Запуск:
  PYTHONPATH=. ./.venv-smoke/bin/python scripts/agents_ai_inference_rf_ui_smoke.py
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


AI_DEFAULTS = dict(
    enabled=True, backend="cloud_ai", agent_id="", model="", gateway_base_url="",
    system_prompt="", fallback="", persona="",
)

BASE_CTX = dict(
    csrf_token="csrf", saved=False, err=None, preset_applied=False,
    created_slug=None, persona_created=False,
    ai=AI_DEFAULTS, backends=[], default_fallback="", default_model="",
    default_gateway_url="",
    activity={"total": 0, "recent": 0, "last_at": None}, window_days=30,
    agent_id_max=200, fallback_max=500, model_max=200, gateway_url_max=300,
    system_prompt_max=4000, personas=[], persona_label="", role_cards=[],
    recent_messages=[], active="agents", name_max=80, role_title_max=80,
    behavior_max=4000,
)


def render(**over):
    ctx = {**BASE_CTX, **over}
    return env.get_template("agents.html").render(**ctx)


FORM_MARK = 'action="/agents/ai-inference-rf"'
CHECKBOX_MARK = 'name="ai_inference_rf"'

# 1. Платформа видит форму/чекбокс флага
html_p = render(session={"is_platform": True}, ai_inference_rf=False)
check("платформа — форма /agents/ai-inference-rf рендерится", FORM_MARK in html_p)
check("платформа — чекбокс ai_inference_rf рендерится", CHECKBOX_MARK in html_p)

# 2. Тенант НЕ видит форму/чекбокс флага (только платформа, is_platform-only)
html_t = render(session={"is_platform": False}, ai_inference_rf=False)
check("тенант — форма /agents/ai-inference-rf НЕ рендерится", FORM_MARK not in html_t)
check("тенант — чекбокс ai_inference_rf НЕ рендерится", CHECKBOX_MARK not in html_t)

# 3. ai_inference_rf=True → checked
html_on = render(session={"is_platform": True}, ai_inference_rf=True)
check("платформа + rf=True — чекбокс checked",
      'name="ai_inference_rf" value="1" checked' in html_on)

# 4. ai_inference_rf=False → unchecked
html_off = render(session={"is_platform": True}, ai_inference_rf=False)
check("платформа + rf=False — чекбокс НЕ checked",
      'name="ai_inference_rf" value="1" checked' not in html_off
      and CHECKBOX_MARK in html_off)

# 5. CSRF-поле присутствует в форме флага (тот же паттерн, что соседние формы /agents)
check("форма флага несёт csrf_token",
      html_p.count(FORM_MARK) == 1 and 'name="csrf_token" value="csrf"' in html_p)


def _summary():
    print(f"\n{'ВСЕ ОК' if not FAILS else 'ПРОВАЛЫ: ' + ', '.join(FAILS)}")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    _summary()
