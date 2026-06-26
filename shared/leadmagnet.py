"""Конструктор воронки выдачи лид-магнита — ЕДИНЫЙ источник истины.

Здесь живут: канонический генератор текста согласия 152-ФЗ из структурных полей,
схема полей конструктора (для рендера формы в панели) и валидация. Чистый Python без
внешних зависимостей — импортируется и ботом (multiplex/funnel), и панелью (форма+preview),
и smoke-скриптами (.venv-smoke). Формулировка согласия эквивалентна действующему тексту
Школы (bot-telegram/texts.py::greeting), но с подстановкой реквизитов конкретного тенанта.

⚠️ 152-ФЗ: текст согласия НЕ задаётся свободным вводом — он строится здесь из проверенного
шаблона по структурным полям. Менять формулировку — только тут, согласовав с юр-требованиями.
"""
from __future__ import annotations

import re

# Цель обработки по умолчанию (если тенант не задал свою).
DEFAULT_DATA_PURPOSE = "отправить материалы и быть на связи по вашему запросу"

_INN_RE = re.compile(r"^\d{10}$|^\d{12}$")            # ИНН юр.лица (10) или ИП/физлица (12)
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_HTTP_RE = re.compile(r"^https?://[^\s]+$", re.IGNORECASE)


def _truthy(v) -> bool:
    return bool(str(v or "").strip())


def _is_inn(v: str) -> bool:
    return bool(_INN_RE.match((v or "").strip()))


def _is_email(v: str) -> bool:
    return bool(_EMAIL_RE.match((v or "").strip()))


def _is_http_url(v: str) -> bool:
    return bool(_HTTP_RE.match((v or "").strip()))


def _data_list(phone_step: bool) -> str:
    return "имя и номер телефона" if phone_step else "имя"


def build_consent_text(
    operator_name: str,
    operator_inn: str,
    operator_email: str,
    data_purpose: str | None = None,
    privacy_url: str | None = None,
    *,
    phone_step: bool = True,
) -> str:
    """152-ФЗ-блок согласия из структурных полей тенанта.

    Соответствует составу действующего согласия Школы: оператор+ИНН, перечень данных, цель,
    срок (до отзыва), локализация хранения (РФ), способ отзыва (email), опц. ссылка на политику,
    финальная фраза про «Даю согласие». Возвращает готовый plain-текст (без HTML/markdown).
    """
    purpose = (data_purpose or "").strip() or DEFAULT_DATA_PURPOSE
    lines = [
        "Чтобы продолжить, нужно согласие на обработку персональных данных:",
        f"• Оператор: {operator_name}, ИНН {operator_inn}",
        f"• Данные: {_data_list(phone_step)}",
        f"• Цель: {purpose}",
        "• Срок: до отзыва согласия",
        "• Хранение — на серверах в России",
        f"• Отозвать согласие: {operator_email}",
    ]
    if _truthy(privacy_url):
        lines.append("Полные условия — в политике обработки данных (кнопка ниже).")
    lines.append("Нажимая «Даю согласие», вы соглашаетесь на обработку указанных данных.")
    return "\n".join(lines)


# ── Схема полей конструктора (контракт формы панели + валидации) ──────────────
# kind: bool | text | longtext | email | url | inn | tg_file | select | channel_id
# required: True (всегда при funnel_enabled) | "leadmagnet:link" / "leadmagnet:file" / "gate"
#           (условно-обязательно) | False
FUNNEL_FIELDS: list[dict] = [
    {"key": "funnel_enabled", "label": "Включить воронку выдачи лид-магнита", "kind": "bool", "required": False},
    {"key": "welcome_text", "label": "Приветствие на /start", "kind": "longtext", "required": False},

    {"key": "operator_name", "label": "Оператор ПДн (юр.лицо / ИП)", "kind": "text", "required": True},
    {"key": "operator_inn", "label": "ИНН оператора", "kind": "inn", "required": True},
    {"key": "operator_email", "label": "E-mail для отзыва согласия", "kind": "email", "required": True},
    {"key": "data_purpose", "label": "Цель обработки данных", "kind": "text", "required": False},
    {"key": "privacy_url", "label": "Ссылка на политику обработки данных", "kind": "url", "required": False},
    {"key": "company_name", "label": "Название в платёжных описаниях", "kind": "text", "required": False},

    {"key": "phone_step_enabled", "label": "Спрашивать номер телефона", "kind": "bool", "required": False},

    {"key": "gate_enabled", "label": "Требовать подписку на канал", "kind": "bool", "required": False},
    {"key": "gate_channel_id", "label": "ID канала-гейта", "kind": "channel_id", "required": "gate"},
    {"key": "gate_channel_url", "label": "Ссылка на канал-гейт", "kind": "url", "required": "gate"},

    {"key": "leadmagnet_kind", "label": "Тип лид-магнита", "kind": "select", "options": ["link", "file"], "required": False},
    {"key": "leadmagnet_url", "label": "Ссылка на лид-магнит", "kind": "url", "required": "leadmagnet:link"},
    # leadmagnet_product_id ставит обработчик загрузки файла (создаёт tenant-продукт lead_magnet);
    # leadmagnet_file_id — продвинутый ручной путь (сырой tg file_id). Для kind=file достаточно любого.
    {"key": "leadmagnet_product_id", "label": "Файл-материал (продукт)", "kind": "hidden", "required": False},
    {"key": "leadmagnet_file_id", "label": "Файл лид-магнита (tg file_id, продвинуто)", "kind": "tg_file", "required": False},
    {"key": "leadmagnet_caption", "label": "Подпись к выдаче", "kind": "longtext", "required": False},

    {"key": "video_note_file_id", "label": "Видео-кружок перед выдачей (опц.)", "kind": "tg_file", "required": False},
]

FUNNEL_KEYS: list[str] = [f["key"] for f in FUNNEL_FIELDS]


def validate_funnel_fields(d: dict) -> list[str]:
    """Проверка набора полей конструктора. Возвращает список человекочитаемых ошибок (пусто = ок).

    Если воронка выключена (funnel_enabled пуст) — обязательных проверок нет (тенант ещё настраивает).
    При включённой воронке требуем валидные реквизиты оператора (для согласия) и сам лид-магнит.
    """
    errs: list[str] = []
    if not _truthy(d.get("funnel_enabled")):
        return errs  # выключенная воронка не обязана быть заполненной

    # Реквизиты оператора (для согласия 152-ФЗ)
    if not _truthy(d.get("operator_name")):
        errs.append("Укажите оператора ПДн (юр.лицо/ИП) — без него нельзя сформировать согласие.")
    if not _truthy(d.get("operator_inn")):
        errs.append("Укажите ИНН оператора.")
    elif not _is_inn(d.get("operator_inn")):
        errs.append("ИНН должен состоять из 10 или 12 цифр.")
    if not _truthy(d.get("operator_email")):
        errs.append("Укажите e-mail для отзыва согласия.")
    elif not _is_email(d.get("operator_email")):
        errs.append("E-mail для отзыва согласия указан некорректно.")
    if _truthy(d.get("privacy_url")) and not _is_http_url(d.get("privacy_url")):
        errs.append("Ссылка на политику должна начинаться с http:// или https://.")

    # Лид-магнит
    kind = (d.get("leadmagnet_kind") or "").strip()
    if kind not in ("link", "file"):
        errs.append("Выберите тип лид-магнита: ссылка или файл.")
    elif kind == "link":
        if not _truthy(d.get("leadmagnet_url")):
            errs.append("Укажите ссылку на лид-магнит.")
        elif not _is_http_url(d.get("leadmagnet_url")):
            errs.append("Ссылка на лид-магнит должна начинаться с http:// или https://.")
    elif kind == "file":
        # Файл настроен, если загружен (product_id) ИЛИ задан сырой tg file_id (продвинуто).
        if not _truthy(d.get("leadmagnet_product_id")) and not _truthy(d.get("leadmagnet_file_id")):
            errs.append("Загрузите файл лид-магнита (или укажите tg file_id).")

    # Гейт-канал (если включён)
    if _truthy(d.get("gate_enabled")):
        if not _truthy(d.get("gate_channel_id")):
            errs.append("Укажите ID канала-гейта или выключите гейт подписки.")
        if not _truthy(d.get("gate_channel_url")):
            errs.append("Укажите ссылку на канал-гейт.")
        elif not _is_http_url(d.get("gate_channel_url")):
            errs.append("Ссылка на канал-гейт должна начинаться с http:// или https://.")

    return errs
