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

    print("5. ветвления show_if работают корректно:")
    # Проверяем ветвление на вопросе b2b_or_b2c (есть show_if для b2b_decision и b2c_objections)
    visible_b2c = brief_schema.visible_questions({"b2b_or_b2c": "B2C"})
    visible_b2c_keys = [q["key"] for q in visible_b2c]
    check("B2C: содержит b2c_objections", "b2c_objections" in visible_b2c_keys)
    check("B2C: НЕ содержит b2b_decision", "b2b_decision" not in visible_b2c_keys)

    visible_b2b = brief_schema.visible_questions({"b2b_or_b2c": "B2B"})
    visible_b2b_keys = [q["key"] for q in visible_b2b]
    check("B2B: содержит b2b_decision", "b2b_decision" in visible_b2b_keys)
    check("B2B: НЕ содержит b2c_objections", "b2c_objections" not in visible_b2b_keys)

    visible_both = brief_schema.visible_questions({"b2b_or_b2c": "Оба"})
    visible_both_keys = [q["key"] for q in visible_both]
    check("Оба: содержит b2b_decision", "b2b_decision" in visible_both_keys)
    check("Оба: содержит b2c_objections", "b2c_objections" in visible_both_keys)

    print("6. validate_answers ловит неизвестный вариант multichoice:")
    multichoice_q = next((q for q in idx.values() if q["type"] == "multichoice"), None)
    if multichoice_q:
        # Валидный ответ не должен иметь ошибок
        good = {multichoice_q["key"]: ["Telegram", "VK"]}
        errs_good = brief_schema.validate_answers(good)
        check(f"multichoice с валидными вариантами → нет ошибок по {multichoice_q['key']}",
              not any(multichoice_q["key"] in e for e in errs_good))

        # Невалидный вариант в списке должен вызвать ошибку
        bad = {multichoice_q["key"]: ["Telegram", "НЕТ_ТАКОГО"]}
        errs_bad = brief_schema.validate_answers(bad)
        check("multichoice с невалидным вариантом → ошибка",
              any(multichoice_q["key"] in e and "недопустимый вариант" in e for e in errs_bad),
              f"errs={errs_bad}")

    print("7. validate_answers требует заполнить required multichoice:")
    if multichoice_q and multichoice_q.get("required"):
        # Пустой список для required multichoice
        empty = {multichoice_q["key"]: []}
        errs_empty = brief_schema.validate_answers(empty)
        check("пустой список для required multichoice → ошибка",
              any(multichoice_q["key"] in e and "обязательный" in e for e in errs_empty),
              f"errs={errs_empty}")

        # None для required multichoice
        none_ans = {multichoice_q["key"]: None}
        errs_none = brief_schema.validate_answers(none_ans)
        check("None для required multichoice → ошибка",
              any(multichoice_q["key"] in e and "обязательный" in e for e in errs_none),
              f"errs={errs_none}")

    print()
    if FAILS:
        print("❌ ПРОВАЛЫ: " + ", ".join(FAILS))
        sys.exit(1)
    print("✅ brief_schema smoke — OK")


if __name__ == "__main__":
    main()
