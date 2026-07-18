# Base-образ движковых сервисов risuy. Отдельно от мультиплекс-образа бот+панель.
FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY engine/common/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY engine/ ./engine/
# CMD задаёт каждый сервис в docker-compose (command:).
