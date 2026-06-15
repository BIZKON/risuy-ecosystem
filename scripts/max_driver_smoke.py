#!/usr/bin/env python3
"""Смоук чистых функций MAX-драйвера (bot-telegram/max_driver.py) — без сети/aiohttp.

parse_message_created (разбор update_type=message_created), max_client_link. HTTP/long-poll
(send/run) — сетевые, здесь НЕ гоняются.

Запуск: PYTHONPATH=. ./.venv-smoke/bin/python scripts/max_driver_smoke.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "bot-telegram"))

import max_driver  # noqa: E402

FAILS: list[str] = []


def check(name, cond, detail=""):
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail != "" else ""))
    if not cond:
        FAILS.append(name)


def mc(user_id, chat_id, text):
    return {"update_type": "message_created",
            "message": {"body": {"text": text}, "sender": {"user_id": user_id}, "recipient": {"chat_id": chat_id}}}


def main() -> None:
    print("1. parse_message_created:")
    check("валидное → (user_id, chat_id, text)", max_driver.parse_message_created(mc(119, 160, "привет")) == (119, 160, "привет"))
    check("user_id ≠ chat_id (личка MAX) — берём оба", max_driver.parse_message_created(mc(119, 160, "x")) == (119, 160, "x"))
    check("текст триммится", max_driver.parse_message_created(mc(5, 9, "  хай  ")) == (5, 9, "хай"))
    check("bot_started (не message_created) → None", max_driver.parse_message_created({"update_type": "bot_started", "chat_id": 1}) is None)
    check("пустой текст → None", max_driver.parse_message_created(mc(5, 9, "  ")) is None)
    check("нет sender → None (не падает)", max_driver.parse_message_created({"update_type": "message_created", "message": {"body": {"text": "x"}, "recipient": {"chat_id": 9}}}) is None)
    check("кривой payload → None", max_driver.parse_message_created({"update_type": "message_created"}) is None)
    check("None → None", max_driver.parse_message_created(None) is None)

    print("2. max_client_link:")
    url, label = max_driver.max_client_link(910921843083)
    check("url пустой (нет публичного профиля)", url == "")
    check("подпись содержит id и 'MAX'", "910921843083" in label and "MAX" in label, repr(label))

    print()
    if FAILS:
        print(f"❌ ПРОВАЛЫ ({len(FAILS)}): " + ", ".join(FAILS)); sys.exit(1)
    print("✅ max_driver smoke — все проверки зелёные")


if __name__ == "__main__":
    main()
