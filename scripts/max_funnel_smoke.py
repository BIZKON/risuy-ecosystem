#!/usr/bin/env python3
"""Smoke: MaxFunnelChannel + dispatch (без сети/БД).

Запуск:
  PYTHONPATH=bot-telegram BOT_TOKEN=x DATABASE_URL=postgresql://x/x \
  CHANNEL_ID=-100 CHANNEL_URL=https://t.me/x GUIDE_URL=https://x \
  /Users/konstantin/Downloads/risuy-ecosystem/.venv-smoke/bin/python scripts/max_funnel_smoke.py
"""
import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import funnel           # noqa: E402
import funnel_channels  # noqa: E402


class FakeMAXBot:
    """Стаб MAXBot — записывает вызовы без сети."""

    def __init__(self):
        self.calls: list = []

    async def send(self, chat_id, text, **kw):
        self.calls.append(("send", text))

    async def send_keyboard(self, chat_id, text, btns):
        self.calls.append(("kb", btns[0]["payload"]))

    async def send_link(self, chat_id, text, url, label):
        self.calls.append(("link", url))

    async def send_media(self, chat_id, *, media_type, content, caption="", filename="file"):
        self.calls.append(("media", media_type))
        return True

    async def answer_callback(self, callback_id):
        self.calls.append(("answer_cb", callback_id))

    async def is_channel_member(self, chat_id, user_id):
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
        "consent_text": "СОГЛАСИЕ MAX",
        "phone_step": True,
        "gate": {"enabled": False},
        "leadmagnet": {},
        "privacy_url": None,
        "legal_privacy_url": None,
    }

    # ── Тест 1: нет согласия, кнопка не нажата → показать кнопку consent_yes ──
    bot1 = FakeMAXBot()
    ch1 = funnel_channels.MaxFunnelChannel(bot1, 20, 20)
    await funnel.dispatch(
        ch1, cfg,
        {"consent": False, "status": "new"},
        {"text": "привет", "consent_pressed": False},
    )
    if not any(c[0] == "kb" and c[1] == {"cmd": "consent_yes"} for c in bot1.calls):
        fails.append(f"не показал кнопку согласия MAX: {bot1.calls}")

    # ── Тест 2: согласие нажато → set_consent → ask_phone (phone_step=True) ──
    bot2 = FakeMAXBot()
    ch2 = funnel_channels.MaxFunnelChannel(bot2, 20, 20)
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
        fails.append(f"после согласия MAX не спросил телефон: {bot2.calls}")

    if fails:
        print("\n".join("❌ " + f for f in fails))
        raise SystemExit(1)

    print("🟢 max_funnel_smoke зелёный")


asyncio.run(main())
