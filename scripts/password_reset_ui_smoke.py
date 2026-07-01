#!/usr/bin/env python3
"""Render-смоук шаблонов сброса пароля (чистый Jinja, без БД/HTTP/app).
Проверяет: CSRF-поле, action форм, ветки sent и valid/invalid.
  PYTHONPATH=. ./.venv-smoke/bin/python scripts/password_reset_ui_smoke.py
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

FAILS: list[str] = []


def check(name, cond):
    print(f"  {'OK ' if cond else 'FAIL'} {name}")
    if not cond:
        FAILS.append(name)


def render(name, **ctx):
    return env.get_template(name).render(**ctx)


# forgot: форма ввода email
html = render("forgot_password.html", csrf_token="TESTCSRF", sent=False)
check("forgot: CSRF-поле", 'name="csrf_token"' in html and "TESTCSRF" in html)
check("forgot: форма email→/forgot-password", 'action="/forgot-password"' in html and 'name="email"' in html)

# forgot: экран «письмо отправлено»
html = render("forgot_password.html", csrf_token="TESTCSRF", sent=True)
check("forgot: подтверждение при sent=True", "отправ" in html.lower())

# reset: валидный токен → форма нового пароля
html = render("reset_password.html", csrf_token="TESTCSRF", token="TOK", valid=True, err="", password_min=10)
check("reset(valid): форма нового пароля", 'name="new_password"' in html and 'action="/reset-password"' in html)
check("reset(valid): токен в скрытом поле", 'value="TOK"' in html)

# reset: невалидный токен → без формы, есть ссылка на повторный запрос
html = render("reset_password.html", csrf_token="TESTCSRF", token="", valid=False, err="", password_min=10)
check("reset(invalid): нет формы пароля", 'name="new_password"' not in html)
check("reset(invalid): ссылка на /forgot-password", "/forgot-password" in html)

print(f"\n{'🟢 password_reset_ui_smoke зелёный' if not FAILS else 'ПРОВАЛЫ: ' + ', '.join(FAILS)}")
sys.exit(1 if FAILS else 0)
