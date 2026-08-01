#!/usr/bin/env python3
"""Рендер-смоук кабинета после фикса «Запас» (subscription / usage / account) + тесты денег.

Без БД и HTTP: чистый Jinja по шаблонам панели.

⚠️ ВАЖНО ПРО ПРЕДЫДУЩУЮ ВЕРСИЮ ЭТОГО СМОУКА: она подавала plans=[] и invoices=[], поэтому
«запрещённые» строки проверялись на НЕотрендеренных блоках и смоук был ложно-зелёным —
на той же странице с реальными тарифами жили «сообщений ИИ» и «7,5 ₽ за сообщение».
Здесь тарифы берутся из НАСТОЯЩЕГО admin-panel/config.py, а история рендерится непустой.

Undefined — дефолтный (как в проде: Jinja2Templates не ставит StrictUndefined), иначе
смоук свалится на служебных флагах вроде paid_flash.

Запуск:  python3 scripts/cabinet_billing_ui_smoke.py
"""
from __future__ import annotations

import os
import re
import sys
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "admin-panel"))
sys.path.insert(0, ROOT)

# config.py требует четыре обязательные переменные — подставляем фиктивные, чтобы читать
# НАСТОЯЩИЙ каталог тарифов, а не его копию в тесте.
os.environ.setdefault("DATABASE_URL", "postgresql://smoke/smoke")
os.environ.setdefault("SESSION_SECRET", "x" * 32)
os.environ.setdefault("ADMIN_USERNAME", "smoke")
os.environ.setdefault(                       # config требует argon2-PHC строку, не plain
    "ADMIN_PASSWORD_HASH",
    "$argon2id$v=19$m=65536,t=3,p=4$c21va2VzbW9rZXNtb2tl$c21va2VzbW9rZXNtb2tlc21va2VzbW9rZXNt",
)

import config                                          # noqa: E402
from jinja2 import Environment, FileSystemLoader       # noqa: E402
from shared.money import micro_to_rub_str              # noqa: E402

TPL = os.path.join(ROOT, "admin-panel", "templates")
FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ✓ " if cond else "  ✗ ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


env = Environment(loader=FileSystemLoader(TPL), autoescape=True)
now = datetime.now(timezone.utc)


def plans_for(current_key=None):
    """Зеркало app._plans_for_picker: тот же набор ключей, тот же порядок."""
    return [{"key": k, "is_current": (k == current_key), **config.SERVICE_PLANS[k]}
            for k in config.SERVICE_PLAN_ORDER]


def invoices():
    return [
        {"id": "1", "period_start": now - timedelta(days=34), "period_end": now - timedelta(days=4),
         "plan_name": "Стартовый", "amount_display": "7 500 ₽", "status": "paid",
         "paid_at": now - timedelta(days=34), "card_last4": "4242"},
        {"id": "2", "period_start": now - timedelta(days=64), "period_end": now - timedelta(days=34),
         "plan_name": "Эконом", "amount_display": "3 750 ₽", "status": "paid",
         "paid_at": now - timedelta(days=64), "card_last4": None},
    ]


def wallet(available="3 412,50", debt="0", has_debt=False, pool="3 250",
           burned=False, topup="162,50", rate="1,5", exists=True, has_paid=False):
    return {
        "available": available, "debt": debt, "has_debt": has_debt, "pool": pool,
        "pool_expires_at": now + timedelta(days=26), "pool_burned": burned,
        "topup": topup, "rate": rate, "wallet_exists": exists, "has_paid": has_paid,
        "topup_min": 100, "topup_max": 100000, "receipt_required": True, "payments": [],
    }


def sub(exists=True, canceled=False, expired=False, amount="7 500 ₽", key="start", name="Стартовый"):
    return {
        "exists": exists, "active": exists and not canceled and not expired,
        "canceled": canceled, "expired": expired, "plan_key": key if exists else None,
        "plan_name": name, "amount_display": amount,
        "period_start": now - timedelta(days=4), "period_end": now + timedelta(days=26),
    }


BASE = {
    "csrf_token": "t", "session": {"is_platform": False}, "active": "subscription",
    "yookassa_enabled": True, "support_url": "", "receipt_required": True,
    "contact_url": "", "period_days": 30, "economics": None,
}

# Строки снятой модели биллинга: их не должно быть НИГДЕ на клиентской странице,
# включая карточки тарифов (именно на них прошлая версия смоука была слепа).
FORBIDDEN = ["Превышение", "сообщений ИИ", "сообщение ИИ", "сообщения ИИ", "Отвязать карту",
             "Запросы по тарифу", "Экономика сервиса", "Себестоимость", "Маржа",
             "2 250", "7,5 ₽", "сверх тарифа", "Стоимость сообщения"]


def render(tpl, ctx):
    return env.get_template(tpl).render(**ctx)


def full(**over):
    ctx = {**BASE, "sub": sub(), "wallet": wallet(), "plans": plans_for("start"),
           "invoices": invoices()}
    ctx.update(over)
    return render("subscription.html", ctx)


print("ДЕНЬГИ — форматирование (заявленный фикс знака)")
for micro, want in [(-148808346, "-148,81"), (-1000500000, "-1 000,50"), (-1500000, "-1,50"),
                    (-10000, "-0,01"), (-1, "0"), (0, "0"), (3750000000, "3 750"),
                    (3412500000, "3 412,50")]:
    got = micro_to_rub_str(micro)
    check(f"{micro} → {want!r}", got == want, f"получено {got!r}")

print("ДЕНЬГИ — второй форматтер панели _fmt_amount (знак у отрицательных)")
# Отдельная от micro_to_rub_str функция с тем же дефектом: маржа в блоке «Экономика
# сервиса» печаталась как «-58,-21 ₽», а -0,21 вообще теряла знак («0,-21»).
# Требует зависимостей панели (asyncpg, fastapi); без них проверка пропускается.
_cwd = os.getcwd()
try:
    os.chdir(os.path.join(ROOT, "admin-panel"))      # app.py грузит templates/ относительно cwd
    import app as panel_app                          # noqa: E402
    for v, want in [("-58.21", "-58,21"), ("-1990.50", "-1 990,50"), ("-0.21", "-0,21"),
                    ("-1000", "-1 000"), ("0", "0"), ("1990.50", "1 990,50")]:
        got = panel_app._fmt_amount(v)
        check(f"_fmt_amount({v}) → {want!r}", got == want, f"получено {got!r}")
except ImportError as exc:
    print(f"  · пропущено: нет зависимостей панели ({exc.name}); "
          f"запустите в окружении с admin-panel/requirements.txt")
finally:
    os.chdir(_cwd)

print("СОСТОЯНИЕ 1 — подписка активна, пул жив, аванс есть (с РЕАЛЬНЫМИ тарифами)")
html = full()
check("есть «Доступно»", "Доступно:" in html)
check("есть «Включённый лимит»", "Включённый лимит:" in html)
check("есть «Аванс»", "Аванс:" in html)
check("есть курс тарификации", "Курс тарификации" in html)
check("нет обещания автосписания", "Следующий платёж" not in html)
for bad in FORBIDDEN:
    check(f"нет «{bad}»", bad not in html)

print("ПРОДЛЕНИЕ — свой тариф покупаем ТОЛЬКО по окончании периода")
# Досрочная оплата того же плана уходит в pool_mode='overwrite' и обнуляет неизрасходованный
# лимит, а оферта п. 4.5 сжигает остаток лишь по окончании расчётного периода.
check("при живом периоде продление заблокировано", "Продление после" in html)
check("объяснено, почему нельзя досрочно", "обнулила бы остаток" in html)
n_live = html.count('action="/subscription/select"')
check("формы для своего тарифа при живом периоде нет", n_live == 1, f"форм: {n_live}")
check("мёртвой кнопки «Ваш текущий тариф» нет", "Ваш текущий тариф" not in html)

html_exp = full(sub=sub(expired=True), wallet=wallet(available="162,50", pool=None, burned=True))
check("после окончания периода форма продления появляется", "Продлить тариф" in html_exp)
n_exp = html_exp.count('action="/subscription/select"')
check("после окончания периода формы у обоих тарифов", n_exp == 2, f"форм: {n_exp}")

print("ПУСТАЯ ИСТОРИЯ — текст зависит от РЕАЛЬНО прошедших оплат")
html_pend = full(invoices=[], wallet=wallet(has_paid=False))
check("без успешных оплат — предложение оплатить", "оплатите первый период" in html_pend)
html_succ = full(invoices=[], wallet=wallet(has_paid=True))
check("после успешной оплаты предложения оплатить нет", "оплатите первый период" not in html_succ)
check("после успешной оплаты отсылка к «Запасу»", "показаны выше" in html_succ)

print("ИСТОРИЯ — сетка переверстана с 6 колонок на 4")
head = re.search(r'bhistory__head[^>]*>(.*?)</div>', html, re.S)
check("шапка истории отрендерилась", head is not None)
if head:
    n = len(re.findall(r"<span", head.group(1)))
    check("в шапке ровно 4 колонки", n == 4, f"колонок: {n}")
row = re.search(r'bperiod__row[^>]*>(.*?)</summary>', html, re.S)
if row:
    n = len(re.findall(r"<span", row.group(1)))
    check("в строке периода ровно 4 колонки", n == 4, f"колонок: {n}")
check("в истории нет «+ превышение»", "+ превышение" not in html)

print("СОСТОЯНИЕ 2 — кошелька нет, подписки нет (7 тенантов из 9 на проде)")
html2 = full(sub=sub(exists=False), plans=plans_for(None),
             wallet=wallet(available="0", pool=None, topup="0", rate=None, exists=False),
             invoices=[])
check("тариф «НЕ ВЫБРАН»", "НЕ ВЫБРАН" in html2)
check("подсказка про пустой счёт", "Лицевой счёт пока пуст" in html2)
check("курс скрыт", "Курс тарификации:" not in html2)
check("пустая история объяснена", "Платежей пока нет" in html2)

print("СОСТОЯНИЕ 3 — период истёк, пул сгорел")
html3 = full(sub=sub(expired=True), wallet=wallet(available="162,50", pool=None, burned=True))
check("сказано, что остаток не перенесён", "не перенесён" in html3)
check("виден признак истёкшего периода", "период истёк" in html3)

print("СОСТОЯНИЕ 4 — задолженность (единственный живой кошелёк на проде)")
html4 = full(wallet=wallet(available="7 351,19", debt="148,81", has_debt=True, pool="7 500", topup="0"))
check("есть строка задолженности", "Задолженность:" in html4)
check("объяснено, что услуги оказаны", "услуги уже оказаны" in html4)
check("сходится: лимит и долг показаны рядом", "7 500" in html4 and "148,81" in html4)

print("СОСТОЯНИЕ 5 — договорной тариф (price_microrub = 0)")
html5 = full(sub=sub(amount=None, key="custom", name="Индивидуальный"), plans=plans_for("custom"))
# Проверяем ИМЕННО строку текущего тарифа: подстрока «0 ₽ за период» встречается и в
# карточке «3 750 ₽ за период», поэтому наивный поиск по всей странице даёт ложный провал.
head5 = re.search(r'billing-card__sub[^>]*>(.*?)</p>', html5, re.S)
check("строка тарифа отрендерилась", head5 is not None)
if head5:
    line = head5.group(1)
    check("в строке тарифа нет суммы за период", "₽ за период" not in line, line.strip()[:90])
    check("показана договорная цена", "Цена договорная" in line)

print("СОСТОЯНИЕ 6 — подписка отменена")
html6 = full(sub=sub(canceled=True))
check("видно, что отменена", "подписка отменена" in html6)
check("кнопки отмены больше нет", "Отменить подписку" not in html6)

print("ГРАНИЦА ДАННЫХ — экономика платформы")
econ = {"rows": [], "used_tokens": "1", "cost": "1", "revenue": "1", "margin": "1",
        "margin_negative": False, "balance": None, "days_left": None, "monthly_cost": None,
        "price_in": "1", "price_out": "1", "price_blended": "1", "out_share_pct": 30}
check("клиенту экономика не видна", "Экономика сервиса" not in html)
check("платформе экономика видна",
      "Экономика сервиса" in full(session={"is_platform": True}, economics=econ))

print("СОСЕДНИЕ ЭКРАНЫ")
usage = render("usage.html", {**BASE, "active": "usage", "rows": [], "daily": [],
                              "balance": "3 412,50", "balance_negative": False,
                              "charged_14d": "0", "ai_blocked": False})
check("usage.html рендерится", len(usage) > 100)
acct = render("account.html", {**BASE, "active": "account", "is_platform": False,
                               "can_edit_name": True, "can_edit_password": True,
                               "login": "client_x", "role_label": "Владелец кабинета",
                               "display_name": "", "email": "a@b.ru", "email_verified": True,
                               "created_at": now, "login_methods": [], "linking_hint": False,
                               "tenant_name": "Клиент", "tenant_status": "active",
                               "wallet": wallet(debt="148,81", has_debt=True),
                               "password_min": 10, "name_max": 60, "saved": None, "err": None})
check("account.html показывает «Доступно»", "Доступно" in acct)
check("account.html показывает задолженность", "Задолженность" in acct)

print()
if FAILS:
    print(f"ПРОВАЛЕНО: {len(FAILS)}")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("ВСЁ ЗЕЛЁНОЕ")
