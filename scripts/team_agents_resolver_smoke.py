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


def row(slug, *, is_default=False, enabled=True, system_prompt=None, fallback_text="",
        agent_id="", backend=None, kb_enabled=False):
    return {"id": f"id:{slug}", "slug": slug, "is_default": is_default, "enabled": enabled,
            "name": slug, "role_preset": None,
            "system_prompt": f"p:{slug}" if system_prompt is None else system_prompt,
            "backend": backend, "agent_id": agent_id, "fallback_text": fallback_text,
            "escalation_chat_id": "", "escalation_topic_id": None,
            "is_orchestrator": False, "memory_enabled": False, "kb_enabled": kb_enabled}


def legacy(*, system_prompt="легаси-промпт", fallback="легаси-фолбэк", agent_id="legacy-agent"):
    """Легаси-конфиг тенанта (tenant_settings) — то, что отдаёт get_tenant_ai_overrides."""
    return {"enabled": True, "backend": "cloud_ai", "agent_id": agent_id,
            "model": "легаси-модель", "gateway_base_url": "", "system_prompt": system_prompt,
            "fallback": fallback, "kb_enabled": False}


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

    # ── Э3: наследование от легаси при склейке конфига (_merge_agent_cfg, чистая) ──
    # Главный кейс этапа: автосев/создание агента в панели даёт строку с ПУСТЫМ промптом.
    # Пока промпт брался как `or ''`, живой тенант на легаси-ключе оставался без инструкций.
    m = db._merge_agent_cfg(row("auto", is_default=True, system_prompt=""), legacy())
    check("пустой промпт агента → легаси-промпт", m["system_prompt"] == "легаси-промпт",
          repr(m["system_prompt"]))
    # Свой промпт агента легаси не перебивается — иначе отделы потеряли бы роли.
    m = db._merge_agent_cfg(row("sales", is_default=True, system_prompt="свой"), legacy())
    check("свой промпт агента побеждает легаси", m["system_prompt"] == "свой")
    # Симметрия, ради которой правка и делалась: фолбэк ведёт себя так же.
    m = db._merge_agent_cfg(row("auto", is_default=True, fallback_text=""), legacy())
    check("пустой фолбэк агента → легаси-фолбэк", m["fallback"] == "легаси-фолбэк")
    # agent_id: агент без провижининга наследует cloud-ai агента тенанта.
    m = db._merge_agent_cfg(row("auto", is_default=True, agent_id=""), legacy())
    check("пустой agent_id → легаси agent_id", m["agent_id"] == "legacy-agent")
    m = db._merge_agent_cfg(row("auto", is_default=True, agent_id="свой-агент"), legacy())
    check("свой agent_id побеждает", m["agent_id"] == "свой-агент")
    # Мусорный backend не должен утечь в вызов LLM.
    m = db._merge_agent_cfg(row("auto", is_default=True, backend="чепуха"), legacy())
    check("невалидный backend → cloud_ai", m["backend"] == "cloud_ai", m["backend"])
    m = db._merge_agent_cfg(row("auto", is_default=True, backend="gateway"), legacy())
    check("валидный backend сохраняется", m["backend"] == "gateway")
    # Тумблер базы знаний — per-agent, легаси на него не влияет.
    m = db._merge_agent_cfg(row("auto", is_default=True, kb_enabled=True), legacy())
    check("kb_enabled берётся у агента", m["kb_enabled"] is True)

    if FAILS:
        print("\n".join("❌ " + f for f in FAILS))
        raise SystemExit(1)
    print("🟢 team_agents_resolver_smoke зелёный")


if __name__ == "__main__":
    main()
