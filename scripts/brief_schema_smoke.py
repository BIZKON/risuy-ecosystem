#!/usr/bin/env python3
"""Смоук схемы брифа: структура валидна, ветвления ссылаются на реальные вопросы,
validate_answers ловит пропуски required и неизвестные варианты. Без БД."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import importlib
brief_schema = importlib.import_module("shared.brief_schema")

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


def main() -> None:
    idx = brief_schema.question_index()

    print("1. структура схемы:")
    check("BRIEF_VERSION — int >= 1", isinstance(brief_schema.BRIEF_VERSION, int) and brief_schema.BRIEF_VERSION >= 1)
    check("SECTIONS непустой", len(brief_schema.SECTIONS) >= 1)
    allowed_types = {"text", "textarea", "choice", "multichoice", "repeatable"}
    for sec in brief_schema.SECTIONS:
        check(f"секция {sec.get('key')}: есть title", bool(sec.get("title")))
        for q in sec["questions"]:
            check(f"вопрос {q.get('key')}: есть key/type/label",
                  bool(q.get("key") and q.get("type") and q.get("label")))
            check(f"вопрос {q.get('key')}: тип разрешён", q.get("type") in allowed_types)

    print("2. ветвления ссылаются на существующие вопросы:")
    for q in idx.values():
        cond = q.get("show_if")
        if cond:
            check(f"show_if.q '{cond.get('q')}' существует", cond.get("q") in idx)

    print("3. validate_answers ловит пропуск required:")
    errs_empty = brief_schema.validate_answers({})
    check("пустые ответы → есть ошибки required", len(errs_empty) > 0, f"errs={len(errs_empty)}")

    print("4. validate_answers ловит неизвестный choice:")
    # первый choice-вопрос
    choice_q = next((q for q in idx.values() if q["type"] == "choice"), None)
    if choice_q:
        bad = {choice_q["key"]: "___нет_такого_варианта___"}
        errs = brief_schema.validate_answers(bad)
        check("неизвестный вариант choice → ошибка", any(choice_q["key"] in e for e in errs))

    print()
    if FAILS:
        print("❌ ПРОВАЛЫ: " + ", ".join(FAILS))
        sys.exit(1)
    print("✅ brief_schema smoke — OK")


if __name__ == "__main__":
    main()
