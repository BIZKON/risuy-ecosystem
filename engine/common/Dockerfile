# Base-образ движковых сервисов risuy. Отдельно от мультиплекс-образа бот+панель.
FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY engine/common/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
# Коллекторный слой (тяжёлый MTProto-стек Telethon) — отдельным слоем ПОСЛЕ common.
# ВНИМАНИЕ ([critic-fix M3]): кэш Docker инвалидируется СВЕРХУ ВНИЗ — правка
# engine/common/requirements.txt (слой выше) КАСКАДНО сбрасывает и этот Telethon-слой
# (переустановка ~минуты). Приемлемо P1 (один образ на движок — решение S0M). Хочешь
# развязать — вынести Telethon ВЫШЕ волатильного common ИЛИ разнести per-service (S7M).
# cryptg НЕ ставим: C-extension тянет build-essential в slim; Telethon без него
# работает, лишь медленнее крипто — приемлемо для сбора (ускорение крипто — S7M).
COPY engine/collectors/requirements.txt ./collectors-requirements.txt
RUN pip install --no-cache-dir -r collectors-requirements.txt
# shared/ — реюз vault (from shared import vault) в движковом образе, как у бота.
COPY shared/ ./shared/
COPY engine/ ./engine/
# CMD задаёт каждый сервис в docker-compose (command:).
