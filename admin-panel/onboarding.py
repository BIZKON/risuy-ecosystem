"""Онбординг тенанта: getting-started чеклист из РЕАЛЬНЫХ сигналов (не ручные галочки).
derive_steps — чистая (тестируется без БД); compute_state — собирает сигналы через db-хелперы.
Fail-soft: любой сбой → пустое состояние (дашборд не падает)."""
import db

# Ключи секретов, означающие «бот подключён» (канал-агностично: TG основной + MAX).
_BOT_TOKEN_KEYS = {"telegram_bot_token", "max_bot_token"}

# 5 шагов до «первой ценности». key — сигнал готовности; href — куда ведёт; cta — призыв.
_STEPS = (
    {"key": "bot",    "label": "Подключить бота",           "href": "/keys",        "cta": "Подключить Telegram-бота"},
    {"key": "team",   "label": "Собрать ИИ-команду",        "href": "/my-team",     "cta": "Создать ИИ-сотрудника"},
    {"key": "kb",     "label": "Загрузить базу знаний",     "href": "/knowledge",   "cta": "Загрузить документ"},
    {"key": "funnel", "label": "Включить воронку",          "href": "/lead-magnet", "cta": "Настроить лид-магнит"},
    {"key": "aha",    "label": "Проверить агента вживую",   "href": "/dialogs",     "cta": "Написать боту тест"},
)


def derive_steps(signals: dict) -> dict:
    """Из сигналов готовности {bot,team,kb,funnel,aha: bool} → шаги с done + прогресс. Чистая."""
    steps = [{**s, "done": bool(signals.get(s["key"]))} for s in _STEPS]
    done = sum(1 for s in steps if s["done"])
    total = len(steps)
    pct = round(done / total * 100) if total else 0
    return {"steps": steps, "done_count": done, "total": total, "pct": pct, "complete": done == total}


async def compute_state(tid) -> dict:
    """Состояние онбординг-чеклиста тенанта из реальных данных. tid None / сбой → пустое (0/5)."""
    if not tid:
        return derive_steps({})
    try:
        keynames = {r["key_name"] for r in await db.list_tenant_secrets(tid)}
        team = await db.list_team_agents(tid)
        kb_docs = await db.kb_list_documents()           # RLS-scoped активным тенантом
        funnel = await db.get_funnel_config_panel(tid)
        counts = await db.dashboard_counts({})           # RLS-scoped активным тенантом
        signals = {
            "bot": bool(_BOT_TOKEN_KEYS & keynames),
            "team": len(team) > 0,
            "kb": len(kb_docs) > 0,
            "funnel": bool(str(funnel.get("funnel_enabled") or "").strip()),
            "aha": (counts["total"] or 0) > 0,           # первый лид = бот зарегистрировал человека
        }
    except Exception:  # noqa: BLE001 — онбординг не должен ронять дашборд
        return derive_steps({})
    return derive_steps(signals)
