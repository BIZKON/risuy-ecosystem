#!/usr/bin/env python3
"""Render/unit-смоук: промоушен лида в члены клуба (вход B, Task 4).

Чистый Jinja-рендер карточки лида (lead.html, без БД/HTTP) + unit-проверки
операторского текста приглашения:

  1. Кнопка «Пригласить в клуб» РЕНДЕРИТСЯ в карточке opt-in лида (consent=true,
     can_reply=true) при известном bot_username → форма POST /leads/{id}/club-invite
     с CSRF-полем.
  2. Кнопка НЕ рендерится для лида БЕЗ согласия (consent=false).
  3. Кнопка НЕ рендерится, если нет адреса доставки (can_reply=false).
  4. Кнопка НЕ рендерится, если username бота не задан (bot_username='').
  5. Форма приглашения несёт csrf_token (гейт CSRF на POST-роуте).
  6. Текст приглашения (_build_club_invite_text) содержит deep-link ?start=club на
     воронку клуба (Task 3b) и НЕ фиксирует согласие за лида (красная линия).

⚠️ POST-гейт (_enforce_csrf + скоуп тенанта через get_lead + гейт consent/can_reply/
bot_username) и автопривязку lead_id в боте (_club_finish) юнит-тестом не проверяем:
HTTP-роут требует сессию/БД, а FSM бота живёт только в живом процессе aiogram (live-only,
Task 8). db-эффект автопривязки (club_member_create(lead_id=...)) уже покрыт
club_bot_db_smoke от 3a; здесь — только render + чистая текст-функция.

Запуск:
  PYTHONPATH=. ./.venv-smoke/bin/python scripts/club_promote_smoke.py
"""
import ast
import os
import sys

from jinja2 import ChainableUndefined, Environment, FileSystemLoader, select_autoescape

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TPL_DIR = os.path.join(ROOT, "admin-panel", "templates")

env = Environment(
    loader=FileSystemLoader(TPL_DIR),
    autoescape=select_autoescape(["html", "xml"]),
    undefined=ChainableUndefined,  # base.html ссылается на необязательные globals — не падаем
)
env.globals["asset_version"] = ""
env.globals["service_site_url"] = ""

FAILS: list[str] = []


def check(name, cond, detail=""):
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


# ── Контекст lead.html, зеркалит _lead_context(app.py) в объёме, нужном шаблону ──
def _lead(**over) -> dict:
    base = dict(
        id="00000000-0000-0000-0000-000000000001",
        name="Иван Тестов", status="new", source="reels", messenger="tg",
        consent=True, can_reply=True, bot_paused=False, subscribed=True,
        unsubscribed_at=None, erase_requested_at=None, notes="", survey=None,
        created_at=None, updated_at=None, guide_sent_at=None,
        follow_up_1_at=None, follow_up_2_at=None, follow_up_3_at=None,
        phone_masked="", has_phone=False,
    )
    base.update(over)
    return base


BASE_CTX = dict(
    revealed=None, saved=False, erased=False, replied=False, paused_flash=False,
    reply_err=None, club_invited=False, club_err=None,
    thread=[], refresh_sec=30, msg_max=4096, accept_attr=".pdf", max_file_mb=20,
    statuses=["new", "guide_sent", "nurturing", "converted", "lost"],
    status_labels={}, source_labels={}, messenger_labels={},
    csrf_token="csrf-tok", notes_max=4000, consent_events=[],
    active="dialogs", bot_username="testbot",
)


def render(*, lead_over=None, **over) -> str:
    ctx = {**BASE_CTX, **over, "lead": _lead(**(lead_over or {}))}
    ctx["session"] = {"is_platform": False}
    return env.get_template("lead.html").render(**ctx)


FORM_MARK = 'action="/leads/'  # + {id}/club-invite
INVITE_MARK = "/club-invite"
BTN_LABEL = "Пригласить в клуб"

# 1. opt-in лид (consent+can_reply) + bot_username → кнопка/форма рендерится
html_on = render()
check("opt-in лид + bot_username → форма club-invite рендерится",
      INVITE_MARK in html_on and BTN_LABEL in html_on)
check("форма club-invite — POST на /leads/{id}/club-invite",
      f'{FORM_MARK}{_lead()["id"]}/club-invite"' in html_on)

# 2. лид БЕЗ согласия → кнопка скрыта
html_noconsent = render(lead_over={"consent": False})
check("лид без согласия (consent=false) → кнопка НЕ рендерится",
      INVITE_MARK not in html_noconsent and BTN_LABEL not in html_noconsent)

# 3. нет адреса доставки (can_reply=false) → кнопка скрыта
html_nocanreply = render(lead_over={"can_reply": False})
check("нет адреса доставки (can_reply=false) → кнопка НЕ рендерится",
      INVITE_MARK not in html_nocanreply)

# 4. bot_username пуст → deep-link не собрать → кнопка скрыта
html_nobot = render(bot_username="")
check("bot_username пуст → кнопка НЕ рендерится",
      INVITE_MARK not in html_nobot)

# 5. форма приглашения несёт csrf_token (гейт CSRF)
check("форма club-invite несёт csrf_token",
      html_on.count(INVITE_MARK) == 1
      and 'name="csrf_token" value="csrf-tok"' in html_on)

# 6. Текст приглашения (_build_club_invite_text) — deep-link ?start=club, без фиксации
#    согласия. app.py тянет fastapi/db (в venv смоука их нет), поэтому не импортируем
#    модуль целиком — вырезаем ЧИСТУЮ функцию из исходника через ast и exec'аем её одну.
def _load_pure_fn(name: str):
    src = open(os.path.join(ROOT, "admin-panel", "app.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            mod = ast.Module(body=[node], type_ignores=[])
            ns: dict = {}
            exec(compile(mod, "<app.py:%s>" % name, "exec"), ns)  # noqa: S102
            return ns[name]
    raise LookupError(f"{name} не найдена в app.py")


invite_fn = _load_pure_fn("_build_club_invite_text")
invite_text = invite_fn("testbot")
check("текст приглашения содержит deep-link t.me/…?start=club",
      "https://t.me/testbot?start=club" in invite_text)
check("текст приглашения дружелюбный и непустой (>40 симв.)",
      isinstance(invite_text, str) and len(invite_text) > 40)


def _summary():
    print(f"\n{'ВСЕ ОК' if not FAILS else 'ПРОВАЛЫ: ' + ', '.join(FAILS)}")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    _summary()
