#!/usr/bin/env python3
"""Render-смоук: панель знакомств «Клуб» на /club (club.html, Task 7b-панель).
Чистый Jinja-рендер (без БД/HTTP):
- кнопка «Предложить знакомство» рендерится ТОЛЬКО у рекомендации, где ОБА участника
  (карточка m и рекомендованный rec) достижимы (есть lead_id) — иначе бот не сможет
  доставить уведомление/контакты;
- форма несёт csrf_token + from_member/to_member id + POST на /club/intro;
- секция «Предложенные знакомства»: requested → «ожидает ответа», контактов нет;
  accepted → контакты обоих участников видны (из фикстуры reveal); declined → «отклонено»;
- регресс club_catalog_ui_smoke (каталог/фильтр/рекомендации без кнопки) не сломан.
Запуск:
  PYTHONPATH=. ./.venv-smoke/bin/python scripts/club_intro_ui_smoke.py
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


MEMBER_M_ID = "11111111-1111-1111-1111-111111111111"       # достижим (lead_id есть)
MEMBER_P_REACHABLE_ID = "22222222-2222-2222-2222-222222222222"  # достижим
MEMBER_P_UNREACHABLE_ID = "33333333-3333-3333-3333-333333333333"  # НЕ достижим (без lead_id)

MEMBERS = [
    {
        "id": MEMBER_M_ID,
        "display_name": "ИП Соколова Анна",
        "city": "Казань",
        "okved": "62.01",
        "status": "active",
        "inn": "165000000000",
        "lead_id": "aaaaaaaa-0000-0000-0000-000000000001",
        "prospect": None,
        "matches": [
            {
                "id": MEMBER_P_REACHABLE_ID,
                "display_name": "ООО «Стройсервис»",
                "match_score": 85,
                "match_reason": "Он до вас в цепочке, тот же город",
                "lead_id": "bbbbbbbb-0000-0000-0000-000000000002",
            },
            {
                "id": MEMBER_P_UNREACHABLE_ID,
                "display_name": "ООО «Без Телеграма»",
                "match_score": 60,
                "match_reason": "Смежный ОКВЭД",
                "lead_id": None,
            },
        ],
    },
    {
        "id": MEMBER_P_UNREACHABLE_ID,
        "display_name": "ООО «Без Телеграма»",
        "city": "Москва",
        "okved": "41.20",
        "status": "active",
        "inn": "7700000000",
        "lead_id": None,  # M недостижим для этой карточки — кнопка тоже не должна рендериться
        "prospect": None,
        "matches": [
            {
                "id": MEMBER_M_ID,
                "display_name": "ИП Соколова Анна",
                "match_score": 60,
                "match_reason": "Смежный ОКВЭД",
                "lead_id": "aaaaaaaa-0000-0000-0000-000000000001",
            },
        ],
    },
]

INTROS = [
    {
        "id": "c0000000-0000-0000-0000-000000000001",
        "status": "requested",
        "from": {"display_name": "ИП Соколова Анна"},
        "to": {"display_name": "ООО «Стройсервис»"},
        "reveal": None,
    },
    {
        "id": "c0000000-0000-0000-0000-000000000002",
        "status": "accepted",
        "from": {"display_name": "ИП Соколова Анна"},
        "to": {"display_name": "ООО «Партнёр»"},
        "reveal": {
            "from": {"display_name": "ИП Соколова Анна", "lead_phone": "+7 900 111-22-33"},
            "to": {"display_name": "ООО «Партнёр»", "lead_phone": "+7 900 444-55-66"},
        },
    },
    {
        "id": "c0000000-0000-0000-0000-000000000003",
        "status": "declined",
        "from": {"display_name": "ООО «Стройсервис»"},
        "to": {"display_name": "ИП Соколова Анна"},
        "reveal": None,
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
    support_url="",
    help_dismissed=True,  # help_card не относится к этому смоуку — не шумим
    intros=INTROS,
    intro_flash=None,
    intro_err=None,
)


def render(**over):
    ctx = {**BASE_CTX, **over}
    return env.get_template("club.html").render(**ctx)


INTRO_FORM_MARK = 'action="/club/intro"'
INTRO_BTN_MARK = "Предложить знакомство"

# 1. Кнопка «Предложить знакомство» рендерится для пары достижимых участников
#    (M=Соколова lead_id есть, rec=Стройсервис lead_id есть).
html = render()
check("кнопка рендерится хотя бы раз (пара достижима)", INTRO_BTN_MARK in html)
check("форма несёт action=/club/intro", INTRO_FORM_MARK in html)

# 2. Кнопка НЕ рендерится, если rec (получатель) без lead_id — «Без Телеграма».
#    Считаем вхождения формы: должно быть ровно одна (Соколова→Стройсервис), не две.
n_forms = html.count(INTRO_FORM_MARK)
check("кнопка не рендерится для недостижимого получателя (ровно 1 форма, не 2)", n_forms == 1, f"n={n_forms}")

# 3. Кнопка несёт csrf_token + from_member/to_member id нужной пары.
check("форма несёт csrf_token", 'name="csrf_token" value="csrf"' in html)
check("форма несёт from_member=M.id", f'name="from_member" value="{MEMBER_M_ID}"' in html)
check("форма несёт to_member=rec.id (достижимый)", f'name="to_member" value="{MEMBER_P_REACHABLE_ID}"' in html)
check(
    "to_member недостижимого получателя НЕ уходит в форму",
    f'name="to_member" value="{MEMBER_P_UNREACHABLE_ID}"' not in html,
)

# 3b. Симметрия: если M (карточка-владелец) сам недостижим — кнопка тоже не рендерится,
# даже если рекомендованный партнёр достижим (карточка «Без Телеграма» → rec=Соколова).
html_m_unreachable = render(members=[MEMBERS[1]])
check(
    "кнопка не рендерится, когда M (владелец карточки) недостижим",
    INTRO_BTN_MARK not in html_m_unreachable,
)

# 4. Секция «Предложенные знакомства»: requested → «ожидает ответа», контактов нет.
check("секция «Предложенные знакомства» рендерится", "Предложенные знакомства" in html)
check("requested — статус «ожидает ответа»", "ожидает ответа" in html)
check("requested — телефон Стройсервиса (accepted-контакт) отсутствует у requested-строки",
      html.count("+7 900") == 2)  # только у accepted-intro (from+to), не у requested/declined

# 5. accepted → контакты обоих видны.
check("accepted — статус «принято»", "принято" in html)
check("accepted — контакт from виден", "+7 900 111-22-33" in html)
check("accepted — контакт to виден", "+7 900 444-55-66" in html)

# 6. declined → «отклонено», без контактов.
check("declined — статус «отклонено»", "отклонено" in html)

# 7. Пустой список intros → empty-state.
html_no_intros = render(intros=[])
check("без intros — «Пока нет предложенных знакомств»", "Пока нет предложенных знакомств" in html_no_intros)

# 8. Флеш-сообщения PRG (?intro=1 / ?intro_err=...).
html_flash = render(intro_flash="1", intro_err=None)
check("intro_flash — сообщение «Знакомство предложено.»", "Знакомство предложено." in html_flash)
html_err = render(intro_flash=None, intro_err="Один из участников не найден в клубе этого клиента.")
check("intro_err — текст ошибки рендерится", "Один из участников не найден" in html_err)

# 9. Регресс club_catalog_ui_smoke: базовые элементы каталога всё ещё на месте.
check("регресс — участник рендерится (display_name)", "ИП Соколова Анна" in html)
check("регресс — фильтр город/ОКВЭД на месте", 'action="/club"' in html and 'name="city"' in html)
check("регресс — секция рекомендаций на месте", "Рекомендуем познакомиться" in html)


def _summary():
    print(f"\n{'ВСЕ ОК' if not FAILS else 'ПРОВАЛЫ: ' + ', '.join(FAILS)}")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    _summary()
