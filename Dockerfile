# Root Dockerfile для Timeweb App Platform — МУЛЬТИПЛЕКС двух приложений из одного
# репозитория (общий main, без отдельной ветки). Timeweb Apps собирает из корня,
# поэтому здесь один образ, который умеет запускать И бота, И админ-панель.
# Какое из приложений запускать — выбирает run_cmd конкретного Timeweb-приложения:
#   • бот (app 201859): запускается дефолтным CMD ниже  → python bot.py
#   • панель (новый app): переопределяет команду запуска (см. admin-panel/README.md):
#         sh -c "cd /app/admin-panel && uvicorn app:app --host 0.0.0.0 --port ${PORT:-8080} ..."
#
# ВАЖНО: сборка бота сохранена 1:1 с прежним поведением (bot-telegram/ копируется
# в /app плоско, дефолтный CMD = python bot.py). Добавление панели НЕ меняет того,
# как стартует бот, и не тянет ботовых зависимостей в панель и наоборот по рантайму.
FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# --- Системные пакеты: ffmpeg для конверсии голосовых вложений оператора ---
# Бот при заливке kind='voice' (см. _drain_outbox_uploads) гонит исходник через ffmpeg
# в ogg/opus для Telegram sendVoice: браузеры дают разные контейнеры/кодеки записи с
# микрофона (Safari → mp4/aac, Chrome → webm/opus), а sendVoice требует именно
# ogg/opus. Отдельный слой ПЕРЕД Python-кодом — кэшируется и не пересобирается при
# правках бота/панели. Если ffmpeg недоступен в рантайме — бот падает в fallback
# kind='audio' и шлёт исходник как есть (sendAudio), так что слой «мягкая» зависимость.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# --- Зависимости бота (как было) ---
COPY bot-telegram/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# --- Зависимости панели (доп. слой; на бота не влияет) ---
COPY admin-panel/requirements.txt ./requirements-admin.txt
RUN pip install --no-cache-dir -r requirements-admin.txt

# --- Код бота: плоско в /app (как было) → bot.py доступен по дефолтному CMD ---
COPY bot-telegram/ ./

# --- Код панели: в подпапку /app/admin-panel → запускается своим run_cmd ---
COPY admin-panel/ ./admin-panel/

# --- shared/ (vault/money/metering, reseller-платформа): копия в ОБА корня ---
# Бот работает из /app, панель — из /app/admin-panel (run_cmd делает cd);
# двойная копия даёт `from shared import …` обоим без правки sys.path/run_cmd.
COPY shared/ ./shared/
COPY shared/ ./admin-panel/shared/

# App Platform пробросит свой $PORT; бот поднимет на нём health-эндпоинт,
# панель — uvicorn на ${PORT:-8080}.
EXPOSE 8080

# Дефолт — БОТ (поведение app 201859 не меняется). Панель переопределяет команду.
CMD ["python", "bot.py"]
