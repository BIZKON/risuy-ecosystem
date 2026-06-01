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
# Можно @username (для публичного канала) или числовой id вида -100xxxxxxxxxx.
CHANNEL_ID = _req("CHANNEL_ID")
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
