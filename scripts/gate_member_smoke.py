#!/usr/bin/env python3
"""Smoke: per-channel гейт подписки (чистый, без сети/БД).

FakeVKBot с _api-стабом → VKBot.is_member True/False/fail-closed на исключении.
Проверяем, что validate_funnel_fields не требует новые поля vk_gate_group_id/max_gate_chat_id.

Запуск:
  PYTHONPATH=bot-telegram /Users/konstantin/Downloads/risuy-ecosystem/.venv-smoke/bin/python scripts/gate_member_smoke.py
"""
import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bot-telegram"))
sys.path.insert(0, os.path.join(ROOT, "shared"))

from vk_driver import VKBot  # noqa: E402
from leadmagnet import validate_funnel_fields, FUNNEL_KEYS  # noqa: E402


class FakeVKBot:
    """Фейковый VKBot для тестирования is_member без сети."""

    def __init__(self, api_response=1, raise_exc=None):
        self._resp = api_response
        self._exc = raise_exc
        # Минимальные атрибуты VKBot
        self.token = "fake"
        self.group_id = 0
        self._send_counter = 0
        self._session = None

    async def _api(self, method: str, **params):
        """Стаб VK API: возвращает заданный ответ или бросает исключение."""
        if self._exc is not None:
            raise self._exc
        return self._resp


async def main() -> None:
    fails: list[str] = []

    # --- Тест 1: is_member возвращает True (VK API вернул 1) ---
    bot1 = FakeVKBot(api_response=1)
    # Подменяем _api метод на экземпляре
    result1 = await VKBot.is_member(bot1, group_id=123, user_id=456)
    if result1 is not True:
        fails.append(f"is_member должен вернуть True при ответе API=1, получено: {result1!r}")

    # --- Тест 2: is_member возвращает False (VK API вернул 0) ---
    bot2 = FakeVKBot(api_response=0)
    result2 = await VKBot.is_member(bot2, group_id=123, user_id=456)
    if result2 is not False:
        fails.append(f"is_member должен вернуть False при ответе API=0, получено: {result2!r}")

    # --- Тест 3: fail-closed при исключении ---
    bot3 = FakeVKBot(raise_exc=RuntimeError("сеть недоступна"))
    result3 = await VKBot.is_member(bot3, group_id=123, user_id=456)
    if result3 is not False:
        fails.append(f"is_member должен fail-closed (False) при исключении, получено: {result3!r}")

    # --- Тест 4: validate_funnel_fields не требует vk_gate_group_id/max_gate_chat_id ---
    # Конфиг без новых полей — воронка включена, реквизиты заполнены, лид-магнит = ссылка
    cfg_no_gate_fields = {
        "funnel_enabled": "1",
        "operator_name": "ООО Тест",
        "operator_inn": "1234567890",
        "operator_email": "test@example.com",
        "leadmagnet_kind": "link",
        "leadmagnet_url": "https://example.com/guide",
    }
    errs = validate_funnel_fields(cfg_no_gate_fields)
    if errs:
        fails.append(f"validate_funnel_fields без vk/max полей дала ошибки: {errs}")

    # --- Тест 5: новые ключи есть в FUNNEL_KEYS ---
    if "vk_gate_group_id" not in FUNNEL_KEYS:
        fails.append("vk_gate_group_id отсутствует в FUNNEL_KEYS")
    if "max_gate_chat_id" not in FUNNEL_KEYS:
        fails.append("max_gate_chat_id отсутствует в FUNNEL_KEYS")

    if fails:
        for f in fails:
            print(f"❌ {f}")
        raise SystemExit(1)

    print("🟢 gate_member_smoke зелёный")


if __name__ == "__main__":
    asyncio.run(main())
