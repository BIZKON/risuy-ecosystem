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
        "• Для подготовки ответов используются автоматизированные системы, включая ИИ; "
        "идентифицирующие контакты обезличиваются",
        f"• Отозвать согласие: {operator_email}",
    ]
    if _truthy(privacy_url):
        lines.append("Полные условия — в политике обработки данных (кнопка ниже).")
    lines.append("Нажимая «Даю согласие», вы соглашаетесь на обработку указанных данных.")
    return "\n".join(lines)


def build_privacy_policy(
    operator_name: str,
    operator_inn: str,
    operator_email: str,
    operator_ogrn: str | None = None,
    operator_address: str | None = None,
    data_purpose: str | None = None,
    *,
    phone_step: bool = True,
    transborder: bool = True,
) -> str:
    """Полный текст «Политики обработки персональных данных» (152-ФЗ ст. 18.1) из реквизитов оператора.

    ⚠️ ШАБЛОН: формулировки выверены под типовую модель (имя+телефон, согласие как основание, хранение
    в РФ, без трансграничной передачи), НО финально документ должен УТВЕРДИТЬ ЮРИСТ оператора —
    ответственность по 152-ФЗ несёт оператор (тенант). Возвращает plain-текст (разделы пронумерованы)."""
    purpose = (data_purpose or "").strip() or DEFAULT_DATA_PURPOSE
    data = _data_list(phone_step)
    req = [f"наименование (ФИО): {operator_name}", f"ИНН: {operator_inn}"]
    if (operator_ogrn or "").strip():
        req.append(f"ОГРН/ОГРНИП: {operator_ogrn.strip()}")
    if (operator_address or "").strip():
        req.append(f"адрес: {operator_address.strip()}")
    req.append(f"e-mail: {operator_email}")
    requisites = "; ".join(req)

    if transborder:
        clause_63 = (
            "6.3. Для подготовки ответов на обращения оператор использует сторонние "
            "автоматизированные сервисы обработки (в том числе системы искусственного интеллекта). "
            "При этом персональные данные могут передаваться обработчикам, в том числе за пределы "
            "Российской Федерации. Идентифицирующие контакты (номер телефона, адрес электронной почты, "
            "ИНН) обезличиваются до передачи в такие сервисы."
        )
    else:
        clause_63 = "6.3. Трансграничная передача персональных данных не осуществляется."
    clause_65 = (
        "6.5. Для подготовки ответов на обращения оператор использует автоматизированные системы, "
        "в том числе системы искусственного интеллекта. Идентифицирующие контакты (номер телефона, "
        "адрес электронной почты, ИНН) обезличиваются до передачи в такие системы."
    )

    return "\n".join([
        "ПОЛИТИКА ОБРАБОТКИ ПЕРСОНАЛЬНЫХ ДАННЫХ",
        "",
        "1. Общие положения",
        f"1.1. Настоящая Политика определяет порядок обработки персональных данных оператором "
        f"{operator_name} (далее — Оператор; реквизиты: {requisites}) и меры по обеспечению их безопасности.",
        "1.2. Политика разработана в соответствии с Федеральным законом от 27.07.2006 № 152-ФЗ "
        "«О персональных данных» и действует в отношении всех персональных данных, которые Оператор "
        "получает от субъектов персональных данных.",
        "1.3. Оператор публикует настоящую Политику в свободном доступе во исполнение ч. 2 ст. 18.1 152-ФЗ.",
        "",
        "2. Основные понятия",
        "2.1. Персональные данные (ПДн) — любая информация, относящаяся к прямо или косвенно определённому "
        "или определяемому физическому лицу (субъекту ПДн).",
        "2.2. Обработка ПДн — любое действие с ПДн (сбор, запись, хранение, использование, удаление и др.).",
        "2.3. Субъект ПДн — физическое лицо, обратившееся к Оператору и предоставившее свои данные.",
        "",
        "3. Состав обрабатываемых персональных данных",
        f"3.1. Оператор обрабатывает следующие категории ПДн: {data}.",
        "3.2. Специальные категории ПДн и биометрические ПДн Оператором не обрабатываются.",
        "",
        "4. Цели обработки персональных данных",
        f"4.1. Цель обработки: {purpose}.",
        "",
        "5. Правовые основания обработки",
        "5.1. Правовым основанием обработки является согласие субъекта ПДн, предоставляемое при обращении "
        "к Оператору (в том числе через чат-бота).",
        "",
        "6. Порядок и условия обработки",
        "6.1. Обработка осуществляется с согласия субъекта ПДн смешанным способом (автоматизированно и без "
        "использования средств автоматизации).",
        "6.2. Хранение ПДн осуществляется на серверах, расположенных на территории Российской Федерации "
        "(ч. 5 ст. 18 152-ФЗ).",
        clause_63,
        "6.4. Срок обработки — до достижения цели обработки либо до отзыва согласия субъектом ПДн. "
        "После отзыва согласия данные подлежат обезличиванию/удалению в срок не позднее 30 дней.",
        clause_65,
        "",
        "7. Права субъекта персональных данных",
        "7.1. Субъект ПДн вправе получать информацию об обработке своих данных, требовать их уточнения, "
        "блокирования или уничтожения, а также отозвать согласие на обработку в любой момент.",
        f"7.2. Для реализации своих прав субъект ПДн направляет обращение на e-mail: {operator_email}. "
        "Отозвать согласие также можно командой «/revoke» в чат-боте.",
        "",
        "8. Обеспечение безопасности персональных данных",
        "8.1. Оператор принимает правовые, организационные и технические меры для защиты ПДн от "
        "неправомерного доступа, уничтожения, изменения, блокирования, копирования и распространения.",
        "",
        "9. Заключительные положения",
        "9.1. Оператор вправе вносить изменения в настоящую Политику. Актуальная редакция размещается "
        "по адресу публикации Политики.",
    ])


def legal_doc_url(base: str | None, slug: str | None, doc: str = "privacy") -> str:
    """Публичный URL юр-страницы тенанта, которую отдаёт бот: {base}/legal/{slug}/{doc}.

    ЕДИНЫЙ источник сборки ссылки: панель показывает её тенанту, бот строит ту же в get_funnel_config.
    Возвращает пусто, если не задан публичный base бота или slug тенанта, либо doc вне
    ('privacy','consent') — тогда панель просто не рисует кнопку (никаких битых ссылок тенанту).
    """
    b = (base or "").strip().rstrip("/")
    s = (slug or "").strip().strip("/")
    if not b or not s or doc not in ("privacy", "consent"):
        return ""
    return f"{b}/legal/{s}/{doc}"


# ── Схема полей конструктора (контракт формы панели + валидации) ──────────────
# kind: bool | text | longtext | email | url | inn | tg_file | select | channel_id
# required: True (всегда при funnel_enabled) | "leadmagnet:link" / "leadmagnet:file" / "gate"
#           (условно-обязательно) | False
FUNNEL_FIELDS: list[dict] = [
    {"key": "funnel_enabled", "label": "Включить воронку выдачи лид-магнита", "kind": "bool", "required": False},
    {"key": "welcome_text", "label": "Приветствие на /start", "kind": "longtext", "required": False},

    {"key": "operator_name", "label": "Оператор ПДн (юр.лицо / ИП)", "kind": "text", "required": True},
    {"key": "operator_inn", "label": "ИНН оператора", "kind": "inn", "required": True},
    {"key": "operator_email", "label": "E-mail для отзыва согласия / запросов субъекта", "kind": "email", "required": True},
    {"key": "operator_ogrn", "label": "ОГРН / ОГРНИП оператора (для Политики)", "kind": "text", "required": False},
    {"key": "operator_address", "label": "Юридический / почтовый адрес оператора (для Политики)", "kind": "text", "required": False},
    {"key": "data_purpose", "label": "Цель обработки данных", "kind": "text", "required": False},
    {"key": "privacy_url", "label": "Ссылка на политику обработки данных", "kind": "url", "required": False},
    {"key": "company_name", "label": "Название в платёжных описаниях", "kind": "text", "required": False},

    {"key": "phone_step_enabled", "label": "Спрашивать номер телефона", "kind": "bool", "required": False},

    {"key": "gate_enabled", "label": "Требовать подписку на канал", "kind": "bool", "required": False},
    {"key": "gate_channel_id", "label": "ID канала-гейта", "kind": "channel_id", "required": "gate"},
    {"key": "gate_channel_url", "label": "Ссылка на канал-гейт", "kind": "url", "required": "gate"},
    {"key": "vk_gate_group_id", "label": "ID VK-сообщества для гейта (VK-канал)", "kind": "text", "required": False},
    {"key": "max_gate_chat_id", "label": "ID MAX-канала для гейта", "kind": "text", "required": False},

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
