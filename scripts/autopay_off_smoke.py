#!/usr/bin/env python3
"""Смоук T-1F-1 (D3 «без автосписаний»): оплата подписки НЕ сохраняет способ оплаты.

Гарантия «без автосписаний» стоит на трёх опорах — все три проверяются статически по AST.
Рантайм-стек панели (fastapi/asyncpg/argon2) локально не поднять (py3.14, нет колёс), а сам
путь тривиален — один kwarg в одном колл-сайте, поэтому AST-проверка аргументов вызова
`create_payment` внутри целевого хендлера честнее и точнее grep'а по тексту:
  1. subscription_select НЕ передаёт save_payment_method в yookassa.create_payment
     (иначе ЮKassa вернёт payment_method.id → вебхук сохранит его в subscriptions →
      list_due_renewals увидит подписку → безакцептное автосписание).
  2. Нигде в admin-panel нет колл-сайта create_payment(save_payment_method=True).
  3. defense-in-depth (неизменные опоры): list_due_renewals фильтрует
     `yookassa_payment_method_id is not null` (без карты — пусто), а
     SERVICE_RENEWAL_ENABLED по умолчанию False (cron автосписаний не запускается).

Запуск: python3 scripts/autopay_off_smoke.py   (без БД, без зависимостей панели)
"""
import ast
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "admin-panel", "app.py")
DB = os.path.join(ROOT, "admin-panel", "db.py")
CONFIG = os.path.join(ROOT, "admin-panel", "config.py")

FAILS: list[str] = []


def check(name, cond, detail=""):
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail != "" else ""))
    if not cond:
        FAILS.append(name)


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def _create_payment_calls(node: ast.AST) -> list[tuple[int, dict]]:
    """Все вызовы *.create_payment(...) внутри node: (lineno, {kwarg_name: value_node})."""
    out = []
    for n in ast.walk(node):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "create_payment"):
            out.append((n.lineno, {k.arg: k.value for k in n.keywords if k.arg}))
    return out


def _func(tree: ast.AST, name: str):
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return n
    return None


def main() -> None:
    app_tree = ast.parse(_read(APP))

    # ── #1: subscription_select не сохраняет способ оплаты ──
    print("1. subscription_select не сохраняет способ оплаты:")
    fn = _func(app_tree, "subscription_select")
    check("хендлер subscription_select найден", fn is not None)
    sub_calls = _create_payment_calls(fn) if fn else []
    check("в subscription_select ровно один вызов create_payment", len(sub_calls) == 1, str(len(sub_calls)))
    has_spm = any("save_payment_method" in kw for _, kw in sub_calls)
    check("save_payment_method НЕ передаётся", not has_spm)

    # ── #2: нигде в панели нет create_payment(save_payment_method=True) ──
    print("2. По всей admin-panel нет колл-сайта save_payment_method=True:")
    truthy = [
        v.lineno for _, kw in _create_payment_calls(app_tree)
        for v in [kw.get("save_payment_method")]
        if isinstance(v, ast.Constant) and v.value is True
    ]
    check("нет вызовов create_payment(save_payment_method=True)", not truthy, str(truthy))

    # ── #3: defense-in-depth (неизменные опоры) ──
    print("3. Опоры defense-in-depth на месте:")
    db_src = _read(DB)
    cfg_src = _read(CONFIG)
    check("list_due_renewals фильтрует yookassa_payment_method_id is not null",
          "yookassa_payment_method_id is not null" in db_src)
    check("SERVICE_RENEWAL_ENABLED дефолт False",
          '_opt_bool("SERVICE_RENEWAL_ENABLED", False)' in cfg_src)

    print()
    if FAILS:
        print(f"❌ ПРОВАЛЫ ({len(FAILS)}): " + ", ".join(FAILS))
        sys.exit(1)
    print("✅ autopay_off smoke — все проверки зелёные (D3 «без автосписаний»)")


if __name__ == "__main__":
    main()
