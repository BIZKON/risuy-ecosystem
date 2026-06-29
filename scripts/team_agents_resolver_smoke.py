#!/usr/bin/env python3
"""Чистый смоук слоёв резолвера команды: _pick_team_agent (диалог>канал>дефолт). БД не нужна.
Запуск: PYTHONPATH=bot-telegram ./.venv-smoke/bin/python scripts/team_agents_resolver_smoke.py"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bot-telegram"))
os.environ.setdefault("BOT_TOKEN", "x")
os.environ.setdefault("DATABASE_URL", "postgresql://x/y")
os.environ.setdefault("CHANNEL_ID", "-1001234567890")
os.environ.setdefault("CHANNEL_URL", "https://t.me/x")
os.environ.setdefault("GUIDE_URL", "https://example.com/guide")
import db  # noqa: E402  (bot-telegram/db.py)

FAILS: list[str] = []


def check(name, cond, detail=""):
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


def row(slug, *, is_default=False, enabled=True):
    return {"slug": slug, "is_default": is_default, "enabled": enabled,
            "name": slug, "role_preset": None, "system_prompt": f"p:{slug}",
            "backend": None, "agent_id": "", "fallback_text": "",
            "escalation_chat_id": "", "escalation_topic_id": None,
            "is_orchestrator": False, "memory_enabled": False}


def main() -> None:
    rows = [row("sales", is_default=True), row("support"), row("off", enabled=False)]
    # диалог побеждает всё
    p = db._pick_team_agent(rows, lead_agent_slug="support", channel_slug="sales")
    check("диалог→support", p and p["slug"] == "support")
    # канал, если нет диалога
    p = db._pick_team_agent(rows, lead_agent_slug=None, channel_slug="support")
    check("канал→support", p and p["slug"] == "support")
    # дефолт, если нет диалога/канала
    p = db._pick_team_agent(rows, lead_agent_slug=None, channel_slug=None)
    check("дефолт→sales", p and p["slug"] == "sales")
    # выключенный агент игнорируется на всех слоях → падаем ниже
    p = db._pick_team_agent(rows, lead_agent_slug="off", channel_slug=None)
    check("выключенный диалог-агент игнор → дефолт", p and p["slug"] == "sales")
    # пустой набор → None (вызыватель уйдёт на легаси-фолбэк)
    check("нет агентов → None", db._pick_team_agent([], lead_agent_slug=None, channel_slug=None) is None)
    # несуществующий slug канала → None-канал → дефолт
    p = db._pick_team_agent(rows, lead_agent_slug=None, channel_slug="nope")
    check("неизвестный канал → дефолт", p and p["slug"] == "sales")

    if FAILS:
        print("\n".join("❌ " + f for f in FAILS))
        raise SystemExit(1)
    print("🟢 team_agents_resolver_smoke зелёный")


if __name__ == "__main__":
    main()
