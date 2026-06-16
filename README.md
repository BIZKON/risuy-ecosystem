# Рисуй с душой — лид-магнит экосистема

Воронка-«Приёмная»: со всех площадок человек попадает в бота, оставляет имя и телефон,
подписывается на канал и получает бесплатный гайд. Дальше — мягкий прогрев. Все заявки —
в одной базе, Настя смотрит их в панели.

**Всё на Timeweb (РФ), без сторонних конструкторов.**

> ⚠️ **Этот README описывает ПЕРВУЮ итерацию (TG-бот + лиды).** Проект с тех пор вырос в
> reseller-платформу: реализованы и в проде — **админ-панель** (`admin-panel/`, Python + Jinja),
> **мультитенантность + RLS**, **каналы VK/MAX** (разговор Лии, триггеры, эскалация, продажи,
> «Диалоги»-композер, рассылки), **биллинг ЮKassa**, **база знаний RAG**. Схема — НЕ один
> `db/schema.sql`, а ~34 файла `schema_*`/`migrate_*` в `db/`. Актуальные детали: `admin-panel/README.md`
> и `docs/`. Разделы ниже сохранены как история старта.

## Стек

| Слой | Чем делаем | Где живёт |
|------|------------|-----------|
| Бот Telegram | Python + aiogram 3 | App Platform (Docker) |
| Каналы VK/MAX | Python (raw VK/MAX Bot API) в том же боте (multiplex) | App Platform ✅ |
| База | PostgreSQL (мультитенант + RLS) | Timeweb Managed PostgreSQL |
| Панель | Python + Jinja (Starlette/FastAPI-стиль), серверный рендер | App Platform ✅ |
| Платежи | ЮKassa (касса школы + касса тенанта) | в панели/боте ✅ |
| AI-ассистент Лия | AI Gateway (RU-модель) + база знаний RAG | ✅ |

## Структура

```
risuy-ecosystem/
├── db/                       # ~34 файла: schema_* (admin/billing/metering/orders/products/
│   │                         #   team/tenancy/vault/kb/persona/service) + migrate_* + RLS
│   ├── schema.sql            # базовая leads (НЕ вся БД — см. остальные schema_*/migrate_*)
│   └── panel_role.sql        # least-privilege роль panel_rw
├── bot-telegram/             # бот: воронка + Лия + multiplex (TG/VK/MAX) + worker (outbox/рассылки)
│   ├── bot.py · config.py · handlers.py · nurture.py · db.py · texts.py
│   ├── multiplex.py          # тенант-боты + каналы VK/MAX (vk_driver.py / max_driver.py)
│   ├── worker.py · messaging.py · ai.py · triggers.py · escalation.py · yookassa.py
├── admin-panel/              # Python + Jinja: лиды/диалоги/рассылки/продукты/биллинг/мультитенант
│   ├── app.py · db.py · auth.py · security.py · yookassa.py · templates/ · static/
├── shared/                   # код, общий боту И панели (копируется в оба образа): metering/money/vault
├── landing/ · service-site/  # лендинги (static)
├── scripts/                  # смоуки (*_smoke.py)
├── docs/                     # стратегия, дизайн-доки (layer-c-vk-max-channels.md), handoffs
├── .gitignore
└── .pre-commit-config.yaml   # gitleaks — чтобы не утёк токен
```

## Статус сборки

- [x] Фундамент: схема БД + структура репо
- [x] Telegram-бот: имя → телефон → согласие → гейт подписки → выдача гайда → прогрев
- [x] Панель (Python + Jinja): лиды, диалоги, рассылки, продукты, биллинг, мультитенант + RLS
- [x] Каналы VK/MAX: разговор Лии, триггеры, эскалация, продажи, «Диалоги»-композер, рассылки
- [x] Платежи ЮKassa (касса школы + касса тенанта), база знаний RAG
- [ ] Отдельный FastAPI-API для внешних потребителей — по необходимости

---

## Запуск Telegram-бота на Timeweb

### 1. База — Managed PostgreSQL
1. В панели Timeweb создай кластер **Managed PostgreSQL** (регион РФ).
2. Возьми строку подключения → это `DATABASE_URL`.
3. Примени схему. ⚠️ `db/schema.sql` поднимает ТОЛЬКО базовую `leads` — рабочая БД собирается из
   ~34 файлов `db/schema_*`/`migrate_*` (admin, billing, metering, orders, products, team, tenancy,
   vault, kb, RLS-политики и миграции) в правильном порядке (база → schema_* → migrate_* → RLS).
   Применяются owner-DSN через `~/.claude/scripts/twc-migrate.sh` (СНАЧАЛА risuy_dev, потом прод).
   Гранты роли панели — `db/panel_role.sql`. Детали — `admin-panel/README.md`.

### 2. Бот и канал
1. Создай бота у **@BotFather** → `BOT_TOKEN`.
2. Создай канал «Рисуй с душой» (на него будем проверять подписку).
3. **Добавь бота администратором канала** — без прав админа проверка подписки не работает.
4. `CHANNEL_ID` — `@username` публичного канала (или `-100…` для приватного), `CHANNEL_URL` — ссылка вида `https://t.me/...`.

### 3. Деплой на App Platform
1. App Platform → создать приложение → деплой из этого репозитория (папка `bot-telegram/`, тип — Dockerfile) или из Docker Hub.
2. Пропиши переменные окружения (см. `bot-telegram/.env.example`): `BOT_TOKEN`, `DATABASE_URL`, `CHANNEL_ID`, `CHANNEL_URL`, `GUIDE_URL`, при желании `PRIVACY_URL`, `VIDEO_NOTE_FILE_ID`.
3. Запусти деплой. Бот работает на long-polling; health-эндпоинт на `$PORT` нужен только чтобы App Platform видел живой контейнер.

### Локальный запуск (для проверки)
```bash
cd bot-telegram
cp .env.example .env          # заполни значения
pip install -r requirements.txt
export $(grep -v '^#' .env | xargs)
python bot.py
```

---

## Ссылки на площадки (метка источника)

Бот сам метит, откуда пришёл человек, через deep-link:
```
https://t.me/ИМЯ_БОТА?start=reels
https://t.me/ИМЯ_БОТА?start=dzen
https://t.me/ИМЯ_БОТА?start=youtube
https://t.me/ИМЯ_БОТА?start=vk
```
Метка падает в поле `source` — по ней панель покажет, какая площадка реально приносит заявки.

## Видео-кружок Насти (необязательно)
Чтобы бот слал личный видео-кружок перед гайдом: отправь кружок самому боту (в коде на минуту добавь хендлер-логгер `message.video_note.file_id`), скопируй `file_id` в `VIDEO_NOTE_FILE_ID`. Либо пропусти — без него бот просто отдаёт текст и ссылку.

## Заметки
- **152-ФЗ:** ПДн (имя, телефон) хранятся только в Managed PostgreSQL в РФ. Согласие бот берёт до сбора данных.
- **MAX/VK:** ✅ реализованы в ТОМ ЖЕ боте (мультиплекс каналов), пишут в `leads` по `messenger`/
  `max_user_id`/`vk_user_id`. MAX требует верифицированной организации в business.max.ru. Живой тест
  с боевыми токенами — за владельцем (см. handoff).
- **FSM-состояние** хранится в памяти процесса (MemoryStorage) — при рестарте незавершённый диалог начнётся заново через `/start`; сами лиды в БД не теряются. Под высокую нагрузку позже можно переключить на Redis.
