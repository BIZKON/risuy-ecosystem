#!/usr/bin/env python3
"""Smoke: PII-маскировка shared.pii (mask → LLM → unmask). Чистый (без БД/сети) — .venv-smoke.
Запуск:  ./.venv-smoke/bin/python scripts/pii_smoke.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from shared import pii  # noqa: E402


def main() -> None:
    fails: list[str] = []

    def check(cond, msg):
        if not cond:
            fails.append(msg)

    # 1) Телефон РФ в разных форматах — маскируется, цифры не утекают, unmask восстанавливает
    for raw in ("+79111234567", "8 (911) 123-45-67", "+7 911 123 45 67", "8-911-123-45-67"):
        t = f"Мой номер {raw}, перезвоните"
        m, mp = pii.redact_text(t)
        sig = "".join(ch for ch in raw if ch.isdigit())[-7:]  # значимые цифры номера (не из плейсхолдера)
        check("[PHONE_1]" in m, f"телефон не замаскирован: {raw!r} → {m!r}")
        check(sig not in m, f"в маске остались цифры телефона: {raw!r} → {m!r}")
        check(pii.unmask_text(m, mp) == t, f"unmask не восстановил телефон: {raw!r}")

    # 2) E-mail
    t = "Пишите на ivan.petrov@example.ru пожалуйста"
    m, mp = pii.redact_text(t)
    check("[EMAIL_1]" in m and "example.ru" not in m, f"email не замаскирован: {m!r}")
    check(pii.unmask_text(m, mp) == t, "unmask не восстановил email")

    # 3) ИНН маскируется ТОЛЬКО по контексту (после слова «ИНН»); слово остаётся, цифры → [INN_1]
    m, mp = pii.redact_text("Мой ИНН 7707083893, выставите счёт")
    check("ИНН [INN_1]" in m and "7707083893" not in m, f"контекстный ИНН не замаскирован: {m!r}")
    check(pii.unmask_text(m, mp) == "Мой ИНН 7707083893, выставите счёт", "unmask ИНН не восстановил")
    # номер заказа БЕЗ слова «ИНН» (даже совпавший по контрольной сумме) НЕ трогаем — анти-false-positive
    m2, _ = pii.redact_text("заказ № 7707083893 готов")
    check("[INN" not in m2 and "7707083893" in m2, f"номер заказа ошибочно замаскирован как ИНН: {m2!r}")

    # 3b) Телефон, написанный ВПЛОТНУЮ к слову (типовой ввод в чат) — маскируется (был лик, ревью)
    for raw in ("телефон89111234567", "звонил8(911)123-45-67"):
        m, mp = pii.redact_text(raw)
        check("[PHONE_1]" in m and "1234567" not in m, f"слитный с буквой телефон не замаскирован: {raw!r} → {m!r}")

    # 4) Консистентность: один и тот же номер (8… и +7…) → ОДИН плейсхолдер за вызов
    m, mp = pii.redact_text("звони 89111234567 или +7 911 123-45-67")
    check(m.count("[PHONE_1]") == 2 and "[PHONE_2]" not in m, f"8/+7 один номер → один плейсхолдер: {m!r}")

    # 5) Два разных номера → разные плейсхолдеры
    m, mp = pii.redact_text("ему +79990001122, мне +79993334455")
    check("[PHONE_1]" in m and "[PHONE_2]" in m, f"два номера → два плейсхолдера: {m!r}")

    # 6) Текст без ПДн — не меняется, маппинг пуст
    clean = "Здравствуйте! Расскажите про тарифы, пожалуйста."
    m, mp = pii.redact_text(clean)
    check(m == clean and mp.empty(), f"чистый текст изменён/непустой маппинг: {m!r}")

    # 7) messages[] round-trip + LLM эхо плейсхолдера → unmask
    msgs = [
        {"role": "system", "content": "Ты ассистент."},
        {"role": "user", "content": "Запишите Иванова на +7 911 123-45-67, почта a@b.ru"},
    ]
    masked, mp = pii.redact_messages(msgs)
    joined = " ".join(x["content"] for x in masked)
    check("[PHONE_1]" in joined and "[EMAIL_1]" in joined, f"messages не замаскированы: {joined!r}")
    check("911" not in joined and "a@b.ru" not in joined, f"ПДн утекли в messages: {joined!r}")
    # модель вернула ответ с плейсхолдерами → unmask вернёт оригиналы пользователю
    llm = "Записал. Перезвоню на [PHONE_1] и продублирую на [EMAIL_1]."
    out = pii.unmask_text(llm, mp)
    check("+7 911 123-45-67" in out and "a@b.ru" in out, f"unmask ответа LLM не сработал: {out!r}")
    check("[PHONE_1]" not in out and "[EMAIL_1]" not in out, f"в ответе остались плейсхолдеры: {out!r}")

    # 8) Короткие числа (не телефон) не трогаем
    m, _ = pii.redact_text("их было 12345 штук на 99 рублей")
    check("[PHONE" not in m and "12345" in m, f"короткое число замаскировано как телефон: {m!r}")

    # 8b) Осиротевший плейсхолдер (прошлый серверный контекст /call или галлюцинация) → СРЕЗАЕТСЯ,
    # клиент не видит служебный токен (даже при пустом текущем mapping)
    empty = pii.Mapping()
    check(pii.unmask_text("перезвоню на [PHONE_1] и [EMAIL_2]", empty) == "перезвоню на  и ",
          "осиротевшие плейсхолдеры не срезаны")
    # известный плейсхолдер восстановлен, осиротевший — срезан, в одном ответе
    _m, mp2 = pii.redact_text("звони +79111234567")
    out = pii.unmask_text("ваш [PHONE_1], а старый [PHONE_9]", mp2)
    check("+79111234567" in out and "[PHONE_9]" not in out and "[PHONE_1]" not in out,
          f"смешанный unmask (известный+осиротевший) неверен: {out!r}")

    # 9) Устойчивость к мусорному content (fail-closed на стороне вызывающего; mask не падает)
    masked, _ = pii.redact_messages([{"role": "user", "content": None}, {"role": "user"}])
    check(isinstance(masked, list), "redact_messages упал на None/без content")

    if fails:
        print("\n".join("❌ " + f for f in fails))
        raise SystemExit(1)
    print("🟢 pii_smoke зелёный")


if __name__ == "__main__":
    main()
