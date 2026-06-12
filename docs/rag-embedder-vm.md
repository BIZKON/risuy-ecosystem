# RF-RAG: эмбеддер TEI на VM (под ключ для владельца)

Self-host эмбеддер для своей базы знаний: **HuggingFace Text Embeddings Inference (TEI)**
с моделью **`intfloat/multilingual-e5-base`** (768-dim, русский). Готовый Docker-образ —
сборки нет. Данные не покидают РФ-инфру (никакого OpenAI). Бот и панель зовут его по HTTP.

> Это **платный ресурс (VM)** — поднимаешь ты. Я подготовил рецепт; боевые ключи/ресурсы
> не трогаю. Защита — Bearer-токен (у самого TEI авторизации нет), он же `EMBEDDER_TOKEN`
> в env бота и панели.

## 1. VM (Timeweb Cloud Server)

- Регион **ru-1**, Ubuntu 24.04, **2 ГБ RAM** хватает для e5-base (для запаса — 4 ГБ).
- Диск ≥ 15 ГБ (образ TEI + веса модели ~1 ГБ кэшируются в `/opt/tei-data`).
- Создание — `twc server create` с `--user-data` (SSH-ключ через cloud-init, см. скилл
  `timeweb-telegram-deploy`, Lessons A/B). IPv4 нужен (для вызова с App Platform).

## 2. Docker + зеркало реестра (на VM)

```sh
# Docker
curl -fsSL https://get.docker.com | sh
# Зеркало gcr (анонимный лимит DockerHub на Timeweb выбивается — скилл, Lesson G)
mkdir -p /etc/docker
printf '{\n  "registry-mirrors": ["https://mirror.gcr.io"]\n}\n' > /etc/docker/daemon.json
systemctl restart docker
# Фаервол: SSH + порт эмбеддера (TEI закрыт токеном через Caddy, см. ниже)
apt-get install -y ufw && ufw allow 22/tcp && ufw allow 8080/tcp && ufw --force enable
```

## 3. TEI + Caddy (Bearer-защита) через docker-compose

`/opt/rag/docker-compose.yml`:
```yaml
services:
  tei:
    image: ghcr.io/huggingface/text-embeddings-inference:cpu-1.6
    command: ["--model-id", "intfloat/multilingual-e5-base", "--auto-truncate"]
    volumes: ["/opt/tei-data:/data"]   # кэш весов модели — переживает рестарт
    restart: unless-stopped
  caddy:
    image: caddy:2
    ports: ["8080:8080"]               # наружу — только защищённый Caddy
    environment:
      EMBEDDER_TOKEN: "${EMBEDDER_TOKEN}"
    volumes: ["./Caddyfile:/etc/caddy/Caddyfile"]
    depends_on: ["tei"]
    restart: unless-stopped
```

`/opt/rag/Caddyfile` (401, если нет верного `Authorization: Bearer …`):
```
:8080 {
	@unauth not header Authorization "Bearer {$EMBEDDER_TOKEN}"
	respond @unauth 401
	reverse_proxy tei:80
}
```

Запуск (токен — в shell-переменную, не в файл/историю; начни строку с пробела):
```sh
 cd /opt/rag && EMBEDDER_TOKEN='<СГЕНЕРИРУЙ: openssl rand -hex 24>' docker compose up -d
```
> Чтобы токен переживал перезагрузку VM — положи `EMBEDDER_TOKEN=…` в `/opt/rag/.env`
> (docker compose читает его сам) и `chmod 600 .env`. В git это НЕ коммитим.

## 4. Проверка (на VM)

```sh
docker compose logs -f tei            # ждём "Ready" / "Starting HTTP server" (первая загрузка весов — пара минут)
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8080/health -H "Authorization: Bearer $EMBEDDER_TOKEN"   # 200
curl -s http://localhost:8080/embed -H "Authorization: Bearer $EMBEDDER_TOKEN" \
  -H 'content-type: application/json' -d '{"inputs":["query: привет"]}' | head -c 120   # вектор из 768 чисел
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8080/health    # 401 без токена — защита работает
```

## 5. Подключение к боту и панели (env обоих приложений)

`EMBEDDER_URL` + `EMBEDDER_TOKEN` — в env **и бота (201859), и панели (205025)**
(панель пишет векторы при ингесте, бот читает при retrieval). Через merge-safe скрипт:
```sh
bash ~/.claude/scripts/twc-set-env.sh 201859 EMBEDDER_URL=http://<vm-ip>:8080 EMBEDDER_TOKEN=<тот же токен>
bash ~/.claude/scripts/twc-set-env.sh 205025 EMBEDDER_URL=http://<vm-ip>:8080 EMBEDDER_TOKEN=<тот же токен>
```

## 6. Загрузка контента (ингест)

После применения DDL (`db/schema_kb.sql`) и поднятого эмбеддера:
```sh
python3 scripts/kb_ingest.py \
  --dsn "postgresql://gen_user:<owner-pass>@81.31.246.136:5432/risuy?sslmode=require" \
  --embedder http://<vm-ip>:8080 --token <тот же токен> \
  --title "Тарифы и услуги" --file <файл1.md> <файл2.md>
  # --role <слаг персоны>   # пусто = общая справка для всех ролей
```

## 7. Включение RAG

Панель → app_settings ключ `kb_enabled` = `1` (тумблер в «Базах знаний» — добавлю в UI).
Пока выключено — бот отвечает как раньше (RAG аддитивен, гейт в `bot-telegram/kb.py`).

---
**Себестоимость:** только VM (~500–1000 ₽/мес) против 2060 ₽/мес у managed-KB Timeweb.
Векторы — на уже оплаченном кластере 4171827 (0 ₽ доп.), эмбеддинги OpenAI не нужны.
