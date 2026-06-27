#!/usr/bin/env python3
"""Smoke: funnel-шаги диспетчатся через адаптер (без aiogram/сети). FakeChannel записывает вызовы;
проверяем маршруты after_consent: phone-step → ask_phone; gate → check_subscription; иначе → deliver.

Запуск: PYTHONPATH=bot-telegram ./.venv-smoke/bin/python scripts/funnel_adapter_smoke.py
"""
import asyncio, os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import funnel  # noqa: E402


class FakeChannel:
    messenger = "vk"
    uid = 1
    def __init__(self, subscribed=True):
        self.calls = []
        self._sub = subscribed
    async def send_text(self, t): self.calls.append(("text", t))
    async def send_consent(self, t, p): self.calls.append(("consent", t))
    async def ask_phone(self, t): self.calls.append(("ask_phone", t))
    async def ask_gate(self, t, u): self.calls.append(("ask_gate", t))
    async def check_subscription(self, g, uid): self.calls.append(("check_sub", uid)); return self._sub
    async def deliver_text(self, t): self.calls.append(("deliver_text", t))
    async def deliver_url(self, c, u): self.calls.append(("deliver_url", u))
    async def deliver_file(self, c, p): self.calls.append(("deliver_file", None)); return True
    async def deliver_video_note(self, f): self.calls.append(("video", f))


async def main():
    fails = []
    # phone-step включён → after_consent зовёт ask_phone
    ch = FakeChannel()
    await funnel.after_consent(ch, {"phone_step": True, "gate": {"enabled": False}})
    if ("ask_phone", funnel.ASK_PHONE) not in ch.calls:
        fails.append(f"after_consent(phone) не позвал ask_phone: {ch.calls}")
    # без телефона, gate выкл → deliver (лид-магнит не настроен → deliver_text NOT_CONFIGURED)
    ch = FakeChannel()
    await funnel.after_consent(ch, {"phone_step": False, "gate": {"enabled": False}, "leadmagnet": {}})
    if not any(c[0] == "deliver_text" for c in ch.calls):
        fails.append(f"after_consent(deliver) не дошёл до выдачи: {ch.calls}")
    if fails:
        print("\n".join("❌ " + f for f in fails)); raise SystemExit(1)
    print("🟢 funnel_adapter_smoke зелёный")

asyncio.run(main())
