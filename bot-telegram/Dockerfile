FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# App Platform пробросит свой $PORT; бот поднимет на нём health-эндпоинт.
EXPOSE 8080

CMD ["python", "bot.py"]
