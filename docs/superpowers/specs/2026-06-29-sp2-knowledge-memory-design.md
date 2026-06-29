# Спека — СП-2: Знание/RAG + Долгая память для ИИ-команды

**Дата:** 2026-06-29
**Проект:** risuy-ecosystem · `bot-telegram` + `admin-panel` + `db`
**Статус:** дизайн одобрен (брейншторм 2026-06-29) → готов к плану реализации
**Связано:** roadmap `docs/superpowers/specs/2026-06-29-agent-team-departments-roadmap.md` (СП-2); опирается на СП-1 «ИИ-команда» (`da5f54e`) и A1 «ИИ-команда клиента» (`af7d85a`).
**Решение по объёму:** две компоненты (Знание и Память) в ОДНОЙ спеке по выбору владельца, но с чёткими границами — план исполняет их последовательно.

## 1. Проблема и цель

Настроенные через `/my-team` team-агенты **не видят базу знаний компании** и **не имеют долгой памяти**. RAG «пришит» только к старому одиночному School-боту; для команды теряется. База знаний — одна общая (без `tenant_id`), что и не даёт изоляции между клиентами.

**Цель СП-2:** дать каждому тенанту **свою базу знаний** (с изоляцией) и **долгую память** на агента, чтобы team-агенты отвечали с опорой на знание компании и контекст прошлых диалогов. РФ-резидентно (152-ФЗ): эмбеддер self-host TEI, хранение — РФ-кластер.

## 2. Текущее состояние (факты из кода)

- **KB одна общая, без tenant_id.** `db/schema_kb.sql`: `kb_documents`(title, source, **role_tag**, content) и `kb_chunks`(content, `embedding vector(768)`, metadata) — **колонки `tenant_id` НЕТ**. `role_tag` = слаг одной из платформенных `config.PERSONA_PRESETS` (пусто = общая справка).
- **RAG только в School-пути.** `bot-telegram/handlers.py:275-343` (`on_free_text`): `if ai_cfg["kb_enabled"]: kb.retrieve_context() → kb.augment() → ai.ask_ai(обогащённый)`. Конфиг — легаси `db.get_ai_overrides` (app_settings).
- **Team-путь без KB.** `bot-telegram/multiplex.py:185-246` (`t_text`): `resolve_team_agent_cfg(...)` → `ai.ask_ai(message.text, ...)` — **ни одного вызова `kb.*`**, сырой текст. Резолвер `bot-telegram/db.py:1490-1534` **хардкодит `"kb_enabled": False  # СП-2`**.
- **`kb_search`** (`bot-telegram/db.py:1817-1844`): фильтр `where (role_tag='' or role_tag=$2) and dist<=$3` — **без `tenant_id`** (=риск утечки, как только KB включат для мультиплекса).
- **Память — схема готова, движка нет.** `db/schema_team_agents.sql:44-58` `agent_memory`(`tenant_id`, `agent_id`, `kind`, content, `embedding vector(768)`, metadata) + индексы `(tenant_id,agent_id)`/hnsw/gin + **RLS tenant_isolation**. Читает/пишет **только** `scripts/team_agents_db_smoke.py`. Чекбокс `/my-team` «Долгая память (готовится)» персистит `memory_enabled`, но потребителя нет.
- **РФ-эмбеддер есть.** TEI `intfloat/multilingual-e5-base` (768), self-host; `kb.embed_query` (`query:`-префикс), `kb.augment` склеивает «📚 Справочные факты…». Контент в OpenAI/США НЕ уходит.
- **UI «Базы знаний»** (`/knowledge`) — сейчас **только платформа** (`is_platform` в `base.html`), School-scoped, без tenant_id.

## 3. Подход

1. **`tenant_id` + RLS в KB** (`kb_documents`, `kb_chunks`) — как уже сделано в `agent_memory`. Чистая изоляция; заодно закрывает дыру утечки.
2. **Единый helper обогащения** `build_augmented_context()` (новый, в `bot-telegram/kb.py` или `ai.py`) — собирает `[KB-чанки] + [память]` для запроса. **Общий для School- и team-пути** → устраняет корень расхождения (RAG больше не живёт только в одной ветке). School-путь рефакторится на него без смены поведения.
3. **РФ-эмбеддер TEI e5 (768)** переиспользуем и для KB, и для памяти — одна размерность, одна 152-ФЗ-постура.
4. **Per-agent тумблеры** `kb_enabled`/`memory_enabled` (симметрия) — резолвер их отдаёт, `multiplex.t_text` исполняет.

## 4. Дизайн

### 4.A Данные и миграции (`db/`, hand-written + owner-DSN)
- `+tenant_id uuid` в `kb_documents` и `kb_chunks` (FK `tenants(id)` on delete cascade) + индексы по `tenant_id` + **RLS политика `tenant_isolation`** (паттерн `nullif(current_setting('app.tenant_id',true),'')::uuid` как в `agent_memory`).
- `kb_search` фильтрует по `tenant_id` дополнительно к `role_tag`/дистанции.
- `+kb_enabled boolean not null default true` в `team_agents`.
- **Бэкфилл School-строк:** существующие `kb_documents`/`kb_chunks` → зарезервированный platform/School-scope (sentinel `tenant_id`; точное значение — по факту наличия School-тенанта в `tenants`, решается в плане). School-путь продолжает работать на этом scope.
- **`role_tag` интерпретируется В ПРЕДЕЛАХ tenant-scope** (конфликта нет, разделены `tenant_id`): под platform/School-sentinel — прежние персоны `config.PERSONA_PRESETS` (School-путь без изменений), под тенантом — слаги его team-агентов.
- pgvector уже включён; миграции идемпотентны, журнал.

### 4.B Знание/RAG для команды (`bot-telegram/`)
- `kb_search(embedding, tenant_id, agent_slug, top_k, max_distance)` — `where tenant_id=$t and (coalesce(metadata->>'role_tag','')='' or metadata->>'role_tag'=$slug) and (embedding<=>$q)<=$d`.
- В `multiplex.t_text`: после `resolve_team_agent_cfg` — если `cfg["kb_enabled"]`: `build_augmented_context(message.text, tenant_id, agent.slug)` → передать обогащённый текст в `ai.ask_ai`.
- Снять хардкод `kb_enabled=False` в `resolve_team_agent_cfg` → отдавать колонку агента.
- `role_tag` теперь = слаг team-агента тенанта (отдел): пусто = общая справка тенанта (все агенты), `=slug` = знание отдела.

### 4.C Долгая память (`bot-telegram/`)
- **Запись:** при длине диалога ≥ ⚙️порога (N сообщений / превышение окна) — суммаризация диалога (LLM **через PII-маску** `mask→LLM→unmask`) → `kb.embed` → `insert agent_memory(tenant_id, agent_id, kind='summary', content, embedding)`. (v1 — только `summary`; `fact`/`session` — задел.)
- **Ретрив:** если `cfg["memory_enabled"]` → `embed(query)` → поиск по `agent_memory` (RLS-scoped тенантом, `where agent_id=$a` + дистанция) → топ-сводки в `build_augmented_context`.
- Оживить чекбокс — «Долгая память» (убрать «готовится»).

### 4.D UI «Базы знаний» — оба контура (`admin-panel/`)
- Раздел `/knowledge` сделать видимым **и платформе** (под `active_tenant`, как `/my-team`), **и тенанту** (self-serve в его кабинете) — nav в обеих ветках `is_platform`; ролевое состояние «клиент не выбран» для платформы (CTA на «Клиенты»), бейдж активного клиента.
- Загрузка/список/удаление документов **per-tenant** (запись под RLS active_tenant) + ⚙️ опц. привязка документа к отделу (агенту) через `role_tag`. Ингест (`scripts/kb_ingest`/`admin-panel/kb.py`) тегирует чанки `tenant_id`+`role_tag`.
- В `/my-team` — добавить тумблеры `kb_enabled` (новый) рядом с `memory_enabled`.

### 4.E Сборка промпта (`bot-telegram/ai.py`)
- `_build_chat_messages`: порядок и явные разделители — `system_prompt агента` → `📚 Знание компании:` (KB-чанки) → `🧠 Контекст прошлых диалогов:` (сводки памяти) → недавняя история → текущий вопрос. Пусто — секция опускается. Лимиты длины (top-k KB, top-k память) ⚙️ конфигурируемые.

### 4.F 152-ФЗ и безопасность
- **Изоляция `tenant_id` (RLS)** и в KB, и в памяти → нет утечки чанков/сводок между клиентами (чинит найденную дыру).
- Сводки памяти **через PII-маску** перед LLM; хранение — РФ-кластер; эмбеддинг — РФ TEI. Контент в иностранные сервисы не уходит.
- ⚠️ **Боевой запуск тенантам гейтится планом #2** (РФ-LLM). СП-2 строится и проверяется на `risuy_dev`; флаг включения для тенанта не снимаем здесь.

## 5. Не делаем (YAGNI v1)
Гибридный поиск (BM25+вектор), реранкер, авто-извлечение фактов (только summary), кросс-агентная/кросс-диалоговая память, оркестрация (СП-4), метрики per-agent (отдельная задача — это не движок знания/памяти), авто-реиндексация по расписанию.

## 6. Тестирование и выкатка
- **Смоуки** (`.venv-smoke`, как СП-1): (a) `kb_search` tenant-scoped — клиент A НЕ видит чанки клиента B (RLS на `risuy_dev`); (b) память write→retrieve под RLS (A≠B); (c) резолвер отдаёт `kb_enabled`/`memory_enabled` (не хардкод); (d) `build_augmented_context` собирает KB+память в нужном порядке (pure); (e) render-смоук `/knowledge` (платформа/тенант/нет-клиента) + тумблеры `/my-team`.
- **3-линзовое адверсариальное ревью** (особо: security/изоляция tenant_id, отсутствие PII в не-РФ, отсутствие регрессии School-пути).
- Миграции owner-DSN ПЕРЕД кодом; деплой App Platform (push → авто-редеплой), как A1. Live-проверка route-уровня; боевой ИИ-ответ — на `risuy_dev`, не на проде тенанта (гейт #2).

## 7. Затрагиваемые файлы (по компонентам)
- **Данные:** `db/schema_kb.sql` (tenant_id+RLS), `db/schema_team_agents.sql` или новая миграция (team_agents.kb_enabled), `db/panel_role.sql` (гранты), backfill-скрипт.
- **Знание:** `bot-telegram/db.py` (`kb_search` +tenant, `resolve_team_agent_cfg` снять kb_enabled-хардкод), `bot-telegram/kb.py` (`build_augmented_context`), `bot-telegram/multiplex.py` (`t_text` — обогащение), `bot-telegram/handlers.py` (School-путь на общий helper), `bot-telegram/ai.py` (`_build_chat_messages` секции).
- **Память:** `bot-telegram/` (движок записи/ретрива памяти — модуль `memory.py` или в `kb.py`), `multiplex.py` (вызовы).
- **UI:** `admin-panel/app.py` + `templates/{base,knowledge,my_team}.html` (видимость /knowledge обоим, per-tenant загрузка, тумблер kb_enabled), `admin-panel/kb.py`/`scripts/kb_ingest*` (tenant-тегирование).
- **Тесты:** новые `scripts/*_smoke.py` + расширение `platform_team_access_smoke.py`.

## 8. Открытые вопросы
Нет (дефолты подтверждены: знание с привязкой к отделу через `role_tag=slug`; память v1 = только сводки). Точное значение sentinel-scope для School-KB бэкфилла — определяется в плане после проверки `tenants`.
