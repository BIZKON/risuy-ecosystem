"""Минимальный клиент Timeweb cloud-ai для УПРАВЛЕНИЯ агентом из панели (раздел «Базы
знаний» — обучение Лии). Зеркалит yookassa.py: stdlib urllib в треде (asyncio.to_thread),
без сторонних зависимостей.

Что умеет:
  • list_agents      — список cloud-ai агентов (с settings, вкл. system_prompt);
  • set_system_prompt — обучить агента: GET полного settings → заменить system_prompt → PATCH
    (партиальный settings Timeweb отвергает 400 — шлём ВЕСЬ settings обратно);
  • list_models       — каталог моделей (id → public_name, для подписи «DeepSeek V4 Pro»);
  • list_knowledge_bases — базы знаний (RAG; каждая = платная векторная БД).

Авторизация — Bearer аккаунт-токеном (config.TIMEWEB_AI_TOKEN, только из env). Запрос идёт
напрямую к api.timeweb.cloud (из ru-1 доступен; это инфра Timeweb, как и БД). Нет токена →
TimewebAIError ДО сети (раздел покажет подсказку).
"""
from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request

import config


class TimewebAIError(Exception):
    """Сбой обращения к Timeweb cloud-ai (выключено / сеть / не-2xx / битый ответ)."""


def _request(method: str, path: str, *, body: dict | None = None, timeout: float = 25.0) -> dict:
    """Синхронный вызов Timeweb API (исполняется в треде). Возвращает распарсенный JSON."""
    if not config.TIMEWEB_AI_ENABLED:
        raise TimewebAIError("Управление агентом выключено: не задан TIMEWEB_AI_TOKEN в env панели")
    url = f"{config.TIMEWEB_API_BASE.rstrip('/')}/{path.lstrip('/')}"
    data = json.dumps(body).encode() if body is not None else None
    headers = {
        "Authorization": f"Bearer {config.TIMEWEB_AI_TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode()[:500]
        except Exception:
            pass
        raise TimewebAIError(f"Timeweb AI HTTP {e.code}: {detail}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise TimewebAIError(f"Timeweb AI недоступен: {e}") from e
    except (ValueError, json.JSONDecodeError) as e:
        raise TimewebAIError("Timeweb AI вернул невалидный ответ") from e


async def list_agents() -> list[dict]:
    """Все cloud-ai агенты. Список включает полный settings (system_prompt в т.ч.)."""
    data = await asyncio.to_thread(_request, "GET", "/cloud-ai/agents")
    return data.get("agents", [])


async def get_agent(agent_id) -> dict:
    data = await asyncio.to_thread(_request, "GET", f"/cloud-ai/agents/{agent_id}")
    return data.get("agent", data)


async def set_system_prompt(agent_id, prompt: str) -> dict:
    """Обучить агента: меняем ТОЛЬКО system_prompt, но PATCH-им ВЕСЬ settings (партиал → 400).
    GET свежий settings (чтобы не затереть model-параметры/refine_query) → подменяем промпт."""
    agent = await get_agent(agent_id)
    settings = dict(agent.get("settings") or {})
    settings["system_prompt"] = prompt
    data = await asyncio.to_thread(
        _request, "PATCH", f"/cloud-ai/agents/{agent_id}", body={"settings": settings}
    )
    return data.get("agent", data)


async def list_models() -> dict:
    """{model_id: public_name} — для подписи модели агента («DeepSeek V4 Pro»)."""
    data = await asyncio.to_thread(_request, "GET", "/cloud-ai/models")
    return {m["id"]: m.get("public_name") or m.get("name") for m in data.get("models", [])}


async def list_knowledge_bases() -> list[dict]:
    """Базы знаний (RAG). Каждая — платная векторная БД (dbaas_preset + сеть + токен-пакет)."""
    data = await asyncio.to_thread(_request, "GET", "/cloud-ai/knowledge-bases")
    return data.get("knowledge_bases", [])
