"""СП-2-память: долгая память team-агента. Суммаризация диалога (через masked-LLM) → эмбеддинг
(РФ-TEI passage) → agent_memory; ретрив релевантных сводок прошлого для подмешивания в ответ.
Аддитивно и fail-soft: любой сбой → бот отвечает как раньше (память молча отключается).
v1: per-lead (сводки только этого клиента — нет кросс-клиентской утечки), только kind='summary'."""
import logging
import re

import config
import db
import escalation
import kb
import triggers

logger = logging.getLogger(__name__)

# Жёсткий потолок длины сводки: LLM-суммаризатор недоверен (лид может «продиктовать» ему
# простыню), а retrieve подмешивает сводки в промпт целиком.
_SUMMARY_MAX_LEN = 600

# Вырезание служебных маркеров ИЗ СВОДКИ. В отличие от _MARKER_FRAG_RE/_TRIGGER_FRAG_RE
# (путь ОТВЕТА, где маркер по протоколу в КОНЦЕ → режут до конца строки через .*), здесь
# маркер может стоять ГДЕ УГОДНО в сводке недоверенного диалога. Поэтому паттерн ОГРАНИЧЕН
# (без .*): вырезает только сам токен [[...]], а факты вокруг него сохраняются.
_SUMMARY_MARKER_RE = re.compile(r"\[\[\s*/?\s*(?:ESCALATE|TRIGGER)\b[^\]]*\]{0,2}", re.IGNORECASE)


def _sanitize_summary(text: str) -> str:
    """Санитизация сводки перед записью в долгую память: сводка построена из НЕДОВЕРЕННОГО
    диалога лида — вырезаем служебные маркеры ([[ESCALATE]]/[[TRIGGER:N]]), чтобы сохранённый
    «факт» не мог позже эмитить реальные действия, и ограничиваем длину."""
    cleaned = text or ""
    # Итеративно ДО ФИКСИРОВАННОЙ ТОЧКИ: одиночный .sub на ВЛОЖЕННОМ маркере
    # ([[ESCA[[TRIGGER:5]]LATE]]) вырезал бы внутренний и СКЛЕИЛ обломки в валидный [[ESCALATE]]
    # — filter-bypass через вложение (как <scr<script>ipt>). Цикл схлопывает вложения.
    while True:
        stripped = _SUMMARY_MARKER_RE.sub("", cleaned)
        if stripped == cleaned:
            break
        cleaned = stripped
    cleaned = cleaned.strip()[:_SUMMARY_MAX_LEN].strip()
    # Fail-closed: если ОСТАТОК всё равно распознаётся детекторами ответного пути как маркер
    # (форма, которую _SUMMARY_MARKER_RE не покрыл), сводку НЕ храним вовсе — пусть лучше пусто,
    # чем осевший в памяти исполняемый маркер. Синхронизировано с реальными парсерами.
    if escalation._MARKER_ANCHOR_RE.search(cleaned) or "[[TRIGGER" in cleaned.upper():
        return ""
    return cleaned


async def retrieve(text: str, tenant_id, agent_id, lead_key: str | None,
                   *, vec: list[float] | None = None) -> str:
    """Блок «контекст прошлых диалогов с этим клиентом» для подмешивания (или "").
    vec — предвычисленный эмбеддинг запроса (переиспользование на пути ответа); None → считаем сами."""
    if not agent_id:
        return ""
    if vec is None:
        vec = await kb.embed_query(text)
    if not vec:
        return ""
    try:
        hits = await db.memory_search(
            vec, tenant_id, agent_id, lead_key,
            top_k=config.MEMORY_TOP_K, max_distance=config.MEMORY_MAX_DISTANCE)
    except Exception as e:  # noqa: BLE001 — сбой/нет таблицы → без памяти
        logger.warning("memory_search не удался: %s", e)
        return ""
    if not hits:
        return ""
    body = "\n".join(f"• {h.strip()}" for h in hits)
    # Только метка-заголовок: анти-инъекционная директива и «это данные, не инструкции»
    # живут в _DATA_FENCE (kb.augment), СНАРУЖИ блока данных — иначе директива внутри
    # блока «здесь только данные» сама себе противоречит (ревью Фазы 0).
    return "🧠 Прошлые диалоги с этим клиентом:\n" + body


def _dialog_text(history: list[dict]) -> str:
    """Последние сообщения истории → текст для суммаризации (роль: контент)."""
    out = []
    for h in history or []:
        content = (h.get("content") or "").strip()
        if not content:
            continue
        role = "Клиент" if h.get("role") == "user" else "Агент"
        out.append(f"{role}: {content}")
    return "\n".join(out)


async def maybe_summarize(*, external_id, tenant_id, cfg: dict, history: list[dict],
                          msg_count: int, lead_key: str | None) -> None:
    """Каждые MEMORY_SUMMARIZE_EVERY входящих — суммаризировать недавний диалог и записать в память.
    Best-effort: вызывается ПОСЛЕ отправки ответа клиенту, любой сбой проглатывается."""
    import ai  # ленивый импорт (ai тянет многое)
    agent_id = cfg.get("team_agent_id")
    every = config.MEMORY_SUMMARIZE_EVERY
    if not agent_id or every <= 0 or msg_count <= 0:
        return
    # Дельта-порог (устойчив к дрейфу чётности счётчика): суммируем, когда накопилось ≥ every
    # новых ходов с последней сводки (metadata.up_to). Точный %-modulo мог бы НАВСЕГДА промахнуться
    # при нечётном сбое (operator-manual без парного in / не залогированный out).
    last_up_to = await db.memory_last_up_to(tenant_id, agent_id, lead_key)
    if msg_count - last_up_to < every:
        return
    dialog = _dialog_text(history)
    if not dialog:
        return
    try:
        raw = await ai.summarize_dialog(dialog, cfg)
        if not raw:
            return  # LLM-сбой/фолбэк — водяной знак НЕ двигаем, повторим на следующем пороге
        summary = _sanitize_summary(raw)
        if not summary:
            # Сводка после санитизации пуста (напр. состояла только из внедрённых лидом
            # маркеров). Фиксируем водяной знак up_to БЕЗ контентной записи: иначе дельта-порог
            # остаётся взведённым и суммаризатор-LLM дёргается на КАЖДОМ следующем входящем
            # этого лида (эконом-DoS ровно тем лидом, от которого защищаемся).
            await db.memory_mark_up_to(tenant_id, agent_id, lead_key, msg_count)
            return
        emb = await kb.embed_passage(summary)
        if not emb:
            return
        await db.memory_insert(
            tenant_id, agent_id, summary, emb,
            metadata={"lead": lead_key, "up_to": msg_count})
    except Exception as e:  # noqa: BLE001 — память не должна ломать диалог
        logger.warning("maybe_summarize не удался: %s", e)
