# Root Dockerfile для Timeweb App Platform.
# Timeweb Apps собирает из корня репозитория, поэтому здесь собираем
# Telegram-бота из подпапки bot-telegram/. Исходный bot-telegram/Dockerfile
# оставлен без изменений (для локального запуска / Docker Hub).
FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY bot-telegram/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot-telegram/ .

# App Platform пробросит свой $PORT; бот поднимет на нём health-эндпоинт.
EXPOSE 8080

CMD ["python", "bot.py"]
