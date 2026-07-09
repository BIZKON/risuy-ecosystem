"""Оркестратор: из ответов брифа собирает черновик настройки ИИ-сотрудника.

Чистая функция без побочных эффектов — только возвращает proposal. Запись в прод —
в панели за HumanGate. LLM-разбор с обезличиванием; при любом сбое — детерминированный
фолбэк из maps_to. Никогда не выдумывает данные: чего нет — в gaps.
"""
from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.request

from shared import brief_schema
from shared import pii  # обезличивание ПДн перед внешним ИИ (152-ФЗ): mask → LLM → unmask

logger = logging.getLogger(__name__)

_EMPTY = {"settings": {"persona": {}, "funnel": {}, "triggers": [], "channels": {}},
          "products": [], "recommendations": [], "gaps": []}


def _get(answers: dict, key: str) -> str:
    """Достаёт ответ по ключу вопроса из плоских или секционных answers."""
    if key in answers:
        v = answers[key]
        return v if isinstance(v, str) else v
    for v in answers.values():
        if isinstance(v, dict) and key in v:
            return v[key]
    return ""


def fallback_proposal(answers: dict) -> dict:
    """Детерминированный черновик из maps_to, без LLM. Не выдумывает — пробелы в gaps."""
    p = json.loads(json.dumps(_EMPTY))  # глубокая копия
    funnel = p["settings"]["funnel"]

    for field, qkey in [("company_name", "company_name"), ("operator_name", "operator_name"),
                        ("operator_inn", "operator_inn"), ("operator_email", "operator_email")]:
        val = str(_get(answers, qkey) or "").strip()
        if val:
            funnel[field] = val

    # продукты — по строкам (название — цена — описание)
    raw = str(_get(answers, "products_list") or "").strip()
    for line in [x for x in raw.splitlines() if x.strip()]:
        parts = [s.strip() for s in line.split("—")]
        prod = {"name": parts[0][:200], "caption": (parts[2] if len(parts) > 2 else "")[:4000],
                "kind": "main", "currency": "RUB", "price": None, "link": None}
        p["products"].append(prod)

    # тон → рекомендация по персоне (не пишем поведение сами — только предлагаем)
    tone = str(_get(answers, "tone") or "").strip()
    if tone:
        p["settings"]["persona"]["behavior_prompt"] = f"Общение с клиентами: {tone}."

    # пробелы: обязательные для 152-ФЗ
    for field, label in [("operator_inn", "ИНН оператора"), ("operator_email", "email оператора")]:
        if not funnel.get(field):
            p["gaps"].append({"field": field, "question": f"Не указан {label} — нужен для 152-ФЗ"})

    p["recommendations"].append(
        {"title": "Проверьте черновик перед применением",
         "why": "Собрано автоматически из ответов; отредактируйте формулировки под бренд",
         "section": "all"})
    return p


def _build_prompt(answers: dict) -> str:
    idx = brief_schema.question_index()
    lines = ["Ты — конфигуратор ИИ-продавца. По ответам брифа собери JSON-черновик настройки.",
             "СТРОГО: не выдумывай данные (ИНН, цены, реквизиты). Чего нет — клади в gaps.",
             "Отвечай ТОЛЬКО валидным JSON вида:",
             '{"settings":{"persona":{"name","role","behavior_prompt","knowledge"},'
             '"funnel":{"company_name","operator_name","operator_inn","operator_email","welcome_text"},'
             '"triggers":[{"kind","value"}],"channels":{}},"products":[{"name","price","currency",'
             '"caption","kind"}],"recommendations":[{"title","why","section"}],"gaps":[{"field","question"}]}',
             "", "Ответы клиента:"]
    for key, q in idx.items():
        val = _get(answers, key)
        if val:
            lines.append(f"- {q['label']}: {val}")
    return "\n".join(lines)


# ── Реальный вызов LLM панели: Timeweb AI Gateway (OpenAI-совместимый) ────────────
# У панели НЕТ готового "разговорного" клиента (admin-panel/timeweb_ai.py — только
# управление cloud-ai агентом: list/get/set_system_prompt/create_agent, не "спросить
# текстом"). Разговорный клиент есть только у бота (bot-telegram/ai.py::ask_gateway/
# ask_agent_openai). Для брифа нет привязанного тенант-агента (это же онбординг с нуля,
# агент ещё не создан) — поэтому переиспользуем Timeweb AI Gateway тем же путём, что и
# бот: POST {base}/chat/completions, OpenAI-формат, свой ключ AI Gateway (ОТДЕЛЬНЫЙ от
# TIMEWEB_AI_TOKEN аккаунт-токена панели — см. reference_timeweb_ai_gateway). Ключ
# читаем из env панели BRIEF_LLM_GATEWAY_TOKEN (новая опциональная переменная, panel-
# only, НЕ трогаем admin-panel/config.py) — если её нет, сразу падаем в analyze()'а
# фолбэк. Синхронный urllib в потоке — тот же паттерн, что admin-panel/timeweb_ai.py/
# yookassa.py/dadata.py (без новых зависимостей).
_GATEWAY_DEFAULT_BASE = "https://api.timeweb.ai/v1"
_GATEWAY_DEFAULT_MODEL = "deepseek/deepseek-v4-pro"
_GATEWAY_TIMEOUT = 420.0  # щедрый timeout: reasoning-модель, holистический разбор брифа


def _gateway_request(token: str, base_url: str, model: str, prompt: str) -> str:
    """Синхронный POST на Timeweb AI Gateway /chat/completions (исполняется в треде)."""
    url = f"{base_url.rstrip('/')}/chat/completions"
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "response_format": {"type": "json_object"},
    }).encode()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=_GATEWAY_TIMEOUT) as resp:
        raw = json.loads(resp.read().decode())
    return (raw["choices"][0]["message"]["content"] or "").strip()


async def _default_llm(prompt: str) -> str:
    """Реальный вызов LLM через Timeweb AI Gateway + обезличивание (152-ФЗ).

    Обезличиваем ТОЛЬКО пользовательскую часть промпта (ответы клиента), не служебную
    инструкцию, — но здесь prompt уже единый текст от _build_prompt, поэтому маскируем
    целиком (структурные ПДн: телефон/email/ИНН/СНИЛС/паспорт — единственное, что ловит
    shared.pii, безопасно применять и к служебным строкам, т.к. они таких паттернов не
    содержат). Деобфускация (unmask) — на сыром JSON-тексте ответа, ДО json.loads: если
    LLM процитировала плейсхолдер внутри строкового поля, оригинал должен вернуться.

    Нет BRIEF_LLM_GATEWAY_TOKEN в env → NotImplementedError (analyze() поймает и уйдёт
    в детерминированный фолбэк — сеть не трогаем, если ключ не настроен).
    """
    import os

    token = os.environ.get("BRIEF_LLM_GATEWAY_TOKEN", "").strip()
    if not token:
        raise NotImplementedError(
            "BRIEF_LLM_GATEWAY_TOKEN не задан в env панели — LLM-разбор брифа недоступен, "
            "используется детерминированный фолбэк"
        )
    base_url = os.environ.get("BRIEF_LLM_GATEWAY_URL", "").strip() or _GATEWAY_DEFAULT_BASE
    model = os.environ.get("BRIEF_LLM_MODEL", "").strip() or _GATEWAY_DEFAULT_MODEL

    masked_prompt, mapping = pii.redact_text(prompt)
    try:
        raw = await asyncio.to_thread(_gateway_request, token, base_url, model, masked_prompt)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
        raise RuntimeError(f"Timeweb AI Gateway недоступен: {e}") from e
    return pii.unmask_text(raw, mapping)


def _merge_over_fallback(fb: dict, llm_obj: dict) -> dict:
    """Накладывает валидные поля LLM поверх фолбэка (LLM обогащает, не ломает форму)."""
    out = json.loads(json.dumps(fb))
    s = llm_obj.get("settings") or {}
    for grp in ("persona", "funnel", "channels"):
        if isinstance(s.get(grp), dict):
            out["settings"].setdefault(grp, {}).update({k: v for k, v in s[grp].items() if v})
    if isinstance(s.get("triggers"), list):
        out["settings"]["triggers"] = s["triggers"]
    for k in ("products", "recommendations", "gaps"):
        if isinstance(llm_obj.get(k), list) and llm_obj[k]:
            out[k] = llm_obj[k]
    return out


async def analyze(answers: dict, *, llm=None) -> dict:
    """Главная точка: LLM-разбор поверх детерминированного фолбэка. Никогда не крешит."""
    fb = fallback_proposal(answers)
    call = llm or _default_llm
    try:
        raw = await call(_build_prompt(answers))
        obj = json.loads(raw)
        if not isinstance(obj, dict):
            raise ValueError("LLM вернул не объект")
        return _merge_over_fallback(fb, obj)
    except Exception:
        logger.warning("orchestrator LLM failed, using fallback", exc_info=True)
        return fb
