"""Вызов AI-ассистента Лии — агент Timeweb Cloud (cloud-ai, русская модель).

Запрос идёт напрямую на api.timeweb.cloud (НЕ через Telegram-прокси — тот только
для api.telegram.org). На любой сбой возвращаем мягкий фолбэк, чтобы пользователь
не остался без ответа. Логику воронки модуль не трогает.
"""
import asyncio
import json
import logging
import time
import uuid
from decimal import Decimal

import aiohttp

import config
import db
import escalation
import triggers
from shared import pii  # PII-маскировка перед внешним ИИ (152-ФЗ): mask → LLM → unmask
from shared.metering import charge_usage, get_tenant_plan
from shared.money import ceil_mul

logger = logging.getLogger(__name__)

_ENDPOINT = "https://api.timeweb.cloud/api/v1/cloud-ai/agents/{agent_id}/call"
_TIMEOUT = aiohttp.ClientTimeout(total=60)  # reasoning-модели (DeepSeek v4) иногда думают >30с → не рубим
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
    try:  # fail-closed: при сбое маскировки НЕ отправляем сырые ПДн во внешний ИИ
        masked_text, _pii = pii.redact_text(text)
    except Exception as e:  # noqa: BLE001
        logger.error("AI: PII-маскировка не удалась — сырьё не отправляем: %s", e)
        return eff_fallback, None
    payload: dict = {"message": masked_text}
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
    return pii.unmask_text(answer, _pii), msg_id  # вернуть оригиналы ПДн пользователю


# ── Wave 5: cloud-ai агент через OpenAI-совместимый эндпоинт (промпт из ПАНЕЛИ) ──
# Нативный /call (выше) берёт промпт ЖЁСТКО из настроек агента Timeweb и не принимает
# его per-request. OpenAI-эндпоинт /cloud-ai/agents/{id}/v1/chat/completions ПРИНИМАЕТ
# messages[] с role:"system" → промпт из app_settings (резолвит get_ai_overrides:
# персона>канал>глобал) доезжает до агента В КАЖДОМ запросе, без PATCH/редеплоя.
# Контекст диалога раньше держал серверный parent_message_id — теперь он собирается
# историей сообщений (см. ask_ai/history). Метеринг НЕ меняется: эндпоинт бьёт в ТОГО
# ЖЕ агента → его used_tokens растёт тем же счётчиком, что читает снапшот-воркер;
# per-call usage из ответа НЕ списываем (иначе двойной счёт со снапшотами — DECISIONS).
_OPENAI_TIMEOUT = aiohttp.ClientTimeout(total=60)  # система-промпт ~20k + thinking-модель → щедрее /call


def _build_chat_messages(
    system_prompt: str | None, history: list[dict] | None, text: str
) -> list[dict]:
    """Собирает messages[] для OpenAI-эндпоинта: [system?] + история + текущий вопрос.
    history — список {"role": "user"|"assistant", "content": str} (готовит get_ai_history).
    Финальный user-turn — text КАК ЕСТЬ (он мог быть дополнен RAG-контекстом в handlers,
    поэтому берём аргумент, а не последнюю запись истории).

    Подряд идущие одинаковые роли СХЛОПЫВАЮТСЯ в одну (контент через \\n\\n). Зачем:
    OpenAI-формат это допускает, но строгие сервера/модели предпочитают чередование, а
    гонка диалога реально даёт user-user — лид прислал 2-е сообщение, пока бот отвечал на
    1-е (его «in» уже в истории, ещё без ответа Лии), и текущий вопрос лёг бы вторым user."""
    raw: list[dict] = []
    sp = (system_prompt or "").strip()
    if sp:
        raw.append({"role": "system", "content": sp})
    for h in history or []:
        content = (h.get("content") or "").strip()
        role = h.get("role")
        if content and role in ("user", "assistant"):
            raw.append({"role": role, "content": content})
    raw.append({"role": "user", "content": text})

    messages: list[dict] = []
    for m in raw:
        if messages and messages[-1]["role"] == m["role"]:
            messages[-1]["content"] += "\n\n" + m["content"]  # склейка, контент не теряем
        else:
            messages.append(dict(m))
    return messages


async def ask_agent_openai(
    messages: list[dict], *, agent_id: str | None = None
) -> str | None:
    """Зовёт cloud-ai агента через OpenAI-совместимый /chat/completions с messages[]
    (вкл. role:"system" из панели). Возвращает ТЕКСТ ответа либо None при ЖЁСТКОМ сбое
    (не настроен / сеть / не-200 / пустой ответ) — тогда вызывающий (ask_ai) фолбэчит
    на нативный /call, чтобы Лия не молчала (§8.7). Токен — config.TIMEWEB_AI_TOKEN (env,
    секрет), хост — config.TIMEWEB_AI_OPENAI_BASE."""
    eff_agent = (agent_id or "").strip() or config.AGENT_ID
    if not eff_agent or not config.TIMEWEB_AI_TOKEN:
        logger.warning("AI(agent-openai) не настроен: пуст agent_id или TIMEWEB_AI_TOKEN")
        return None

    base = config.TIMEWEB_AI_OPENAI_BASE.rstrip("/")
    url = f"{base}/cloud-ai/agents/{eff_agent}/v1/chat/completions"
    headers = {
        "authorization": f"Bearer {config.TIMEWEB_AI_TOKEN}",
        "content-type": "application/json",
    }
    try:  # fail-closed: при сбое маскировки НЕ отправляем сырые ПДн во внешний ИИ
        masked_messages, _pii = pii.redact_messages(messages)
    except Exception as e:  # noqa: BLE001
        logger.error("AI(agent-openai): PII-маскировка не удалась — сырьё не отправляем: %s", e)
        return None
    # model в этом эндпоинте игнорируется (берётся модель агента), но шлём непустой —
    # для совместимости со строгими OpenAI-парсерами. stream=False — ждём полный ответ.
    payload = {"model": "tw-cloud-ai", "messages": masked_messages, "stream": False}

    try:
        async with aiohttp.ClientSession(timeout=_OPENAI_TIMEOUT) as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                status = resp.status
                raw = await resp.text()
    except Exception as e:  # таймаут, сеть, DNS и т.п.
        logger.error("AI(agent-openai) запрос не удался: %s", e)
        return None

    if status != 200:
        logger.error("AI(agent-openai) HTTP %s: %s", status, raw[:300])
        return None

    try:
        data = json.loads(raw)
        answer = (data["choices"][0]["message"]["content"] or "").strip()
    except Exception as e:  # не-JSON / неожиданная схема ответа
        logger.error("AI(agent-openai) ответ не разобран: %s | %s", e, raw[:200])
        return None

    if not answer:
        logger.error("AI(agent-openai) пустой ответ: %s", raw[:200])
        return None
    return pii.unmask_text(answer, _pii)  # вернуть оригиналы ПДн пользователю


# ── Бэкенд «gateway»: Timeweb AI Gateway (OpenAI-совместимый, прямой вызов модели) ──
_GATEWAY_DEFAULT_BASE = "https://api.timeweb.ai/v1"
_DEFAULT_MODEL = "deepseek/deepseek-v4-pro"  # ID шлюза с префиксом провайдера (голый → 404)


async def ask_gateway(
    text: str, *, base_url: str | None = None, model: str | None = None,
    system_prompt: str | None = None, fallback: str | None = None,
    history: list[dict] | None = None,
) -> tuple[str, dict | None]:
    """Спрашивает модель через Timeweb AI Gateway — OpenAI-совместимый /chat/completions.
    Многооборотно: system (если задан) + история диалога + текущее сообщение. Gateway
    stateless (нет серверного parent-контекста, как у cloud-ai агента), поэтому контекст
    несём в messages[] историей — тем же _build_chat_messages, что и cloud_ai-OpenAI (иначе
    модель видит каждое сообщение как первое и здоровается заново). Ключ — config.AI_GATEWAY_TOKEN
    (env, секрет). На любой сбой — мягкий фолбэк, чтобы пользователь не остался без ответа.

    Возвращает (ответ, meta|None): meta = {model, usage, request_id} для метеринга
    (gateway, в отличие от cloud-ai, отдаёт usage в каждом ответе — ТЗ §5.2);
    None — фолбэк/нет usage, списывать нечего."""
    eff_base = (base_url or "").strip().rstrip("/") or _GATEWAY_DEFAULT_BASE
    eff_model = (model or "").strip() or _DEFAULT_MODEL
    eff_fallback = (fallback or "").strip() or _FALLBACK

    if not config.AI_GATEWAY_TOKEN:
        logger.warning("AI Gateway не настроен: пуст AI_GATEWAY_TOKEN")
        return eff_fallback, None

    url = f"{eff_base}/chat/completions"
    headers = {
        "authorization": f"Bearer {config.AI_GATEWAY_TOKEN}",
        "content-type": "application/json",
    }
    messages = _build_chat_messages(system_prompt, history, text)
    try:  # fail-closed: при сбое маскировки НЕ отправляем сырые ПДн во внешний ИИ
        masked_messages, _pii = pii.redact_messages(messages)
    except Exception as e:  # noqa: BLE001
        logger.error("AI Gateway: PII-маскировка не удалась — сырьё не отправляем: %s", e)
        return eff_fallback, None
    payload = {"model": eff_model, "messages": masked_messages, "stream": False}

    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                status = resp.status
                raw = await resp.text()
    except Exception as e:  # таймаут, сеть, DNS и т.п.
        logger.error("AI Gateway запрос не удался: %s", e)
        return eff_fallback, None

    if status != 200:
        logger.error("AI Gateway HTTP %s: %s", status, raw[:300])
        return eff_fallback, None

    try:
        data = json.loads(raw)
        answer = (data["choices"][0]["message"]["content"] or "").strip()
    except Exception as e:  # не-JSON / неожиданная схема ответа
        logger.error("AI Gateway ответ не разобран: %s | %s", e, raw[:200])
        return eff_fallback, None

    if not answer:
        logger.error("AI Gateway пустой ответ: %s", raw[:200])
        return eff_fallback, None
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else None
    meta = {
        "model": (data.get("model") or "").strip() or eff_model,
        "usage": usage,
        "request_id": (data.get("id") or "").strip() or uuid.uuid4().hex,
    } if usage else None
    return pii.unmask_text(answer, _pii), meta  # вернуть оригиналы ПДн пользователю


# ── Cost-capture gateway-вызова (Wave 3, ТЗ §5.2; DECISIONS п.16) ─────────────
# Точный per-call метеринг: usage из ответа → себестоимость по model_prices →
# charge_usage сразу.
#
# ⚠️ Надёжность v1 — AT-MOST-ONCE (DECISIONS п.16): списание идёт fire-and-forget
# после ответа лиду; idempotence_key gw:{tid}:{request_id} страхует от ДУБЛЯ, но
# НЕ гарантирует доставку — при рестарте/недоступности БД в момент ответа
# gateway-списание теряется НЕВОССТАНОВИМО (usage живёт только в памяти; снапшоты
# used_tokens покрывают cloud-ai, но НЕ gateway — у него отдельный счётчик).
# Допустимо для v1: дефолтный бэкенд — cloud_ai (метрируется снапшотами), gateway
# включается опционально. Durable-outbox для gateway — отдельная волна (DECISIONS п.16).
_PRICE_WARN_INTERVAL = 3600.0
_price_warned: dict[str, float] = {}
# Держим ссылки на летящие capture-таски: иначе event loop хранит их слабо и GC
# может собрать незавершённую таску до списания. done_callback снимает ссылку.
_capture_tasks: set = set()


def schedule_gateway_capture(meta: dict) -> None:
    """Ставит cost-capture gateway-вызова фоновой таской (ответ лиду её не ждёт)."""
    task = asyncio.create_task(_capture_gateway_usage(meta))
    _capture_tasks.add(task)
    task.add_done_callback(_capture_tasks.discard)


async def _capture_gateway_usage(meta: dict) -> None:
    model = "?"
    try:
        tid = db.tenant_id()
        if tid is None:
            return
        usage = meta.get("usage") or {}
        # Парсинг usage — ВНУТРИ try: нечисловой ответ шлюза не должен ронять таску
        # «молча» через необработанное исключение (Task exception never retrieved).
        t_in = int(usage.get("prompt_tokens") or 0)
        t_out = int(usage.get("completion_tokens") or 0)
        if t_in + t_out <= 0:
            return
        model = meta.get("model") or "?"
        async with db.pool.acquire() as conn:
            plan = await get_tenant_plan(conn, tid)
            if plan["billing_mode"] == "per_message":
                return  # такие планы метрирует скан сообщений (metering_worker)
            price = await conn.fetchrow(
                "select price_in_microrub_per_1k as pin, price_out_microrub_per_1k as pout "
                "from model_prices where provider = 'timeweb-ai-gateway' and model = $1 "
                "order by effective_from desc limit 1",
                model,
            )
            if price is None:
                # Цены НЕ выдумываем (guardrail ТЗ §10) — не списываем и зовём
                # владельца вписать тариф из ЛК. Лог рейт-лимитирован.
                now = time.monotonic()
                if now - _price_warned.get(model, 0.0) > _PRICE_WARN_INTERVAL:
                    _price_warned[model] = now
                    logger.error(
                        "Метеринг gateway: нет цены модели %r (provider=timeweb-ai-gateway) "
                        "в model_prices — расход НЕ списывается. Впишите тариф из ЛК Timeweb.",
                        model,
                    )
                return
            # µRUB: (вход×цена_1k + выход×цена_1k) / 1000, округление вверх один раз.
            cost = ceil_mul(t_in * price["pin"] + t_out * price["pout"], Decimal("0.001"))
            await charge_usage(
                conn, tid, cost,
                {
                    "kind": "llm", "provider": "timeweb-ai-gateway", "model": model,
                    "units": {"tokens_in": t_in, "tokens_out": t_out,
                              "tokens_total": t_in + t_out},
                    "request_id": meta.get("request_id"),
                },
                f"gw:{tid}:{meta.get('request_id')}",
                allow_negative=True,
            )
            # Блокировка — по ТЕКУЩЕМУ балансу (а не balance_after возможной dup-строки,
            # финдинг №3), на том же conn (без второго acquire — финдинг №6). Тенант по
            # умолчанию (Школа) не блокируется никогда (§8.7, финдинг №8).
            if plan["prepaid"] and tid != db.default_tenant_id():
                bal = await conn.fetchval(
                    "select balance_microrub from credit_wallets where tenant_id = $1", tid)
                if bal is not None and int(bal) <= 0:
                    await db.set_ai_wallet_blocked(tid, True, conn=conn)
                    logger.error(
                        "Кошелёк тенанта %s исчерпан (баланс %s µRUB) — ИИ на мягкой паузе",
                        tid, bal,
                    )
    except Exception:  # noqa: BLE001 — метеринг не должен ронять ответивший путь
        logger.warning("Метеринг gateway-вызова не записан (model=%s)", model, exc_info=True)


# Рейт-лимит лога фолбэка OpenAI→/call: во время аварии эндпоинта фолбэк случается на
# КАЖДОЕ сообщение — без троттла лог затопило бы. Раз в час достаточно как сигнал.
_FALLBACK_WARN_INTERVAL = 3600.0
_fallback_warned_at = 0.0


def _fallback_due() -> bool:
    global _fallback_warned_at
    now = time.monotonic()
    if now - _fallback_warned_at > _FALLBACK_WARN_INTERVAL:
        _fallback_warned_at = now
        return True
    return False


# ── СП-2-память: суммаризация диалога для долгой памяти ───────────────────────
_SUMMARY_SYSTEM = (
    "Ты — модуль памяти. Сожми диалог в 3-5 кратких фактов о клиенте и его задаче "
    "(потребность, договорённости, контекст), которые помогут продолжить разговор позже. "
    "Только факты из диалога, без выдумок. Без приветствий и воды."
)


async def summarize_dialog(dialog_text: str, cfg: dict) -> str | None:
    """Сводка диалога для долгой памяти. Идёт через тот же masked-бэкенд (mask→LLM→unmask),
    что и ответы → сырьё ПДн во внешний ИИ не уходит. None при сбое/фолбэке (память не пишется)."""
    if not (dialog_text or "").strip():
        return None
    backend = (cfg.get("backend") or "").strip()
    try:
        if backend == "gateway":
            ans, meta = await ask_gateway(
                dialog_text, base_url=cfg.get("gateway_base_url"), model=cfg.get("model"),
                system_prompt=_SUMMARY_SYSTEM, fallback=None)
            # gateway при сбое возвращает непустой _FALLBACK с meta=None → meta как сигнал
            # успеха (ТЗ §5.2: на реальном ответе usage всегда есть → meta != None).
            return ans.strip() if (meta is not None and ans and ans.strip()) else None
        msgs = _build_chat_messages(_SUMMARY_SYSTEM, None, dialog_text)
        ans = await ask_agent_openai(msgs, agent_id=cfg.get("agent_id"))
        return ans.strip() if (ans and ans.strip()) else None
    except Exception as e:  # noqa: BLE001
        logger.warning("summarize_dialog не удался: %s", e)
        return None


async def ask_ai(
    text: str, parent_message_id: str | None, cfg: dict,
    *, history: list[dict] | None = None,
) -> tuple[str, str | None, dict | None, list[int]]:
    """ЕДИНАЯ точка ответа Лии: бэкенд-диспетчер + ОБЯЗАТЕЛЬНАЯ вырезка СЛУЖЕБНЫХ маркеров:
    эскалации (A3, [[ESCALATE]]) и intent-триггеров (Слой B, [[TRIGGER:N]]). Возвращает
    (ответ_БЕЗ_маркеров, msg_id, esc_payload, trigger_indices).
      • esc_payload != None → горячий лид (вызывающий → escalation.escalate);
      • trigger_indices — номера сработавших intent-триггеров (вызывающий → triggers.fire_intent).
    Вырезка здесь, а не у вызывающих, — единая точка: ни School, ни мультиплекс-тенант НЕ утечёт
    клиенту служебный маркер (ревью A3). Оба парсера вырезают и парный, и усечённый маркер."""
    answer, msg_id = await _ask_ai_backend(text, parent_message_id, cfg, history=history)
    answer, esc_payload = escalation.parse_escalation(answer)
    answer, trigger_indices = triggers.parse_trigger_markers(answer)
    return answer, msg_id, esc_payload, trigger_indices


async def _ask_ai_backend(
    text: str, parent_message_id: str | None, cfg: dict,
    *, history: list[dict] | None = None,
) -> tuple[str, str | None]:
    """Диспетчер бэкенда ИИ по cfg['backend'] (из app_settings — db.get_ai_overrides).
    Возвращает (ответ, msg_id|None). msg_id всегда None для gateway и cloud_ai-OpenAI
    (серверного parent-контекста нет — контекст несёт history); ненулевой msg_id может
    вернуть лишь нативный /call-фолбэк (он сохранится в FSM, но OpenAI-путь его игнорит).

    history — последние ходы диалога [{"role","content"}] (готовит db.get_ai_history);
    нужны и cloud_ai-OpenAI, и gateway (оба stateless: контекст несём в messages[], а не
    серверным parent_message_id)."""
    if cfg.get("backend") == "gateway":
        answer, meta = await ask_gateway(
            text, base_url=cfg.get("gateway_base_url"), model=cfg.get("model"),
            system_prompt=cfg.get("system_prompt"), fallback=cfg.get("fallback"),
            history=history,
        )
        if meta:
            # Fire-and-forget: ответ лиду не ждёт списания. Таска наследует
            # contextvar tenant_id текущей задачи (create_task копирует контекст);
            # ссылка держится в _capture_tasks, чтобы GC не съел до завершения.
            schedule_gateway_capture(meta)
        return answer, None

    # cloud_ai (Wave 5): OpenAI-эндпоинт агента с role:"system" из панели + история.
    messages = _build_chat_messages(cfg.get("system_prompt"), history, text)
    answer = await ask_agent_openai(messages, agent_id=cfg.get("agent_id"))
    if answer is not None:
        return answer, None
    # Жёсткий сбой OpenAI-эндпоинта (не настроен/сеть/не-200) → фолбэк на нативный /call:
    # промпт из панели теряется (агент ответит по своему промпту Timeweb), НО Лия не молчит
    # (§8.7). Нативный путь сам отдаст мягкий текст-фолбэк, если и он недоступен.
    # ⚠️ Нативный /call физически НЕ принимает messages[]/историю (именно ради этого был
    # сделан Wave 5) → фолбэк-ответ ОДНОХОДОВЫЙ, без последних N сообщений и без промпта
    # панели. Это осознанный размен §8.7 (Лия отвечает > Лия молчит); со стороны /call
    # контекст не вернуть. Лог рейт-лимитирован (иначе в аварию писал бы на каждое сообщение)
    # и поднят до error — сигнал владельцу «промпт панели не доезжает» (сверь хост
    # TIMEWEB_AI_OPENAI_BASE). Видимый ops-алерт/виджет в панели — отдельный шаг (handoff).
    if _fallback_due():
        logger.error(
            "AI(agent-openai) недоступен (база %s) — фолбэк на нативный /call: промпт панели "
            "и история НЕ применяются. Проверьте доступность хоста OpenAI-эндпоинта.",
            config.TIMEWEB_AI_OPENAI_BASE,
        )
    return await ask_liya(
        text, parent_message_id,
        agent_id=cfg.get("agent_id"), fallback=cfg.get("fallback"),
    )
