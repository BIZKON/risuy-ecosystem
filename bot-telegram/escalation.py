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
    ("intent", "Тип запроса"), ("product", "Курс"), ("goal", "Цель"),
    ("summary", "Сводка диалога"), ("next_step", "Менеджеру"),
]
# Русские подписи для машинных enum-кодов (Лия ставит коды; менеджер видит русский).
# Коды стабильны для логики/фильтров — переводим ТОЛЬКО на показе. Неизвестный код → как есть.
_VALUE_RU = {
    "reason": {
        "qualified": "квалифицирован (готов к записи)",
        "client_request": "просит менеджера / живого человека",
        "missing_data": "нужны данные/уточнение у менеджера",
    },
    "intent": {
        "enroll": "запись на курс",
        "extend": "продление / заморозка / перенос",
        "schedule": "расписание",
        "payment": "оплата",
        "other": "другое",
    },
}


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


def format_card(payload: dict, *, tg_user_id: int, lead_id: str | None = None,
                panel_base: str | None = None, raw: str | None = None,
                client_link: tuple[str, str] | None = None) -> str:
    """Карточка лида менеджерам (plain, без разметки). Поля обрезаются; пустые/нестроковые
    пропускаются; нет данных → сырой сигнал. Внизу — ссылки: диалог в панели (читать+ответить
    через бота) и прямой ЧС клиента. client_link=(url, подпись) — канал-специфичная ссылка на
    клиента; None → дефолт Telegram (tg://user?id=, обратная совместимость)."""
    lines = ["🔥 Горячий лид — передача менеджеру"]
    shown = False
    for key, label in _FIELDS:
        v = payload.get(key) if isinstance(payload, dict) else None
        if isinstance(v, (str, int, float)) and str(v).strip():
            # Нормализуем пробелы/переводы строк: payload из LLM → клиент не должен через \n
            # подделать визуальные строки-«поля» в чате менеджеров (ревью A3, disputed-харднинг).
            # «Сводка диалога» — щедрее по длине (контекст для менеджера).
            cap = 900 if key == "summary" else 300
            clean_v = re.sub(r"\s+", " ", str(v)).strip()[:cap]
            # Машинные enum-коды (reason/intent) → русская подпись для менеджера.
            ru = _VALUE_RU.get(key)
            if ru:
                clean_v = ru.get(clean_v.lower(), clean_v)
            lines.append(f"{label}: {clean_v}")
            shown = True
    if not shown:
        lines.append("(данные лида не разобраны — сырой сигнал ниже)")
        if raw:
            lines.append(raw[:500])
    # Ссылки для менеджера. Telegram авто-линкует https:// и tg:// в plain-тексте.
    lines.append("—")
    if panel_base and lead_id:
        lines.append(f"💬 Открыть диалог и ответить клиенту: {panel_base}/dialogs/{lead_id}")
    url, label = client_link or (f"tg://user?id={tg_user_id}", "Написать клиенту в Telegram")
    lines.append(f"👤 {label}: {url}" if url else f"👤 {label}")
    return "\n".join(lines)[:3500]


async def resolve_escalation_target(tid) -> tuple[int, int | None] | None:
    """(chat_id, topic_id) куда слать карточку для тенанта tid — либо None (эскалация не настроена).

    Приоритет (Слой A): per-tenant адрес из tenant_settings (клиент задаёт в панели «Мой
    ИИ-сотрудник») → env-фолбэк ТОЛЬКО для дефолт-тенанта (Школа), чтобы текущее env-поведение
    Школы не сломать. Клиентский тенант без заданного адреса → None (карточку не шлём)."""
    cfg = await db.get_tenant_escalation(tid)
    if cfg["enabled"] and cfg["chat_id"] is not None:
        return cfg["chat_id"], cfg["topic_id"]
    if tid == db.default_tenant_id() and config.MANAGER_GROUP_ID is not None:
        return config.MANAGER_GROUP_ID, config.MANAGER_TOPIC_ID
    return None


def client_link(messenger: str, external_id: int) -> tuple[str, str] | None:
    """Канал-специфичная ссылка на клиента для карточки менеджеру. None → дефолт Telegram (tg://).
    Публичный резолвер канала → (url, подпись); переиспользуется триггерами (triggers._notify)."""
    if messenger == "vk":
        import vk_driver  # чистые функции (stdlib-импорт), без aiohttp на уровне модуля
        return vk_driver.vk_client_link(external_id)
    if messenger == "max":
        import max_driver
        return max_driver.max_client_link(external_id)
    return None  # tg → None (format_card дефолтит tg://)


async def escalate(bot, tg_user_id: int, payload: dict, *,
                   messenger: str = "tg", raw: str | None = None,
                   target_override: "tuple[int, int | None] | None" = None) -> None:
    """Передать лида менеджерам в адрес ТЕНАНТА (дедуп: одна карточка на лид). НЕ бросает —
    эскалация не должна ронять ответ клиенту. Порядок: резолв адреса тенанта → атомарный claim →
    отправка → при сбое release. Адрес per-tenant (db.tenant_id() из contextvar). tg_user_id —
    внешний id лида в канале messenger; bot — разговорный бот (фолбэк отправки; для vk None,
    карточку шлёт единый нотификатор в TG-группу). claim/get_lead_id/release — по messenger."""
    try:
        # СП-1: per-agent адрес отдела (из team_agents через cfg) перекрывает общий адрес тенанта;
        # пусто → фолбэк на per-tenant resolve_escalation_target (адрес отдела не задан = общий тенанта).
        target = target_override or await resolve_escalation_target(db.tenant_id())
        if target is None:
            return  # адрес эскалации у тенанта не задан → карточку не шлём (маркер уже вырезан в ask_ai)
        chat_id, topic_id = target
        import messaging  # ленивый импорт: parse_escalation/format_card тестируемы без aiogram
        import notifier   # единый сервис-бот; None → фолбэк на разговорный бот тенанта
        send_bot = notifier.get_notifier_bot() or bot
        if send_bot is None:
            return  # ни нотификатора, ни разговорного бота (vk без нотификатора) → слать нечем
        if not await db.claim_lead_escalation(tg_user_id, messenger=messenger):
            return  # уже эскалирован / нет лида / гонка проиграна
        try:
            lead_id = await db.get_lead_id(tg_user_id, messenger=messenger)
            text = format_card(
                payload, tg_user_id=tg_user_id, lead_id=lead_id,
                panel_base=config.PANEL_BASE_URL or None, raw=raw,
                client_link=client_link(messenger, tg_user_id),
            )
            await messaging.raw_send_text(
                send_bot, chat_id, text, message_thread_id=topic_id, rich=False,
            )
        except Exception:
            await db.release_lead_escalation(tg_user_id, messenger=messenger)  # откат claim → ретрай позже
            raise
    except Exception:  # noqa: BLE001
        logger.warning("Эскалация менеджерам не удалась (%s=%s)", messenger, tg_user_id, exc_info=True)


async def escalate_web(tid, payload: dict, *, raw: str | None = None) -> bool:
    """Эскалация горячего лида из ВЕБ-ЧАТА сайта (анонимный посетитель, без лид-записи в БД).

    Отличия от escalate(): (1) адрес берём по ЯВНОМУ tid (в веб-запросе contextvar tenant_id НЕ
    выставлен — нет middleware мультиплекса); (2) нет claim/дедупа по лиду (лида нет — дедуп per-IP
    делает вызывающий, bot._esc_allow_web); (3) нет разговорного фолбэк-бота (веб-контекст) —
    шлём ТОЛЬКО единым нотификатором. Контакт — из payload (что собрала Лия: телефон/@username/
    почта). Не бросает: ответ посетителю не должен падать из-за эскалации. True — карточка ушла."""
    try:
        target = await resolve_escalation_target(tid)
        if target is None:
            return False  # адрес эскалации у тенанта не задан → карточку не шлём
        chat_id, topic_id = target
        import messaging  # ленивый импорт (как в escalate): pure-хелперы тестируемы без aiogram
        import notifier
        send_bot = notifier.get_notifier_bot()
        if send_bot is None:
            logger.warning("Веб-эскалация: нотификатор не настроен (NOTIFIER_BOT_TOKEN) — слать нечем")
            return False
        # tg_user_id=0 не используется: client_link задан явно (нет tg://-ссылки на анон-веб-лида).
        card = format_card(
            payload, tg_user_id=0, lead_id=None, panel_base=None, raw=raw,
            client_link=("", "контакт лида — в полях карточки выше (если оставил)"),
        )
        text = "🌐 Лид с ВЕБ-ЧАТА сайта (info.pro-agent-ai.ru)\n" + card
        await messaging.raw_send_text(send_bot, chat_id, text, message_thread_id=topic_id, rich=False)
        return True
    except Exception:  # noqa: BLE001
        logger.warning("Веб-эскалация не удалась (tid=%s)", tid, exc_info=True)
        return False
