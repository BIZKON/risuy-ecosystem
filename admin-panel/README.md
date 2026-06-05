# Админ-панель лидов «Школа Лесова»

Внутренняя панель для оператора ПДн (ИП Иванов И. В.): воронка, карточки лидов,
правка `status`/`notes`, раскрытие телефона под аудитом, CSV-экспорт и операции
152-ФЗ (отзыв согласия → обезличивание ≤30 дней). FastAPI + Jinja2 + asyncpg,
server-rendered, без внешних CDN/шрифтов/JS. deny-by-default: каждый маршрут с ПДн
закрыт серверной сессией.

Полная спека и обоснование решений — `/tmp/admin-panel-plan.md` (если сохранена)
и комментарии в исходниках. Юр-привязка (оператор, 30 дней) — `landing/privacy.html`
§6.3–6.5, `landing/consent.html` §6.

---

## Архитектура деплоя: ОБЩИЙ `main`, мультиплекс одного образа

Панель — **отдельное Timeweb App-Platform приложение** (свой id, свой поддомен
`*.twc1.net`), но собирается из **того же репозитория и той же ветки `main`**, что и
бот (app `201859`). Backend App-Platform не поддерживает подкаталог сборки, поэтому
**корневой `/Dockerfile` — мультиплекс**: один образ умеет запускать и бота, и панель.

- Бот (app 201859) запускается **дефолтным** `CMD` корневого Dockerfile → `python bot.py`.
  Его сборка сохранена 1:1 (код `bot-telegram/` копируется в `/app` плоско). Панель
  бота **не ломает** — добавлены только доп. слой `pip install` и `COPY admin-panel/`.
- Панель (новый app) **переопределяет команду запуска** (`run_cmd`), указывая uvicorn
  из подпапки `/app/admin-panel`.

> Оба приложения авто-деплоятся на push в `main`. Это значит, что push дёргает
> редеплой **обоих** контейнеров. У бота это краткий рестарт long-polling (возможен
> мимолётный `TelegramConflictError` на пересменке — он сам проходит). Если такое
> дёрганье нежелательно — это единственный минус общего `main` против отдельной
> ветки; см. открытое решение §1 плана.

### `run_cmd` приложения-панели (вставить в настройки Timeweb App)

```
sh -c "cd /app/admin-panel && uvicorn app:app --host 0.0.0.0 --port ${PORT:-8080} --proxy-headers --forwarded-allow-ips='*' --limit-concurrency 64 --timeout-keep-alive 5 --no-access-log"
```

- `sh -c` обязателен — иначе `${PORT}` не подставится.
- `cd /app/admin-panel` — там лежат `app.py`, `templates/`, `static/` (рабочая
  директория должна быть этой, иначе Jinja2/StaticFiles не найдут `templates`/`static`).
- `--proxy-headers --forwarded-allow-ips='*'` — TLS терминируется на балансировщике
  Timeweb, контейнер видит plain HTTP (см. «HTTPS за LB» ниже).
- `--no-access-log` — не писать query-strings в логи (не светить ПДн/фильтры).

---

## Переменные окружения

Задаются **только** через Timeweb API/панель (`PATCH /apps/<PANEL_ID> {envs}`), не в git.
`.env*` в корневом `.gitignore`. Старт панели **падает** (fail-fast, `config.py`), если
обязательная переменная пуста или секрет неверного формата.

| ENV | Обяз. | Назначение |
|---|---|---|
| `DATABASE_URL` | да | DSN роли **`panel_rw`** (НЕ owner-DSN бота). Тот же кластер Managed PG, ru-1. Хост/порт/БД — как у бота, `user=panel_rw`. |
| `SESSION_SECRET` | да | Подпись session-cookie (itsdangerous). **≥32 символов.** `python -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `ADMIN_USERNAME` | да | Логин оператора. |
| `ADMIN_PASSWORD_HASH` | да | argon2id **PHC-строка** (`$argon2id$v=19$...`), не пароль. Как сгенерить — ниже. |
| `SESSION_IDLE_MIN` | нет | Скользящий idle-таймаут сессии, мин. По умолчанию `30`. |
| `SESSION_MAX_HOURS` | нет | Жёсткий потолок жизни сессии, ч. По умолчанию `8`. |
| `COOKIE_SECURE` | нет | `1`/`0`. По умолчанию `1` (прод за HTTPS-LB). Ставить `0` ТОЛЬКО для локального HTTP-теста. |
| `LOGIN_ALLOWLIST_CIDR` | нет | Опц. CIDR сети оператора — advisory bypass троттла логина (IP спуфится → удобство, не контроль). |
| `PORT` | нет | Timeweb пробрасывает свой. Локально по умолчанию `8080`. |
| `ERASE_AFTER_DAYS` | нет | Срок обезличивания после отзыва согласия. По умолчанию `30` (152-ФЗ). |

Залить все секреты одним PATCH (батч = один редеплой). В выводе CLI/логах — маскировать.

---

## Разовая подготовка БД (owner-DSN, один раз)

Панель ходит под ролью `panel_rw` с минимальными правами. Перед первым запуском —
применить миграцию схемы и создать роль **owner-DSN** (роль-владелец БД; у `panel_rw`
прав на DDL/CREATE ROLE нет). Данные не покидают РФ — тот же кластер.

```bash
# 1) Таблицы панели (admin_sessions/admin_login_throttle/admin_audit) + колонка
#    leads.erase_requested_at. Идемпотентно (IF NOT EXISTS).
psql "$OWNER_DATABASE_URL" -f db/schema_admin.sql

# 2) Роль panel_rw + гранты least-privilege (SELECT leads, UPDATE только
#    status/notes/erase_requested_at, INSERT-only admin_audit, CRUD сессии/троттл).
#    CREATE ROLE идемпотентен (guard в файле); пароль роли НЕ в файле — задаётся отдельно.
psql "$OWNER_DATABASE_URL" -f db/panel_role.sql

# 3) Выдать роли пароль БЕЗ следа в shell-истории и журнале запросов.
#    Запустить psql интерактивно и ввести вручную (строку начать с ПРОБЕЛА):
#      \set HISTCONTROL ignorespace
#       ALTER ROLE panel_rw PASSWORD 'СГЕНЕРИРОВАННЫЙ_ПАРОЛЬ';
#    Пароль сгенерить офлайн:  python -c "import secrets; print(secrets.token_urlsafe(32))"
```

DSN панели для `DATABASE_URL` (тот же host/port/db, что у бота, но `panel_rw`):

```
postgresql://panel_rw:СГЕНЕРИРОВАННЫЙ_ПАРОЛЬ@<host>:<port>/<db>?sslmode=verify-full
```

Хост/порт/БД и owner-DSN — снять live из бота:
`GET /apps/201859 → envs.DATABASE_URL` (см. «Снять provider/repo/DSN бота» ниже).

---

## Генерация argon2-хеша пароля (офлайн, не в репозиторий)

Хеш генерится локально и кладётся в env `ADMIN_PASSWORD_HASH`. Сам пароль нигде не
хранится. Параметры ≥ OWASP (совпадают с `auth.py`: `time_cost=3, memory_cost=65536,
parallelism=4`).

```bash
pip install argon2-cffi
python -c "import getpass; from argon2 import PasswordHasher; \
print(PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4, hash_len=32, salt_len=16) \
.hash(getpass.getpass('Пароль оператора: ')))"
```

Вывод вида `$argon2id$v=19$m=65536,t=3,p=4$...` — это и есть значение
`ADMIN_PASSWORD_HASH`. `getpass` не печатает пароль на экран и не оставляет его в
истории шелла. Ротация пароля = заново сгенерить хеш → обновить env → redeploy.

---

## Создание Timeweb-приложения панели (twc / API)

Параметры (зафиксированы планом §4.5):

| Поле | Значение |
|---|---|
| `type` | `backend` |
| `framework` | `docker` |
| `preset_id` | **1003** (~510 ₽/мес, ru-1 СПб) |
| `project_id` | **2077791** |
| `provider_id` | **снять live** у бота (НЕ выдумывать) |
| `repository_id` | **снять live** у бота (тот же репозиторий) |
| `branch_name` | `main` (общий с ботом) |
| `is_auto_deploy` | `true` |
| `run_cmd` | см. блок «run_cmd» выше |
| `name` / `comment` | **без символов `+` `/` `:`** — валидатор Timeweb бьёт 400 |

### 1) Снять provider/repo/DSN бота (live, не выдумывать)

```bash
# provider_id, repository_id, branch, текущий DATABASE_URL (host/пароль кластера):
twc api get /apps/201859        # или: curl -H "Authorization: Bearer $TWC_TOKEN" https://api.timeweb.cloud/api/v1/apps/201859
# забрать: repository.provider_id, repository.id (repository_id), envs.DATABASE_URL
```

### 2) Создать приложение

Через `twc apps create` (или `POST /apps`), подставив снятые `provider_id`/
`repository_id`. Имя/коммент — только буквы/цифры/пробел/дефис (**без `+` `/` `:`**):

```bash
twc apps create \
  --name "lesov-leads-panel" \
  --type backend \
  --framework docker \
  --preset-id 1003 \
  --project-id 2077791 \
  --provider-id <PROVIDER_ID_БОТА> \
  --repository-id <REPOSITORY_ID_БОТА> \
  --branch main \
  --is-auto-deploy true
```

Затем **отдельно** задать `run_cmd` (так надёжнее, чем экранировать вложенные кавычки
в одной строке `apps create`). Удобнее всего вставить в UI приложения поле «Команда
запуска», либо через PATCH (значение — ровно блок из раздела «run_cmd» выше):

```bash
twc api patch /apps/<PANEL_ID> --data @run_cmd.json
# где run_cmd.json:
# { "run_cmd": "sh -c \"cd /app/admin-panel && uvicorn app:app --host 0.0.0.0 --port ${PORT:-8080} --proxy-headers --forwarded-allow-ips='*' --limit-concurrency 64 --timeout-keep-alive 5 --no-access-log\"" }
```

> Точные имена флагов/полей CLI могут отличаться — сверяться с `twc apps create --help`
> и схемой `POST /apps`. Передача `run_cmd` файлом (`--data @run_cmd.json`) снимает
> проблему вложенных одинарных/двойных кавычек в shell.

### 3) Залить env одним PATCH

```bash
twc api patch /apps/<PANEL_ID> --data '{"envs": {
  "DATABASE_URL": "postgresql://panel_rw:...@<host>:<port>/<db>?sslmode=verify-full",
  "SESSION_SECRET": "<token_urlsafe(48)>",
  "ADMIN_USERNAME": "<логин>",
  "ADMIN_PASSWORD_HASH": "$argon2id$v=19$...",
  "COOKIE_SECURE": "1"
}}'
```

### 4) Проверить деплой (acceptance, §4.10 плана)

```bash
twc api get /apps/<PANEL_ID>           # status: deploy → active
twc api get /apps/<PANEL_ID>/deploys   # последний → success
twc api get /apps/<PANEL_ID>/logs      # "Uvicorn running on ..."
# забрать поддомен: GET /apps/<PANEL_ID> → domains[]
PANEL=https://<поддомен>.twc1.net
curl -s $PANEL/healthz                  # {"status":"ok"}
curl -s -o /dev/null -w '%{http_code}\n' $PANEL/        # 303 (на /login) — ПДн закрыты
curl -s -o /dev/null -w '%{http_code}\n' $PANEL/leads/not-a-uuid   # 404/422, НЕ 500
```

Сквозная проверка правки (под owner/panel-DSN):
`psql "$DATABASE_URL" -c "select status, notes, updated_at from leads order by updated_at desc limit 3;"`

---

## HTTPS за балансировщиком Timeweb

TLS терминируется на LB; контейнер говорит plain HTTP.
- **НЕ** добавлять http→https редирект в приложении — за LB это бесконечный цикл.
  HTTPS форсируется на edge (настройка приложения Timeweb).
- Cookie `Secure` — **безусловно** (браузер↔LB всегда HTTPS на `*.twc1.net`). `__Host-`
  префикс зависит только от Secure+Path=/+без Domain (это мы и ставим), не от схемы,
  которую видит контейнер.
- `X-Forwarded-For`/`-Proto` — **advisory** только для аудита, не security-контроль.

---

## Локальный запуск (без Timeweb, без бота)

`admin-panel/Dockerfile` — standalone-образ только панели:

```bash
cd admin-panel
docker build -t lesov-panel .
docker run --rm -p 8080:8080 \
  -e DATABASE_URL='postgresql://panel_rw:...@host:5432/db' \
  -e SESSION_SECRET='<≥32 символов>' \
  -e ADMIN_USERNAME='admin' \
  -e ADMIN_PASSWORD_HASH='$argon2id$v=19$...' \
  -e COOKIE_SECURE=0 \
  lesov-panel
# открыть http://localhost:8080/login
```

`COOKIE_SECURE=0` нужен для локального HTTP (иначе браузер отвергнет `__Host-`/Secure
cookie и логин «не залогинится»). В проде всегда `1`.

---

## Стоимость

Панель ~**510 ₽/мес** (preset 1003). Итого экосистема: бот ~510 + панель ~510 +
общий PG-кластер + лендинг (копейки).
