#!/usr/bin/env python3
"""Смоук Слоя C — продажи в VK/MAX (часть 3): чистая логика без сети/aiogram.
selling.selling_command / shop_button_rows; vk_driver.parse_message_new (+payload);
max_driver.parse_message_callback. БД не нужна (всё pure). Запуск:
    PYTHONPATH=bot-telegram ./.venv-smoke/bin/python scripts/vkmax_selling_smoke.py
"""
import os
import sys
from decimal import Decimal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bot-telegram"))

import selling      # noqa: E402
import vk_driver     # noqa: E402
import max_driver    # noqa: E402

FAILS: list[str] = []


def check(name, cond, detail=""):
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail != "" else ""))
    if not cond:
        FAILS.append(name)


def main() -> None:
    # ── 1. selling.selling_command ──
    print("1. selling_command (команда из кнопки/текста):")
    check("payload buy → ('buy', id)", selling.selling_command(None, {"cmd": "buy", "id": 7}) == ("buy", 7))
    check("payload buy id-строка → ('buy', 7)", selling.selling_command(None, {"cmd": "buy", "id": "7"}) == ("buy", 7))
    check("payload buy без id → None", selling.selling_command(None, {"cmd": "buy"}) is None)
    check("payload buy кривой id → None", selling.selling_command(None, {"cmd": "buy", "id": "abc"}) is None)
    check("payload shop → ('shop', None)", selling.selling_command(None, {"cmd": "shop"}) == ("shop", None))
    check("текст '/shop' → ('shop', None)", selling.selling_command("/shop", None) == ("shop", None))
    check("текст 'купить' → ('shop', None)", selling.selling_command("купить", None) == ("shop", None))
    check("текст 'Магазин ' (регистр/пробел) → shop", selling.selling_command("Магазин ", None) == ("shop", None))
    check("свободная фраза 'хочу купить курс' → None (не подстрока)", selling.selling_command("хочу купить курс", None) is None)
    check("обычный вопрос → None", selling.selling_command("а сколько стоит?", None) is None)
    check("пусто → None", selling.selling_command("", None) is None)
    # payload приоритетнее текста
    check("payload buy + текст 'магазин' → buy (payload приоритет)", selling.selling_command("магазин", {"cmd": "buy", "id": 3}) == ("buy", 3))

    # ── 2. selling.shop_button_rows ──
    print("2. shop_button_rows (кнопки витрины):")
    rows = selling.shop_button_rows([
        {"id": 5, "name": "Курс А", "price": Decimal("9900.00"), "currency": "RUB"},
        {"id": 9, "name": "Курс Б", "price": Decimal("500.00"), "currency": "RUB"},
    ])
    check("кнопок = 2", len(rows) == 2)
    check("payload первой → buy id=5", rows[0]["payload"] == {"cmd": "buy", "id": 5})
    check("label содержит имя и цену (₽)", "Курс А" in rows[0]["label"] and "₽" in rows[0]["label"], rows[0]["label"])

    # ── 3. vk_driver.parse_message_new (+payload) ──
    print("3. VK parse_message_new:")
    txt = vk_driver.parse_message_new({"type": "message_new", "object": {"message": {"from_id": 111, "peer_id": 111, "text": "привет"}}})
    check("текст → (from,peer,text,None)", txt == (111, 111, "привет", None), repr(txt))
    btn = vk_driver.parse_message_new({"type": "message_new", "object": {"message": {"from_id": 222, "peer_id": 222, "text": "💳 Купить", "payload": '{"cmd":"buy","id":5}'}}})
    check("кнопка → payload распарсен", btn == (222, 222, "💳 Купить", {"cmd": "buy", "id": 5}), repr(btn))
    check("сообщество (from_id<0) → None", vk_driver.parse_message_new({"type": "message_new", "object": {"message": {"from_id": -1, "peer_id": 5, "text": "x"}}}) is None)
    check("пустой текст без payload → None", vk_driver.parse_message_new({"type": "message_new", "object": {"message": {"from_id": 5, "peer_id": 5, "text": ""}}}) is None)
    check("битый payload-JSON → text без payload", vk_driver.parse_message_new({"type": "message_new", "object": {"message": {"from_id": 5, "peer_id": 5, "text": "hi", "payload": "{не json"}}}) == (5, 5, "hi", None))
    check("не message_new → None", vk_driver.parse_message_new({"type": "message_edit", "object": {}}) is None)

    # ── 4. max_driver.parse_message_callback ──
    print("4. MAX parse_message_callback:")
    # РЕАЛЬНЫЙ формат MAX: message — СИБЛИНГ callback на верхнем уровне (офиц. SDK/OpenAPI), НЕ вложен.
    real = {"update_type": "message_callback",
            "callback": {"callback_id": "cb1", "payload": '{"cmd":"buy","id":9}', "user": {"user_id": 333}},
            "message": {"recipient": {"chat_id": 444}}}
    check("callback (message-СИБЛИНГ, реальный формат) → (user,chat,payload,id)",
          max_driver.parse_message_callback(real) == (333, 444, {"cmd": "buy", "id": 9}, "cb1"))
    # Фолбэк: message вложен в callback (иная версия формата) — тоже должен распарситься.
    nested = {"update_type": "message_callback", "callback": {"callback_id": "cb2", "payload": '{"cmd":"buy","id":1}', "user": {"user_id": 5}, "message": {"recipient": {"chat_id": 6}}}}
    check("callback (message в callback) → фолбэк работает", max_driver.parse_message_callback(nested) == (5, 6, {"cmd": "buy", "id": 1}, "cb2"))
    check("message_created → None (не callback)", max_driver.parse_message_callback({"update_type": "message_created", "message": {}}) is None)
    check("нет chat_id → None", max_driver.parse_message_callback({"update_type": "message_callback", "callback": {"callback_id": "x", "user": {"user_id": 1}}, "message": {"recipient": {}}}) is None)
    check("битый payload → payload None, остальное ок", max_driver.parse_message_callback({"update_type": "message_callback", "callback": {"callback_id": "x", "payload": "{bad", "user": {"user_id": 1}}, "message": {"recipient": {"chat_id": 2}}}) == (1, 2, None, "x"))
    # message_created по-прежнему парсится
    mc = max_driver.parse_message_created({"update_type": "message_created", "message": {"body": {"text": "магазин"}, "sender": {"user_id": 1}, "recipient": {"chat_id": 2}}})
    check("message_created (текст) парсится", mc == (1, 2, "магазин"), repr(mc))

    print()
    if FAILS:
        print(f"❌ ПРОВАЛЫ ({len(FAILS)}): " + ", ".join(FAILS)); sys.exit(1)
    print("✅ vkmax_selling smoke — все проверки зелёные")


if __name__ == "__main__":
    main()
