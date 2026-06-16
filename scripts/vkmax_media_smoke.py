#!/usr/bin/env python3
"""Смоук Слоя C3 — чистые хелперы медиа-вложений VK/MAX (без сети/aiohttp).
vk_driver.vk_attachment / vk_media_type_for_kind; max_driver.max_attachment / max_media_type_for_kind.
Сетевые upload/send — здесь НЕ гоняются. Запуск:
    PYTHONPATH=bot-telegram ./.venv-smoke/bin/python scripts/vkmax_media_smoke.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bot-telegram"))

import max_driver   # noqa: E402
import vk_driver     # noqa: E402

FAILS: list[str] = []


def check(name, cond, detail=""):
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail != "" else ""))
    if not cond:
        FAILS.append(name)


def main() -> None:
    print("1. VK vk_attachment (строка вложения):")
    # owner сообщества отрицательный
    check("photo → 'photo<owner>_<id>'", vk_driver.vk_attachment("photo", -123, 456) == "photo-123_456")
    check("doc → 'doc<owner>_<id>'", vk_driver.vk_attachment("doc", -7, 9) == "doc-7_9")

    print("2. VK vk_media_type_for_kind (kind → VK media_type):")
    check("photo → photo", vk_driver.vk_media_type_for_kind("photo") == "photo")
    check("document → doc", vk_driver.vk_media_type_for_kind("document") == "doc")
    check("voice → doc", vk_driver.vk_media_type_for_kind("voice") == "doc")
    check("audio → doc", vk_driver.vk_media_type_for_kind("audio") == "doc")

    print("3. MAX max_media_type_for_kind (kind → MAX upload-тип):")
    check("photo → image", max_driver.max_media_type_for_kind("photo") == "image")
    check("document → file", max_driver.max_media_type_for_kind("document") == "file")
    check("voice → file", max_driver.max_media_type_for_kind("voice") == "file")

    print("4. MAX max_attachment (сборка из ответа upload-сервера):")
    # image с объектом photos → payload {photos:{...}}
    photos = {"k1": {"token": "ptok"}}
    check("image+photos → {type:image, payload:{photos}}",
          max_driver.max_attachment("image", {"photos": photos}) == {"type": "image", "payload": {"photos": photos}})
    # image с token (без photos) → payload {token}
    check("image+token → {type:image, payload:{token}}",
          max_driver.max_attachment("image", {"token": "t1"}) == {"type": "image", "payload": {"token": "t1"}})
    # file → {type:file, payload:{token}}
    check("file+token → {type:file, payload:{token}}",
          max_driver.max_attachment("file", {"token": "f1"}) == {"type": "file", "payload": {"token": "f1"}})
    # защита от пустого/None
    check("None → file payload token=None (не падает)",
          max_driver.max_attachment("file", None) == {"type": "file", "payload": {"token": None}})

    print()
    if FAILS:
        print(f"❌ ПРОВАЛЫ ({len(FAILS)}): " + ", ".join(FAILS)); sys.exit(1)
    print("✅ vkmax_media smoke — все проверки зелёные")


if __name__ == "__main__":
    main()
