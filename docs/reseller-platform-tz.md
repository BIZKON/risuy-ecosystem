# ТЗ: Reseller-платформа «ИИ-Агент Про» (multi-tenant white-label слой)

**Версия:** 1.0-risuy (адаптация ТЗ «Reseller-платформа X10 Daily» v1.0 под этот проект)
**Для:** Claude Code agent team (DB / Bot / Panel / Site)
**Расположение в репо:** `docs/reseller-platform-tz.md`
**Связанные документы:** `docs/DECISIONS.md` (создаётся вместе с этим ТЗ), `README.md`,
handoff-серия в `~/Downloads/risuy-ecosystem-HANDOFF.md`

> **Происхождение.** Документ адаптирован из ТЗ x10-daily. Оригинал писался под стек
> Next.js 16 / Drizzle / Better Auth / TS-SDK ЮKassa — здесь этого стека НЕТ.
> Все разделы переведены на реальные артефакты risuy-ecosystem: Python (aiogram-бот +
> FastAPI-панель + Jinja), сырой SQL schema-first через `twc-migrate.sh`, свой
> stdlib-клиент ЮKassa (`yookassa.py`, уже в проде: два магазина, чеки 54-ФЗ,
> вебхук с перепроверкой), Timeweb App Platform + Managed PG 4171827.
> Таблица соответствия «оригинал → адаптация» — §12.

---

## 0. Контекст и цель

Сейчас «ИИ-Агент Про» — **single-tenant**: один клиент (Школа Лесова) = один бот
(@App_LesovSales26_bot) + одна панель + одна БД. Подписка уже продаётся
(`service_invoices`, тарифы `SERVICE_PLANS`, оплата ЮKassa на `info.pro-agent-ai.ru`
и в панели), но второй клиент потребовал бы клонировать всю инфраструктуру руками.

Превращаем продукт в **multi-tenant white-label платформу**: каждый клиент получает
изолированный экземпляр (свой Telegram-бот, свои ИИ-сотрудники, своя база знаний,
свои лиды/диалоги/рассылки/платежи), вводит свои ключи (BOT_TOKEN, касса школы,
VK) — и система работает. Расход ИИ метрируется со счётчиком, в который зашита
**наша наценка ×3 как реселлера** токенов Timeweb.

**Что этот слой НЕ делает.** Он не переписывает воронку бота, ИИ-диспетчер
(`ask_ai`), RAG (pgvector + TEI), разделы панели и сайт — они существуют и работают.
Этот слой — **коммерческая и multi-tenant обёртка**: tenancy + RLS, клиентские
кабинеты, кошелёк + метеринг ×3, хранилище секретов тенанта. Метеринг
**подключается хуком** к существующей точке вызова ИИ (`bot-telegram/ai.py::ask_ai`),
а не заменяет её.

**Definition of Done всего проекта — один бинарный критерий (см. §8):** реальный
сквозной прогон «регистрация тенанта → пополнение кошелька через ТЕСТОВЫЙ магазин
ЮKassa → сообщение боту тенанта → ответ ИИ → в леджере списание cost×3 → баланс
уменьшился → раздел панели это показал», без моков платежа и без моков леджера.
Зелёные unit-тесты на стабах статусом «готово» не считаются.

---

## 1. Бизнес-модель: ДВЕ раздельные линии денег

Ядро. Не смешивать в одном счётчике.

| Линия | Что это | Периодичность | Куда идёт в БД |
|---|---|---|---|
| **Подписка (тариф)** | Доступ к платформе (панель + бот + white-label). Наш SaaS-доход. | Рекуррент (месяц/год) | `subscriptions` + `payments(type='subscription')` |
| **Расход ИИ ×3** | Потребление LLM (DeepSeek через Timeweb). `charged = cost × 3`. | Pay-as-you-go из кошелька | `usage_ledger` + `credit_wallets` |

**Поток средств:** клиент платит подписку за доступ → отдельно пополняет кошелёк →
потребление списывается из кошелька по `cost × multiplier`. Пополнение кошелька =
`payments(type='topup')`.

**Совместимость с действующим прайсом.** Текущие тарифы «ИИ-Агент Про» продают
*сообщения* («500 сообщений ИИ, 7,5 ₽ сверх»), а не токены. Чтобы не ломать
проданное, у плана есть `billing_mode`:
- `cost_multiplier` — канон реселлера: `charged = ceil(cost × multiplier)`;
- `per_message` — фикс-цена за сообщение ИИ из плана (текущая модель; эмпирика
  прода: ~760 токенов/сообщение ≈ 0,27 ₽ себестоимости — фикс 5–7,5 ₽ держит
  маржу 18–27×, см. блок «Экономика сервиса» панели).

Оба режима пишут в ОДИН леджер (units + cost + charged); отличается только формула
`charged`. Дефолт новых планов — `cost_multiplier` ×3. Включённая в подписку квота
выражается кредитами: `included_credits_microrub` (для `per_message`-планов —
произведение квоты на цену сообщения).

**Жёсткое правило безопасности модели:** множитель наценки (`multiplier`) и цена
сообщения живут **только на сервере** — в таблице `plans`. Они **никогда** не
приходят из клиентского запроса и не видны/не редактируемы клиентом. Иначе клиент
подделает запрос и обнулит маржу. (Сегодняшний прецедент уже в проде: блок
«Экономика сервиса» с себестоимостью отдаётся только роли `admin` — клиент видит
только `charged`.)

---

## 2. Зафиксированные архитектурные решения (locked)

Не пересматривать в рамках этого ТЗ. Отклонения логировать в `docs/DECISIONS.md`.

- **Стек существующий, без новых рантаймов:** Python ≥3.11; бот — aiogram 3.x;
  панель — FastAPI + Jinja2 + asyncpg (сырой SQL, без ORM); сайт — static-nobuild.
  НЕ Next.js, НЕ Drizzle, НЕ Node-зависимости. Деплой = push в main (мультиплекс
  трёх приложений Timeweb; билд бота ~9–10 мин — норма).
- **БД:** Timeweb Managed PostgreSQL, кластер **4171827** (тот же, где прод).
  **Schema-first:** весь DDL — файлами `db/schema_*.sql`, накатываются
  `~/.claude/scripts/twc-migrate.sh` owner-DSN **ПЕРЕД** кодом. Для разработки —
  отдельная база `risuy_dev` НА ТОМ ЖЕ кластере (`create database` — у DBaaS нет
  branching; 0 ₽ доп.): миграции прогоняются сначала на ней.
- **Auth:** РАСШИРЕНИЕ существующей самописной (argon2 + `admin_users` +
  `admin_sessions` + роли) до tenant-aware RBAC. НЕ Better Auth (TS-библиотека,
  в Python-проекте неприменима), НЕ облачные провайдеры — данные пользователей
  остаются на российской инфраструктуре (152-ФЗ).
- **Платежи:** существующий **`yookassa.py`** (stdlib urllib в треде, без SDK) —
  расширяется, не заменяется. Уже умеет: Basic-auth, Idempotence-Key, чеки 54-ФЗ
  (`receipt`, vat_code=1 УСН), два магазина, вебхук с перепроверкой платежа по id.
  НЕ Stripe, НЕ сторонние SDK.
- **Метеринг:** собственный append-only леджер в Postgres. **Без** Lago/OpenMeter —
  лишний сервис при текущем бюджете. Порог возврата к выделенному движку —
  в `DECISIONS.md`.
- **Деньги в БД:** только целые **микро-рубли** (`bigint`, 1 RUB = 1_000_000 µRUB).
  Никаких float/numeric для денег в новых таблицах. Округление — одна функция
  `ceil_mul`, в одну сторону (вверх, в нашу пользу), задокументирована.
  (Существующие `numeric(12,2)` в `orders`/`service_invoices` не трогаем — legacy;
  конверсия на границе.)
- **LLM-слой:** Timeweb — два бэкенда, у них разная наблюдаемость (проверено в
  проде 2026-06-12):
  - `cloud_ai` (агенты, `/call`) — **НЕ отдаёт usage per-call**; зато у агента есть
    накопительный `used_tokens`, а агент принадлежит ровно одному тенанту →
    метеринг по **дельте `used_tokens`**;
  - `gateway` (`api.timeweb.ai/v1`, OpenAI-совместимый) — отдаёт `usage` в каждом
    ответе → точный per-call метеринг.
- **Метрологические бюджеты панели:** P95 ответа маршрута ≤ 500 мс (без внешних
  API в hot path — Timeweb-вызовы только в фоновых задачах/отдельных экранах);
  ответ ИИ лиду ≤ 10 с (факт прода на V4 Pro Thinking: 4,6–5,0 с); CSP строгий,
  без inline-JS; PRG для всех мутаций.

---

## 3. Структура модулей (вместо «пакетов монорепо»)

Новые модули добавляются к существующим. Владение — §9.

```
db/
  schema_tenancy.sql      # НОВЫЙ — tenants, memberships, tenant_settings + RLS
  schema_billing.sql      # НОВЫЙ — plans, subscriptions, payments, webhook_events
  schema_metering.sql     # НОВЫЙ — credit_wallets, usage_ledger, model_prices, agent_token_snapshots
  schema_vault.sql        # НОВЫЙ — tenant_secrets
  migrate_tenant_scope.sql# НОВЫЙ — tenant_id в СУЩЕСТВУЮЩИЕ таблицы + backfill «Школа Лесова»
  panel_role.sql          # СУЩЕСТВУЕТ — дополняется грантами новых таблиц
shared/                   # НОВЫЙ пакет, импортируется ботом И панелью
  money.py                # µRUB, ceil_mul — ЕДИНСТВЕННОЕ место округления
  vault.py                # envelope-шифрование (AES-GCM), мастер-ключ из env
  metering.py             # charge_usage() — транзакционное списание (§5.1)
bot-telegram/
  multiplex.py            # НОВЫЙ — реестр тенант-ботов, polling-таски, hot-reload
  ai.py                   # СУЩЕСТВУЕТ — оборачивается cost-capture (§5.2)
admin-panel/
  app.py                  # СУЩЕСТВУЕТ — новые разделы: Кошелёк/Расход, Ключи, тенант-контекст
  billing.py              # НОВЫЙ — подписки/топапы/автосписания поверх yookassa.py
service-site/             # СУЩЕСТВУЕТ — тарифы/оплата уже есть; добавить «Кабинет» (ссылка)
```

Граница `billing` vs `metering`: **billing = деньги внутрь** (подписки + пополнения),
**metering = потребление наружу** (списание cost×3 из кошелька).
Кошелёк (`credit_wallets`) — общая точка: billing пополняет, metering списывает.

---

## 4. Модель данных (SQL, schema-first)

Эскизы. Финальные типы/индексы — за DB-владельцем. Все tenant-scoped таблицы имеют
`tenant_id` и **Postgres RLS-политику** по `current_setting('app.tenant_id')`
(панель и бот выставляют её `set_config(..., true)` в начале транзакции; роль
`panel_rw` НЕ имеет `bypassrls`; owner-обслуживание — отдельным путём).

### 4.1 Tenancy

```sql
-- tenants: один клиент = один изолированный «ИИ-Агент Про»
create table tenants (
    id            uuid primary key default gen_random_uuid(),
    slug          text unique not null,          -- идентификатор white-label
    name          text not null,                 -- «Школа Лесова», ...
    status        text not null default 'provisioning'
                  check (status in ('provisioning','active','suspended','canceled')),
    plan_id       uuid references plans(id),
    created_at    timestamptz not null default now()
);

-- memberships: RBAC внутри тенанта поверх СУЩЕСТВУЮЩЕЙ admin_users
-- (env-админ остаётся bootstrap-суперюзером ПЛАТФОРМЫ мимо БД — наша сторона)
create table memberships (
    id          uuid primary key default gen_random_uuid(),
    tenant_id   uuid not null references tenants(id),
    username    text not null references admin_users(username),
    role        text not null check (role in ('owner','admin','operator')),
    created_at  timestamptz not null default now(),
    unique (tenant_id, username)
);

-- tenant_settings: замена app_settings для tenant-scoped ключей
-- (та же модель «панель пишет / бот читает», но со столбцом tenant_id)
create table tenant_settings (
    tenant_id  uuid not null references tenants(id),
    key        text not null,
    value      text not null default '',
    updated_at timestamptz not null default now(),
    primary key (tenant_id, key)
);
```

### 4.2 Auth

Таблицы `admin_users / admin_sessions / admin_audit / admin_login_throttle`
**существуют** (schema_team.sql, schema_admin.sql) — не пересоздавать. Расширение:
сессия получает `active_tenant_id`; `load_session` резолвит доступные тенанты через
`memberships`; платформенный env-админ видит все тенанты (селектор в шапке панели).

### 4.3 Billing (деньги внутрь)

```sql
create table plans (
    id                        uuid primary key default gen_random_uuid(),
    code                      text unique not null,        -- 'econom','start','custom'
    name                      text not null,
    price_microrub            bigint not null,             -- цена подписки за период
    "interval"                text not null default 'month' check ("interval" in ('month','year')),
    included_credits_microrub bigint not null default 0,   -- кредиты, входящие в подписку
    billing_mode              text not null default 'cost_multiplier'
                              check (billing_mode in ('cost_multiplier','per_message')),
    markup_multiplier         numeric(4,2) not null default 3.00,  -- ← ТОЛЬКО сервер
    per_message_microrub      bigint,                      -- для billing_mode='per_message'
    features                  jsonb not null default '{}'::jsonb
);
-- seed: текущие SERVICE_PLANS (econom 3750₽/500 сообщ ≈ per_message 7.5₽;
-- start 7500₽/1500 ≈ 5₽) переносятся из config.py в БД. config.SERVICE_PLANS
-- остаётся фолбэком single-tenant до Wave 3, затем читается из plans.

create table subscriptions (
    id                          uuid primary key default gen_random_uuid(),
    tenant_id                   uuid not null references tenants(id),
    plan_id                     uuid not null references plans(id),
    status                      text not null default 'trialing'
                                check (status in ('trialing','active','past_due','canceled')),
    current_period_start        timestamptz not null,
    current_period_end          timestamptz not null,
    yookassa_payment_method_id  text,        -- сохранённый метод для автосписаний
    created_at                  timestamptz not null default now()
);

create table payments (
    id                  uuid primary key default gen_random_uuid(),
    tenant_id           uuid not null references tenants(id),
    type                text not null check (type in ('subscription','topup')),
    yookassa_payment_id text unique,
    idempotence_key     text unique not null,
    amount_microrub     bigint not null,
    status              text not null default 'pending'
                        check (status in ('pending','waiting_for_capture','succeeded','canceled')),
    captured_at         timestamptz,
    raw                 jsonb                -- сырой ответ ЮKassa для аудита
);

-- журнал идемпотентности входящих уведомлений (вебхук уже перепроверяет платёж
-- по id — это сохраняется; журнал добавляет защиту от повторной доставки)
create table webhook_events (
    id           uuid primary key default gen_random_uuid(),
    provider     text not null default 'yookassa',
    external_id  text unique not null,       -- id события/платежа от провайдера
    event_type   text,
    payload      jsonb,
    status       text not null default 'received'
                 check (status in ('received','processed','failed')),
    processed_at timestamptz
);
```

Существующие `service_invoices` (подписка Школы) и `orders` (продажи школы лидам)
**не ломаются**: `service_invoices` доживает как legacy-витрина первого тенанта и
закрывается после миграции подписки Школы на `subscriptions/payments`
(фиксируется в `DECISIONS.md`); `orders` — это продажи ТЕНАНТА его клиентам,
к биллингу платформы не относится (получает только `tenant_id`).

### 4.4 Metering (потребление ×3) — ядро

```sql
-- кошелёк: авторитетный источник баланса
create table credit_wallets (
    tenant_id         uuid primary key references tenants(id),
    balance_microrub  bigint not null default 0,   -- минус — только для postpaid-планов
    updated_at        timestamptz not null default now()
);

-- APPEND-ONLY. Каждая строка = одно списание. Роли panel_rw выдаются ТОЛЬКО
-- select+insert (UPDATE/DELETE НЕ выдавать — append-only на уровне грантов).
create table usage_ledger (
    id                      bigint generated always as identity primary key,
    tenant_id               uuid not null references tenants(id),
    occurred_at             timestamptz not null default now(),
    kind                    text not null check (kind in ('llm','embedding','message','other')),
    provider                text,                  -- 'timeweb-cloud-ai' | 'timeweb-ai-gateway'
    model                   text,                  -- 'deepseek-v4-pro-thinking', ...
    units                   jsonb not null default '{}'::jsonb, -- {tokens_in,tokens_out,tokens_total,messages}
    cost_microrub           bigint not null,       -- НАША себестоимость (до наценки)
    multiplier              numeric(4,2) not null, -- снимок на момент списания
    charged_microrub        bigint not null,       -- = ceil_mul(cost, multiplier) | per_message
    balance_after_microrub  bigint not null,
    request_id              text,
    idempotence_key         text unique not null   -- ← защита от двойного списания
);

-- себестоимость моделей (НАШИ закупочные тарифы Timeweb).
-- ПЕРЕД вписыванием цен — проверка актуальных тарифов в ЛК/доке Timeweb (дрейфуют!).
-- Факт 2026-06-12: DeepSeek V4 Pro Thinking — вход 234,9 ₽/млн, выход 469,8 ₽/млн.
create table model_prices (
    id               bigint generated always as identity primary key,
    provider         text not null,
    model            text not null,
    price_in_microrub_per_1k  bigint not null,   -- µRUB за 1k входных токенов
    price_out_microrub_per_1k bigint not null,
    effective_from   timestamptz not null default now()
);

-- снапшоты used_tokens агентов cloud-ai: основа метеринга по дельте (§5.2).
create table agent_token_snapshots (
    agent_id     bigint not null,                 -- числовой id агента Timeweb
    tenant_id    uuid not null references tenants(id),
    used_tokens  bigint not null,
    taken_at     timestamptz not null default now(),
    primary key (agent_id, taken_at)
);
```

`charged_microrub = ceil_mul(cost_microrub, multiplier)` (режим `cost_multiplier`)
или `per_message_microrub` плана (режим `per_message`; `cost` всё равно пишется —
для контроля маржи). Кошелёк уменьшается на `charged`, пополняется из
`payments(type='topup', status='succeeded')` и `included_credits` при активации
периода подписки.

### 4.5 Vault (секреты тенанта)

```sql
create table tenant_secrets (
    id           uuid primary key default gen_random_uuid(),
    tenant_id    uuid not null references tenants(id),
    key_name     text not null,   -- 'telegram_bot_token','shop_yookassa_shop_id',
                                  -- 'shop_yookassa_secret_key','vk_token', ...
    ciphertext   bytea not null,  -- AES-GCM (envelope), НИКОГДА не plaintext
    nonce        bytea not null,
    key_version  int not null default 1,   -- для ротации мастер-ключа
    created_at   timestamptz not null default now(),
    last_used_at timestamptz,
    unique (tenant_id, key_name)
);
```

Реализация — `shared/vault.py` (библиотека `cryptography`, AES-256-GCM).
Мастер-ключ `VAULT_MASTER_KEY` — только в env приложений (через `twc-set-env.sh`,
НЕ через UI Timeweb — затирает run_cmd), в репо не живёт. UI ключей — write-only:
показываем «задан/не задан» + `last_used_at`, значение не отображаем никогда.

### 4.6 Tenant-scope существующих таблиц

Существующий пайплайн не переписывается — он **скоупится**:

```sql
-- migrate_tenant_scope.sql (идемпотентно):
-- 1) alter table <t> add column if not exists tenant_id uuid references tenants(id);
--    для: leads, messages, orders, products, broadcasts, broadcast_recipients,
--         broadcast_files, link_tokens, link_clicks, outbox,
--         kb_documents, kb_chunks
-- 2) backfill: update <t> set tenant_id = :school_tenant_id where tenant_id is null;
-- 3) set not null + индексы (tenant_id, created_at);
-- 4) RLS-политики по current_setting('app.tenant_id') на каждую.
```

Первый тенант («Школа Лесова») создаётся миграцией из текущих данных — прод не
останавливается. Ключи `app_settings` вида `ai_*`, `kb_enabled`, `guide_url`,
runtime-снимок бота переезжают в `tenant_settings` (бот и панель читают новую
таблицу с фолбэком на `app_settings` до конца миграции — поведение Школы не
меняется ни на минуту).

---

## 5. Ключевые механизмы (как именно)

### 5.1 `charge_usage()` — транзакционное списание ×3 (сердце метеринга)

Одна функция (`shared/metering.py`), одна транзакция, asyncpg:

```python
async def charge_usage(conn, tenant_id, cost_microrub, meta, idempotence_key):
    async with conn.transaction():
        # 1. ИДЕМПОТЕНТНОСТЬ: ретрай не должен списать дважды
        dup = await conn.fetchrow(
            "select * from usage_ledger where idempotence_key = $1", idempotence_key)
        if dup:
            return dup                                  # уже списано — вернуть как есть

        # 2. БЛОКИРОВКА строки кошелька: защита от овердрафта на гонках
        wallet = await conn.fetchrow(
            "select balance_microrub from credit_wallets "
            "where tenant_id = $1 for update", tenant_id)

        # 3. множитель/цена сообщения — С СЕРВЕРА (план тенанта), не из запроса
        plan = await get_tenant_plan(conn, tenant_id)
        charged = (plan["per_message_microrub"]
                   if plan["billing_mode"] == "per_message"
                   else ceil_mul(cost_microrub, plan["markup_multiplier"]))

        # 4. проверка баланса (prepaid)
        if plan_is_prepaid(plan) and wallet["balance_microrub"] < charged:
            raise InsufficientCreditsError(tenant_id)

        balance_after = wallet["balance_microrub"] - charged
        # 5. атомарно: списать + записать в append-only леджер
        await conn.execute(
            "update credit_wallets set balance_microrub = $2, updated_at = now() "
            "where tenant_id = $1", tenant_id, balance_after)
        return await conn.fetchrow(
            """insert into usage_ledger (tenant_id, kind, provider, model, units,
                   cost_microrub, multiplier, charged_microrub,
                   balance_after_microrub, request_id, idempotence_key)
               values ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11) returning *""",
            tenant_id, meta["kind"], meta.get("provider"), meta.get("model"),
            json.dumps(meta.get("units", {})), cost_microrub,
            plan["markup_multiplier"], charged, balance_after,
            meta.get("request_id"), idempotence_key)
```

Три гвоздя, которые держат конструкцию: **`FOR UPDATE`** (овердрафт на параллельных
списаниях), **`unique(idempotence_key)`** (двойное списание на ретрае),
**целые µRUB + `ceil_mul`** (дрейф float). `ceil_mul` живёт в `shared/money.py`
и не дублируется.

Поведение при `InsufficientCreditsError` в боте: ИИ тенанта отвечает мягким
сервисным сообщением (аналог паузы), оператору тенанта — алерт в панели
(«кошелёк пуст — пополните»). Лид без ответа не остаётся.

### 5.2 Cost-capture вокруг `ask_ai` (бот)

Тонкая обёртка над существующим диспетчером `bot-telegram/ai.py::ask_ai`.
Из-за разной наблюдаемости бэкендов (проверено в проде) — две механики:

- **`gateway`** (`api.timeweb.ai/v1`, OpenAI-формат): ответ содержит `usage`
  (`prompt_tokens` / `completion_tokens`) → себестоимость по `model_prices` →
  `charge_usage()` сразу после ответа. Точный per-call метеринг.
  `idempotence_key = f"gw:{tenant_id}:{request_id}"`.

- **`cloud_ai`** (`/call` агента): **usage в ответе НЕТ** (ключи ответа —
  message/id/finish_reason/response_id, проверено 2026-06-12). Зато каждый агент
  принадлежит ровно одному тенанту, и Timeweb ведёт накопительный `used_tokens`.
  Метеринг — **по дельте**: периодическая задача (вместе с существующими
  фоновыми воркерами бота) снимает `used_tokens` всех агентов тенантов
  (`GET /cloud-ai/agents` — один вызов на всех), пишет `agent_token_snapshots`,
  и для каждой положительной дельты вызывает `charge_usage()` с
  `idempotence_key = f"ca:{agent_id}:{prev_snapshot_taken_at}"`. Разбивка
  вход/выход недоступна → себестоимость дельты считается по смешанной цене
  (доля выхода — конфиг, текущий прод-факт: 0.5 для thinking-моделей).
  Режим `per_message` проще: списание за каждое исходящее сообщение Лии
  (`messages: source='liya', direction='out'`) — `idempotence_key = f"msg:{message_id}"`.

**152-ФЗ.** Оригинальное ТЗ требовало KikuAI Masker перед шлюзом — в risuy
маскер НЕ развёрнут и сейчас НЕ обязателен: оба бэкенда (cloud-ai, AI Gateway) —
**российская инфраструктура Timeweb**, трансграничной передачи нет. Инварианты,
которые уже действуют и сохраняются: в базу знаний — только справка (не ПДн
лидов); метеринг работает с токенами и стоимостью, не с контентом. Если в линейке
появится зарубежный провайдер — Masker становится обязательным звеном ДО вызова
(плейбук self-host уже есть: стек massage-boho), это отдельная волна.

Таблица `model_prices` — наша себестоимость. **Перед вписыванием любых цен —
проверка актуальных тарифов в ЛК Timeweb** (цены дрейфуют; факт 2026-06-12:
V4 Pro Thinking 234,9/469,8 ₽/млн вписан с проверкой).

### 5.3 ЮKassa: рекуррент, webhook, чеки

ЮKassa **не имеет объектов-подписок** как Stripe. Делаем сами поверх
существующего `yookassa.py` (он уже в проде: Basic-auth, Idempotence-Key, чеки):

- **Первый платёж подписки:** `create_payment(..., save_payment_method=true)` →
  сохранить вернувшийся `payment_method_id` в `subscriptions`.
- **Автосписания:** фоновая задача бота-воркера (рядом с retention/nurture —
  отдельный сервис НЕ заводим) на `current_period_end` создаёт безакцептный платёж
  по сохранённому методу; неуспех → `past_due` + ретраи + алерт.
- **ВНЕШНИЙ БЛОКЕР:** автоплатежи/безакцепт **включает менеджер ЮKassa вручную**
  для магазина. По умолчанию выключены. Проверить ДО Wave 2 — без этого рекуррент
  не заработает (первый платёж и ручное продление работают и без него).
- **Webhook:** подписи нет → верификация = (а) allowlist IP ЮKassa +
  (б) **повторный запрос платежа по id** (уже реализовано в
  `/webhooks/yookassa` — сохранить), затем идемпотентность через
  `webhook_events.external_id unique` (добавляется).
- **54-ФЗ:** чек обязателен для фискального магазина — `receipt` уже передаётся
  (прод-фикс 2026-06-12: магазин 1378536 с `yoo_receipt` отвергал платежи без
  чека). Заложен в обе линии: подписка и пополнение. `vat_code` — из плана/env
  (УСН «Доходы» = 1).
- **Два уровня касс не путать:** касса ПЛАТФОРМЫ (наша, принимает подписки и
  топапы тенантов) ≠ касса ТЕНАНТА (его магазин для продаж его лидам,
  `SHOP_YOOKASSA_*` → переезжает в vault тенанта). Вебхук платформы и вебхук
  тенантских продаж — раздельные маршруты.

### 5.4 Мультиплекс ботов (`bot-telegram/multiplex.py`)

Один процесс (существующее приложение 201859) ведёт N тенантских ботов:

- Реестр: `tenants(status='active')` × `tenant_secrets('telegram_bot_token')`.
- На каждого тенанта — свой `aiogram.Bot` + `Dispatcher`-таска **polling**
  (v1; вебхуки — отдельная волна масштабирования, фиксируется в `DECISIONS.md`
  при >20–30 тенантах).
- Hot-reload: периодическая сверка реестра — новый активный тенант с токеном →
  таска поднимается БЕЗ редеплоя; suspended/canceled → таска гасится.
- Каждая таска работает в контексте своего тенанта: все запросы БД — с
  `set_config('app.tenant_id', ...)`; `OPS_CHAT_ID`, гейт-канал, voice — из
  `tenant_settings`/vault тенанта.
- Деплой-мультиплекс Timeweb остаётся как есть; конфликт-окно ~30 с на бота —
  известная норма.

### 5.5 Провижининг тенанта

`status='provisioning'` → чек-лист в панели платформы:
1. оплачена подписка (или включён trial) → создаётся кошелёк (+included credits);
2. владелец тенанта вводит свой BOT_TOKEN (vault) → мультиплекс подхватывает бота;
3. первый ИИ-сотрудник: создание cloud-ai агента через API — **платная мутация**,
   выполняется только после активной подписки (себестоимость агента — на нас,
   расход токенов — метрируется тенанту);
4. (опц.) каналы/источники, касса тенанта, база знаний.
Все шаги идемпотентны; `status='active'` — когда (1) и (2) выполнены.

---

## 6. Кабинет клиента (= существующая панель, тенант-скоуп)

Личный кабинет НЕ пишется с нуля — это **существующая панель** (FastAPI + Jinja,
white-label «ИИ-Агент Про» уже внедрён), которая становится tenant-aware.
Канон UI прежний: CSP без inline-JS, PRG-формы, все поля видимы (хинты вместо
show/hide), существующие классы (`.field`, `.table`, `.billing-card`).

| Раздел | Что меняется |
|---|---|
| **Подписка** | + кошелёк: баланс, кнопка «Пополнить» (топап через кассу платформы), история `payments`. Блок «Экономика сервиса» (себестоимость/маржа) остаётся **только платформенному admin** — прецедент уже в проде. |
| **Расход** (НОВЫЙ) | лента `usage_ledger` тенанта: дата, тип, модель, units, **только `charged`** (себестоимость и multiplier клиенту НЕ видны); агрегаты по дням/моделям. |
| **Ключи** (НОВЫЙ) | write-only ввод/ротация секретов тенанта (vault): BOT_TOKEN, касса школы, VK. Показ — «задан/не задан» + `last_used_at`. |
| **Интеграции / Каналы** | существующие разделы — скоупятся тенантом (deep-links по `bot_username` ИЗ тенантского runtime-снимка). |
| **Команда** | существующий раздел — поверх `memberships` тенанта; роли owner/admin/operator. |
| **Диалоги / Платежи / ИИ-агенты / Базы знаний / Рассылки** | без редизайна — RLS сам ограничивает данные тенантом. |

Платформенная сторона (мы): селектор тенантов для env-админа, сводка по всем
тенантам (выручка, расход, маржа — расширение сегодняшнего блока «Экономика»).

---

## 7. План волн (dependency-aware)

| Волна | Состав | Зависит от | Владелец |
|---|---|---|---|
| **0** | DDL §4 целиком (`schema_tenancy/billing/metering/vault.sql`, `migrate_tenant_scope.sql`) на `risuy_dev`, затем прод owner-DSN; RLS; seed `plans` (перенос SERVICE_PLANS, `markup=3.00`); `model_prices` (цены проверить в ЛК); бэкфилл тенанта «Школа Лесова»; гранты `panel_role.sql` | — | DB |
| **1** | **WP-A Auth+Tenancy** (memberships, active_tenant в сессии, tenant-context middleware панели, селектор тенантов) ‖ **WP-D Vault** (`shared/vault.py` + раздел «Ключи», гарантия «никогда в логи») | 0 | Panel ‖ DB |
| **2** | **WP-B Billing** (`admin-panel/billing.py`: топап + подписка поверх `yookassa.py`, `webhook_events`-идемпотентность, автосписания в боте-воркере, чеки обеих линий) | 1 | Panel |
| **3** | **WP-C Metering** (`shared/metering.py::charge_usage`, cost-capture в `ai.py`, снапшот-воркер `used_tokens`, раздел «Расход») ‖ **Мультиплекс** (`multiplex.py`, hot-reload, тенант-контекст хендлеров) | 1, 2 | Bot/DB ‖ Bot |
| **4** | **Интеграционный E2E** (§8) — бинарный DoD; миграция Школы на `subscriptions` | 0–3 | Panel |

«‖» = параллельно (непересекающееся владение файлами). Деплой каждой волны —
push в main; **DDL — всегда twc-migrate.sh ПЕРЕД пушем кода** (правило проекта).

---

## 8. Бинарные критерии приёмки (audit-resistant)

Каждый критерий — реальный внешне проверяемый факт. **Не «тест зелёный», а «вот
это произошло на самом деле»**. Платёжные прогоны — против **тестового** магазина
ЮKassa (1379463, креды уже в проекте).

1. **Реальный платёж ЮKassa.** Топап доходит до `succeeded`, подтверждённого
   **повторным запросом платежа по id** (не телом webhook). Реальный HTTP round-trip.
2. **Списание ×3 записано точно.** Метрируемая операция с известными токенами и
   ценой модели → ровно одна строка `usage_ledger`, где
   `charged_microrub == ceil_mul(cost_microrub, 3.00)` и
   `balance_after == prev_balance − charged`. Целочисленная арифметика ассертится.
3. **Идемпотентность доказана.** `charge_usage` с одним `idempotence_key` дважды →
   ОДНА строка леджера, ОДНО списание. Тот же вебхук ЮKassa дважды → кошелёк
   пополнен ОДИН раз (`webhook_events.external_id`).
4. **Овердрафт исключён на гонках.** N параллельных `charge_usage` при кошельке
   на M<N списаний → ровно M успешны, N−M отклонены, баланс не ниже пола.
   Реальные параллельные транзакции (asyncpg pool), ассерт финального баланса.
5. **Секрет зашифрован и не утёк в логи.** `ciphertext` ≠ plaintext; расшифровка
   возвращает оригинал; **grep по логам прогона** (stdout/stderr приложений) на
   значение секрета — НОЛЬ совпадений.
6. **Tenant-изоляция.** Тенант A не читает кошелёк/леджер/секреты/лидов тенанта B
   ни через один маршрут панели (authz-тест на двух тенантах, включая прямые URL
   и подмену id в формах). RLS-тест на уровне SQL под ролью `panel_rw`.
7. **Школа не сломана.** После каждой волны: бот Школы отвечает, воронка живёт,
   панель Школы показывает её данные. (Регресс single-tenant → multi-tenant —
   главный операционный риск; первый тенант = прод с живыми лидами.)
8. **МАСТЕР-КРИТЕРИЙ — сквозной happy path.** Скрипт: создание тенанта →
   оператор тенанта входит в панель → топап кошелька реальным тестовым платежом
   (подтверждён `succeeded`) → сообщение тенантскому боту → ответ ИИ → леджер
   показывает cost×3 → кошелёк уменьшился → раздел «Расход» вернул новый баланс
   и запись. **Бинарно: проходит end-to-end без мока платежа и без мока леджера —
   или не сделано.**

---

## 9. Владение файлами (anti-conflict)

| Владелец | Владеет |
|---|---|
| **DB** | `db/*.sql` (схемы, миграции, гранты, RLS), `shared/vault.py`, `shared/money.py`, `shared/metering.py` |
| **Bot** | `bot-telegram/**` (multiplex, cost-capture в ai.py, снапшот-воркер, автосписания-cron) |
| **Panel** | `admin-panel/**` (tenant-context, billing.py, разделы Кошелёк/Расход/Ключи, селектор тенантов) |
| **Site** | `service-site/**` (тарифы/оплата — есть; ссылка «Войти в кабинет») |

Истинно необратимые решения (точность денег = целые µRUB; место множителя =
`plans`/сервер; направление округления = вверх; backfill Школы как первого
тенанта) логировать в `docs/DECISIONS.md`, не блокируясь на подтверждение.

---

## 10. Guardrails / ловушки (явно избегать)

- **Овердрафт на параллельных списаниях** → `SELECT ... FOR UPDATE` строки кошелька.
- **Двойное начисление на ретрае** → `webhook_events.external_id unique` +
  `usage_ledger.idempotence_key unique`. ЮKassa повторяет уведомления.
- **Дрейф float на деньгах** → только целые µRUB, единая `ceil_mul` (shared/money.py).
- **Множитель/цена из клиента** → только сервер (план). Иначе обнуляют маржу.
- **Секреты в логах** → структурное логирование с редакцией; grep-тест (§8.5);
  тело vault не логируется никогда. Прецедент дисциплины уже в проекте:
  `twc-set-env.sh` не печатает значения.
- **Автоплатежи ЮKassa не включены менеджером** → внешний блокер, проверить до Wave 2.
- **env приложений через UI Timeweb** → ЗАТИРАЕТ run_cmd; только `twc-set-env.sh`
  (грабля №2 проекта). Новые ключи (VAULT_MASTER_KEY) — только так.
- **cloud-ai не отдаёт usage per-call** → НЕ изобретать оценку токенов по длине
  текста; метеринг по дельте `used_tokens` (механика §5.2) либо gateway-бэкенд.
- **RLS-обход** → роль panel_rw без `bypassrls`; каждый запрос — после
  `set_config('app.tenant_id')`; тест §8.6 обязателен.
- **Слом прода Школы** → каждая волна заканчивается критерием §8.7; миграции
  идемпотентны; фолбэк app_settings → tenant_settings до конца переезда.
- **Claude Code рапортует стабы как готовое** → приёмка только по §8 (реальный
  платёж, реальная строка леджера), не по зелёным тестам.
- **Цены моделей из памяти** → перед записью в `model_prices` — проверка ЛК Timeweb.

---

## 11. Safety / deny (правила проекта, действуют поверх ТЗ)

- DDL — **только** `twc-migrate.sh` owner-DSN, **сначала `risuy_dev`**, затем прод,
  всегда ПЕРЕД пушем кода. Деструктивные миграции (drop/alter с потерей данных) —
  только с явной отмашки владельца.
- **Боевые/платёжные ключи и создание ПЛАТНЫХ ресурсов** (cloud-ai агенты,
  VM, live-ключи ЮKassa) — агент НЕ трогает сам: готовит команды владельцу.
- **LIVE-ключи ЮKassa никогда** не в репо и не в тестах. Тест-окружение — только
  креды ТЕСТОВОГО магазина (1379463).
- `VAULT_MASTER_KEY` — генерируется владельцем (`openssl rand -hex 32`), живёт
  только в env приложений; в репо/логах/чате не появляется.
- Деплой = push в main (мультиплекс); ~30 с TelegramConflictError бота — норма;
  билд бота ~9–10 мин (поллинг деплоя ≥720 с).

---

## 12. Карта адаптации (оригинал x10 → risuy)

| x10-ТЗ | Здесь | Почему |
|---|---|---|
| Next.js 16 + RSC/Server Actions | FastAPI + Jinja + PRG | стек панели уже существует и канонизирован |
| Drizzle ORM + миграции | сырой SQL `db/schema_*.sql` + `twc-migrate.sh` | schema-first правило проекта |
| Better Auth | расширение своей auth (argon2 + admin_sessions) | Python-проект; данные в РФ; lockout-гарантии уже построены |
| `@webzaytsev/yookassa-ts-sdk` | свой `yookassa.py` (stdlib) | уже в проде: чеки, два магазина, вебхук с re-fetch |
| packages/* (Turborepo) | `shared/` + модули в bot/panel | монорепо-пакетов нет; бот и панель делят shared-код |
| KikuAI Masker ДО шлюза (обязателен) | не обязателен: Timeweb = РФ-инфра | трансграничка не возникает; Masker — волна при зарубежных моделях |
| cost-capture по usage из ответа | gateway: usage есть; cloud-ai: дельта `used_tokens` | факт прода: /call не отдаёт usage (2026-06-12) |
| neon → Timeweb PG (Москва) | кластер 4171827 + `risuy_dev` база | уже на Timeweb; branching нет → dev-база на том же кластере |
| мини-аппы TG/VK/MAX | один TG-бот на тенанта (мультиплекс polling) | продукт risuy — бот+панель; вебхуки/MAX — волны масштабирования |
| тарифы только multiplier ×3 | `billing_mode`: ×3 (дефолт) ИЛИ per_message | действующий прайс «ИИ-Агент Про» продаёт сообщения |

---

## 13. Открытые вопросы владельцу (не блокируют Wave 0)

1. **Дефолт `billing_mode` для НОВЫХ клиентов:** ×3 от себестоимости (канон
   реселлера, прозрачен нам) или фикс за сообщение (понятнее клиенту, уже на
   витрине)? Wave 0 сеет оба режима, дефолт — ×3.
2. **Trial:** давать ли новые тенантам trial-период (status='trialing') и сколько
   дней/кредитов? Сейчас заложен механизм, не политика.
3. **Касса платформы для топапов** — тот же магазин подписки 1378536 или отдельный?
   (Технически готовы оба варианта; чеки уже работают.)
4. **Минимальный топап и потолок минуса** для postpaid (если будет) — цифры.

---

*Конец ТЗ v1.0-risuy. Изменения версионировать; необратимые решения — в `docs/DECISIONS.md`.*
