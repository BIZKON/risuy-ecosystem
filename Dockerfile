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

# App Platform пробросит свой $PORT; бот поднимет на нём health-эндпоинт,
# панель — uvicorn на ${PORT:-8080}.
EXPOSE 8080

# Дефолт — БОТ (поведение app 201859 не меняется). Панель переопределяет команду.
CMD ["python", "bot.py"]
