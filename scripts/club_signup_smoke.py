#!/usr/bin/env python3
"""Смоук 3a: validate_club_registration + build_club_consent_text (shared/club.py).
Чистая логика — БЕЗ сети/БД, безопасно гонять где угодно.

Запуск:
  PYTHONPATH=shared:. ./.venv-smoke/bin/python scripts/club_signup_smoke.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from shared.club import build_club_consent_text, validate_club_registration  # noqa: E402

FAILS: list[str] = []


def check(name, cond, detail=""):
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


def main() -> None:
    print("1. validate_club_registration:")
    valid = {
        "display_name": "ООО Ромашка", "city": "Москва", "okved": "62.01",
        "chain_position": "before",
    }
    check("валидный dict → пустой список ошибок", validate_club_registration(valid) == [],
          repr(validate_club_registration(valid)))

    missing_city = dict(valid)
    missing_city.pop("city")
    errs = validate_club_registration(missing_city)
    check("пропуск обязательного city → непустой список", len(errs) > 0, repr(errs))

    missing_name = {"city": "Казань", "okved": "62.01"}
    errs2 = validate_club_registration(missing_name)
    check("пропуск display_name → непустой список", len(errs2) > 0, repr(errs2))

    missing_okved = {"display_name": "ИП Иванов", "city": "Казань"}
    errs3 = validate_club_registration(missing_okved)
    check("пропуск okved → непустой список", len(errs3) > 0, repr(errs3))

    bad_chain = dict(valid, chain_position="sideways")
    errs4 = validate_club_registration(bad_chain)
    check("кривой chain_position → ошибка", len(errs4) > 0, repr(errs4))

    no_chain = {"display_name": "ООО Ромашка", "city": "Москва", "okved": "62.01"}
    check("chain_position не задан (None) → не обязателен, ошибок нет",
          validate_club_registration(no_chain) == [], repr(validate_club_registration(no_chain)))

    empty = {}
    errs5 = validate_club_registration(empty)
    check("пустой dict → минимум 3 ошибки (name/city/okved)", len(errs5) >= 3, repr(errs5))

    print("\n2. build_club_consent_text:")
    text = build_club_consent_text("club_join", "ООО Оператор", "7707083893", "op@x.ru")
    check("club_join текст непустой", bool(text))
    check("club_join текст упоминает клуб предпринимателей", "клуб предпринимателей" in text.lower(), repr(text))
    check("club_join текст упоминает оператора", "ООО Оператор" in text, repr(text))
    # 152-ФЗ состав (приведён к эталону): ИНН, перечень ПДн, локализация, порядок отзыва.
    check("club_join содержит ИНН оператора", "ИНН 7707083893" in text, repr(text))
    check("club_join перечисляет состав ПДн", "название бизнеса" in text and "ОКВЭД" in text)
    check("club_join: хранение в РФ + порядок отзыва", "на серверах в России" in text and "/revoke" in text)
    # ФЗ-38: рекламные «предложения о партнёрстве» ВЫНЕСЕНЫ из согласия на обработку.
    check("club_join НЕ бандлит рекламу (ФЗ-38)", "предложения о партнёрстве" not in text, repr(text))

    print()
    if FAILS:
        print(f"❌ ПРОВАЛЫ ({len(FAILS)}): " + ", ".join(FAILS))
        sys.exit(1)
    print("✅ club_signup_smoke — все проверки зелёные")


if __name__ == "__main__":
    main()
