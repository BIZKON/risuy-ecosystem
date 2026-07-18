"""Слой B: движок ДЕТЕРМИНИРОВАННЫХ триггеров (стоп-слова / кол-во сообщений / документы).
Тип «намерение» — отдельно (маркер в промпте Лии, следующий инкремент).

Per-tenant (db.tenant_id() из contextvar мультиплекса / дефолт Школы). Дедуп НЕТ — решение
владельца «каждый раз»: триггер срабатывает на каждое подходящее сообщение. Первый сработавший
триггер на сообщение → действие, ИИ-ответ на этот ход пропускается.

Действие (action):
  • notify_reply_continue — ответ клиенту (reply_text) + уведомление менеджерам, диалог продолжается;
  • notify_reply_pause    — то же + пауза диалога (дальше отвечает оператор);
  • notify_only           — только уведомление менеджерам (без ответа клиенту).

Уведомление идёт через ЕДИНЫЙ бот-нотификатор (notifier; фолбэк на разговорный бот тенанта).
aiogram/messaging/notifier импортируются ЛЕНИВО внутри отправки — модуль и matching-логика
тестируемы в смоук-venv без aiogram.

Слой C: движок КАНАЛ-АГНОСТИЧЕН. Вход — TriggerCtx (messenger/external_id/text/reply-callable),
а не aiogram (bot, message): один и тот же движок обслуживает Telegram, VK и MAX. Идентичность
лида и ссылка на клиента в карточке выбираются по ctx.messenger; ответ клиенту инкапсулирован в
ctx.reply (канал сам знает, как и куда слать)."""
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import config
import db

logger = logging.getLogger(__name__)


@dataclass
class TriggerCtx:
    """Канал-агностичный контекст входящего сообщения для движка триггеров.

    Развязывает движок от aiogram (bot, message): TG/VK/MAX строят ctx у себя (мультиплекс/
    хендлеры Школы). Поля:
      • messenger     — канал лида ('tg'|'vk'|'max'); выбирает колонку идентичности (db._user_col)
                        и канальную ссылку на клиента в карточке (escalation.client_link);
      • external_id   — внешний id лида В КАНАЛЕ (tg_user_id / vk from_id / max user_id);
      • text          — текст входящего (для документа — подпись/«»): идёт в matching + сниппет карточки;
      • reply         — async-отправка КЛИЕНТУ в его канал (инкапсулирует send + лог source='trigger');
      • notifier_fallback_bot — разговорный бот для фолбэка нотификатора (TG: aiogram Bot; VK/MAX: None —
                        карточку менеджерам шлёт ТОЛЬКО единый нотификатор в TG-группу, как в escalation).
    """
    messenger: str
    external_id: int
    text: str
    reply: Callable[[str], Awaitable[None]]
    notifier_fallback_bot: object = None

_TYPE_RU = {
    "stopwords": "стоп-слово в сообщении",
    "message_count": "достигнуто число сообщений",
    "documents": "клиент прислал документ",
    "intent": "распознано намерение",
}


def match_stopwords(text: str, words) -> str | None:
    """Первое стоп-слово/фраза из words, встреченное в text (регистронезависимо, по границам
    слова — чтобы «кредит» не ловился в «дискредитировать»). None — нет совпадений. Пустые
    значения игнорируются. Поддерживает фразы («когда менеджер свяжется»)."""
    if not text or not words:
        return None
    for w in words:
        w = (w or "").strip()
        if not w:
            continue
        if re.search(r"(?<!\w)" + re.escape(w) + r"(?!\w)", text, re.IGNORECASE):
            return w
    return None


def _parse_int(v) -> int | None:
    s = str(v if v is not None else "").strip()
    return int(s) if s.lstrip("-").isdigit() else None


def format_trigger_card(t: dict, *, external_id: int, reason: str, snippet: str,
                        lead_id: str | None, panel_base: str | None,
                        client_link: tuple[str, str] | None = None) -> str:
    """Карточка уведомления менеджерам (plain, анти-инъекция): тип триггера + причина + фрагмент
    сообщения клиента + ссылки. snippet нормализуем по пробелам (текст лида не доверяем — не
    даём подделать визуальные «поля» переводами строк, как в escalation.format_card).
    client_link=(url, подпись) — канал-специфичная ссылка на клиента; None → дефолт Telegram
    (tg://user?id=, обратная совместимость с TG-картой)."""
    lines = ["🔔 Сработал триггер — " + _TYPE_RU.get(t.get("type"), str(t.get("type")))]
    lines.append("Причина: " + re.sub(r"\s+", " ", reason).strip()[:200])
    snip = re.sub(r"\s+", " ", snippet or "").strip()[:400]
    if snip:
        lines.append("Сообщение клиента: " + snip)
    lines.append("—")
    if panel_base and lead_id:
        lines.append(f"💬 Открыть диалог: {panel_base}/dialogs/{lead_id}")
    url, label = client_link or (f"tg://user?id={external_id}", "Клиент в Telegram")
    lines.append(f"👤 {label}: {url}" if url else f"👤 {label}")
    return "\n".join(lines)[:3500]


# ── Тип «намерение»: распознаёт Лия (маркер [[TRIGGER:N]] в промпте, как [[ESCALATE]]) ──
_TRIGGER_RE = re.compile(r"\[\[TRIGGER:(\d+)\]\]", re.IGNORECASE)
_TRIGGER_FRAG_RE = re.compile(r"\[\[TRIGGER\b.*", re.DOTALL | re.IGNORECASE)


def build_intent_addendum(intent_trigs: list) -> str:
    """Служебный блок для системного промпта Лии: пронумерованный список условий intent-триггеров
    тенанта + инструкция ставить метку [[TRIGGER:N]] (N = позиция, 1-based). reply_text (если есть
    и действие с ответом) даём как ориентир ответа клиенту."""
    lines = [
        "## СЛУЖЕБНЫЕ ТРИГГЕРЫ (клиент НЕ видит):",
        "Если в диалоге выполнилось одно из условий ниже — добавь в САМЫЙ КОНЕЦ ответа служебную "
        "метку [[TRIGGER:N]] (N — номер условия), ПОСЛЕ обычного ответа клиенту, один раз за "
        "срабатывание. Метку клиент НЕ видит и НЕ объясняй её.",
    ]
    for i, t in enumerate(intent_trigs, 1):
        cond = re.sub(r"\s+", " ", (t.get("intent_desc") or "")).strip()
        line = f"{i}. {cond}"
        reply = (t.get("reply_text") or "").strip()
        if reply and t.get("action") in ("notify_reply_continue", "notify_reply_pause"):
            reply_norm = re.sub(r"\s+", " ", reply).strip()
            line += f" — ответь клиенту в духе: «{reply_norm}»"
        lines.append(line)
    return "\n".join(lines)


def parse_trigger_markers(text: str) -> tuple[str, list[int]]:
    """(текст_без_меток, [индексы]). Метки [[TRIGGER:N]] Лия ставит при срабатывании intent-
    триггера — клиент их НЕ видит. Вырезаем парные И усечённый/осиротевший фрагмент (LLM мог
    оборвать ответ на открытой метке), как parse_escalation. Индексы — уникальные, по порядку."""
    if not text or "[[TRIGGER" not in text.upper():
        return text, []
    idxs_raw = [int(m) for m in _TRIGGER_RE.findall(text)]
    cleaned = _TRIGGER_RE.sub("", text)
    if "[[TRIGGER" in cleaned.upper():            # усечённый/битый остаток метки
        cleaned = _TRIGGER_FRAG_RE.sub("", cleaned)
    seen, idxs = set(), []
    for i in idxs_raw:
        if i not in seen:
            seen.add(i)
            idxs.append(i)
    return cleaned.strip(), idxs


async def handle_text(ctx: TriggerCtx) -> bool:
    """Оценить ТЕКСТОВЫЕ триггеры (стоп-слова, кол-во сообщений) текущего тенанта на это сообщение.
    Первый сработавший → действие, return True (вызывающий пропускает ИИ-ответ). Нет триггеров/
    совпадений → False (обычный ИИ-поток; для Школы без настроенных триггеров — всегда False).
    Канал-агностично: идентичность/счётчик — по ctx.messenger/ctx.external_id."""
    trigs = await db.get_active_triggers(db.tenant_id(), types=("stopwords", "message_count"))
    if not trigs:
        return False
    text = ctx.text or ""
    count = None
    for t in trigs:
        if t["type"] == "stopwords":
            hit = match_stopwords(text, t.get("stopwords") or [])
            if hit:
                await _fire(ctx, t, reason=f"стоп-слово «{hit}»")
                return True
        elif t["type"] == "message_count" and t.get("msg_count"):
            if count is None:
                count = await db.count_inbound_messages(ctx.external_id, messenger=ctx.messenger)
            if count == int(t["msg_count"]):
                await _fire(ctx, t, reason=f"{count}-е сообщение в диалоге")
                return True
    return False


async def handle_document(ctx: TriggerCtx) -> bool:
    """Лид прислал документ → триггер типа documents (если настроен). True — обработали
    (вызывающий ИИ не зовёт); False — триггера нет."""
    trigs = await db.get_active_triggers(db.tenant_id(), types=("documents",))
    if not trigs:
        return False
    await _fire(ctx, trigs[0], reason="входящий документ")
    return True


async def _notify(ctx: TriggerCtx, t: dict, *, reason: str) -> None:
    """Карточка менеджерам через ЕДИНЫЙ бот-нотификатор (фолбэк на разговорный бот — только TG;
    для VK/MAX ctx.notifier_fallback_bot=None, как в escalation). Пустой notify_chat_id → выходим
    ДО импорта messaging (тестируемость без aiogram). Ссылка на клиента — по каналу. НЕ бросает."""
    chat_id = _parse_int(t.get("notify_chat_id"))
    if chat_id is None:
        return
    import escalation  # client_link(messenger, external_id) — переиспользуем (vk.com/MAX/tg://)
    # 152-ФЗ: карточка раскрывает контакт клиента менеджеру НАПРЯМУЮ (raw_send_text), МИНУЯ outbox
    # → свой fail-closed гейт. Outbound-сигнал (без согласия) — контакт НЕ раскрываем (§6 путь 5).
    # Единый чок для _fire и fire_intent; дублируется в их началах (defense-in-depth).
    if not await escalation.lead_is_inbound(ctx.external_id, messenger=ctx.messenger):
        return
    import messaging   # ленивый импорт (как escalation): тестируемость без aiogram
    import notifier
    send_bot = notifier.get_notifier_bot() or ctx.notifier_fallback_bot
    if send_bot is None:
        return  # ни нотификатора, ни разговорного бота (VK/MAX без нотификатора) → слать нечем
    lead_id = await db.get_lead_id(ctx.external_id, messenger=ctx.messenger)
    card = format_trigger_card(
        t, external_id=ctx.external_id, reason=reason, snippet=ctx.text,
        lead_id=lead_id, panel_base=config.PANEL_BASE_URL or None,
        client_link=escalation.client_link(ctx.messenger, ctx.external_id))
    try:
        await messaging.raw_send_text(
            send_bot, chat_id, card,
            message_thread_id=_parse_int(t.get("notify_topic_id")), rich=False)
    except Exception:  # noqa: BLE001
        logger.warning("Уведомление по триггеру не ушло (%s=%s)", ctx.messenger, ctx.external_id,
                       exc_info=True)


async def _fire(ctx: TriggerCtx, t: dict, *, reason: str) -> None:
    """Детерминированный триггер: ответ клиенту (canned, заменяет ИИ-ответ) + уведомление +
    опц. пауза. Ответ идёт через ctx.reply (канал инкапсулирует send + лог source='trigger':
    canned-ответ без LLM не тарифицируется per_message-метерингом, который списывает source='liya',
    и не попадает в стат «ответов Лии»). НЕ бросает."""
    action = t.get("action") or "notify_reply_continue"
    reply = (t.get("reply_text") or "").strip()
    try:
        # 152-ФЗ: outbound-сигнал (спарсен без согласия) — НЕ шлём авто-ответ клиенту и НЕ
        # раскрываем контакт менеджеру. Путь идёт мимо outbox_recheck → свой fail-closed гейт
        # (строго '= inbound_optin') в начале _fire, ДО ctx.reply/_notify/pause (§6 путь 5).
        import escalation
        if not await escalation.lead_is_inbound(ctx.external_id, messenger=ctx.messenger):
            return
        if action in ("notify_reply_continue", "notify_reply_pause") and reply:
            await ctx.reply(reply)
        await _notify(ctx, t, reason=reason)
        if action == "notify_reply_pause":
            await db.pause_lead(ctx.external_id, messenger=ctx.messenger)
    except Exception:  # noqa: BLE001
        logger.warning("Срабатывание триггера не выполнено (%s=%s)", ctx.messenger, ctx.external_id,
                       exc_info=True)


async def fire_intent(ctx: TriggerCtx, intent_trigs: list, indices: list[int]) -> None:
    """Сработавшие intent-триггеры (индексы из [[TRIGGER:N]]): уведомление менеджерам + опц. пауза.
    Ответ клиенту отдельно НЕ шлём — Лия уже ответила (инструкция в промпте, build_intent_addendum).
    Невалидный индекс игнорируем."""
    # 152-ФЗ: outbound-сигнал (без согласия) — контакт менеджеру НЕ раскрываем и лид не трогаем
    # (пауза). Путь идёт мимо outbox_recheck → свой fail-closed гейт в начале fire_intent (§6 путь 5).
    # На outbound авто-диалог Лии не должен был запуститься (гейт #6 в handlers) — здесь defense-in-depth.
    import escalation
    if not await escalation.lead_is_inbound(ctx.external_id, messenger=ctx.messenger):
        return
    for i in indices:
        if not (1 <= i <= len(intent_trigs)):
            continue
        t = intent_trigs[i - 1]
        cond = re.sub(r"\s+", " ", (t.get("intent_desc") or "")).strip()
        await _notify(ctx, t, reason=f"намерение: {cond}")
        if (t.get("action") or "") == "notify_reply_pause":
            await db.pause_lead(ctx.external_id, messenger=ctx.messenger)
