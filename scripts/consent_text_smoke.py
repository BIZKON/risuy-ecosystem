#!/usr/bin/env python3
"""Smoke: канонический генератор согласия 152-ФЗ + валидация полей конструктора лид-магнита.
Чистый (без БД/сети) — гоняется на .venv-smoke без секретов.
Запуск:  ./.venv-smoke/bin/python scripts/consent_text_smoke.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)  # пакет shared (как в b5_payments_smoke и др.)

from shared.leadmagnet import (  # noqa: E402
    build_consent_text, build_privacy_policy, legal_doc_url, validate_funnel_fields, FUNNEL_FIELDS)


def main() -> None:
    fails: list[str] = []

    # 1) Генерация подставляет реквизиты и содержит отзыв + упоминание политики
    t = build_consent_text("ИП Петров П.П.", "770000000000", "hello@petrov.ru",
                           data_purpose=None, privacy_url="https://petrov.ru/privacy")
    for must in ("ИП Петров П.П.", "770000000000", "hello@petrov.ru"):
        if must not in t:
            fails.append(f"в согласии нет '{must}'")
    if "отозв" not in t.lower() and "отзыв" not in t.lower():
        fails.append("в согласии нет упоминания отзыва согласия")
    if "политик" not in t.lower():
        fails.append("при наличии privacy_url нет упоминания политики")

    # phone_step=False → телефон в перечне данных не упоминается
    t2 = build_consent_text("ИП", "7700000000", "a@b.ru", phone_step=False)
    if "телефон" in t2.lower():
        fails.append("phone_step=False, а телефон в данных всё равно есть")

    # 2) Пустой набор при включённой воронке → ошибки про оператора и лид-магнит
    errs = validate_funnel_fields({"funnel_enabled": "1"})
    if not any("оператор" in e.lower() for e in errs):
        fails.append(f"нет ошибки про оператора: {errs}")
    if not any("лид-магнит" in e.lower() for e in errs):
        fails.append(f"нет ошибки про лид-магнит: {errs}")

    # 3) Корректный link-набор → без ошибок
    ok = validate_funnel_fields({
        "funnel_enabled": "1", "operator_name": "ИП Петров", "operator_inn": "770000000000",
        "operator_email": "a@b.ru", "leadmagnet_kind": "link", "leadmagnet_url": "https://x.ru/g.pdf"})
    if ok:
        fails.append(f"корректный набор дал ошибки: {ok}")

    # 4) Кривые ИНН / email / url ловятся
    bad = validate_funnel_fields({
        "funnel_enabled": "1", "operator_name": "ИП", "operator_inn": "abc",
        "operator_email": "not-an-email", "leadmagnet_kind": "link", "leadmagnet_url": "ftp://x"})
    if not any("инн" in e.lower() for e in bad):
        fails.append(f"кривой ИНН не пойман: {bad}")
    if not any(("mail" in e.lower()) or ("почт" in e.lower()) for e in bad):
        fails.append(f"кривой email не пойман: {bad}")
    if not any(("ссылк" in e.lower()) or ("url" in e.lower()) for e in bad):
        fails.append(f"кривой url не пойман: {bad}")

    # 5) Выключенная воронка → без обязательных ошибок
    off = validate_funnel_fields({"funnel_enabled": ""})
    if off:
        fails.append(f"выключенная воронка дала ошибки: {off}")

    # 6) FUNNEL_FIELDS — непустой контракт с ключевыми полями
    keys = {f["key"] for f in FUNNEL_FIELDS}
    for need in ("funnel_enabled", "operator_name", "operator_inn", "operator_email", "leadmagnet_kind"):
        if need not in keys:
            fails.append(f"FUNNEL_FIELDS не содержит ключ {need}")

    # 7) Политика обработки ПДн (ст.18.1): полный документ с подстановкой реквизитов
    pp = build_privacy_policy("ИП Петров П.П.", "770000000000", "hello@petrov.ru",
                              operator_ogrn="304770000000017", operator_address="г. Москва, ул. Тестовая, 1",
                              data_purpose=None)
    for must in ("ИП Петров П.П.", "770000000000", "304770000000017", "г. Москва, ул. Тестовая, 1",
                 "hello@petrov.ru", "152-ФЗ", "18.1"):
        if must not in pp:
            fails.append(f"в Политике нет '{must}'")
    if "Российской Федерации" not in pp:
        fails.append("в Политике нет локализации хранения (РФ)")
    if "отозвать" not in pp.lower():
        fails.append("в Политике нет права отзыва согласия")
    # без ОГРН/адреса — документ всё равно собирается (опц. поля)
    pp2 = build_privacy_policy("ООО Тест", "7700000000", "a@b.ru")
    if "ООО Тест" not in pp2 or "152-ФЗ" not in pp2:
        fails.append("Политика без опц. реквизитов не собралась")

    # 8) legal_doc_url — единый сборщик публичной ссылки (панель показывает её тенанту)
    if legal_doc_url("https://bot.example.ru/", "romashka", "privacy") != "https://bot.example.ru/legal/romashka/privacy":
        fails.append("legal_doc_url: не собрал privacy-URL / не срезал хвостовой слеш базы")
    if legal_doc_url("https://bot.example.ru", "romashka", "consent") != "https://bot.example.ru/legal/romashka/consent":
        fails.append("legal_doc_url: не собрал consent-URL")
    for empty_args in (("", "romashka", "privacy"), ("https://b", "", "privacy"), ("https://b", "romashka", "terms")):
        if legal_doc_url(*empty_args) != "":
            fails.append(f"legal_doc_url должен вернуть пусто на {empty_args} (без битых ссылок тенанту)")

    # --- Task 1: раскрытие ИИ + условная трансгран-декларация ---
    pp_tb = build_privacy_policy("ИП Петров П.П.", "770000000000", "hello@petrov.ru", transborder=True)
    assert "за пределы Российской Федерации" in pp_tb, "transborder=True: нет трансгран-раскрытия в 6.3"
    assert "Трансграничная передача персональных данных не осуществляется" not in pp_tb, \
        "transborder=True: ложный абсолют не должен печататься"
    assert "6.5." in pp_tb and "искусственного интеллекта" in pp_tb, "нет раздела 6.5 (раскрытие ИИ)"

    pp_rf = build_privacy_policy("ИП Петров П.П.", "770000000000", "hello@petrov.ru", transborder=False)
    assert "Трансграничная передача персональных данных не осуществляется" in pp_rf, \
        "transborder=False: должна быть декларация «не осуществляется»"
    assert "6.5." in pp_rf and "искусственного интеллекта" in pp_rf, "transborder=False: нет раздела 6.5"

    pp_default = build_privacy_policy("ИП", "7700000000", "a@b.ru")
    assert "за пределы Российской Федерации" in pp_default, "дефолт должен быть безопасной веткой (transborder=True)"

    ct_ai = build_consent_text("ИП", "7700000000", "a@b.ru")
    assert "включая ИИ" in ct_ai, "Согласие: нет строки-раскрытия ИИ"
    print("OK: раскрытие ИИ + условная трансгран-декларация")

    if fails:
        print("\n".join("❌ " + f for f in fails))
        raise SystemExit(1)
    print("🟢 consent_text_smoke зелёный")


if __name__ == "__main__":
    main()
