#!/usr/bin/env python3
"""Смоук чистых функций VK-драйвера (bot-telegram/vk_driver.py) — без сети/aiohttp.

parse_message_new (разбор Long Poll-события), vk_client_link, next_random_id, _handle_failed
(логика failed 1/2/3). HTTP/Long Poll-цикл (send/run/_get_lp) — сетевые, здесь НЕ гоняются.

Запуск: PYTHONPATH=. ./.venv-smoke/bin/python scripts/vk_driver_smoke.py
"""
import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "bot-telegram"))

import vk_driver  # noqa: E402

FAILS: list[str] = []


def check(name, cond, detail=""):
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail != "" else ""))
    if not cond:
        FAILS.append(name)


def msg_new(from_id, peer_id, text):
    return {"type": "message_new", "object": {"message": {"from_id": from_id, "peer_id": peer_id, "text": text}}}


async def main() -> None:
    print("1. parse_message_new:")
    check("валидное сообщение юзера → (from_id, peer_id, text)",
          vk_driver.parse_message_new(msg_new(123, 123, "привет")) == (123, 123, "привет"))
    check("текст триммится", vk_driver.parse_message_new(msg_new(5, 5, "  хай  ")) == (5, 5, "хай"))
    check("сообщение от сообщества (from_id<0) → None", vk_driver.parse_message_new(msg_new(-10, 5, "x")) is None)
    check("пустой текст → None", vk_driver.parse_message_new(msg_new(5, 5, "   ")) is None)
    check("не message_new → None", vk_driver.parse_message_new({"type": "message_edit", "object": {}}) is None)
    check("кривой payload → None (не падает)", vk_driver.parse_message_new({"type": "message_new"}) is None)
    check("None → None", vk_driver.parse_message_new(None) is None)
    check("беседа (peer_id 2e9+) тоже парсится", vk_driver.parse_message_new(msg_new(77, 2000000001, "чат")) == (77, 2000000001, "чат"))

    print("2. vk_client_link:")
    url, label = vk_driver.vk_client_link(456)
    check("ссылка на профиль vk.com/id<id>", url == "https://vk.com/id456")
    check("подпись про ВКонтакте", "ВКонтакте" in label)

    print("3. next_random_id:")
    ids = [vk_driver.next_random_id(i) for i in range(1, 6)]
    check("все положительные", all(i > 0 for i in ids))
    check("в int32-диапазоне", all(i <= 0x7FFFFFFF for i in ids))
    check("уникальны для разных счётчиков", len(set(ids)) == len(ids), repr(ids))

    print("4. _handle_failed (failed 1 — без сети):")
    bot = vk_driver.VKBot("tok", 111, on_message=None)
    srv, key, ts = await bot._handle_failed(1, {"ts": "999"}, "S", "K", "100")
    check("failed=1 → новый ts, server/key те же", (srv, key, ts) == ("S", "K", "999"), repr((srv, key, ts)))
    # failed 2/3 зовут _get_lp (сеть) — мокаем
    async def fake_lp():
        return "S2", "K2", "200"
    bot._get_lp = fake_lp
    srv2, key2, ts2 = await bot._handle_failed(2, {}, "S", "K", "100")
    check("failed=2 → новый server/key, ts СТАРЫЙ", (srv2, key2, ts2) == ("S2", "K2", "100"))
    srv3, key3, ts3 = await bot._handle_failed(3, {}, "S", "K", "100")
    check("failed=3 → полный пере-get (новые server/key/ts)", (srv3, key3, ts3) == ("S2", "K2", "200"))

    print()
    if FAILS:
        print(f"❌ ПРОВАЛЫ ({len(FAILS)}): " + ", ".join(FAILS)); sys.exit(1)
    print("✅ vk_driver smoke — все проверки зелёные")


if __name__ == "__main__":
    asyncio.run(main())
