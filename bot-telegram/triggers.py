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
тестируемы в смоук-venv без aiogram."""
import logging
import re

import config
import db

logger = logging.getLogger(__name__)

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


def format_trigger_card(t: dict, *, tg_user_id: int, reason: str, snippet: str,
                        lead_id: str | None, panel_base: str | None) -> str:
    """Карточка уведомления менеджерам (plain, анти-инъекция): тип триггера + причина + фрагмент
    сообщения клиента + ссылки. snippet нормализуем по пробелам (текст лида не доверяем — не
    даём подделать визуальные «поля» переводами строк, как в escalation.format_card)."""
    lines = ["🔔 Сработал триггер — " + _TYPE_RU.get(t.get("type"), str(t.get("type")))]
    lines.append("Причина: " + re.sub(r"\s+", " ", reason).strip()[:200])
    snip = re.sub(r"\s+", " ", snippet or "").strip()[:400]
    if snip:
        lines.append("Сообщение клиента: " + snip)
    lines.append("—")
    if panel_base and lead_id:
        lines.append(f"💬 Открыть диалог: {panel_base}/dialogs/{lead_id}")
    lines.append(f"👤 Клиент в Telegram: tg://user?id={tg_user_id}")
    return "\n".join(lines)[:3500]


async def handle_text(bot, message) -> bool:
    """Оценить ТЕКСТОВЫЕ триггеры (стоп-слова, кол-во сообщений) текущего тенанта на это сообщение.
    Первый сработавший → действие, return True (вызывающий пропускает ИИ-ответ). Нет триггеров/
    совпадений → False (обычный ИИ-поток; для Школы без настроенных триггеров — всегда False)."""
    trigs = await db.get_active_triggers(db.tenant_id(), types=("stopwords", "message_count"))
    if not trigs:
        return False
    text = message.text or ""
    count = None
    for t in trigs:
        if t["type"] == "stopwords":
            hit = match_stopwords(text, t.get("stopwords") or [])
            if hit:
                await _fire(bot, message, t, reason=f"стоп-слово «{hit}»")
                return True
        elif t["type"] == "message_count" and t.get("msg_count"):
            if count is None:
                count = await db.count_inbound_messages(message.from_user.id)
            if count == int(t["msg_count"]):
                await _fire(bot, message, t, reason=f"{count}-е сообщение в диалоге")
                return True
    return False


async def handle_document(bot, message) -> bool:
    """Лид прислал документ → триггер типа documents (если настроен). True — обработали
    (вызывающий ИИ не зовёт); False — триггера нет."""
    trigs = await db.get_active_triggers(db.tenant_id(), types=("documents",))
    if not trigs:
        return False
    await _fire(bot, message, trigs[0], reason="входящий документ")
    return True


async def _fire(bot, message, t: dict, *, reason: str) -> None:
    """Выполнить действие триггера: ответ клиенту + уведомление менеджерам + опц. пауза.
    НЕ бросает — триггер не должен ронять обработку сообщения."""
    import messaging  # ленивый импорт (как escalation): тестируемость без aiogram
    tg = message.from_user.id
    action = t.get("action") or "notify_reply_continue"
    reply = (t.get("reply_text") or "").strip()
    try:
        # 1. ответ клиенту (заменяет ИИ-ответ на этот ход). source="trigger" (НЕ "liya"):
        # canned-ответ без LLM → НЕ должен тарифицироваться per_message-метерингом (он списывает
        # source='liya') и не попадать в стат «ответов Лии».
        if action in ("notify_reply_continue", "notify_reply_pause") and reply:
            await messaging.send_text(bot, tg, reply, source="trigger", rich=True)
        # 2. уведомление менеджерам через нотификатор (фолбэк на разговорный бот тенанта)
        chat_id = _parse_int(t.get("notify_chat_id"))
        if chat_id is not None:
            import notifier
            send_bot = notifier.get_notifier_bot() or bot
            lead_id = await db.get_lead_id(tg)
            snippet = message.text or getattr(message, "caption", None) or ""
            card = format_trigger_card(
                t, tg_user_id=tg, reason=reason, snippet=snippet,
                lead_id=lead_id, panel_base=config.PANEL_BASE_URL or None)
            try:
                await messaging.raw_send_text(
                    send_bot, chat_id, card,
                    message_thread_id=_parse_int(t.get("notify_topic_id")), rich=False)
            except Exception:  # noqa: BLE001
                logger.warning("Уведомление по триггеру не ушло (tg=%s)", tg, exc_info=True)
        # 3. пауза диалога
        if action == "notify_reply_pause":
            await db.pause_lead(tg)
    except Exception:  # noqa: BLE001
        logger.warning("Срабатывание триггера не выполнено (tg=%s)", tg, exc_info=True)
