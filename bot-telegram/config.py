"""Конфигурация Telegram-бота. Всё из переменных окружения — секреты не в коде."""
import logging
import os


def _req(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(f"Не задана обязательная переменная окружения: {name}")
    return val


BOT_TOKEN = _req("BOT_TOKEN")
DATABASE_URL = _req("DATABASE_URL")

# Канал для гейта подписки. Бот ДОЛЖЕН быть админом канала, иначе проверка не сработает.
# Надёжнее числовой id вида -100xxxxxxxxxx (работает и для приватных каналов), чем @username.
# Числовую строку приводим к int, чтобы get_chat_member получил корректный chat_id.
_channel_id_raw = _req("CHANNEL_ID")
CHANNEL_ID = int(_channel_id_raw) if _channel_id_raw.lstrip("-").isdigit() else _channel_id_raw
# Ссылка для подписки, которую видит пользователь, напр. https://t.me/risuysdushoy
CHANNEL_URL = _req("CHANNEL_URL")

# Ссылка на гайд (GetCourse) — то, что бот отдаёт после прохождения воронки.
GUIDE_URL = _req("GUIDE_URL")

# Ссылка на политику обработки ПДн (необязательно, но желательно для 152-ФЗ).
PRIVACY_URL = os.environ.get("PRIVACY_URL", "")

# file_id видео-кружка Насти (необязательно). Записывается заранее, см. README.
VIDEO_NOTE_FILE_ID = os.environ.get("VIDEO_NOTE_FILE_ID", "")

# Прокси для Telegram API: нужен, если api.telegram.org недоступен напрямую
# из дата-центра (РФ-хостинг часто не достукивается до Telegram).
# Формат: socks5://user:pass@host:port или http://user:pass@host:port.
# Пусто = подключаться к Telegram напрямую (поведение по умолчанию).
TELEGRAM_PROXY = os.environ.get("TELEGRAM_PROXY", "")

# AI-ассистент Лия — агент Timeweb Cloud (cloud-ai). Без этих значений AI-ответы
# отключены (бот отдаёт мягкий фолбэк). Только из env, в репозиторий не коммитим.
AGENT_ID = os.environ.get("AGENT_ID", "")
TIMEWEB_AI_TOKEN = os.environ.get("TIMEWEB_AI_TOKEN", "")

# Порт для health-эндпоинта (Timeweb App Platform проксирует сюда). Бот работает на long-polling.
PORT = int(os.environ.get("PORT", "8080"))

# Тайминги прогрева в секундах от момента выдачи гайда.
FOLLOW_UP_DELAYS = [
    int(os.environ.get("FOLLOW_UP_1_DELAY", str(60 * 60))),          # +1 час
    int(os.environ.get("FOLLOW_UP_2_DELAY", str(60 * 60 * 24))),     # +1 день
    int(os.environ.get("FOLLOW_UP_3_DELAY", str(60 * 60 * 24 * 3))), # +3 дня
]

# ── Очередь и рассылки (worker.run) ──────────────────────────────────────────
# Единый process-wide rate-limit на ВЕСЬ исходящий трафик одного Bot (воронка,
# Лия, прогрев, outbox, рассылки) — общий лимит Telegram ~30/с. Берём 15/с с
# headroom под интерактив; рассылка уступает приоритет интерактиву (§5.3 плана).
BROADCAST_RATE = float(os.environ.get("BROADCAST_RATE", "15"))   # msg/s, token-bucket
BROADCAST_BATCH = int(os.environ.get("BROADCAST_BATCH", "50"))   # claim-батч получателей за тик
OUTBOX_BATCH = int(os.environ.get("OUTBOX_BATCH", "20"))         # claim-батч точечных ответов за тик
MAX_SEND_ATTEMPTS = int(os.environ.get("MAX_SEND_ATTEMPTS", "3"))   # потолок попыток на получателя рассылки
OUTBOX_MAX_ATTEMPTS = int(os.environ.get("OUTBOX_MAX_ATTEMPTS", "10"))  # потолок попыток на запись outbox
OUTBOX_MAX_AGE_HOURS = int(os.environ.get("OUTBOX_MAX_AGE_HOURS", "24"))  # старше — в failed (не висеть вечно)
# Reclaim застрявших 'sending' (краш/редеплой). 10 мин > rolling-overlap Timeweb (§5.5).
RECLAIM_AFTER_SECONDS = int(os.environ.get("RECLAIM_AFTER_SECONDS", str(60 * 10)))
WORKER_INTERVAL = int(os.environ.get("WORKER_INTERVAL", "5"))     # период цикла воркера, сек
# Circuit-breaker молодого бота: при доле failed среди первых N > порога — авто-пауза (§5.9).
CB_MIN_SAMPLE = int(os.environ.get("CB_MIN_SAMPLE", "20"))       # сколько отправок накопить перед оценкой
CB_FAIL_RATIO = float(os.environ.get("CB_FAIL_RATIO", "0.30"))   # доля failed для авто-паузы

# Служебный чат для первичной заливки файла рассылки (НЕ первый получатель, §5.6).
# Тот же чат используется для первичной заливки файла ПРОДУКТА (каталог оферов):
# бот заливает products.file ОДИН раз сюда → products.file_tg_id, дальше переиспользует.
# Гарантированно доступный chat_id (админ/служебная группа). Пусто = файловые
# рассылки/продукты не материализуются (текстовые/ссылочные работают).
OPS_CHAT_ID_raw = os.environ.get("OPS_CHAT_ID", "")
OPS_CHAT_ID = int(OPS_CHAT_ID_raw) if OPS_CHAT_ID_raw.lstrip("-").isdigit() else (OPS_CHAT_ID_raw or None)


# ── ГРАНИЦА РАССЫЛОК: канал гейта НИКОГДА не получает контент рассылок ─────────
# Канал (@lesov_art_school) бот использует ТОЛЬКО для проверки подписки нового
# человека — это даёт нативный рост канала. Любые рассылки (текст/ссылка/файл/
# продукт) идут ИСКЛЮЧИТЕЛЬНО в личные чаты лидов:
#   • адресаты материализуются строго `select … from leads` (db._AUDIENCE_WHERE) —
#     канал в leads не лежит, личный tg_user_id > 0, id канала = -100… → коллизия
#     адресата невозможна структурно;
#   • единственная точка, где бот шлёт контент в НЕ-лид-чат, — первичная заливка
#     файла в OPS_CHAT_ID ради переиспользуемого file_id. Если оператор по ошибке
#     укажет туда канал гейта, файл засветился бы в канале. Запрещаем инвариантом:
#     при совпадении OPS_CHAT_ID с каналом (по числовому id ИЛИ @username) —
#     файловую заливку ОТКЛЮЧАЕМ (OPS_CHAT_ID=None) и громко логируем. Текстовые/
#     ссылочные рассылки при этом продолжают работать. OPS_CHAT_ID может быть
#     личным чатом оператора с ботом ИЛИ приватной группой — но не каналом гейта.
def _channel_aliases(channel_id, channel_url) -> set:
    """Все идентификаторы канала гейта: числовой id и @username (нижний регистр)."""
    aliases: set = set()
    if isinstance(channel_id, int):
        aliases.add(channel_id)
    elif isinstance(channel_id, str) and channel_id:
        aliases.add(channel_id.lstrip("@").lower())
    if channel_url and "t.me/" in channel_url:
        uname = channel_url.rstrip("/").rsplit("/", 1)[-1].lstrip("@").lower()
        if uname:
            aliases.add(uname)
    return aliases


def is_gate_channel(chat, channel_id=None, channel_url=None) -> bool:
    """True, если chat указывает на канал гейта (по числовому id или @username).

    Граница рассылок: канал гейта не может быть адресатом/служебным чатом рассылок.
    channel_id/channel_url по умолчанию берутся из модуля (передаются явно в тестах).
    """
    if chat is None:
        return False
    cid = CHANNEL_ID if channel_id is None else channel_id
    curl = CHANNEL_URL if channel_url is None else channel_url
    aliases = _channel_aliases(cid, curl)
    if isinstance(chat, int):
        return chat in aliases
    return chat.lstrip("@").lower() in aliases


if OPS_CHAT_ID is not None and is_gate_channel(OPS_CHAT_ID):
    logging.getLogger(__name__).error(
        "OPS_CHAT_ID совпадает с каналом гейта — файловая заливка ОТКЛЮЧЕНА: "
        "рассылки не должны светиться в канале (он только для проверки подписки). "
        "Укажи ЛИЧНЫЙ chat_id оператора с ботом или ПРИВАТНУЮ группу."
    )
    OPS_CHAT_ID = None

# ── Каталог продуктов (оферов) ────────────────────────────────────────────────
# Лимит файла продукта = жёсткий потолок Telegram Bot API на отправку (50 МБ).
# Панель валидирует размер/тип файла ДО записи байтов в products.file (расширение+
# MIME+magic-byte, отказ исполняемым) — здесь дублируем верхнюю границу как защиту
# на стороне бота перед заливкой в OPS_CHAT_ID (битый/слишком большой файл не валит
# воркер, продукт просто не получает file_tg_id и логируется). Меняется редко.
MAX_PRODUCT_FILE_MB = int(os.environ.get("MAX_PRODUCT_FILE_MB", "50"))
MAX_PRODUCT_FILE_BYTES = MAX_PRODUCT_FILE_MB * 1024 * 1024
# Сколько продуктов-офферов заливать за один тик воркера (обычно их единицы — каталог
# мал, заливка однократна; батч держим маленьким, чтобы не занимать bucket надолго).
PRODUCT_UPLOAD_BATCH = int(os.environ.get("PRODUCT_UPLOAD_BATCH", "5"))
# Потолок попыток заливки файла офера (симметрично OUTBOX_MAX_ATTEMPTS/MAX_SEND_ATTEMPTS):
# валидный-по-magic, но отвергаемый Telegram файл не должен переселектироваться вечно
# каждым тиком (5с), тратя токен бакета и засоряя OPS_CHAT_ID. После N неудач офер
# выпадает из очереди заливки (products.upload_attempts >= лимит), file_tg_id остаётся
# null → рассылка-продукт с таким файлом уйдёт на паузу (см. worker._prepare_product_broadcast).
PRODUCT_UPLOAD_MAX_ATTEMPTS = int(os.environ.get("PRODUCT_UPLOAD_MAX_ATTEMPTS", "5"))

# Публичный базовый URL БОТА (его поддомен на Timeweb) — на нём живёт трекинг-редирект
# /r/<token>. Ссылка {link} в рассылке строится как BOT_PUBLIC_BASE_URL + /r/<click_token>.
# Пусто = плейсхолдер {link} не подставляется (рассылка без трекинга всё равно идёт).
BOT_PUBLIC_BASE_URL = os.environ.get("BOT_PUBLIC_BASE_URL", "").rstrip("/")

# TTL абсолютной чистки текста переписки (самый объёмный ПДн-поток), дни. §6.4.
MESSAGES_TTL_DAYS = int(os.environ.get("MESSAGES_TTL_DAYS", "90"))
# Срок обезличивания по отзыву согласия (erase_requested_at + N дней). Совпадает с панелью/privacy.
ERASE_AFTER_DAYS = int(os.environ.get("ERASE_AFTER_DAYS", "30"))
# Период цикла retention-обезличивания, сек (раз в час достаточно).
RETENTION_INTERVAL = int(os.environ.get("RETENTION_INTERVAL", str(60 * 60)))
