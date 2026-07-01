"""Псевдонимизация базы лидов для обезличенной выгрузки (Приказ РКН №140) — чистая логика, без БД.

Прямые идентификаторы → стабильный subject_code; для справочника (map) — реверс с обработкой отзыва
согласия. Свободный notes в обезличенный набор НЕ идёт (нет NER) — только boolean has_notes. CSV
formula-guard нейтрализует ведущие =,+,-,@,TAB,CR (анти-инъекция формул в Excel/LibreOffice).
"""
import hashlib

# Заголовки CSV — единый источник правды (порядок столбцов = порядок значений в *_row).
ANON_HEADER = [
    "subject_code", "messenger", "source", "consent", "subscribed", "status",
    "created_at", "updated_at", "guide_sent_at", "follow_up_1_at", "follow_up_2_at",
    "follow_up_3_at", "unsubscribed_at", "erase_requested_at", "ai_persona",
    "bot_paused", "escalated_at", "has_notes",
]
MAP_HEADER = [
    "subject_code", "name", "phone", "tg_user_id", "vk_user_id", "max_user_id",
    "max_chat_id", "web_session_id", "notes", "erase_status",
]

_CSV_FORMULA_LEAD = ("=", "+", "-", "@", "\t", "\r")
_ERASE_FLAG = "отзыв — обезличивание в процессе"


def subject_code(lead_id) -> str:
    """Стабильный псевдоним субъекта из uuid лида: 'СУБЪЕКТ-' + первые 16 hex sha256(str(lead.id)).
    Вход — канонический uuid в нижнем регистре с дефисами (как отдаёт asyncpg/psql). Необратим без map."""
    digest = hashlib.sha256(str(lead_id).encode("utf-8")).hexdigest()
    return "СУБЪЕКТ-" + digest[:16]


def csv_safe(value) -> str:
    """Formula-guard: если строка начинается с опасного символа — префиксуем апострофом. None→''."""
    s = "" if value is None else str(value)
    if s and s[0] in _CSV_FORMULA_LEAD:
        return "'" + s
    return s


def valid_persona(slug, allowed) -> str:
    """ai_persona — слаг; пускаем в выгрузку только если он в allow-list `allowed`.
    Иначе (None/произвольный текст) → пусто (БД хранит свободный text).
    `allowed` — МЕРДЖ-набор слагов (пресеты + динамический реестр персон), который вычисляет
    и передаёт async-вызывающий (anon.py — sync-модуль, БД не дёргает). Динамический slug —
    это РОЛЬ (не ПДн лида), поэтому он легитимно проходит в обезличенный набор; без мерджа
    ai_persona динамической персоны отфильтровался бы (валиден был бы только пресет).
    ⚠️ Допущение: персоны — РОЛИ (liya/mark/role-xxxx/…), не персоналии. Если появится именной
    slug (имя конкретного сотрудника) — пересмотреть, должен ли ai_persona попадать в выгрузку."""
    return slug if slug in allowed else ""


def _s(value) -> str:
    return "" if value is None else str(value)


def _iso(dt) -> str:
    return dt.isoformat() if dt is not None else ""


def _yn(v) -> str:
    return "да" if v else "нет"


def anon_row(rec, allowed_personas) -> list[str]:
    """Строка обезличенного CSV из записи стрима (whitelist-колонки + has_notes). Прямых идентификаторов
    и raw-notes в rec НЕТ — гарантируется SELECT'ом stream_leads_anon. subject_code из rec['id']."""
    return [
        subject_code(rec["id"]),
        _s(rec["messenger"]), _s(rec["source"]),
        _yn(rec["consent"]), _yn(rec["subscribed"]), _s(rec["status"]),
        _iso(rec["created_at"]), _iso(rec["updated_at"]), _iso(rec["guide_sent_at"]),
        _iso(rec["follow_up_1_at"]), _iso(rec["follow_up_2_at"]), _iso(rec["follow_up_3_at"]),
        _iso(rec["unsubscribed_at"]), _iso(rec["erase_requested_at"]),
        valid_persona(rec["ai_persona"], allowed_personas),
        _yn(rec["bot_paused"]), _iso(rec["escalated_at"]),
        _yn(rec["has_notes"]),
    ]


def map_row(rec) -> list[str]:
    """Строка справочника соответствия subject_code → прямые идентификаторы. Для лида с выставленным
    erase_requested_at ВСЕ ПДн обнуляем (обработка отзыва — не выдаём реверс субъекту, потребовавшему
    прекращения), оставляя только subject_code + флаг."""
    erased = rec["erase_requested_at"] is not None

    def keep(value) -> str:
        return "" if erased else _s(value)

    return [
        subject_code(rec["id"]),
        keep(rec["name"]), keep(rec["phone"]),
        keep(rec["tg_user_id"]), keep(rec["vk_user_id"]), keep(rec["max_user_id"]),
        keep(rec["max_chat_id"]), keep(rec["web_session_id"]),
        keep(rec["notes"]),
        _ERASE_FLAG if erased else "",
    ]
