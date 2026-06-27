#!/usr/bin/env python3
"""Smoke: контракт дожима shared.nurture — parse_nurture_steps (ленивый, для бота/предзаполнения) +
normalize_and_validate (строгий, для формы панели). Чистый (без БД/сети) — гоняется на .venv-smoke.
Запуск:  ./.venv-smoke/bin/python scripts/nurture_contract_smoke.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from shared.nurture import (  # noqa: E402
    parse_nurture_steps, normalize_and_validate, NURTURE_MAX_STEPS)


def main() -> None:
    fails: list[str] = []

    # ── parse_nurture_steps ───────────────────────────────────────────────────
    s = parse_nurture_steps('[{"delay_seconds":7200,"text":"к1"},{"delay_seconds":86400,"text":"к2"}]')
    if [x["text"] for x in s] != ["к1", "к2"]:
        fails.append(f"parse: валидный JSON не распознан: {s}")
    if parse_nurture_steps([{"delay_seconds": 60, "text": "x"}]) != [{"delay_seconds": 60, "text": "x"}]:
        fails.append("parse: список (не строка) не принят")
    if parse_nurture_steps("не json") != []:
        fails.append("parse: битый JSON не дал []")
    if parse_nurture_steps('[{"delay_seconds":0,"text":"a"},{"delay_seconds":10,"text":""}]') != []:
        fails.append("parse: невалидные шаги (delay<=0 / пустой текст) не отброшены")
    many = parse_nurture_steps([{"delay_seconds": 1, "text": str(i)} for i in range(5)])
    if len(many) != NURTURE_MAX_STEPS:
        fails.append(f"parse: не обрезано до {NURTURE_MAX_STEPS}: {len(many)}")

    # ── normalize_and_validate ────────────────────────────────────────────────
    clean, errs = normalize_and_validate(
        True, [{"delay_seconds": 7200, "text": "привет"}, {"delay_seconds": None, "text": ""}])
    if errs or clean != [{"delay_seconds": 7200, "text": "привет"}]:
        fails.append(f"validate: валидный набор: clean={clean} errs={errs}")

    _, errs = normalize_and_validate(True, [{"delay_seconds": 3600, "text": ""}])
    if not any("текст" in e.lower() for e in errs):
        fails.append(f"validate: присутствующий шаг без текста не пойман: {errs}")

    _, errs = normalize_and_validate(True, [{"delay_seconds": 0, "text": "a"}])
    if not any("задержк" in e.lower() for e in errs):
        fails.append(f"validate: нулевая задержка не поймана: {errs}")

    _, errs = normalize_and_validate(True, [{"delay_seconds": None, "text": ""}])
    if not errs:
        fails.append("validate: дожим включён без касаний — нет ошибки")

    # минимальная задержка 1 минута (движок тикает раз в минуту)
    _, errs = normalize_and_validate(True, [{"delay_seconds": 30, "text": "a"}])
    if not any("минут" in e.lower() for e in errs):
        fails.append(f"validate: задержка < 1 минуты не отбракована: {errs}")

    # запрет пропусков: пустое касание ПЕРЕД заполненным → ошибка (а не молчаливое схлопывание)
    clean, errs = normalize_and_validate(
        True, [{"delay_seconds": None, "text": ""}, {"delay_seconds": 3600, "text": "к2"}])
    if not any("пропуск" in e.lower() or "поряд" in e.lower() for e in errs):
        fails.append(f"validate: пропуск (дыра) перед заполненным шагом не пойман: {errs}")
    # трейлинг-пустые шаги после заполненных — ОК (не ошибка)
    clean, errs = normalize_and_validate(
        True, [{"delay_seconds": 3600, "text": "к1"}, {"delay_seconds": None, "text": ""}])
    if errs or clean != [{"delay_seconds": 3600, "text": "к1"}]:
        fails.append(f"validate: трейлинг-пустой шаг дал ошибку: clean={clean} errs={errs}")

    clean, errs = normalize_and_validate(False, [{"delay_seconds": None, "text": ""}])
    if errs or clean:
        fails.append(f"validate: выключенный пустой дал ошибки/шаги: errs={errs} clean={clean}")

    # текст обрезается до капа
    long_clean, _ = normalize_and_validate(True, [{"delay_seconds": 60, "text": "я" * 5000}])
    if long_clean and len(long_clean[0]["text"]) > 1500:
        fails.append("validate: текст не обрезан до капа")

    if fails:
        print("\n".join("❌ " + f for f in fails))
        raise SystemExit(1)
    print("🟢 nurture_contract_smoke зелёный")


if __name__ == "__main__":
    main()
