#!/usr/bin/env python3
"""Smoke: VkFunnelChannel + dispatch (без сети/БД).

Запуск:
  PYTHONPATH=bot-telegram BOT_TOKEN=x DATABASE_URL=postgresql://x/x \
  CHANNEL_ID=-100 CHANNEL_URL=https://t.me/x GUIDE_URL=https://x \
  ./.venv-smoke/bin/python scripts/vk_funnel_smoke.py
"""
import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import funnel          # noqa: E402
import funnel_channels  # noqa: E402


class FakeVKBot:
    """Стаб VKBot — записывает вызовы без сети."""

    def __init__(self):
        self.calls: list = []

    async def send(self, peer, text, **kw):
        self.calls.append(("send", text))

    async def send_keyboard(self, peer, text, btns):
        self.calls.append(("kb", btns[0]["payload"]))

    async def send_link(self, peer, text, url, label):
        self.calls.append(("link", url))

    async def send_document(self, peer, b, **kw):
        self.calls.append(("doc", None))
        return True

    async def is_member(self, gid, uid):
        return True


async def main() -> None:
    fails: list[str] = []

    # Монкей-патчим db-писатели (без БД): set_consent/set_phone → no-op
    import db

    async def _noop(*a, **k):
        return None

    db.set_consent = _noop
    db.set_phone = _noop

    cfg = {
        "enabled": True,
        "consent_text": "СОГЛАСИЕ VK",
        "phone_step": True,
        "gate": {"enabled": False},
        "leadmagnet": {},
        "privacy_url": None,
        "legal_privacy_url": None,
    }

    # ── Тест 1: нет согласия, кнопка не нажата → показать кнопку consent_yes ──
    bot1 = FakeVKBot()
    ch1 = funnel_channels.VkFunnelChannel(bot1, 10, 10)
    await funnel.dispatch(
        ch1, cfg,
        {"consent": False, "status": "new"},
        {"text": "привет", "consent_pressed": False},
    )
    if not any(c[0] == "kb" and c[1] == {"cmd": "consent_yes"} for c in bot1.calls):
        fails.append(f"не показал кнопку согласия VK: {bot1.calls}")

    # ── Тест 2: согласие нажато → set_consent → ask_phone (phone_step=True) ──
    bot2 = FakeVKBot()
    ch2 = funnel_channels.VkFunnelChannel(bot2, 10, 10)
    await funnel.dispatch(
        ch2, cfg,
        {"consent": False, "status": "new"},
        {"text": "", "consent_pressed": True},
    )
    phone_asked = any(
        c[0] == "send" and "Напишите номер" in (c[1] or "")
        for c in bot2.calls
    )
    if not phone_asked:
        fails.append(f"после согласия VK не спросил телефон: {bot2.calls}")

    if fails:
        print("\n".join("❌ " + f for f in fails))
        raise SystemExit(1)

    print("🟢 vk_funnel_smoke зелёный")


asyncio.run(main())
