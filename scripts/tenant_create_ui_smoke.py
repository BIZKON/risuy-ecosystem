#!/usr/bin/env python3
"""Render-смоук «Создать клиента» в /tenants: форма создания показана ТОЛЬКО платформе,
тенанту — нет; флеши created/err; кнопка «Сделать активным» в строках. Чистый Jinja (без БД/HTTP).
undefined=Undefined (как прод Jinja2Templates).

  PYTHONPATH=. ./.venv-smoke/bin/python scripts/tenant_create_ui_smoke.py
"""
import os
import sys

from jinja2 import Environment, FileSystemLoader, Undefined, select_autoescape

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
env = Environment(
    loader=FileSystemLoader(os.path.join(ROOT, "admin-panel", "templates")),
    autoescape=select_autoescape(["html", "xml"]),
    undefined=Undefined,
)
env.globals["asset_version"] = ""
env.globals["service_site_url"] = ""

FAILS = []
def check(n, c):  # noqa: E302
    print(f"  {'OK ' if c else 'FAIL'} {n}")
    if not c:
        FAILS.append(n)


class Sess:
    def __init__(self, is_platform):
        self.is_platform = is_platform
        self.active_tenant_id = None
        self.active_tenant_name = None
        self.csrf_token = "csrf"
        self.actor = "admin" if is_platform else "client_x"
        self.username = self.actor
        self.role = "admin" if is_platform else "operator"


def render(**ctx):
    base = dict(csrf_token="csrf", active="tenants", tenants=[], switched_flash=False,
                created_flash=False, create_err=None)
    return env.get_template("tenants.html").render(**{**base, **ctx})


PLAT = Sess(is_platform=True)
TEN = Sess(is_platform=False)
ROWS = [{"id": "11111111-1111-1111-1111-111111111111", "name": "Кабинет владельца",
         "slug": "client-abc123", "status": "active"}]

print("tenants.html — форма создания")
html = render(session=PLAT, tenants=[])
check("платформа: форма «Создать клиента» показана", "Создать клиента" in html and 'action="/tenants/create"' in html)
check("платформа: поле name + required", 'name="name"' in html and "required" in html)
check("платформа: empty-state «Клиентов пока нет» + форма", "Клиентов пока нет" in html)
check("заглушка Wave 2/3 убрана", "Wave 2/3" not in html)

html = render(session=PLAT, tenants=ROWS)
check("платформа со строками: форма создания + кнопка «Сделать активным»", "Создать клиента" in html and "Сделать активным" in html)
check("платформа: имя клиента в строке", "Кабинет владельца" in html)

html = render(session=TEN, tenants=ROWS)
check("тенант: формы создания НЕТ", "Создать клиента" not in html and 'action="/tenants/create"' not in html)

print("tenants.html — флеши")
html = render(session=PLAT, created_flash=True)
check("created → флеш «Клиент создан и сделан активным»", "Клиент создан и сделан активным" in html)
html = render(session=PLAT, create_err="Введите название клиента.")
check("err → флеш ошибки", "Введите название клиента." in html)


print("\n" + ("ВСЕ ОК" if not FAILS else "ПРОВАЛЫ: " + ", ".join(FAILS)))
sys.exit(1 if FAILS else 0)
