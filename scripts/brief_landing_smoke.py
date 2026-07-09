#!/usr/bin/env python3
"""Смоук рендера бриф-лендинга: _brief_html содержит секции схемы и экранирует
брендинг; парсинг ответов из формы. БЕЗ БД и БЕЗ сети (мокаем).
  PYTHONPATH=bot-telegram:. python3 scripts/brief_landing_smoke.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "bot-telegram"))
for k, v in {"BOT_TOKEN": "123:smoke", "DATABASE_URL": "postgresql://x/y",
             "CHANNEL_ID": "-100123", "CHANNEL_URL": "https://t.me/x", "GUIDE_URL": "https://x"}.items():
    os.environ.setdefault(k, v)

import bot as botmod  # noqa: E402  (bot-telegram/bot.py)

FAILS: list[str] = []


def check(name, cond, detail=""):
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


def main() -> None:
    html = botmod._brief_html(title="Бриф", company='Иван «Тест» & Ко', action="/brief/abc")
    print("1. рендер:")
    check("содержит секцию «О бизнесе»", "О бизнесе" in html)
    check("содержит секцию «Реквизиты оператора (152-ФЗ)»", "Реквизиты оператора" in html)
    check("брендинг экранирован (нет сырых кавычек-инъекций)", "«Тест»" in html and "<script>Иван" not in html)
    check("есть форма на action", 'action="/brief/abc"' in html)
    check("есть 152-ФЗ уведомление", "персональные данные" in html.lower() or "не вставляйте" in html.lower())

    print("2. парсинг ответов формы (мультизначные чекбоксы):")
    # эмулируем aiohttp MultiDict как список пар
    pairs = [("q_company_name", "Клиент"), ("q_channels_used", "Telegram"), ("q_channels_used", "VK")]
    answers = botmod._brief_parse(pairs)
    check("company_name разобран", answers.get("company_name") == "Клиент")
    check("channels_used — список", answers.get("channels_used") == ["Telegram", "VK"])

    print()
    if FAILS:
        print("❌ ПРОВАЛЫ: " + ", ".join(FAILS))
        sys.exit(1)
    print("✅ brief_landing smoke — OK")


if __name__ == "__main__":
    main()
