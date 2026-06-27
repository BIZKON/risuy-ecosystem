#!/usr/bin/env python3
"""Smoke: диспетчер шага воронки по DB-state (канал-агностично). FakeChannel + fake-lead dict.
Запуск: PYTHONPATH=bot-telegram BOT_TOKEN=x DATABASE_URL=postgresql://x/x CHANNEL_ID=-100 \
  CHANNEL_URL=https://t.me/x GUIDE_URL=https://x ./.venv-smoke/bin/python scripts/funnel_dispatch_smoke.py"""
import asyncio, os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import funnel  # noqa: E402


class FakeChannel:
    messenger = "vk"; uid = 7
    def __init__(self): self.calls = []
    async def send_text(self, t): self.calls.append(("text", t))
    async def send_consent(self, t, p): self.calls.append(("consent", t))
    async def ask_phone(self, t): self.calls.append(("ask_phone", t))
    async def ask_gate(self, t, u): self.calls.append(("ask_gate", t))
    async def check_subscription(self, g, uid): return True
    async def deliver_text(self, t): self.calls.append(("deliver_text", t))
    async def deliver_url(self, c, u): self.calls.append(("deliver_url", u))
    async def deliver_file(self, c, p): return True
    async def deliver_video_note(self, f): pass


async def main():
    fails = []
    if not funnel.looks_like_phone("+7 (999) 000-11-22"): fails.append("looks_like_phone отверг валидный")
    if funnel.looks_like_phone("привет"): fails.append("looks_like_phone принял мусор")
    if funnel.requisites_filled({"consent_text": ""}): fails.append("requisites_filled true без consent_text")
    if fails:
        print("\n".join("❌ " + f for f in fails)); raise SystemExit(1)
    print("🟢 funnel_dispatch_smoke зелёный (хелперы)")

asyncio.run(main())
