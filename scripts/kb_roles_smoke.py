#!/usr/bin/env python3
"""Чистый смоук СП-2b: normalize_role нормализует role_tag отдела (без БД).
  PYTHONPATH=admin-panel:. ./.venv-smoke/bin/python scripts/kb_roles_smoke.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "admin-panel"))

from knowledge_roles import normalize_role  # noqa: E402

FAILS: list[str] = []


def check(name, cond):
    print(f"  {'OK ' if cond else 'FAIL'} {name}")
    if not cond:
        FAILS.append(name)


ALLOWED = {"sales", "support"}
check("пусто → общая справка ''", normalize_role("", ALLOWED) == "")
check("пробелы → ''", normalize_role("   ", ALLOWED) == "")
check("валидный slug отдела сохраняется", normalize_role("sales", ALLOWED) == "sales")
check("slug с пробелами обрезается и сохраняется", normalize_role("  support  ", ALLOWED) == "support")
check("чужой/несуществующий slug → '' (не тегируем)", normalize_role("liya", ALLOWED) == "")
check("мусор → ''", normalize_role("'; drop", ALLOWED) == "")
check("пустой allowed → любой slug сбрасывается в ''", normalize_role("sales", set()) == "")

print("\n" + ("ВСЕ ОК" if not FAILS else "ПРОВАЛЫ: " + ", ".join(FAILS)))
sys.exit(1 if FAILS else 0)
