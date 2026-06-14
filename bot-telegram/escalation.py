"""A3: авто-эскалация горячего лида менеджерам.

Лия по достижении квалификации (или по запросу клиента) добавляет в КОНЕЦ ответа служебный
маркер [[ESCALATE]]{json}[[/ESCALATE]] (см. промпт §12). Бот:
  1) ВСЕГДА вырезает маркер из ответа клиенту (даже если эскалация выключена / JSON битый) —
     клиент служебный блок не видит;
  2) если эскалация включена (MANAGER_GROUP_ID задан) и маркер был — постит карточку лида в
     группу менеджеров (опц. в тему форума), с дедупом (одна карточка на лид).

Карточка — PLAIN-текст без parse_mode: payload приходит из LLM (не доверяем) → отсутствие
разметки исключает HTML-инъекцию в чат менеджеров.
"""
import json
import logging
import re

import config
import db

logger = logging.getLogger(__name__)

# Нежадный матч ПОЛНОЙ пары — из неё достаём JSON. DOTALL: JSON может содержать переводы строк.
_MARKER_RE = re.compile(r"\[\[ESCALATE\]\](.*?)\[\[/ESCALATE\]\]", re.DOTALL | re.IGNORECASE)
# Якорь маркера: '[[ESCALATE' или '[[/ESCALATE' (опенер/клозер), БЕЗ требования ']]' — чтобы
# ловить усечённый по лимиту токенов опенер ('[[ESCALATE', '[[ESCALATE]') и НЕ срабатывать на
# голую подстроку 'ESCALATE]]' без '[[' (та не маркер → не ложная эскалация). Ревью A3.
_MARKER_ANCHOR_RE = re.compile(r"\[\[/?ESCALATE", re.IGNORECASE)
# Любой ФРАГМЕНТ маркера (опенер/клозер с 0/1/2 закрывающими скобками) до конца текста: вырезаем
# осиротевший/усечённый остаток, чтобы клиент НИКОГДА не увидел даже обрывок маркера + ПДн.
_MARKER_FRAG_RE = re.compile(r"\[\[/?ESCALATE(?:\]\]?)?.*", re.DOTALL | re.IGNORECASE)

# Поля карточки: (ключ в json, подпись). Порядок = порядок в карточке.
_FIELDS = [
    ("reason", "Причина"), ("name", "Имя"), ("phone", "Телефон"),
    ("intent", "Интент"), ("product", "Курс"), ("goal", "Цель"),
    ("summary", "Итог"), ("next_step", "Менеджеру"),
]


def parse_escalation(text: str) -> tuple[str, dict | None]:
    """(текст_без_маркера, payload|None). Маркер вырезается ВСЕГДА — и ПАРНЫЙ, и ОСИРОТЕВШИЙ/
    усечённый (LLM мог оборвать ответ на открытом маркере по лимиту токенов). payload — dict из
    JSON парного маркера ({} при битом/усечённом — чтобы менеджер всё равно получил сигнал,
    а не «тихую потерю»). None — маркера в тексте не было вовсе (быстрый выход)."""
    if not text or not _MARKER_ANCHOR_RE.search(text):
        return text, None
    payload: dict | None = None
    cleaned = text
    blocks = _MARKER_RE.findall(text)
    if blocks:
        cleaned = _MARKER_RE.sub("", cleaned)
        raw = (blocks[-1] or "").strip()  # последний полный блок
        payload = {}
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict):
                payload = obj
        except Exception:  # noqa: BLE001 — битый JSON: маркер вырезан, шлём сырьё менеджеру
            payload = {}
    # Defense-in-depth: вырезать ЛЮБОЙ оставшийся фрагмент маркера до конца текста (усечённый
    # опенер '[[ESCALATE'/'[[ESCALATE]' / битый закрывающий / одиночный клозер) — иначе обрывок
    # маркера + собранные ПДн клиента утекли бы клиенту. Якорь по ОПЕНЕРУ, не по ']]'.
    if _MARKER_ANCHOR_RE.search(cleaned):
        cleaned = _MARKER_FRAG_RE.sub("", cleaned)
        if payload is None:
            payload = {}  # маркер БЫЛ (хоть и обрезанный) → сигнал эскалации, payload пуст
    return cleaned.strip(), payload


def format_card(payload: dict, *, tg_user_id: int, raw: str | None = None) -> str:
    """Карточка лида менеджерам (plain, без разметки). Поля обрезаются; пустые/нестроковые
    пропускаются; нет данных → сырой сигнал для ручного разбора."""
    lines = ["🔥 Горячий лид — передача менеджеру"]
    shown = False
    for key, label in _FIELDS:
        v = payload.get(key) if isinstance(payload, dict) else None
        if isinstance(v, (str, int, float)) and str(v).strip():
            # Нормализуем пробелы/переводы строк: payload из LLM → клиент не должен через \n
            # подделать визуальные строки-«поля» в чате менеджеров (ревью A3, disputed-харднинг).
            clean_v = re.sub(r"\s+", " ", str(v)).strip()[:300]
            lines.append(f"{label}: {clean_v}")
            shown = True
    if not shown:
        lines.append("(данные лида не разобраны — сырой сигнал ниже)")
        if raw:
            lines.append(raw[:500])
    lines.append(f"tg_user_id: {tg_user_id}")
    return "\n".join(lines)[:3500]


async def escalate(bot, tg_user_id: int, payload: dict, *, raw: str | None = None) -> None:
    """Передать лида менеджерам (дедуп: одна карточка на лид). НЕ бросает — эскалация не
    должна ронять ответ клиенту. Порядок: атомарный claim → отправка → при сбое release
    (чтобы лид не потерялся и следующее квал-сообщение попробовало снова)."""
    if not config.MANAGER_ESCALATION_ENABLED:
        return
    import messaging  # ленивый импорт: parse_escalation/format_card тестируемы без aiogram
    try:
        if not await db.claim_lead_escalation(tg_user_id):
            return  # уже эскалирован / нет лида / гонка проиграна
        try:
            text = format_card(payload, tg_user_id=tg_user_id, raw=raw)
            await messaging.raw_send_text(
                bot, config.MANAGER_GROUP_ID, text,
                message_thread_id=config.MANAGER_TOPIC_ID, rich=False,
            )
        except Exception:
            await db.release_lead_escalation(tg_user_id)  # откат claim → ретрай позже
            raise
    except Exception:  # noqa: BLE001
        logger.warning("Эскалация менеджерам не удалась (tg=%s)", tg_user_id, exc_info=True)
