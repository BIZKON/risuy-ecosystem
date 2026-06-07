"""Конфигурация Telegram-бота. Всё из переменных окружения — секреты не в коде."""
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
# Гарантированно доступный chat_id (админ/служебная группа). Пусто = файловые
# рассылки не материализуются (текстовые работают).
OPS_CHAT_ID_raw = os.environ.get("OPS_CHAT_ID", "")
OPS_CHAT_ID = int(OPS_CHAT_ID_raw) if OPS_CHAT_ID_raw.lstrip("-").isdigit() else (OPS_CHAT_ID_raw or None)

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
