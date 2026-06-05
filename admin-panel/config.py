"""Конфигурация админ-панели лидов. Всё из переменных окружения — секреты не в коде.

Зеркалит паттерн bot-telegram/config.py: один helper _req() + fail-fast.
Дополнительно — реальные guard'ы на формат секретов (§3.14 плана), а не только
их наличие: панель должна падать на старте, если ADMIN_PASSWORD_HASH не argon2-PHC
или SESSION_SECRET слишком короткий. Это единственная защита от «забыли задать env».
"""
import os


def _req(name: str) -> str:
    """Обязательная переменная: пусто/отсутствует → падаем на старте (как в боте)."""
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(f"Не задана обязательная переменная окружения: {name}")
    return val


def _opt_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as e:
        raise RuntimeError(f"Переменная {name} должна быть целым числом, получено: {raw!r}") from e


def _opt_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# --- DSN роли panel_rw (НЕ owner-DSN бота). Тот же кластер Managed PG, ru-1. ---
DATABASE_URL = _req("DATABASE_URL")

# --- Секреты ---
# Подпись session-cookie (tamper-evidence для opaque sid). Минимум 32 байта.
SESSION_SECRET = _req("SESSION_SECRET")
if len(SESSION_SECRET) < 32:
    raise RuntimeError(
        "SESSION_SECRET слишком короткий: нужно ≥32 символов "
        f"(сейчас {len(SESSION_SECRET)}). Сгенерируйте: python -c \"import secrets;print(secrets.token_urlsafe(48))\""
    )

# Логин единственного оператора (не секрет, но обязателен; сравнивается constant-time).
ADMIN_USERNAME = _req("ADMIN_USERNAME")

# Argon2id PHC-строка (хеш, НЕ пароль). Генерится офлайн, в репозиторий не коммитим.
ADMIN_PASSWORD_HASH = _req("ADMIN_PASSWORD_HASH")
if not ADMIN_PASSWORD_HASH.startswith("$argon2"):
    raise RuntimeError(
        "ADMIN_PASSWORD_HASH должен быть argon2-PHC строкой ($argon2id$v=19$...). "
        "Похоже, передан plain-пароль. Сгенерируйте хеш офлайн через argon2-cffi."
    )

# --- Опциональная сеть оператора: advisory bypass per-IP троттла (§3.4). ---
# IP легко спуфится за LB → это удобство, не security-контроль.
LOGIN_ALLOWLIST_CIDR = os.environ.get("LOGIN_ALLOWLIST_CIDR", "")

# --- Параметры сессий (§3.2) ---
SESSION_IDLE_MIN = _opt_int("SESSION_IDLE_MIN", 30)     # скользящий idle-таймаут, минуты
SESSION_MAX_HOURS = _opt_int("SESSION_MAX_HOURS", 8)    # жёсткий потолок жизни сессии, часы

# --- Cookie ---
# Безусловно Secure по умолчанию: браузер↔LB всегда HTTPS на *.twc1.net (§3.3/§4.6),
# даже если контейнер видит plain HTTP. Снять (=0) можно только для локальной отладки по http.
COOKIE_SECURE = _opt_bool("COOKIE_SECURE", True)

# __Host- префикс требует Secure+Path=/+без Domain. Если Secure отключён (локальная
# отладка по http), браузер отвергнет __Host- cookie → используем обычное имя.
COOKIE_NAME = "__Host-session" if COOKIE_SECURE else "session"

# --- Порт health/uvicorn (Timeweb App Platform пробрасывает свой $PORT). ---
PORT = _opt_int("PORT", 8080)

# --- Лимиты (§3.13) ---
MAX_BODY_BYTES = _opt_int("MAX_BODY_BYTES", 64 * 1024)   # тело запроса > 64 KB → 413
NOTES_MAX_LEN = 4000                                     # обрезка notes первым действием хендлера
EXPORT_ROW_CAP = _opt_int("EXPORT_ROW_CAP", 50_000)      # hard row-cap на CSV-экспорт
PER_PAGE = 50                                            # пагинация списка (объёмы малы)

# --- 152-ФЗ (из landing/privacy.html §6.5, landing/consent.html §6) ---
ERASE_AFTER_DAYS = _opt_int("ERASE_AFTER_DAYS", 30)      # срок обезличивания после отзыва согласия

# --- Справочники (defence-in-depth; значения совпадают со схемой/ботом) ---
# status валидируется против STATUSES в хендлере UPDATE (§2 плана).
STATUSES = ("new", "guide_sent", "nurturing", "converted", "lost")
SOURCES = ("reels", "dzen", "youtube", "vk", "max", "other")
MESSENGERS = ("tg", "max")

# Человекочитаемые подписи для UI (бейджи, фильтры, дашборд).
STATUS_LABELS = {
    "new": "Новый",
    "guide_sent": "Гайд выдан",
    "nurturing": "Прогрев",
    "converted": "Купил",
    "lost": "Потерян",
}
SOURCE_LABELS = {
    "reels": "Reels",
    "dzen": "Дзен",
    "youtube": "YouTube",
    "vk": "VK",
    "max": "MAX",
    "other": "Другое",
}
MESSENGER_LABELS = {"tg": "Telegram", "max": "MAX"}
