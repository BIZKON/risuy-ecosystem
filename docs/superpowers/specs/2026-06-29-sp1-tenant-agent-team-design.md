# Дизайн СП-1 — «Команда отделов»: per-tenant команда ИИ-агентов (фундамент)

**Дата:** 2026-06-29 · **Статус:** дизайн согласован владельцем (с правками) → spec-review → writing-plans → impl.
**Часть roadmap** `docs/superpowers/specs/2026-06-29-agent-team-departments-roadmap.md` — первый под-проект (фундамент).

## Цель
Тенант **self-serve в кабинете** собирает команду ИИ-агентов, привязанных к отделам: каждый агент = роль +
свой системный промпт + привязка к каналу + адрес эскалации отдела. Резолвер выбирает агента по слоям
**диалог > канал > дефолт тенанта**. Фундамент закладывает хуки для бесконечной памяти (на RAG-базе) и для
СП-2/3/4 (знание/действие/оркестрация ссылаются на стабильный `tenant_agents.id`).

## Принятые решения (брейншторм 2026-06-29)
1. **Модель данных — отдельная таблица `tenant_agents`** (с миграцией; прод-DDL за владельцем).
2. **Привязка агента к входящему — по каналу + переопределение на диалог** (зеркало Школьного
   `get_ai_overrides`; авто-маршрутизация — СП-4, не здесь).
3. **БЕЗ ЛИМИТА** на число агентов у тенанта (создаёт сколько нужно — отделы клиента нам неизвестны).
4. **Бесконечная память на RAG-базе** (паттерн Гермеса), особенно для оркестраторов: фундамент закладывает
   флаги + схему хранилища памяти на pgvector; движок чтения/записи — СП-2.
5. **Настраивает тенант сам** (self-serve), интеграторы по желанию.

## Архитектура

### 1. Таблица `tenant_agents` (новый DDL)
RLS-политика `tenant_isolation` по `current_setting('app.tenant_id')` (как `tenant_triggers`); гранты `panel_rw`
(select/insert/update/delete). Колонки:
```
id              uuid pk default gen_random_uuid()
tenant_id       uuid not null references tenants(id) on delete cascade
slug            text not null            -- стабильный id в рамках тенанта (каналы/диалоги/СП-2/3/4 ссылаются)
name            text not null            -- «Отдел продаж» / имя сотрудника (видно тенанту)
role_preset     text                     -- ключ PERSONA_PRESETS (liya/mark/sofya/gleb) либо null=custom
system_prompt   text                     -- свой промпт (лимит ~20k, как cloud_ai; НЕ 4000)
backend         text                     -- ai_backend (cloud_ai/gateway/yandexgpt-позже); null → дефолт тенанта
agent_id        text                     -- Timeweb cloud-ai access_id (если cloud_ai)
fallback_text   text
escalation_chat_id   text                -- адрес уведомления ОТДЕЛА (per-agent эскалация); null → общий тенанта
escalation_topic_id  int
is_default      boolean not null default false   -- агент по умолчанию (последний слой резолвера)
is_orchestrator boolean not null default false   -- агент-координатор (СП-4); включает память
memory_enabled  boolean not null default false   -- бесконечная память на RAG (СП-2 реализует движок)
enabled         boolean not null default true
position        int not null default 0
created_at timestamptz not null default now()
updated_at timestamptz not null default now()
unique (tenant_id, slug)
-- частичный уникальный индекс: один is_default на тенанта
create unique index tenant_agents_one_default on tenant_agents(tenant_id) where is_default;
```
- **Привязка к каналу** — БЕЗ DDL: ключ `tenant_settings.agent_for_channel__<source>` = `slug` (зеркало
  Школьного `ai_*__<source>`; `source` ∈ MESSENGERS/каналов).
- **Переопределение на диалог** — БЕЗ DDL: переиспользуем существующую колонку `leads.ai_persona` (хранит
  `slug` агента команды; семантика «персона на диалог» уже есть в Школе).

### 2. Резолвер агента
Расширить `bot-telegram/db.py::get_tenant_ai_overrides(tid, *, source=None, lead_agent_slug=None)`:
1. **диалог:** если `lead_agent_slug` задан и агент существует+enabled → он;
2. **канал:** иначе `tenant_settings.agent_for_channel__<source>` → агент по slug (enabled);
3. **дефолт:** иначе `is_default`-агент тенанта (enabled);
4. **легаси-фолбэк:** иначе текущие ключи `tenant_settings.ai_system_prompt/ai_agent_id/ai_backend`
   (для немигрированных/Школы) — поведение как сейчас, ничего не ломается.
Возвращает тот же dict-контракт (`enabled/backend/agent_id/model/system_prompt/fallback/kb_enabled` +
новые `agent_slug`, `escalation_chat_id/topic`, `is_orchestrator`, `memory_enabled`). Вызыватель
`bot-telegram/multiplex.py::t_text` пробрасывает `source` (канал) и `lead.ai_persona`. Логика слоёв —
зеркало `get_ai_overrides` (единый паттерн).

### 3. Бесконечная память на RAG-базе (фундамент здесь, движок — СП-2)
**Принцип (паттерн Гермеса, адаптирован под РФ-pgvector):** агент не ограничен окном контекста — старые ходы
суммаризируются → эмбеддятся → хранятся; на новом ходе извлекаются релевантные фрагменты по запросу +
последние N сообщений → собирается контекст. Особенно важно оркестраторам (накопление кросс-диалогового
контекста команды).
**Что закладывает СП-1:**
- Флаги `tenant_agents.is_orchestrator` / `memory_enabled` (выше).
- **Схема хранилища `agent_memory`** (новый DDL, зеркало `db/schema_kb.sql` — pgvector, тот же self-host
  TEI-эмбеддер e5, РФ; размерность вектора ДОЛЖНА совпасть с `kb_chunks`):
```
id          uuid pk default gen_random_uuid()
tenant_id   uuid not null references tenants(id) on delete cascade
agent_id    uuid not null references tenant_agents(id) on delete cascade   -- память приватна агенту
kind        text not null            -- 'summary' | 'fact' | 'session' (тип записи памяти)
text        text not null            -- человекочитаемый фрагмент памяти
embedding   vector(<dim kb_chunks>)  -- эмбеддинг для retrieval (HNSW cosine, как kb)
metadata    jsonb                    -- источник/диалог/время/токены
created_at  timestamptz not null default now()
-- RLS tenant_isolation; HNSW cosine index по embedding; GIN по metadata; index (tenant_id, agent_id)
```
**Что реализует СП-2 (не здесь):** движок записи (суммаризация хода → embed → insert) и чтения
(retrieve top-k по текущему запросу → augment контекста), интеграция в `ask_ai`/`multiplex`, маскировка
ПДн ПЕРЕД эмбеддингом, ретеншн. СП-1 только даёт схему+флаги, чтобы фундамент был цельным и миграция — одна.

### 4. Панель «ИИ-команда» (tenant-facing)
- Nav: ключ `my_team`, route `/my-team`, метка **«ИИ-команда»** (НЕ `team`/`/team` — то платформенная
  «Команда» операторов). Вне блока `{% if session.is_platform %}` (рядом с «Лид-магнит»/«Дожим»). Иконка
  в `nav_icon` (lucide users/bot) + метка в `NAV_TITLES`.
- Раздел заменяет/расширяет «Мой ИИ-сотрудник» (один → много). CRUD: список агентов (карточки как
  «ИИ-сотрудники»), добавить/изменить/удалить/вкл-выкл; поля — имя, роль (пресет), промпт, привязка к каналу,
  адрес эскалации отдела, дефолтный, (опц.) оркестратор+память. **Без лимита** числа агентов.
- Запись: `admin-panel/db.py::set_tenant_agent / delete_tenant_agent / set_default_agent / set_channel_agent`
  под `set_config('app.tenant_id')` + аудит (паттерн `tenant_triggers` CRUD). Гейт: operator пишет только
  свой активный тенант; платформенный супер — любой через switch.
- Шаблон `admin-panel/templates/my_team.html` + хелперы рендера (как `nurture.html`/`triggers`).

### 5. Миграция существующих тенантов
- **DDL:** `db/schema_tenant_agents.sql` (`tenant_agents` + `agent_memory` + RLS + гранты + индексы). Накат:
  dev → подтверждение → прод (за владельцем; owner-DSN).
- **Бэкфилл (idempotent):** для каждого тенанта с непустым `tenant_settings.ai_system_prompt` создать одну
  строку `tenant_agents` (slug=`default`, name из роли/«ИИ-сотрудник», is_default=true, backend/agent_id из
  текущих ключей). Скрипт `scripts/backfill_tenant_agents.py` (owner-DSN, idempotent). До бэкфилла резолвер
  мягко падает на легаси-ключи → действующие тенанты не ломаются.

### 6. 152-ФЗ и границы
- Периметр прежний: агент отвечает через `ask_ai` (PII-маскировка, бэкенд). Боевой запуск всей фичи
  **гейтится планом #2** (РФ-резидентный бэкенд).
- `agent_memory` хранит ПДн → RLS + ретеншн (СП-2) + **маскировка ПЕРЕД эмбеддингом** (СП-2). В СП-1 — только
  схема (данных не пишем).
- **Вне СП-1:** движок памяти (СП-2), знание/RAG-документы отдела (СП-2), tool-use/действие (СП-3),
  авто-маршрутизация/поведение оркестратора (СП-4).

## Тесты
- **Чистый смоук** `scripts/tenant_agents_resolver_smoke.py`: логика резолвера (диалог>канал>дефолт>легаси) на
  фейковых данных — без БД.
- **БД-смоук** `scripts/tenant_agents_db_smoke.py` на `risuy_dev` (FORCE RLS/panel_rw): 2 тенанта — CRUD видит
  только свои агенты (RLS), `unique(tenant_id,slug)` + один `is_default`, резолвер по каналу/диалогу/дефолту,
  бэкфилл создаёт default-агента из легаси-промпта идемпотентно, `agent_memory` создаётся и RLS-скоупится.

## Решённые микровопросы (дефолты утверждены владельцем 2026-06-29)
1. **Размерность вектора `agent_memory`** — взять из `db/schema_kb.sql` (совпасть с `kb_chunks`); решения не требует.
2. **Привязка базы знаний отдела (`kb_enabled`) — НЕ в СП-1**, целиком в СП-2 (чистое разделение).
3. **Переопределение агента на диалог** — реюз существующего механизма выбора персоны на лид (`leads.ai_persona`).
4. **Удаление агента — soft-delete (`enabled=false`)** по умолчанию (сохраняем память/аудит/целостность);
   hard-delete — позже. (Поэтому в CRUD «удалить» = выставить `enabled=false`, не DELETE строки в v1.)
