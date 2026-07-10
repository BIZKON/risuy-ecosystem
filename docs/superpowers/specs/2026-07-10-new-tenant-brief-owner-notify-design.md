# Дизайн: Новый тенант — ручная бриф-ссылка + уведомления владельцу

**Дата:** 2026-07-10
**Статус:** дизайн (спека), реализация не начата
**Автор:** brainstorming-сессия (владелец @profysales)
**Тип:** новая фича, subagent-driven реализация после плана

---

## 1. Контекст и цель

Клиенты часто соглашаются на сервис «на слово» на встрече и просят бриф **сразу**, не
желая самостоятельно регистрироваться на портале. Владельцу нужно:
- **(A)** из админки создать бриф-ссылку **под новую компанию одним действием** (без
  самостоятельной регистрации клиента) — сейчас это два шага (сначала `/tenants`
  «Создать клиента», потом Бриф-Центр);
- **(B)** получать в Telegram-бот **уведомления** о ключевых событиях (новый тенант
  создан; клиент прошёл бриф).

Это **Спек 1**. Партнёрская реферальная подсистема (**C**: ref-ссылки, атрибуция
тенантов к партнёрам, уведомления партнёрам) — отдельный **Спек 2** (модель
зафиксирована в §10, строится сразу после, переиспользует слой доставки уведомлений
из B).

### Что уже есть (опора реализации)
- **Бриф-Центр** (`/brief-center`): `db.list_tenants_min()` (dropdown тенантов),
  `POST /brief-center/create` → `db.create_tenant_brief(tid)` → ссылка `/brief/{token}`.
  Фича сессии 9 (PROD `c3f78c1`).
- **Создание тенанта:** `db.create_tenant_admin(name)` (только `is_platform`, статус
  `active`, без membership); self-signup `db.create_client_account(...)` (статус
  `provisioning`).
- **Уведомления Telegram:** бот-уведомитель `notifier.get_notifier_bot()`
  (`NOTIFIER_BOT_TOKEN`; **если не задан — фолбэк на разговорный бот**),
  `messaging.raw_send_text(bot, chat_id, text)`, единый rate-limiter. Панель→бот идёт
  через очередь-таблицу (бот дренирует), т.к. панель не может напрямую в
  `api.telegram.org` (РФ-блок, прокси только у бота).
- **`outbox`** — очередь исходящих, но **lead-scoped** (`lead_id uuid NOT NULL`), для
  не-лидовых уведомлений владельцу не годится → нужна отдельная мини-очередь.
- **`tenant_brief`** — таблица брифа (статусы pending→submitted→proposed→applied);
  `submit_brief(token, answers)` в боте.
- **`app_settings`** — глобальный KV (для `platform_owner_chat_id`).

---

## 2. Зафиксированные решения (развилки брейншторма)

| Решение | Выбор |
|---|---|
| Первый спек | **A + B** (ручная ссылка + уведомления владельцу); C — Спек 2 |
| Куда шлём уведомления | **Личный чат владельца с ботом** + адрес задаётся **полем в панели** |
| События уведомлений | **Оба:** (1) тенант создан, (2) бриф пройден |
| Атрибуция партнёра (для C) | **Реферальные ссылки** партнёра |
| Механика ref (для C) | **Сайт-ссылка → бот:** `pro-agent-ai.ru/r/КОД` → страница-кнопка → `t.me/bot?start=ref_КОД` |

**Инвариант:** уведомления **никогда не ломают** основной поток — если адрес не задан
или доставка не удалась, создание тенанта / сабмит брифа проходят как обычно.

---

## 3. Архитектура и поток

```
[A] Панель /brief-center «Новая компания» ──► create_tenant_admin + create_tenant_brief
        │                                              │
        │                                              └─►[B-1] enqueue platform_notify (владельцу)
        ▼
   ссылка /brief/{token}  → владелец отправляет клиенту
        ▼
   клиент проходит бриф (бот, submit_brief) ─────────►[B-2] enqueue platform_notify (владельцу)
        ▼
   воркер бота дренирует platform_notify → notifier/разговорный бот → raw_send_text(chat_id)
        ▼
   ты собираешь черновик → применяешь (фича сессии 9)
```

**Границы:**
- **A** — чистая панель: новый роут + блок формы, переиспользует 2 существующие
  db-функции. От B не зависит.
- **B** — слой доставки: настройка `platform_owner_chat_id`, echo-chat_id в боте,
  очередь `platform_notify`, дренаж воркером. Оба события кладут в ОДНУ очередь
  (единый ретраебельный путь).

---

## 4. Кусок A — ручная бриф-ссылка под новую компанию

- В `GET /brief-center` рядом с блоком «выбрать существующего тенанта» — второй блок
  **«Новая компания»**: поле `Название компании` + кнопка «Создать тенанта и бриф-ссылку».
- **Новый роут `POST /brief-center/create-new`** (гейт `_require_admin` + `_enforce_csrf`):
  ```
  name = Form; если пусто → redirect ?err=no_name
  _slug, tid = await db.create_tenant_admin(name, actor, ip, user_agent)  # status='active'
  brief_id, token = await db.create_tenant_brief(tid, actor, ip, user_agent)
  await notify_owner_new_tenant(name)   # B-событие 1 (§5); no-op если адрес не задан
  redirect /brief-center/{brief_id}?saved=created   # там уже показывается ссылка /brief/{token}
  ```
- Существующий путь «выбрать тенанта из списка» (`/brief-center/create`) — не трогаем.
- **Дедупа имени нет** (паритет с `create_tenant_admin`; список тенантов рядом).
- Ошибка создания brief после создания tenant → тенант остаётся (пустой active,
  безвредно), в `errors`-флеш сообщаем; полу-состояния нет (tenant валиден сам по себе).

---

## 5. Кусок B — уведомления владельцу

### 5.1 Настройка адреса
- Глобальная настройка **`platform_owner_chat_id`** в `app_settings` (строка — TG chat_id).
- **Поле в панели** (is_platform): напр. в разделе «Интеграции» или «Команда» — input
  «Chat ID для уведомлений владельца» + сохранение (POST, CSRF, `db.set_app_setting`).
- **Как узнать chat_id:** бот отвечает на `/start`/любое ЛС от неизвестного пользователя
  строкой «Ваш chat_id: `12345` — вставьте его в панели для уведомлений». Маленькое
  добавление в бота (echo). Владелец пишет боту → копирует id → вставляет в панель.

### 5.2 Очередь доставки `platform_notify` (новая таблица)
Не-лидовые уведомления (владелец, в C — партнёр). Аддитивная миграция
`db/migrate_platform_notify.sql`:
```sql
create table if not exists platform_notify (
    id         bigserial primary key,
    chat_id    bigint not null,                 -- целевой TG chat (владелец/партнёр)
    text       text   not null,
    status     text   not null default 'queued',
    attempts   int    not null default 0,
    last_error text,
    created_at timestamptz not null default now(),
    sent_at    timestamptz,
    constraint platform_notify_status_chk check (status in ('queued','sending','sent','failed'))
);
create index if not exists platform_notify_queued_idx on platform_notify (created_at) where status='queued';
-- грант panel_rw: INSERT/SELECT/UPDATE; бот ходит owner-DSN (bypass), доп-грант не нужен.
```
БЕЗ tenant-RLS (платформенный артефакт, как `tenants`).

### 5.3 Постановка в очередь (панель)
- `db.enqueue_platform_notify(text)` → читает `platform_owner_chat_id` из app_settings;
  если пусто → **no-op** (лог «адрес владельца не задан»); иначе INSERT в `platform_notify`.
- Обёртка `notify_owner_new_tenant(name)` → `enqueue_platform_notify("🆕 Новый клиент: {name}")`.
- **Точки вызова события 1** (все пути создания тенанта): роут A `/brief-center/create-new`;
  `/tenants/create` (админ); `/signup/register` (self-signup — самый ценный, владелец его
  не инициировал). Каждый после успешного создания тенанта зовёт `notify_owner_new_tenant`.
  Все — в try/except (сбой уведомления не ломает создание).

### 5.4 Событие 2 (бриф пройден) — бот
- В `submit_brief(token, answers)` (бот, после `status→submitted`) → бот кладёт в
  `platform_notify` (тот же путь): `enqueue_platform_notify_bot(chat_id, text)` c
  `text="✅ {tenant_name} прошёл бриф — пора собирать черновик"` + ссылка на
  `{PANEL_BASE_URL}/brief-center/{brief_id}`. Адрес владельца бот тоже читает из
  app_settings. (Бот пишет в ту же таблицу; т.к. это его же процесс — мог бы слать
  inline, но единый путь через очередь = ретраи + один код доставки.)

### 5.5 Дренаж (воркер бота)
- `bot-telegram/worker.py`: новый `_drain_platform_notify(bot)` рядом с `_drain_outbox`:
  `claim_platform_notify(batch)` (atomic lock, status→sending) → для каждой
  `raw_send_text(notifier_bot_or_conversational, chat_id, text)` → mark `sent`/`failed`
  (+attempts, last_error). Бот-уведомитель: `notifier.get_notifier_bot()`; если None
  (нет `NOTIFIER_BOT_TOKEN`) → фолбэк на разговорный бот (тот же rate-limiter).

---

## 6. Обработка ошибок

- **A:** пустое имя → флеш-ошибка, тенант не создаётся. Сбой `create_tenant_brief`
  после `create_tenant_admin` → тенант остаётся (валиден), флеш «тенант создан, бриф —
  повторите»; без полу-состояния.
- **B:** нет `platform_owner_chat_id` → enqueue no-op (лог). Нет `NOTIFIER_BOT_TOKEN` →
  фолбэк на разговорный бот. Сбой отправки → `failed`+`last_error`, ретрай воркером
  (ограничить attempts, напр. ≤5). **Ни одна ошибка B не ломает A / сабмит брифа**
  (enqueue в try/except, лог).

---

## 7. Тесты

- **A:** db-смоук (контроллер, risuy_dev) — `create_tenant_admin`+`create_tenant_brief`
  цепочкой создают active-тенант + pending-бриф; py_compile; регистрация роута — на деплое.
- **B:** db-смоук `platform_notify_smoke` (контроллер, risuy_dev) — `enqueue_platform_notify`
  при заданном/пустом `platform_owner_chat_id` (INSERT / no-op), `claim`→`sending`,
  mark `sent`/`failed`, идемпотентность claim. Echo chat_id в боте — py_compile.
  Формат текста уведомлений — юнит-проверка.
- Панельные роуты — py_compile (в `.venv-smoke` нет fastapi) + деплой-проверка глазами.

---

## 8. Выкатка

- Аддитивная миграция `platform_notify` — сперва risuy_dev, прод по «да».
- **A может уйти ПЕРВЫМ отдельно** (без DDL, без B) — разблокирует ждущего клиента.
- B — следом (DDL + панель + бот-echo + бот-worker). Бот трогаем (echo + worker drain).
- Env: `platform_owner_chat_id` — не env, а app_settings (задаётся в панели). Проверить,
  настроен ли в проде `NOTIFIER_BOT_TOKEN` (иначе фолбэк на разговорный бот — рабочий).
- Деплой: `git push origin docs/security-audit:main` (одноразово через аккаунт BIZKON) →
  редеплой обоих; commit_sha+active по twc.

---

## 9. Вне скоупа Спека 1 (YAGNI)

- Партнёрская подсистема C (§10) — отдельный спек.
- Кабинет владельца/партнёра, роли партнёра, комиссии, публичные ref-ссылки.
- Уведомления по e-mail (только Telegram).
- Дизейбл кнопки / доп-UX сверх блока «Новая компания».

---

## 10. Приложение: Спек 2 (C) — партнёрская реферальная подсистема (зафиксировано, НЕ в этом спеке)

Строится сразу после Спека 1, переиспользует `platform_notify` + `raw_send_text`.

- **Модель партнёра** (новая таблица `partners`): `id, name, ref_code (авто, напр.
  ref-<hex>), tg_chat_id (для уведомлений), status, created_at`.
- **Атрибуция тенанта:** поле `partner_id` (nullable FK) в `tenants` (или
  `tenant_settings`-ключ) — DDL.
- **Админ заводит партнёра одной формой:** имя + chat_id (партнёр пишет боту → echo id
  → вставляешь) → система генерит `ref_code` и показывает готовую **сайт-ссылку
  `{сайт}/r/{ref_code}`** (копируешь, отдаёшь партнёру).
- **Ref-поток:** `{сайт}/r/{ref_code}` → короткая брендированная страница с кнопкой
  «Получить бриф» → `t.me/bot?start=ref_{ref_code}`. Публичная редирект-страница
  `/r/{code}` — паттерн у бота уже есть (`/r/{token}`, `/brief/{token}`, `/club/{slug}`);
  либо на `service-site` с редиректом в бота.
- **Бот `?start=ref_{code}`:** резолвит `ref_code`→партнёр → спрашивает название
  компании → `create_tenant_admin` + `create_tenant_brief`, ставит `tenant.partner_id`
  → клиент проходит бриф.
- **Уведомления партнёру:** при создании атрибутированного тенанта и при сабмите его
  брифа → `enqueue_platform_notify(partner.tg_chat_id, text)` («Новый тенант от тебя:
  {компания}» / «{компания} прошёл бриф»). Владелец уведомляется параллельно (B).
- Открытые вопросы C (для его брейншторма): счётчик «сколько тенантов от партнёра»
  (отчёт), дедуп/анти-абьюз ref-потока, срок жизни ref-кода, домен/хостинг `/r/`.
