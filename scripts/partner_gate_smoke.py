#!/usr/bin/env python3
"""Смоук гейта принципала «партнёр» — БЕЗ базы: проверяет РЕШЕНИЕ гейта на всех
маршрутах FastAPI, а не ответы сервера.

  PYTHONPATH=admin-panel:. \
    /Users/konstantin/Downloads/risuy-ecosystem/.venv-smoke/bin/python \
    scripts/partner_gate_smoke.py

🔴 Смысл блока 3: белый список обязан оставаться белым. Маршрут, добавленный завтра
другой сессией, должен быть закрыт партнёру по умолчанию — иначе через полгода в его
кабинете окажется чужая воронка.
"""
import os
import pathlib
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "admin-panel"))
os.environ.setdefault("DATABASE_URL", "postgresql://x/y")
os.environ.setdefault("SESSION_SECRET", "smoke-secret-padding-0123456789abcdef")
os.environ.setdefault("ADMIN_USERNAME", "smoke")
os.environ.setdefault("ADMIN_PASSWORD_HASH", "$argon2id$v=19$m=65536,t=3,p=4$c21va2U$c21va2U")

import auth  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


print("1. Партнёру открыт только его префикс:")
check("/partner открыт", auth.partner_may_access("/partner"))
check("/partner/team открыт", auth.partner_may_access("/partner/team"))
check("/partner/join/abc открыт", auth.partner_may_access("/partner/join/abc"))

print("2. Всё остальное закрыто — включая то, чего ещё нет:")
for path in ["/", "/companies", "/partners", "/partners/123", "/tenants", "/subscription",
             "/agents", "/brief-center/1", "/leads", "/orders", "/shop",
             "/partnership-secret-new-route", "/partnerX", "/partner-admin",
             "/../partner", "/partner/../companies", "/api/demo-chat", "//partner",
             "partner", "", "/PARTNER"]:
    check(f"{path!r} закрыт", not auth.partner_may_access(path))

print("3. Перебор ВСЕХ маршрутов приложения — открытым может быть только /partner/*:")
# Маршруты берём СТАТИЧЕСКИ из декораторов, а не импортом приложения: fastapi в venv
# смоуков нет, и ставить его ради проверки незачем. Статический разбор вдобавок честнее —
# он не зависит от окружения и увидит маршрут, добавленный кем угодно и когда угодно.
ROUTE_RE = re.compile(r"^@app\.(?:get|post|put|patch|delete|head|options)\(\s*[\"']([^\"']+)[\"']",
                      re.MULTILINE)
source = pathlib.Path(ROOT, "admin-panel", "app.py").read_text(encoding="utf-8")
routes = sorted(set(ROUTE_RE.findall(source)))
opened = [p for p in routes if auth.partner_may_access(p)]
stray = [p for p in opened if not (p == "/partner" or p.startswith("/partner/"))]
check("посторонних открытых маршрутов нет", not stray, f"лишние: {stray}")
check("маршруты вообще нашлись", len(routes) > 50, f"найдено {len(routes)}")
print(f"  (всего маршрутов: {len(routes)}, открыто партнёру: {len(opened)} — {opened})")

print("4. Отказ гейта — исключением проекта, а не необъявленным именем:")
# 🔴 Найдено сквозной проверкой на проде 16.08: гейт поднимал fastapi.HTTPException,
# которого в app.py НЕТ в импортах (проект пользуется StarletteHTTPException). Каждый
# отказ падал NameError → 500 вместо 403. Гейт при этом держал — но по случайности, а не
# по замыслу, и в логи летела трассировка на каждый промах партнёра.
check("нет raise HTTPException( — только StarletteHTTPException",
      "raise HTTPException(" not in source, "найдено необъявленное имя")
check("StarletteHTTPException импортирован",
      "from starlette.exceptions import HTTPException as StarletteHTTPException" in source)

print("5. Куда партнёр попадает СРАЗУ после входа:")
# 🔴 Регрессия на живой отказ 17.08: партнёр ввёл пароль и получил 403. Форма входа
# отправляет next="/" по умолчанию — а «/» партнёру закрыт. Мой E2E этого не поймал,
# потому что я передавал next=/partner явно, чего живой человек не делает.
check("с дефолтным next=/ ведём в кабинет", auth.landing_for("partner", "/") == "/partner",
      auth.landing_for("partner", "/"))
check("с пустым next тоже", auth.landing_for("partner", "") == "/partner")
check("чужой раздел в next не пропускаем",
      auth.landing_for("partner", "/companies") == "/partner")
check("свой раздел в next уважаем",
      auth.landing_for("partner", "/partner/team") == "/partner/team")
check("оператора не трогаем", auth.landing_for("operator", "/leads") == "/leads")
check("админа не трогаем", auth.landing_for("admin", "/") == "/")

print("6. Страница отказа знает партнёрский случай:")
err = pathlib.Path(ROOT, "admin-panel", "templates", "error.html").read_text(encoding="utf-8")
check("в шаблоне есть ветка partner_denied", "partner_denied" in err)
check("ведёт в кабинет, а не на дашборд", "/partner" in err)
check("признак сравнивается с константой", "auth.PARTNER_DENIED" in source)
# 🔴 Мало объявить признак — надо ПРОВЕСТИ его до страницы. Обработчик исключений
# намеренно выбрасывает exc.detail и подставляет обобщённый текст, поэтому сравнение
# по тексту молча не срабатывало: первая редакция этой правки была мертва.
check("обработчик передаёт признак странице", "partner_denied=denied" in source)
check("страница принимает параметр", "partner_denied: bool = False" in source)

print("7. Шаблон отказа реально меняет ссылки:")
from jinja2 import Environment, FileSystemLoader
env = Environment(loader=FileSystemLoader(pathlib.Path(ROOT, "admin-panel", "templates")))
tpl = env.get_template("error.html")
as_partner = tpl.render(status_code=403, message="Доступ запрещён.", partner_denied=True)
as_other = tpl.render(status_code=403, message="Доступ запрещён.", partner_denied=False)
check("партнёру — в его кабинет", "В мой кабинет" in as_partner, as_partner[:0])
check("партнёру НЕ предлагаем дашборд", "На дашборд" not in as_partner)
check("остальным дашборд на месте", "На дашборд" in as_other)

print()
if FAILS:
    print(f"ПРОВАЛЕНО: {len(FAILS)} — {FAILS}")
    sys.exit(1)
print("ВСЁ ЗЕЛЁНОЕ")
