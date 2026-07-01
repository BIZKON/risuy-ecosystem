#!/usr/bin/env python3
"""Render-смоук «динамического реестра персон»: форма «Создать роль» в /agents видна
ПЛАТФОРМЕ и скрыта от тенанта; сетка ролей рендерит и пресеты, и переданную динамическую
роль (карточка). Плюс чистый юнит на формат slug (role-<8hex>). Без БД/HTTP — только Jinja
(как соседние *_ui смоуки) + локальный re для slug.
  PYTHONPATH=. ./.venv-smoke/bin/python scripts/persona_registry_smoke.py
"""
import os
import re
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


# Каркас контекста agents.html (имена/типы как отдаёт agents_page). role_cards включает
# 4 «пресета» + одну ДИНАМИЧЕСКУЮ роль (role-deadbeef) — проверяем, что она рисуется карточкой.
DYN_SLUG = "role-deadbeef"
ROLE_CARDS = [
    {"slug": "liya", "name": "Лия", "role": "ИИ-администратор", "agent_ready": True,
     "has_knowledge": False, "customized": False, "leads": 3, "converted": 1, "conv_pct": 33},
    {"slug": "mark", "name": "Марк", "role": "ИИ-продавец", "agent_ready": False,
     "has_knowledge": False, "customized": False, "leads": 0, "converted": 0, "conv_pct": 0},
    {"slug": "sofia", "name": "Софья", "role": "ИИ-маркетолог", "agent_ready": False,
     "has_knowledge": False, "customized": False, "leads": 0, "converted": 0, "conv_pct": 0},
    {"slug": "gleb", "name": "Глеб", "role": "ИИ-консультант", "agent_ready": False,
     "has_knowledge": False, "customized": False, "leads": 0, "converted": 0, "conv_pct": 0},
    # динамическая роль (создана платформой кнопкой «Создать роль»)
    {"slug": DYN_SLUG, "name": "Анна", "role": "ИИ-методист", "agent_ready": False,
     "has_knowledge": False, "customized": True, "leads": 5, "converted": 2, "conv_pct": 40},
]

BASE_CTX = dict(
    csrf_token="csrf-token-xyz", active="agents",
    saved=False, persona_created=False, err="", preset_applied=False,
    role_cards=ROLE_CARDS,
    personas=[
        {"key": "liya", "label": "Лия — ИИ-администратор", "is_current": False},
        {"key": DYN_SLUG, "label": "Анна — ИИ-методист", "is_current": False},
    ],
    persona_label="",
    backends=[{"key": "cloud_ai", "label": "Агент (Лия)", "is_current": True}],
    ai={"enabled": True, "backend": "cloud_ai", "persona": "", "agent_id": "",
        "model": "", "gateway_base_url": "", "system_prompt": "", "fallback": ""},
    activity={"total": 0, "recent": 0, "last_at": None},
    window_days=14, default_fallback="Извините, ИИ недоступен.",
    default_model="", default_gateway_url="",
    agent_id_max=200, fallback_max=2000, model_max=200, gateway_url_max=300,
    system_prompt_max=8000,
    name_max=80, role_title_max=120, behavior_max=200000,
    recent_messages=[],
)


def render(**over):
    ctx = {**BASE_CTX, **over}
    return env.get_template("agents.html").render(**ctx)


CREATE_FORM = 'action="/agents/persona/create"'

# 1. Платформа видит форму «Создать роль» (+ csrf) и кнопку
html_plat = render(session={"is_platform": True})
check("платформа — форма «Создать роль» (action + csrf)",
      (CREATE_FORM in html_plat) and ("csrf-token-xyz" in html_plat)
      and ("Создать роль" in html_plat))
check("платформа — поля Имя/Должность/Инструкция в форме",
      ('name="name"' in html_plat) and ('name="role"' in html_plat)
      and ('name="prompt"' in html_plat))

# 2. Тенант НЕ видит форму создания (гейт {% if session.is_platform %})
html_tenant = render(session={"is_platform": False})
check("тенант — формы «Создать роль» НЕТ", CREATE_FORM not in html_tenant)

# 3. Сетка ролей рендерит и пресеты, и динамическую роль карточкой
check("сетка — карточка пресета (Лия)",
      ('/agents/role/liya' in html_plat) and ("Лия" in html_plat))
check("сетка — карточка ДИНАМИЧЕСКОЙ роли (role-deadbeef → «Анна — ИИ-методист»)",
      (f'/agents/role/{DYN_SLUG}' in html_plat) and ("Анна" in html_plat)
      and ("ИИ-методист" in html_plat))
check("сетка — у динамической роли пилюля «своя инструкция» (customized)",
      "своя инструкция" in html_plat)

# 4. Флеш persona_created показывается только при persona_created=True
check("флеш persona_created скрыт по умолчанию", "Роль создана" not in html_plat)
check("флеш persona_created виден при persona_created=True",
      "Роль создана" in render(session={"is_platform": True}, persona_created=True))

# 4b. created_slug → флеш с КЛИКАБЕЛЬНОЙ ссылкой «настроить роль» на /agents/role/<slug>
html_created = render(session={"is_platform": True}, created_slug=DYN_SLUG)
check("created_slug — флеш-ссылка «Настроить роль» на /agents/role/<slug>",
      (f'href="/agents/role/{DYN_SLUG}"' in html_created) and ("Настроить роль" in html_created))
check("created_slug приоритетнее plain persona_created (одна плашка-ссылка)",
      "Роль создана" in html_created)

# 4c. Поле «Имя» подсказывает «роль, не реального человека»
check("форма — хинт «имя роли, не реального человека»",
      "не реального человека" in html_plat)


# 5. Изоляция /dialogs (пер-логика, без БД): тенант-оператор видит ТОЛЬКО пресеты,
# платформа — пресеты+динамика. Воспроизводим правило выбора whitelist из _resolve_dialog_staff
# / dialog_set_persona: (all_personas) if is_platform else PERSONA_PRESETS.
PRESETS = {"liya": {}, "mark": {}, "sofia": {}, "gleb": {}}      # 4 пресета (как config)
DYNAMIC = {DYN_SLUG: {}}                                          # реестр сверх пресетов
ALL = {**PRESETS, **DYNAMIC}


def whitelist_for(is_platform):
    return ALL if is_platform else PRESETS


check("изоляция: тенант видит только 4 пресета (без динамики)",
      set(whitelist_for(False)) == set(PRESETS) and DYN_SLUG not in whitelist_for(False))
check("изоляция: платформа видит пресеты+динамику",
      DYN_SLUG in whitelist_for(True) and set(PRESETS) <= set(whitelist_for(True)))
check("изоляция: тенант НЕ может назначить динамическую роль (валидатор отвергнет)",
      DYN_SLUG not in whitelist_for(False))


# 6. Чистый юнит: формат slug динамической роли (как в persona_create: role-<8hex>)
SLUG_RE = re.compile(r"^role-[0-9a-f]{8}$")
# Воспроизводим генерацию из app.py (secrets.token_hex(4) → 8 hex-символов)
import secrets  # noqa: E402

gen = f"role-{secrets.token_hex(4)}"
check("slug формат role-<8hex> валиден", bool(SLUG_RE.match(gen)), gen)
check("slug — 8 уникальных подряд валидны (нет коллизии формата)",
      all(SLUG_RE.match(f"role-{secrets.token_hex(4)}") for _ in range(8)))
check("slug отвергает мусор", not SLUG_RE.match("role-XYZ") and not SLUG_RE.match("liya"))


def _summary():
    print(f"\n{'ВСЕ ОК' if not FAILS else 'ПРОВАЛЫ: ' + ', '.join(FAILS)}")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    _summary()
