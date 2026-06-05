# Standalone Dockerfile админ-панели — для ЛОКАЛЬНОГО запуска / отладки
# (`docker build -t lesov-panel . && docker run ...` из папки admin-panel).
#
# В проде на Timeweb панель собирается КОРНЕВЫМ Dockerfile (мультиплекс с ботом,
# общий main) — см. admin-panel/README.md. Этот файл нужен, чтобы поднять только
# панель без бота и без корневого контекста.
FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Timeweb пробросит свой $PORT; локально по умолчанию 8080. sh-форма обязательна
# для подстановки ${PORT}. proxy-headers/forwarded-allow-ips — т.к. в проде за LB
# (TLS на балансировщике, контейнер видит plain HTTP — см. README §HTTPS).
EXPOSE 8080
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-8080} --proxy-headers --forwarded-allow-ips='*' --limit-concurrency 64 --timeout-keep-alive 5 --no-access-log"]
