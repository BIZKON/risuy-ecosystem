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


async def ask_liya(text: str, parent_message_id: str | None = None) -> tuple[str, str | None]:
    """Спрашивает агента Лию.

    Возвращает (текст_ответа, id_сообщения). id можно передать в parent_message_id
    следующего запроса — так сохраняется контекст диалога.
    На любой сбой возвращает (мягкий фолбэк, None).
    """
    if not config.AGENT_ID or not config.TIMEWEB_AI_TOKEN:
        logger.warning("AI не настроен: пусты AGENT_ID/TIMEWEB_AI_TOKEN")
        return _FALLBACK, None

    url = _ENDPOINT.format(agent_id=config.AGENT_ID)
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
        return _FALLBACK, None

    if status != 200:
        logger.error("AI HTTP %s: %s", status, raw[:300])
        return _FALLBACK, None

    try:
        data = json.loads(raw)
    except Exception as e:
        logger.error("AI ответ не JSON: %s | %s", e, raw[:200])
        return _FALLBACK, None

    if not isinstance(data, dict):
        logger.error("AI неожиданный ответ: %s", str(data)[:200])
        return _FALLBACK, None

    answer = (data.get("message") or "").strip()
    msg_id = data.get("id")
    if not answer:
        logger.error(
            "AI пустой ответ (finish_reason=%s): %s",
            data.get("finish_reason"), str(data)[:200],
        )
        return _FALLBACK, None
    return answer, msg_id
