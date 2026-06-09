"""Вызов AI-ассистента Лии — агент Timeweb Cloud (cloud-ai, русская модель).

Запрос идёт напрямую на api.timeweb.cloud (НЕ через Telegram-прокси — тот только
для api.telegram.org). На любой сбой возвращаем мягкий фолбэк, чтобы пользователь
не остался без ответа. Логику воронки модуль не трогает.
"""
import json
import logging

import aiohttp

import config

logger = logging.getLogger(__name__)

_ENDPOINT = "https://api.timeweb.cloud/api/v1/cloud-ai/agents/{agent_id}/call"
_TIMEOUT = aiohttp.ClientTimeout(total=30)
_FALLBACK = (
    "Ой, сейчас не получается ответить 🌷\n"
    "Напиши, пожалуйста, менеджеру: lesovschool@yandex.ru"
)


async def ask_liya(
    text: str,
    parent_message_id: str | None = None,
    *,
    agent_id: str | None = None,
    fallback: str | None = None,
) -> tuple[str, str | None]:
    """Спрашивает агента Лию.

    Возвращает (текст_ответа, id_сообщения). id можно передать в parent_message_id
    следующего запроса — так сохраняется контекст диалога.
    На любой сбой возвращает (мягкий фолбэк, None).

    agent_id/fallback — переопределения из app_settings (раздел «ИИ-агенты» панели),
    поверх env. Пустые/None → берём из окружения: config.AGENT_ID и хардкод _FALLBACK.
    Токен (TIMEWEB_AI_TOKEN) переопределять нельзя — он только в env (секрет).
    """
    eff_agent = (agent_id or "").strip() or config.AGENT_ID
    eff_fallback = (fallback or "").strip() or _FALLBACK

    if not eff_agent or not config.TIMEWEB_AI_TOKEN:
        logger.warning("AI не настроен: пуст agent_id или TIMEWEB_AI_TOKEN")
        return eff_fallback, None

    url = _ENDPOINT.format(agent_id=eff_agent)
    headers = {
        "authorization": f"Bearer {config.TIMEWEB_AI_TOKEN}",
        "content-type": "application/json",
    }
    payload: dict = {"message": text}
    if parent_message_id:
        payload["parent_message_id"] = parent_message_id

    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                status = resp.status
                raw = await resp.text()
    except Exception as e:  # таймаут, сеть, DNS и т.п.
        logger.error("AI запрос не удался: %s", e)
        return eff_fallback, None

    if status != 200:
        logger.error("AI HTTP %s: %s", status, raw[:300])
        return eff_fallback, None

    try:
        data = json.loads(raw)
    except Exception as e:
        logger.error("AI ответ не JSON: %s | %s", e, raw[:200])
        return eff_fallback, None

    if not isinstance(data, dict):
        logger.error("AI неожиданный ответ: %s", str(data)[:200])
        return eff_fallback, None

    answer = (data.get("message") or "").strip()
    msg_id = data.get("id")
    if not answer:
        logger.error(
            "AI пустой ответ (finish_reason=%s): %s",
            data.get("finish_reason"), str(data)[:200],
        )
        return eff_fallback, None
    return answer, msg_id


# ── Бэкенд «gateway»: Timeweb AI Gateway (OpenAI-совместимый, прямой вызов модели) ──
_GATEWAY_DEFAULT_BASE = "https://api.timeweb.ai/v1"
_DEFAULT_MODEL = "deepseek-v4-pro"


async def ask_gateway(
    text: str, *, base_url: str | None = None, model: str | None = None,
    system_prompt: str | None = None, fallback: str | None = None,
) -> str:
    """Спрашивает модель через Timeweb AI Gateway — OpenAI-совместимый /chat/completions.
    Однооборотно: system (если задан) + текущее сообщение пользователя; контекст диалога
    НЕ сохраняется (в отличие от cloud-ai агента). Ключ — config.AI_GATEWAY_TOKEN (env,
    секрет). На любой сбой — мягкий фолбэк, чтобы пользователь не остался без ответа."""
    eff_base = (base_url or "").strip().rstrip("/") or _GATEWAY_DEFAULT_BASE
    eff_model = (model or "").strip() or _DEFAULT_MODEL
    eff_fallback = (fallback or "").strip() or _FALLBACK

    if not config.AI_GATEWAY_TOKEN:
        logger.warning("AI Gateway не настроен: пуст AI_GATEWAY_TOKEN")
        return eff_fallback

    url = f"{eff_base}/chat/completions"
    headers = {
        "authorization": f"Bearer {config.AI_GATEWAY_TOKEN}",
        "content-type": "application/json",
    }
    messages: list[dict] = []
    sp = (system_prompt or "").strip()
    if sp:
        messages.append({"role": "system", "content": sp})
    messages.append({"role": "user", "content": text})
    payload = {"model": eff_model, "messages": messages, "stream": False}

    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                status = resp.status
                raw = await resp.text()
    except Exception as e:  # таймаут, сеть, DNS и т.п.
        logger.error("AI Gateway запрос не удался: %s", e)
        return eff_fallback

    if status != 200:
        logger.error("AI Gateway HTTP %s: %s", status, raw[:300])
        return eff_fallback

    try:
        data = json.loads(raw)
        answer = (data["choices"][0]["message"]["content"] or "").strip()
    except Exception as e:  # не-JSON / неожиданная схема ответа
        logger.error("AI Gateway ответ не разобран: %s | %s", e, raw[:200])
        return eff_fallback

    if not answer:
        logger.error("AI Gateway пустой ответ: %s", raw[:200])
        return eff_fallback
    return answer


async def ask_ai(text: str, parent_message_id: str | None, cfg: dict) -> tuple[str, str | None]:
    """Диспетчер бэкенда ИИ по cfg['backend'] (из app_settings — db.get_ai_overrides).
    Возвращает (ответ, msg_id|None). Для gateway msg_id=None (серверного контекста нет);
    для cloud_ai — id ответа агента (хранится в FSM как parent_message_id след. запроса)."""
    if cfg.get("backend") == "gateway":
        answer = await ask_gateway(
            text, base_url=cfg.get("gateway_base_url"), model=cfg.get("model"),
            system_prompt=cfg.get("system_prompt"), fallback=cfg.get("fallback"),
        )
        return answer, None
    return await ask_liya(
        text, parent_message_id,
        agent_id=cfg.get("agent_id"), fallback=cfg.get("fallback"),
    )
