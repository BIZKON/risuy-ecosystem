# СП-2a — Знание/RAG для ИИ-команды (bot-side end-to-end) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** team-агенты, настроенные через `/my-team`, отвечают с опорой на **per-tenant базу знаний** (изоляция клиентов), с per-agent тумблером.

**Architecture:** Добавляем NULLABLE `tenant_id` в `kb_documents`/`kb_chunks` (NULL = платформенная/School-справка) + NULL-aware RLS. Бот (owner-роль, обходит RLS) фильтрует `tenant_id` ЯВНО — как уже делает `resolve_team_agent_cfg`. RAG подмешивается апстрим в `user_text` (как в School-пути), без правки `ai._build_chat_messages`.

**Tech Stack:** Python/asyncpg, pgvector, self-host TEI e5 (768), Jinja2-панель. Смоуки — standalone на `.venv-smoke`.

**Спека:** `docs/superpowers/specs/2026-06-29-sp2-knowledge-memory-design.md`

**Staging (Scope Check):** Этот план = **СП-2a** (DDL + bot-retrieval + резолвер + тумблер + tenant-scoped запись/ингест → агенты ВИДЯТ per-tenant KB, проверяется смоуком с seed-данными). **СП-2b** (полный UI `/knowledge` обоим контурам: платформа-под-клиента + tenant self-serve) и **СП-2-память** — отдельными планами.

## Global Constraints
- **Только русский** в UI-текстах/коммитах/комментариях.
- **РФ-резидентность:** эмбеддер TEI (НЕ OpenAI), хранение — РФ-кластер. Без новых зависимостей.
- Миграции — hand-written, идемпотентные (`IF NOT EXISTS`), owner-DSN ПЕРЕД кодом: `bash ~/.claude/scripts/twc-migrate.sh 4171827 81.31.246.136 risuy gen_user <файл.sql>` (значения DSN — у владельца; **owner-DSN даёт владелец**).
- Смоук-ранер: `PYTHONPATH=admin-panel:bot-telegram:. ./.venv-smoke/bin/python scripts/<name>.py`; DB-смоуки ТОЛЬКО на `risuy_dev` (assert в скрипте).
- Коммиты локально; **прод-DDL, push, деплой — ТОЛЬКО по явному «да» владельца**.
- Перед финальным коммитом — **3-линзовое адверсариальное ревью** (особо security/изоляция), 0 critical/high.
- Бот — owner-роль (обходит RLS) → tenant-фильтр в SQL ЯВНЫЙ. RLS — защита panel_rw.

## File Structure
- `db/schema_kb.sql` — +`tenant_id` (nullable) в `kb_documents`/`kb_chunks`, NULL-aware RLS, индексы (Task 1).
- `db/schema_team_agents.sql` — +`team_agents.kb_enabled` (Task 1).
- `bot-telegram/db.py` — `kb_search(..., tenant_id=...)`, `resolve_team_agent_cfg` отдаёт реальный `kb_enabled` (Task 2).
- `bot-telegram/kb.py` — `retrieve_context(text, tenant_id, persona=...)` (Task 2).
- `bot-telegram/multiplex.py` — `t_text` обогащает `user_text` при `kb_enabled` (Task 3).
- `bot-telegram/handlers.py` — School-путь передаёт платформенный scope (`tenant_id=None`) (Task 3).
- `admin-panel/db.py` — `kb_insert_document(..., tenant_id=...)`, `kb_list_documents(tenant_id=...)`, `kb_delete_document` scoped (Task 4).
- `admin-panel/app.py` + `templates/my_team.html` — тумблер `kb_enabled` на агента (Task 5).
- `scripts/kb_tenant_isolation_smoke.py` — изоляция A≠B (Task 3); `scripts/kb_ingest.py` — +tenant (Task 4).

---

### Task 1: DDL — per-tenant KB (nullable tenant_id + NULL-aware RLS) + team_agents.kb_enabled

**Files:**
- Modify: `db/schema_kb.sql` (после определения таблиц/индексов)
- Modify: `db/schema_team_agents.sql` (рядом с `team_agents`)
- Test: применение через `twc-migrate.sh` на `risuy_dev` + проверка колонок

**Interfaces:**
- Produces: колонки `kb_documents.tenant_id`/`kb_chunks.tenant_id` (uuid NULL), `team_agents.kb_enabled` (bool not null default true); RLS-политики `tenant_isolation` на kb-таблицах.

- [ ] **Step 1: Дописать DDL в `db/schema_kb.sql`** (в конец файла, идемпотентно)

```sql
-- ── СП-2a: per-tenant изоляция базы знаний. tenant_id NULL = платформенная/School-справка
-- (видна owner-боту School-пути; в панели — только при отсутствии активного клиента). Не-NULL
-- = знание конкретного тенанта. Бэкфилл не нужен: существующие School-строки остаются NULL.
alter table kb_documents add column if not exists tenant_id uuid references tenants(id) on delete cascade;
alter table kb_chunks    add column if not exists tenant_id uuid references tenants(id) on delete cascade;
create index if not exists kb_documents_tenant_idx on kb_documents (tenant_id);
create index if not exists kb_chunks_tenant_idx    on kb_chunks (tenant_id);

-- NULL-aware RLS: нет активного клиента (app.tenant_id пуст) → видны/пишутся ТОЛЬКО NULL-строки
-- (платформа/School); активный клиент → ТОЛЬКО его строки. Бот (owner) обходит RLS и фильтрует явно.
do $$ begin
  if not exists (select 1 from pg_policies where tablename='kb_documents' and policyname='tenant_isolation') then
    create policy tenant_isolation on kb_documents for all
      using (case when nullif(current_setting('app.tenant_id', true), '') is null
                  then tenant_id is null
                  else tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid end)
      with check (case when nullif(current_setting('app.tenant_id', true), '') is null
                  then tenant_id is null
                  else tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid end);
  end if;
end $$;
alter table kb_documents enable row level security;
do $$ begin
  if not exists (select 1 from pg_policies where tablename='kb_chunks' and policyname='tenant_isolation') then
    create policy tenant_isolation on kb_chunks for all
      using (case when nullif(current_setting('app.tenant_id', true), '') is null
                  then tenant_id is null
                  else tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid end)
      with check (case when nullif(current_setting('app.tenant_id', true), '') is null
                  then tenant_id is null
                  else tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid end);
  end if;
end $$;
alter table kb_chunks enable row level security;
```

- [ ] **Step 2: Дописать `team_agents.kb_enabled` в `db/schema_team_agents.sql`** (после блока `agent_memory`/RLS, идемпотентно)

```sql
-- СП-2a: per-agent тумблер базы знаний (симметрично memory_enabled). default true.
alter table team_agents add column if not exists kb_enabled boolean not null default true;
```

- [ ] **Step 3: Применить на `risuy_dev` (owner-DSN от владельца)**

Run (owner-DSN предоставляет владелец; на `risuy_dev`):
`bash ~/.claude/scripts/twc-migrate.sh <id> <host> risuy_dev gen_user db/schema_kb.sql`
затем `... db/schema_team_agents.sql`
Expected: `migrations applied successfully` (идемпотентно — повтор без ошибок).

- [ ] **Step 4: Проверить колонки**

Run: `bash ~/.claude/scripts/twc-migrate.sh <id> <host> risuy_dev gen_user <(echo "select column_name from information_schema.columns where table_name in ('kb_documents','kb_chunks','team_agents') and column_name in ('tenant_id','kb_enabled');")`
Expected: 3 строки (`kb_documents.tenant_id`, `kb_chunks.tenant_id`, `team_agents.kb_enabled`).

- [ ] **Step 5: Commit**
```bash
cd ~/Downloads/risuy-ecosystem
git add db/schema_kb.sql db/schema_team_agents.sql
git commit -m 'feat(db): СП-2a — per-tenant KB (nullable tenant_id + NULL-aware RLS) + team_agents.kb_enabled' \
  -m 'Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>'
```

---

### Task 2: Бот — tenant-scoped retrieval + резолвер отдаёт реальный kb_enabled

**Files:**
- Modify: `bot-telegram/db.py` (`kb_search` ~1817; `resolve_team_agent_cfg` :1528)
- Modify: `bot-telegram/kb.py` (`retrieve_context` :56)

**Interfaces:**
- Consumes: `team_agents.kb_enabled` (Task 1), `kb_chunks.tenant_id` (Task 1).
- Produces: `db.kb_search(embedding, tenant_id, persona=None, *, top_k=4, max_distance=0.55)`; `kb.retrieve_context(text, tenant_id, persona=None)`; `cfg["kb_enabled"]` = реальная колонка.

- [ ] **Step 1: `kb_search` — добавить tenant_id-фильтр** (`bot-telegram/db.py`, заменить сигнатуру и SQL)

```python
async def kb_search(
    embedding: list[float], tenant_id, persona: str | None = None,
    *, top_k: int = 4, max_distance: float = 0.55,
) -> list[str]:
    """Top-k чанков базы ЗНАНИЙ ТЕНАНТА (tenant_id; None = платформенная/School-справка) по
    косинусной близости + фильтр по отделу (role_tag). Бот — owner-роль (обходит RLS) → tenant
    фильтруем ЯВНО. Общая справка тенанта (role_tag пуст) видна всем его агентам; чанк отдела —
    только агенту с этим slug. Сбой/нет таблицы → исключение (kb.retrieve_context ловит)."""
    if not embedding or pool is None:
        return []
    vec = "[" + ",".join(f"{x:.6f}" for x in embedding) + "]"
    per = (persona or "").strip()
    async with pool.acquire() as c:
        rows = await c.fetch(
            """
            select content
              from kb_chunks
             where embedding is not null
               and tenant_id is not distinct from $2
               and (coalesce(metadata->>'role_tag', '') = '' or metadata->>'role_tag' = $3)
               and (embedding <=> $1::vector) <= $4
             order by embedding <=> $1::vector
             limit $5
            """,
            vec, tenant_id, per, max_distance, top_k,
        )
    return [r["content"] for r in rows]
```
(`is not distinct from $2` корректно матчит и NULL=NULL для School-scope, и uuid для тенанта.)

- [ ] **Step 2: `retrieve_context` — прокинуть tenant_id** (`bot-telegram/kb.py`)

```python
async def retrieve_context(text: str, tenant_id, persona: str | None = None) -> str:
    """Блок справки для подмешивания (или "" — если RAG пуст). tenant_id=None → платформенная
    справка (School). Фильтр по отделу: общая справка тенанта + чанки персоны/отдела."""
    vec = await embed_query(text)
    if not vec:
        return ""
    try:
        chunks = await db.kb_search(vec, tenant_id, persona)
    except Exception as e:
        logger.warning("kb_search не удался: %s", e)
        return ""
    if not chunks:
        return ""
    body = "\n\n".join(f"• {c.strip()}" for c in chunks)
    return (
        "📚 Справочные факты из базы знаний (опирайся на них, не придумывай сверх них; "
        "если ответа в фактах нет — так и скажи):\n\n" + body
    )
```

- [ ] **Step 3: Резолвер — отдать реальный kb_enabled** (`bot-telegram/db.py`, в `resolve_team_agent_cfg`)

Заменить строку `"kb_enabled": False,                       # СП-2` на:
```python
        "kb_enabled": bool(picked["kb_enabled"]),
```
И добавить `kb_enabled` в `_TEAM_AGENT_COLS` (если его там нет — проверить набор колонок и дописать `kb_enabled`).

- [ ] **Step 4: Обновить School-вызов retrieve_context** (`bot-telegram/handlers.py:325`)

Заменить `kb_context = await kb.retrieve_context(user_text, lead_persona)` на:
```python
        kb_context = await kb.retrieve_context(user_text, None, lead_persona)  # None = платформенная справка School
```

- [ ] **Step 5: Прогон тестов бота**

Run: `cd ~/Downloads/risuy-ecosystem && PYTHONPATH=bot-telegram:. ./.venv-smoke/bin/python -c "import ast; ast.parse(open('bot-telegram/db.py').read()); ast.parse(open('bot-telegram/kb.py').read()); ast.parse(open('bot-telegram/handlers.py').read()); print('parse OK')"`
Expected: `parse OK` (синтаксис; поведенческие проверки — Task 3-смоук).

- [ ] **Step 6: Commit**
```bash
git add bot-telegram/db.py bot-telegram/kb.py bot-telegram/handlers.py
git commit -m 'feat(bot): СП-2a — kb_search/retrieve_context per-tenant; резолвер отдаёт kb_enabled' \
  -m 'Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>'
```

---

### Task 3: Подключить RAG к team-пути + смоук изоляции тенантов

**Files:**
- Modify: `bot-telegram/multiplex.py` (`t_text` ~234, перед `ask_ai`)
- Create: `scripts/kb_tenant_isolation_smoke.py`

**Interfaces:**
- Consumes: `cfg["kb_enabled"]`, `cfg["agent_slug"]` (Task 2), `db.kb_search` (Task 2).

- [ ] **Step 1: Смоук изоляции (упадёт — retrieval ещё не подключён к team, но изоляция kb_search проверяема сразу)**

Создать `scripts/kb_tenant_isolation_smoke.py`:
```python
#!/usr/bin/env python3
"""DB-смоук СП-2a: kb_search видит ТОЛЬКО чанки своего тенанта (изоляция A≠B) на risuy_dev.
  TEAM_DSN="postgresql://gen_user:<pw>@<host>:5432/risuy_dev?sslmode=require" \
  PYTHONPATH=bot-telegram:. ./.venv-smoke/bin/python scripts/kb_tenant_isolation_smoke.py
"""
import asyncio, os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bot-telegram"))
os.environ.setdefault("DATABASE_URL", os.environ.get("TEAM_DSN", "postgresql://x/y"))
import asyncpg  # noqa: E402
import db as bdb  # noqa: E402 (bot-telegram/db.py)
DSN = os.environ["DATABASE_URL"]; assert "/risuy_dev" in DSN.split("?")[0], "только risuy_dev"
FAILS = []
def check(n, c): print(f"  {'OK ' if c else 'FAIL'} {n}"); (FAILS.append(n) if not c else None)
VEC = [0.01] * 768  # фиктивный вектор; max_distance большой → вернёт всё в скоупе
async def main():
    bdb.pool = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    async with bdb.pool.acquire() as c:
        ta = await c.fetchval("insert into tenants(slug,name,status) values('kb-smoke-a','A','active') returning id")
        tb = await c.fetchval("insert into tenants(slug,name,status) values('kb-smoke-b','B','active') returning id")
        da = await c.fetchval("insert into kb_documents(tenant_id,title,content) values($1,'A','a') returning id", ta)
        db_ = await c.fetchval("insert into kb_documents(tenant_id,title,content) values($1,'B','b') returning id", tb)
        await c.execute("insert into kb_chunks(tenant_id,document_id,chunk_index,content,embedding) values($1,$2,0,'ФАКТ-A',$3::vector)", ta, da, "["+",".join("0.01" for _ in range(768))+"]")
        await c.execute("insert into kb_chunks(tenant_id,document_id,chunk_index,content,embedding) values($1,$2,0,'ФАКТ-B',$3::vector)", tb, db_, "["+",".join("0.01" for _ in range(768))+"]")
    try:
        a_hits = await bdb.kb_search(VEC, ta, top_k=10, max_distance=2.0)
        b_hits = await bdb.kb_search(VEC, tb, top_k=10, max_distance=2.0)
        plat = await bdb.kb_search(VEC, None, top_k=10, max_distance=2.0)
        check("A видит ФАКТ-A", "ФАКТ-A" in a_hits)
        check("A НЕ видит ФАКТ-B (изоляция)", "ФАКТ-B" not in a_hits)
        check("B видит ФАКТ-B, не A", ("ФАКТ-B" in b_hits) and ("ФАКТ-A" not in b_hits))
        check("платформа (None) не видит чанки тенантов", ("ФАКТ-A" not in plat) and ("ФАКТ-B" not in plat))
    finally:
        async with bdb.pool.acquire() as c:
            await c.execute("delete from tenants where slug in ('kb-smoke-a','kb-smoke-b')")  # cascade чистит kb_*
    print("ВСЕ ОК" if not FAILS else "ПРОВАЛЫ: " + ", ".join(FAILS)); sys.exit(1 if FAILS else 0)
asyncio.run(main())
```

- [ ] **Step 2: Запустить смоук — изоляция проходит (kb_search уже tenant-scoped из Task 2)**

Run: `cd ~/Downloads/risuy-ecosystem && TEAM_DSN="<owner-dsn risuy_dev>" PYTHONPATH=bot-telegram:. ./.venv-smoke/bin/python scripts/kb_tenant_isolation_smoke.py`
Expected: `ВСЕ ОК` (4 проверки). Если FAIL «A видит ФАКТ-B» — фильтр tenant_id в kb_search не сработал, чинить Task 2 Step 1.

- [ ] **Step 3: Подключить RAG к team-пути** (`bot-telegram/multiplex.py`, в `t_text` ПЕРЕД блоком history ~232)

Вставить:
```python
    # СП-2a: RF-RAG для team-агента — справка из базы знаний ТЕНАНТА (фильтр по отделу = slug
    # агента). Тумблер kb_enabled на агента; эмбеддер недоступен/база пуста → текст без изменений.
    user_text = message.text
    if cfg.get("kb_enabled"):
        kb_context = await kb.retrieve_context(user_text, db.tenant_id(), cfg.get("agent_slug"))
        user_text = kb.augment(user_text, kb_context)
```
И заменить вызов `ai.ask_ai(message.text, None, cfg, history=history)` на `ai.ask_ai(user_text, None, cfg, history=history)`. Убедиться, что `import kb` есть вверху `multiplex.py` (если нет — добавить).

- [ ] **Step 4: Парс-проверка**

Run: `cd ~/Downloads/risuy-ecosystem && ./.venv-smoke/bin/python -c "import ast; ast.parse(open('bot-telegram/multiplex.py').read()); print('parse OK')"`
Expected: `parse OK`.

- [ ] **Step 5: Commit**
```bash
git add bot-telegram/multiplex.py scripts/kb_tenant_isolation_smoke.py
git commit -m 'feat(bot): СП-2a — RAG подключён к team-пути (multiplex.t_text) + смоук изоляции тенантов' \
  -m 'Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>'
```

---

### Task 4: Tenant-scoped запись/ингест KB (чтобы посеять знание клиента)

**Files:**
- Modify: `admin-panel/db.py` (`kb_insert_document` :1848, `kb_list_documents` :1832, `kb_delete_document` :1884)
- Modify: `scripts/kb_ingest.py` (передавать tenant_id)

**Interfaces:**
- Produces: `kb_insert_document(..., tenant_id=None)` пишет `tenant_id` в `kb_documents` и `kb_chunks`; `kb_list_documents(tenant_id=None)`, `kb_delete_document(... tenant_id=None)` scoped.

- [ ] **Step 1: Прочитать текущие функции и добавить tenant_id-параметр**

Прочитать `admin-panel/db.py:1832-1900` (точные сигнатуры/SQL). Затем:
- `kb_insert_document(...)` → добавить параметр `tenant_id=None`; в `insert into kb_documents(... tenant_id ...)` и в каждый `insert into kb_chunks(... tenant_id ...)` добавить колонку `tenant_id` со значением параметра.
- `kb_list_documents(tenant_id=None)` → `where tenant_id is not distinct from $1`.
- `kb_delete_document(doc_id, tenant_id=None, ...)` → `where id=$1 and tenant_id is not distinct from $2`.

(Показать конкретный диф по факту чтения — SQL дописывается точечно.)

- [ ] **Step 2: `scripts/kb_ingest.py` — принимать `--tenant <uuid>`** (default None=платформа) и прокидывать в `kb_insert_document(tenant_id=...)`.

- [ ] **Step 3: Парс-проверка**
Run: `./.venv-smoke/bin/python -c "import ast; ast.parse(open('admin-panel/db.py').read()); ast.parse(open('scripts/kb_ingest.py').read()); print('parse OK')"`
Expected: `parse OK`.

- [ ] **Step 4: Commit**
```bash
git add admin-panel/db.py scripts/kb_ingest.py
git commit -m 'feat(panel): СП-2a — tenant-scoped запись/ингест KB (kb_insert_document/list/delete + kb_ingest --tenant)' \
  -m 'Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>'
```

---

### Task 5: Тумблер kb_enabled на агента в /my-team + 3-линзовое ревью

**Files:**
- Modify: `admin-panel/templates/my_team.html` (рядом с `memory_enabled`, обе формы)
- Modify: `admin-panel/app.py` (`my_team_save` — принять `kb_enabled`), `admin-panel/db.py` (`upsert_team_agent` — писать `kb_enabled`)
- Modify: `scripts/platform_team_access_smoke.py` (проверка рендера тумблера)

- [ ] **Step 1: Добавить проверку рендера тумблера в смоук (упадёт)**

В `scripts/platform_team_access_smoke.py` (после check #7) добавить: рендер с агентом, у которого `kb_enabled`, и проверка чекбокса «База знаний» в карточке. (Использует `render(... agents=[{...kb_enabled:True...}])`.)

- [ ] **Step 2: Запустить — fail.** Run: `... scripts/platform_team_access_smoke.py` → FAIL.

- [ ] **Step 3: Шаблон — чекбокс kb_enabled** (`my_team.html`, рядом с memory_enabled в карточке редактирования и в форме добавления):
```html
    <label class="field field--check"><input type="checkbox" name="kb_enabled" value="1"{% if a.kb_enabled %} checked{% endif %}><span class="field__label">База знаний компании</span></label>
```
(в форме «Добавить агента» — без `{% if a... %}`, по умолчанию отмечен: `checked`).

- [ ] **Step 4: Handler + db** — в `my_team_save` (`app.py`) принять `kb_enabled: str = Form("")` и передать `kb_enabled=bool(kb_enabled)` в `db.upsert_team_agent`; в `upsert_team_agent` (`admin-panel/db.py`) добавить колонку `kb_enabled` в INSERT...ON CONFLICT (по образцу `memory_enabled`). ⚠️ В форме ДОБАВЛЕНИЯ кладём дефолт checked → новый агент с KB включён.

- [ ] **Step 5: Запустить смоук — зелено.** Run: `... scripts/platform_team_access_smoke.py` → `ВСЕ ОК`.

- [ ] **Step 6: 3-линзовое адверсариальное ревью** диффа `git diff <baseline>..HEAD` по линзам: **security** (изоляция tenant_id в kb_search/insert/list/delete; нет утечки A↔B; School-путь не сломан; RLS NULL-aware корректна); **correctness** (kb_enabled проброшен резолвером→multiplex; augment апстрим; парсинг); **152-ФЗ/RU** (эмбеддер РФ; контент не в иностранные сервисы; тексты русские). Внести critical/high, повторить смоуки. 0 critical/high.

- [ ] **Step 7: Commit**
```bash
git add admin-panel/templates/my_team.html admin-panel/app.py admin-panel/db.py scripts/platform_team_access_smoke.py
git commit -m 'feat(panel): СП-2a — тумблер «База знаний» на агента в /my-team (+ smoke)' \
  -m 'Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>'
```

- [ ] **Step 8: Отчёт + гейт** — СП-2a готов локально (агенты видят per-tenant KB; изоляция-смоук зелёный). **Прод-DDL (`twc-migrate.sh` на проде risuy), push, деплой — по явному «да» владельца.** Затем СП-2b (UI /knowledge обоим контурам) и СП-2-память — отдельными планами.

---

## Self-Review

**1. Spec coverage (СП-2a-часть):**
- §4.A tenant_id+RLS в kb + team_agents.kb_enabled → Task 1 ✅ (nullable+NULL-aware RLS — уточнение sentinel из §8 спеки)
- §4.B kb_search по тенанту+отделу, multiplex обогащение, снять kb_enabled-хардкод → Task 2-3 ✅
- §4.F изоляция tenant_id (чинит дыру утечки) → Task 1 RLS + Task 2 явный фильтр + Task 3 смоук ✅
- per-agent тумблер kb_enabled → Task 5 ✅
- tenant-scoped запись (чтобы посеять знание) → Task 4 ✅
- §4.D полный UI /knowledge обоим контурам → **вынесено в СП-2b** (отдельный план); §4.C память → **СП-2-память** ✅ (staging задекларирован)
- §4.E sectioned prompt — НЕ требуется: augment апстрим даёт labeled-блок (как School) ✅

**2. Placeholder scan:** Task 4 Step 1 просит «прочитать и дописать SQL по факту» — это намеренно (точный диф зависит от текущих сигнатур; функции локализованы `admin-panel/db.py:1848-1900`), не вольный placeholder. Остальное — конкретный код. DSN-плейсхолдеры (`<id>`/`<host>`/`<owner-dsn>`) подставляет владелец (секреты не в плане).

**3. Type consistency:** `kb_search(embedding, tenant_id, persona=None, ...)` — единая сигнатура в Task 2 (db) и вызовах Task 2 (kb), Task 3 (smoke); `retrieve_context(text, tenant_id, persona=None)` — Task 2 (def), Task 2 Step 4 (School), Task 3 (multiplex). `cfg["kb_enabled"]`/`cfg["agent_slug"]` — Task 2 (резолвер) → Task 3 (multiplex). Согласовано.
