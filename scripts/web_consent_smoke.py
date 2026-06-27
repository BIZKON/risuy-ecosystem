#!/usr/bin/env python3
"""Smoke: веб-чат требует согласие до ответа. Тестируем чистый гард _consent_required.
Запуск:
  PYTHONPATH=bot-telegram BOT_TOKEN=x DATABASE_URL=postgresql://x/x CHANNEL_ID=-100 \
  CHANNEL_URL=https://t.me/x GUIDE_URL=https://x \
  /Users/konstantin/Downloads/risuy-ecosystem/.venv-smoke/bin/python scripts/web_consent_smoke.py
"""
import os
import sys
import types

# ── заглушки отсутствующих зависимостей (aiogram + внутренние модули бота) ──
# Делаем МИНИМАЛЬНЫЙ stub: bot.py импортирует только имена, нам не нужна логика.

def _stub_module(name: str, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m

# aiogram
_aiogram = _stub_module("aiogram")
_aiogram.Bot = object
_aiogram.Dispatcher = object
_stub_module("aiogram.client", session=None)
_stub_module("aiogram.client.session")
_sub = _stub_module("aiogram.client.session.aiohttp")
_sub.AiohttpSession = object
_stub_module("aiogram.types").BotCommand = object

# Внутренние модули бота (у них нет зависимостей от aiogram в top-level,
# но при импорте bot.py они импортируются — достаточно пустых стабов).
for _mod in ("ai", "config", "db", "escalation", "metering_worker",
             "multiplex", "nurture", "retention", "richfmt", "triggers",
             "worker", "handlers", "messaging"):
    if _mod not in sys.modules:
        _stub_module(_mod)

# handlers.router нужен как атрибут
sys.modules["handlers"].router = object()
# messaging.LoggingMiddleware
sys.modules["messaging"].LoggingMiddleware = object

# ── теперь импортируем bot ───────────────────────────────────────────────────
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bot-telegram"))

import bot  # noqa: E402

# ── тесты ────────────────────────────────────────────────────────────────────
fails = []

# Без consent-ключа — гард должен блокировать
if bot._consent_required({"messages": [{"role": "user", "content": "хай"}]}) is not True:
    fails.append("без consent гард должен требовать согласие")

# С consent=True — не блокировать
if bot._consent_required({"consent": True, "messages": []}) is not False:
    fails.append("с consent=true гард не должен блокировать")

# consent=False явно — блокировать
if bot._consent_required({"consent": False, "messages": [{"role": "user", "content": "тест"}]}) is not True:
    fails.append("с consent=false гард должен блокировать")

# Не-dict — блокировать
if bot._consent_required(None) is not True:
    fails.append("с body=None гард должен блокировать")

if fails:
    print("\n".join("❌ " + f for f in fails))
    raise SystemExit(1)
print("🟢 web_consent_smoke зелёный")
