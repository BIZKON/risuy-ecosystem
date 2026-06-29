# СП-1 «Команда отделов» — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Дать тенанту self-serve команду ИИ-агентов по отделам (таблица `team_agents`), резолвер выбора агента (диалог>канал>дефолт>легаси), панель «ИИ-команда» (CRUD), миграцию-бэкфилл и заложить схему бесконечной памяти (`agent_memory`, движок — СП-2).

**Architecture:** Новая RLS-таблица `team_agents` (зеркало паттерна `tenant_triggers`) + pgvector-таблица `agent_memory` (зеркало `kb_chunks`, движок в СП-2). Бот получает агента через НОВУЮ аддитивную функцию `resolve_team_agent_cfg` (легаси `get_tenant_ai_overrides` остаётся фолбэком — ничего не ломается). Панель — раздел `/my-team` по паттерну `/triggers`.

**Tech Stack:** Python (aiogram-бот + FastAPI/Jinja-панель), asyncpg, Timeweb Managed Postgres (pgvector), смоук-скрипты (idiom репо).

**Спека:** `docs/superpowers/specs/2026-06-29-sp1-tenant-agent-team-design.md`. **Roadmap:** `docs/superpowers/specs/2026-06-29-agent-team-departments-roadmap.md`.

## Global Constraints
- **Только русский** (UI/комменты/докстринги/коммиты); латиница — идентификаторы/пути/SQL.
- ⚠️ **Имя `tenant_agents` ЗАНЯТО** (Wave-3 метеринг-реестр `db/schema_metering_w3.sql`) → таблица команды = **`team_agents`**.
- **БЕЗ ЛИМИТА** числа агентов на тенанта.
- **Аддитивность:** `get_tenant_ai_overrides` НЕ удаляем (4 вызывателя: `multiplex.t_text/t_document` + VK/MAX-ветки) — новый резолвер фолбэчит на него. В СП-1 проводим слои ТОЛЬКО в `t_text` (TG живой; VK/MAX не подняты — слои там = следующий инкремент).
- **RLS:** новые таблицы tenant-scoped, политика `tenant_isolation` по `nullif(current_setting('app.tenant_id', true), '')::uuid`; гранты `panel_rw` + зеркало в `db/panel_role.sql` (иначе `revoke all` при реконсиляции Timeweb снимет грант). Бот ходит owner-ролью `gen_user` (RLS обходит, фильтрует `tenant_id` явно).
- **Soft-delete** агента (`enabled=false`), не DELETE строки (сохраняем память/аудит).
- **Прод-DDL и push — за владельцем** (явное «да»). Смоуки — `risuy_dev` (owner-DSN inline, VPN выкл). Перед коммитом — адверсариальное ревью 3 линзы + зелёные смоуки.
- **152-ФЗ:** периметр прежний; боевой запуск всей фичи гейтится планом #2; `agent_memory` данных в СП-1 НЕ пишет (только схема).
- **graphify** до grep; `agent_memory.embedding` = `vector(768)` (совпасть с `kb_chunks`).

---

## File Structure
- **Create** `db/schema_team_agents.sql` — DDL `team_agents` + `agent_memory` + RLS + индексы + гранты.
- **Modify** `db/panel_role.sql` — зеркало грантов на 2 новые таблицы (после `revoke all`).
- **Modify** `bot-telegram/db.py` — `_pick_team_agent` (чистый) + `resolve_team_agent_cfg` (новый резолвер).
- **Modify** `bot-telegram/multiplex.py` — `t_text`: пробросить source/persona в новый резолвер.
- **Modify** `admin-panel/db.py` — CRUD `team_agents` (list/upsert/set_default/set_channel_agent/disable).
- **Modify** `admin-panel/app.py` — раздел `/my-team` (GET + POST add/edit/disable/default/channel).
- **Create** `admin-panel/templates/my_team.html` — список+формы (зеркало `triggers.html`).
- **Modify** `admin-panel/templates/base.html` — nav `my_team` (заменяет `my_agent` в tenant-ветке) + `nav_icon` + `NAV_TITLES`.
- **Create** `scripts/backfill_team_agents.py` — бэкфилл default-агента из легаси `ai_system_prompt`.
- **Create** `scripts/team_agents_resolver_smoke.py` — чистый смоук слоёв резолвера.
- **Create** `scripts/team_agents_db_smoke.py` — БД-смоук на `risuy_dev` (RLS/CRUD/резолвер/бэкфилл/память).

---

## Task 1: DDL — таблицы `team_agents` + `agent_memory`

**Files:**
- Create: `db/schema_team_agents.sql`
- Modify: `db/panel_role.sql` (после блока `revoke all on all tables in schema public from panel_rw;`)
- Test: применение на `risuy_dev` + проверочный запрос

**Interfaces:**
- Produces: таблицы `team_agents`/`agent_memory` (RLS, гранты `panel_rw`), на которые ссылаются Tasks 2-4.

- [ ] **Step 1: Написать `db/schema_team_agents.sql`**

```sql
-- СП-1 «Команда отделов» (docs/superpowers/specs/2026-06-29-sp1-tenant-agent-team-design.md):
-- per-tenant команда ИИ-агентов по отделам (роль/промпт/привязка/эскалация) + схема бесконечной
-- памяти (agent_memory, pgvector; движок — СП-2). Имя tenant_agents ЗАНЯТО (метеринг w3) → team_agents.
--
-- tenant-scoped, RLS deny-by-default (как tenant_triggers): панель пишет ПОСЛЕ set_config('app.tenant_id');
-- бот (owner gen_user) обходит RLS, фильтрует tenant_id явно. FORCE НЕ ставим.
--
-- ПРИМЕНЕНИЕ: bash ~/.claude/scripts/twc-migrate.sh 4171827 81.31.246.136 <db> gen_user \
--   /Users/konstantin/Downloads/risuy-ecosystem/db/schema_team_agents.sql   — СНАЧАЛА risuy_dev.
-- Идемпотентно (IF NOT EXISTS). Новые таблицы → RLS включаем сразу (нет существующих читателей/данных).

create extension if not exists "pgcrypto";  -- gen_random_uuid()
create extension if not exists "vector";    -- pgvector (для agent_memory.embedding)

create table if not exists team_agents (
    id              uuid primary key default gen_random_uuid(),
    tenant_id       uuid not null references tenants(id) on delete cascade,
    slug            text not null,                       -- стабильный id агента в рамках тенанта
    name            text not null default '',            -- «Отдел продаж» / имя (видно тенанту)
    role_preset     text,                                -- ключ PERSONA_PRESETS либо null=custom
    system_prompt   text    not null default '',
    backend         text,                                -- null → дефолт тенанта (get_tenant_ai_overrides)
    agent_id        text    not null default '',         -- Timeweb cloud-ai access_id (если cloud_ai)
    fallback_text   text    not null default '',
    escalation_chat_id  text not null default '',        -- адрес уведомления ОТДЕЛА; '' → общий тенанта
    escalation_topic_id int,
    is_default      boolean not null default false,
    is_orchestrator boolean not null default false,      -- СП-4
    memory_enabled  boolean not null default false,      -- бесконечная память (движок — СП-2)
    enabled         boolean not null default true,
    position        int     not null default 0,
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now(),
    unique (tenant_id, slug)
);

create index if not exists team_agents_lookup_idx on team_agents (tenant_id, enabled);
-- один is_default на тенанта (частичный уникальный индекс)
create unique index if not exists team_agents_one_default_idx
    on team_agents (tenant_id) where is_default;

-- Бесконечная память агента (pgvector, зеркало kb_chunks: vector(768) = intfloat/multilingual-e5-base).
-- СП-1 создаёт ТОЛЬКО схему; запись/чтение (суммаризация→embed→retrieve) — СП-2.
create table if not exists agent_memory (
    id          uuid primary key default gen_random_uuid(),
    tenant_id   uuid not null references tenants(id) on delete cascade,
    agent_id    uuid not null references team_agents(id) on delete cascade,
    kind        text not null default 'summary',         -- 'summary' | 'fact' | 'session'
    content     text not null,
    embedding   vector(768),                             -- intfloat/multilingual-e5-base (как kb_chunks)
    metadata    jsonb not null default '{}'::jsonb,
    created_at  timestamptz not null default now()
);
create index if not exists agent_memory_scope_idx on agent_memory (tenant_id, agent_id);
create index if not exists agent_memory_embedding_idx
    on agent_memory using hnsw (embedding vector_cosine_ops);
create index if not exists agent_memory_meta_idx
    on agent_memory using gin (metadata jsonb_path_ops);

-- RLS tenant_isolation (паттерн с nullif: пустой app.tenant_id → NULL → 0 строк, без ошибки каста).
do $$ begin
    if not exists (select 1 from pg_policies
                   where tablename = 'team_agents' and policyname = 'tenant_isolation') then
        create policy tenant_isolation on team_agents for all
            using (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
            with check (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);
    end if;
end $$;
alter table team_agents enable row level security;

do $$ begin
    if not exists (select 1 from pg_policies
                   where tablename = 'agent_memory' and policyname = 'tenant_isolation') then
        create policy tenant_isolation on agent_memory for all
            using (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
            with check (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);
    end if;
end $$;
alter table agent_memory enable row level security;

do $$ begin
    if exists (select 1 from pg_roles where rolname = 'panel_rw') then
        grant select, insert, update, delete on team_agents  to panel_rw;
        grant select, insert, update, delete on agent_memory to panel_rw;
    end if;
end $$;
```

- [ ] **Step 2: Зеркалировать гранты в `db/panel_role.sql`**

Найти блок `revoke all on all tables in schema public from panel_rw;` и ПОСЛЕ существующих per-table грантов (рядом с грантами `kb_documents`/`kb_chunks`) добавить:
```sql
-- ── СП-1 «Команда отделов»: team_agents + agent_memory (объекты в db/schema_team_agents.sql) ──
-- Перевыдаются здесь, т.к. revoke all выше снимает грант при реконсиляции Timeweb. Бот (owner) не требует.
grant select, insert, update, delete on team_agents  to panel_rw;
grant select, insert, update, delete on agent_memory to panel_rw;
```

- [ ] **Step 3: Применить на `risuy_dev` и проверить**

```bash
cd /Users/konstantin/Downloads/risuy-ecosystem
bash ~/.claude/scripts/twc-migrate.sh 4171827 81.31.246.136 risuy_dev gen_user \
  "$(pwd)/db/schema_team_agents.sql"
# проверка (owner-DSN на risuy_dev): таблицы есть, RLS включён
ANON_SCHEMA_DSN="postgresql://gen_user:<pw>@81.31.246.136:5432/risuy_dev?sslmode=require" \
./.venv-smoke/bin/python - <<'PY'
import asyncio,os,asyncpg
async def m():
    c=await asyncpg.connect(os.environ["ANON_SCHEMA_DSN"])
    try:
        for t in ("team_agents","agent_memory"):
            ok=await c.fetchval("select relrowsecurity from pg_class where relname=$1",t)
            cols=await c.fetchval("select count(*) from information_schema.columns where table_name=$1",t)
            print(f"{t}: RLS={ok} колонок={cols}")
    finally: await c.close()
asyncio.run(m())
PY
```
Expected: `team_agents: RLS=True колонок=16` и `agent_memory: RLS=True колонок=7`.

- [ ] **Step 4: Прод-DDL — НЕ применять сейчас.** Помечается для деплоя (за владельцем): тот же `twc-migrate.sh ... risuy ...` после «да». Зафиксировать в коммите факт «dev накатан, прод ждёт».

---

## Task 2: Бот-резолвер `resolve_team_agent_cfg` (аддитивно) + проводка в `t_text`

**Files:**
- Modify: `bot-telegram/db.py` (рядом с `get_tenant_ai_overrides`, ~L1432; `_AI_BACKENDS` уже есть на L1242)
- Modify: `bot-telegram/multiplex.py` (`t_text`, заменить L202)
- Test: `scripts/team_agents_resolver_smoke.py` (чистый, без БД)

**Interfaces:**
- Consumes: `get_tenant_ai_overrides(tid)` (легаси-фолбэк), `get_lead_persona`/`get_lead_source`, таблица `team_agents` (Task 1).
- Produces: `resolve_team_agent_cfg(tid, *, source=None, lead_agent_slug=None) -> dict` (контракт `get_tenant_ai_overrides` + `agent_slug`, `escalation_chat_id`, `escalation_topic_id`, `is_orchestrator`, `memory_enabled`); чистый `_pick_team_agent(rows, *, lead_agent_slug, channel_slug) -> dict | None`.

- [ ] **Step 1: Написать чистый смоук** `scripts/team_agents_resolver_smoke.py`

```python
#!/usr/bin/env python3
"""Чистый смоук слоёв резолвера команды: _pick_team_agent (диалог>канал>дефолт). БД не нужна.
Запуск: PYTHONPATH=bot-telegram ./.venv-smoke/bin/python scripts/team_agents_resolver_smoke.py"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bot-telegram"))
os.environ.setdefault("BOT_TOKEN", "x")
os.environ.setdefault("DATABASE_URL", "postgresql://x/y")
import db  # noqa: E402  (bot-telegram/db.py)

FAILS: list[str] = []


def check(name, cond, detail=""):
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


def row(slug, *, is_default=False, enabled=True):
    return {"slug": slug, "is_default": is_default, "enabled": enabled,
            "name": slug, "role_preset": None, "system_prompt": f"p:{slug}",
            "backend": None, "agent_id": "", "fallback_text": "",
            "escalation_chat_id": "", "escalation_topic_id": None,
            "is_orchestrator": False, "memory_enabled": False}


def main() -> None:
    rows = [row("sales", is_default=True), row("support"), row("off", enabled=False)]
    # диалог побеждает всё
    p = db._pick_team_agent(rows, lead_agent_slug="support", channel_slug="sales")
    check("диалог→support", p and p["slug"] == "support")
    # канал, если нет диалога
    p = db._pick_team_agent(rows, lead_agent_slug=None, channel_slug="support")
    check("канал→support", p and p["slug"] == "support")
    # дефолт, если нет диалога/канала
    p = db._pick_team_agent(rows, lead_agent_slug=None, channel_slug=None)
    check("дефолт→sales", p and p["slug"] == "sales")
    # выключенный агент игнорируется на всех слоях → падаем ниже
    p = db._pick_team_agent(rows, lead_agent_slug="off", channel_slug=None)
    check("выключенный диалог-агент игнор → дефолт", p and p["slug"] == "sales")
    # пустой набор → None (вызыватель уйдёт на легаси-фолбэк)
    check("нет агентов → None", db._pick_team_agent([], lead_agent_slug=None, channel_slug=None) is None)
    # несуществующий slug канала → None-канал → дефолт
    p = db._pick_team_agent(rows, lead_agent_slug=None, channel_slug="nope")
    check("неизвестный канал → дефолт", p and p["slug"] == "sales")

    if FAILS:
        print("\n".join("❌ " + f for f in FAILS))
        raise SystemExit(1)
    print("🟢 team_agents_resolver_smoke зелёный")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Запустить — упадёт** (`AttributeError: module 'db' has no attribute '_pick_team_agent'`)

Run: `cd /Users/konstantin/Downloads/risuy-ecosystem && PYTHONPATH=bot-telegram ./.venv-smoke/bin/python scripts/team_agents_resolver_smoke.py`
Expected: FAIL.

- [ ] **Step 3: Реализовать в `bot-telegram/db.py`** (после `get_tenant_ai_overrides`, ~L1465)

```python
# ── СП-1 «Команда отделов»: per-tenant резолвер агента (team_agents). Аддитивно поверх
# get_tenant_ai_overrides (легаси-фолбэк). Слои: диалог(leads.ai_persona) > канал
# (tenant_settings.agent_for_channel__<source>) > is_default-агент > легаси tenant_settings.
# Бот ходит owner-ролью → RLS обходит, фильтрует tenant_id явно. ──
def _pick_team_agent(rows, *, lead_agent_slug, channel_slug):
    """Выбрать агента из набора строк team_agents по слоям диалог>канал>дефолт. Чистая (без БД).
    rows — list[dict-like] с ключами slug/enabled/is_default. None — никто не подошёл (→ легаси-фолбэк)."""
    by_slug = {r["slug"]: r for r in rows if r["enabled"]}
    if lead_agent_slug and lead_agent_slug in by_slug:
        return by_slug[lead_agent_slug]
    if channel_slug and channel_slug in by_slug:
        return by_slug[channel_slug]
    for r in rows:
        if r["enabled"] and r["is_default"]:
            return r
    return None


_TEAM_AGENT_COLS = ("slug, name, role_preset, system_prompt, backend, agent_id, fallback_text, "
                    "escalation_chat_id, escalation_topic_id, is_default, is_orchestrator, "
                    "memory_enabled, enabled")


async def resolve_team_agent_cfg(tid, *, source=None, lead_agent_slug=None) -> dict:
    """Конфиг ИИ для тенант-бота с выбором агента команды (team_agents). Слои диалог>канал>дефолт;
    если команды нет/никто не подошёл — ФОЛБЭК на легаси get_tenant_ai_overrides (поведение как раньше).
    Бот фильтрует tenant_id явно (owner обходит RLS). Сбой → легаси-фолбэк (ИИ не молчит из-за БД)."""
    if not tid:
        return await get_tenant_ai_overrides(tid)
    try:
        async with pool.acquire() as c:
            rows = [dict(r) for r in await c.fetch(
                f"select {_TEAM_AGENT_COLS} from team_agents where tenant_id = $1 and enabled",
                tid)]
            channel_slug = None
            if source:
                channel_slug = await c.fetchval(
                    "select value from tenant_settings where tenant_id = $1 "
                    "and key = $2", tid, f"agent_for_channel__{source}")
    except Exception:  # noqa: BLE001 — таблицы ещё нет / сбой → легаси
        return await get_tenant_ai_overrides(tid)
    if not rows:
        return await get_tenant_ai_overrides(tid)  # тенант не мигрирован/пустая команда
    picked = _pick_team_agent(rows, lead_agent_slug=lead_agent_slug,
                              channel_slug=(channel_slug or "").strip() or None)
    if picked is None:
        return await get_tenant_ai_overrides(tid)
    backend = (picked["backend"] or "").strip()
    if backend not in _AI_BACKENDS:
        backend = "cloud_ai"
    legacy = await get_tenant_ai_overrides(tid)  # для model/gateway_base_url/enabled тенанта
    return {
        "enabled": legacy["enabled"],
        "backend": backend,
        "agent_id": (picked["agent_id"] or "").strip(),
        "model": legacy["model"],
        "gateway_base_url": legacy["gateway_base_url"],
        "system_prompt": picked["system_prompt"] or "",
        "fallback": picked["fallback_text"] or legacy["fallback"],
        "kb_enabled": False,                       # СП-2
        "agent_slug": picked["slug"],
        "escalation_chat_id": (picked["escalation_chat_id"] or "").strip(),
        "escalation_topic_id": picked["escalation_topic_id"],
        "is_orchestrator": bool(picked["is_orchestrator"]),
        "memory_enabled": bool(picked["memory_enabled"]),
    }
```

- [ ] **Step 4: Запустить смоук — зелёный**

Run: `cd /Users/konstantin/Downloads/risuy-ecosystem && PYTHONPATH=bot-telegram ./.venv-smoke/bin/python scripts/team_agents_resolver_smoke.py`
Expected: `🟢 team_agents_resolver_smoke зелёный`.

- [ ] **Step 5: Провести слои в `t_text`** (`bot-telegram/multiplex.py`, заменить строку 202)

Было:
```python
    cfg = await db.get_tenant_ai_overrides(db.tenant_id())
```
Стало:
```python
    # СП-1: выбор агента команды по слоям диалог>канал>дефолт (фолбэк на легаси внутри резолвера).
    _persona = await db.get_lead_persona(message.from_user.id)
    _source = await db.get_lead_source(message.from_user.id)
    cfg = await db.resolve_team_agent_cfg(db.tenant_id(), source=_source, lead_agent_slug=_persona)
```
(VK/MAX-ветки и `t_document` — НЕ трогаем в СП-1: каналы не подняты, легаси-путь работает как прежде.)

- [ ] **Step 6: Проверка компиляции**

Run: `cd /Users/konstantin/Downloads/risuy-ecosystem && python3 -c "import ast; ast.parse(open('bot-telegram/db.py').read()); ast.parse(open('bot-telegram/multiplex.py').read()); print('OK')"`
Expected: `OK`.

---

## Task 3: Панель — CRUD `team_agents` + раздел `/my-team`

**Files:**
- Modify: `admin-panel/db.py` (новая секция CRUD рядом с `tenant_triggers`, ~L3475)
- Modify: `admin-panel/app.py` (новые маршруты рядом с `/triggers`, ~L4987)
- Create: `admin-panel/templates/my_team.html`
- Modify: `admin-panel/templates/base.html` (nav)
- Test: `scripts/team_agents_db_smoke.py` (Task ниже использует те же функции; здесь — компиляция/jinja)

**Interfaces:**
- Consumes: `_insert_audit`, `session.actor/active_tenant_id/csrf_token`, `_ip/_ua/_enforce_csrf`, `config.PERSONA_PRESETS`.
- Produces (admin-panel/db.py): `list_team_agents(tid)`, `upsert_team_agent(tid, *, slug, name, role_preset, system_prompt, escalation_chat_id, escalation_topic_id, is_orchestrator, memory_enabled, actor, ip, user_agent)`, `set_default_team_agent(tid, slug, *, actor, ip, user_agent)`, `set_channel_agent(tid, source, slug, *, actor, ip, user_agent)`, `disable_team_agent(tid, slug, *, actor, ip, user_agent)`.

- [ ] **Step 1: CRUD в `admin-panel/db.py`** (вставить после блока `delete_tenant_trigger`, ~L3553)

```python
# ── СП-1 «Команда отделов»: CRUD team_agents (RLS). Раздел панели «ИИ-команда» (/my-team). ──
# Паттерн tenant_triggers: каждая мутация в транзакции ПОСЛЕ set_config('app.tenant_id') + _insert_audit.
_TEAM_AGENT_SELECT = ("id, slug, name, role_preset, system_prompt, escalation_chat_id, "
                      "escalation_topic_id, is_default, is_orchestrator, memory_enabled, enabled, position")


async def list_team_agents(tenant_id) -> list[asyncpg.Record]:
    """Все агенты команды тенанта (enabled и выключенные) для раздела «ИИ-команда». Под RLS."""
    if not tenant_id:
        return []
    async with pool.acquire() as c:
        async with c.transaction():
            await c.execute("select set_config('app.tenant_id', $1, true)", str(tenant_id))
            return await c.fetch(
                f"select {_TEAM_AGENT_SELECT} from team_agents where tenant_id = $1 "
                "order by position, created_at", tenant_id)


async def upsert_team_agent(
    tenant_id, *, slug: str, name: str, role_preset: str | None, system_prompt: str,
    escalation_chat_id: str, escalation_topic_id: int | None,
    is_orchestrator: bool, memory_enabled: bool,
    actor: str, ip: str | None, user_agent: str | None,
) -> None:
    """Создать/обновить агента команды по (tenant_id, slug) + аудит. Валидность/длины — у вызывающего."""
    if not tenant_id:
        raise ValueError("upsert_team_agent: tenant_id обязателен")
    async with pool.acquire() as c:
        async with c.transaction():
            await c.execute("select set_config('app.tenant_id', $1, true)", str(tenant_id))
            pos = int(await c.fetchval(
                "select coalesce(max(position), 0) + 1 from team_agents where tenant_id = $1",
                tenant_id) or 1)
            await c.execute(
                """
                insert into team_agents
                    (tenant_id, slug, name, role_preset, system_prompt,
                     escalation_chat_id, escalation_topic_id, is_orchestrator, memory_enabled, position)
                values ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
                on conflict (tenant_id, slug) do update set
                    name = excluded.name, role_preset = excluded.role_preset,
                    system_prompt = excluded.system_prompt,
                    escalation_chat_id = excluded.escalation_chat_id,
                    escalation_topic_id = excluded.escalation_topic_id,
                    is_orchestrator = excluded.is_orchestrator,
                    memory_enabled = excluded.memory_enabled,
                    enabled = true, updated_at = now()
                """,
                tenant_id, slug, name, role_preset, system_prompt,
                escalation_chat_id, escalation_topic_id, is_orchestrator, memory_enabled, pos,
            )
            await _insert_audit(
                c, actor=actor, action="team_agent_upsert", ip=ip, user_agent=user_agent,
                detail={"tenant_id": str(tenant_id), "slug": slug, "role_preset": role_preset,
                        "prompt_set": bool(system_prompt)},
            )


async def set_default_team_agent(
    tenant_id, slug: str, *, actor: str, ip: str | None, user_agent: str | None,
) -> bool:
    """Сделать агента дефолтным (снять флаг с прочих — один is_default на тенанта). True — успех."""
    if not tenant_id:
        return False
    async with pool.acquire() as c:
        async with c.transaction():
            await c.execute("select set_config('app.tenant_id', $1, true)", str(tenant_id))
            await c.execute("update team_agents set is_default = false, updated_at = now() "
                            "where tenant_id = $1 and is_default", tenant_id)
            res = await c.execute(
                "update team_agents set is_default = true, updated_at = now() "
                "where tenant_id = $1 and slug = $2 and enabled", tenant_id, slug)
            if res.endswith(" 0"):
                return False
            await _insert_audit(
                c, actor=actor, action="team_agent_set_default", ip=ip, user_agent=user_agent,
                detail={"tenant_id": str(tenant_id), "slug": slug})
            return True


async def set_channel_agent(
    tenant_id, source: str, slug: str, *, actor: str, ip: str | None, user_agent: str | None,
) -> None:
    """Привязать канал (source) к агенту (slug) через tenant_settings.agent_for_channel__<source>.
    slug='' → снять привязку (пустое value). Под RLS + аудит."""
    if not tenant_id:
        raise ValueError("set_channel_agent: tenant_id обязателен")
    key = f"agent_for_channel__{source}"
    async with pool.acquire() as c:
        async with c.transaction():
            await c.execute("select set_config('app.tenant_id', $1, true)", str(tenant_id))
            await c.execute(
                """
                insert into tenant_settings (tenant_id, key, value) values ($1,$2,$3)
                on conflict (tenant_id, key) do update set value = excluded.value, updated_at = now()
                """, tenant_id, key, slug)
            await _insert_audit(
                c, actor=actor, action="team_agent_set_channel", ip=ip, user_agent=user_agent,
                detail={"tenant_id": str(tenant_id), "source": source, "slug": slug or None})


async def disable_team_agent(
    tenant_id, slug: str, *, actor: str, ip: str | None, user_agent: str | None,
) -> bool:
    """Soft-delete агента (enabled=false; память/аудит сохраняются). True — выключен."""
    if not tenant_id:
        return False
    async with pool.acquire() as c:
        async with c.transaction():
            await c.execute("select set_config('app.tenant_id', $1, true)", str(tenant_id))
            res = await c.execute(
                "update team_agents set enabled = false, is_default = false, updated_at = now() "
                "where tenant_id = $1 and slug = $2", tenant_id, slug)
            if res.endswith(" 0"):
                return False
            await _insert_audit(
                c, actor=actor, action="team_agent_disable", ip=ip, user_agent=user_agent,
                detail={"tenant_id": str(tenant_id), "slug": slug})
            return True
```

- [ ] **Step 2: Маршруты в `admin-panel/app.py`** (вставить рядом с `/triggers`, ~L5117)

```python
# ── СП-1 «Команда отделов»: раздел тенанта «ИИ-команда» (/my-team). Паттерн /triggers (PRG+CSRF). ──
import re as _re_team  # модуль re уже импортирован в app.py; используем глобальный re


def _team_saved_text(saved: str | None) -> str | None:
    return {"saved": "Агент сохранён.", "default": "Дефолтный агент обновлён.",
            "channel": "Привязка канала сохранена.", "disabled": "Агент выключен."}.get(saved or "")


def _team_err_text(err: str | None) -> str | None:
    return {
        "no_tenant": "Кабинет ещё не привязан к клиенту. Обратитесь в поддержку.",
        "bad_slug": "Код агента — латиница/цифры/дефис, 1–40 символов.",
        "no_name": "Укажите название агента (отдел/имя).",
        "bad_chat": "ID Telegram-чата должен быть числом вида -1002576119452.",
        "not_found": "Агент не найден.",
    }.get(err or "")


def _present_team_agent(r) -> dict:
    return {
        "slug": r["slug"], "name": r["name"] or "", "role_preset": r["role_preset"] or "",
        "system_prompt": r["system_prompt"] or "", "escalation_chat_id": r["escalation_chat_id"] or "",
        "escalation_topic_id": r["escalation_topic_id"], "is_default": r["is_default"],
        "is_orchestrator": r["is_orchestrator"], "memory_enabled": r["memory_enabled"],
        "enabled": r["enabled"],
    }


@app.get("/my-team", response_class=HTMLResponse)
async def my_team_page(
    request: Request,
    session: auth.Session = Depends(require_session),
    saved: str | None = None,
    err: str | None = None,
):
    tid = session.active_tenant_id
    rows = await db.list_team_agents(tid) if tid else []
    return templates.TemplateResponse(
        request,
        "my_team.html",
        {
            "active": "my_team",
            "session": session,
            "csrf_token": session.csrf_token,
            "has_tenant": bool(tid),
            "agents": [_present_team_agent(r) for r in rows],
            "presets": [{"key": k, "label": v.get("role", k)} for k, v in config.PERSONA_PRESETS.items()],
            "prompt_max": config.TENANT_AI_PROMPT_MAX,
            "support_url": _safe_support_url(config.SUPPORT_URL),
            "saved": _team_saved_text(saved),
            "err": _team_err_text(err),
        },
    )


_TEAM_SLUG_RE = r"^[a-z0-9\-]{1,40}$"


@app.post("/my-team/save")
async def my_team_save(
    request: Request,
    session: auth.Session = Depends(require_session),
    slug: str = Form(""),
    name: str = Form(""),
    role_preset: str = Form(""),
    system_prompt: str = Form(""),
    escalation_chat_id: str = Form(""),
    escalation_topic_id: str = Form(""),
    is_orchestrator: str = Form(""),
    memory_enabled: str = Form(""),
    csrf_token: str = Form(""),
):
    await _enforce_csrf(request, session, csrf_token)
    tid = session.active_tenant_id
    if not tid:
        return RedirectResponse(url="/my-team?err=no_tenant", status_code=303)
    slug = slug.strip().lower()
    if not re.match(_TEAM_SLUG_RE, slug):
        return RedirectResponse(url="/my-team?err=bad_slug", status_code=303)
    if not name.strip():
        return RedirectResponse(url="/my-team?err=no_name", status_code=303)
    chat = escalation_chat_id.strip()
    if chat and not re.match(config.ESCALATION_CHAT_ID_RE, chat):
        return RedirectResponse(url="/my-team?err=bad_chat", status_code=303)
    topic_raw = escalation_topic_id.strip()
    topic = int(topic_raw) if topic_raw.isdigit() else None
    preset = role_preset.strip() if role_preset.strip() in config.PERSONA_PRESETS else None
    await db.upsert_team_agent(
        tid, slug=slug, name=name.strip()[:120], role_preset=preset,
        system_prompt=system_prompt.strip()[: config.TENANT_AI_PROMPT_MAX],
        escalation_chat_id=chat, escalation_topic_id=topic,
        is_orchestrator=bool(is_orchestrator), memory_enabled=bool(memory_enabled),
        actor=session.actor, ip=_ip(request), user_agent=_ua(request),
    )
    return RedirectResponse(url="/my-team?saved=saved", status_code=303)


@app.post("/my-team/default")
async def my_team_default(
    request: Request, session: auth.Session = Depends(require_session),
    slug: str = Form(""), csrf_token: str = Form(""),
):
    await _enforce_csrf(request, session, csrf_token)
    tid = session.active_tenant_id
    if not tid:
        return RedirectResponse(url="/my-team?err=no_tenant", status_code=303)
    ok = await db.set_default_team_agent(tid, slug.strip().lower(),
                                         actor=session.actor, ip=_ip(request), user_agent=_ua(request))
    return RedirectResponse(url="/my-team?saved=default" if ok else "/my-team?err=not_found",
                            status_code=303)


@app.post("/my-team/channel")
async def my_team_channel(
    request: Request, session: auth.Session = Depends(require_session),
    source: str = Form(""), slug: str = Form(""), csrf_token: str = Form(""),
):
    await _enforce_csrf(request, session, csrf_token)
    tid = session.active_tenant_id
    if not tid:
        return RedirectResponse(url="/my-team?err=no_tenant", status_code=303)
    src = source.strip()
    if src not in config.MESSENGERS:
        return RedirectResponse(url="/my-team?err=not_found", status_code=303)
    await db.set_channel_agent(tid, src, slug.strip().lower(),
                               actor=session.actor, ip=_ip(request), user_agent=_ua(request))
    return RedirectResponse(url="/my-team?saved=channel", status_code=303)


@app.post("/my-team/disable")
async def my_team_disable(
    request: Request, session: auth.Session = Depends(require_session),
    slug: str = Form(""), csrf_token: str = Form(""),
):
    await _enforce_csrf(request, session, csrf_token)
    tid = session.active_tenant_id
    if not tid:
        return RedirectResponse(url="/my-team?err=no_tenant", status_code=303)
    ok = await db.disable_team_agent(tid, slug.strip().lower(),
                                     actor=session.actor, ip=_ip(request), user_agent=_ua(request))
    return RedirectResponse(url="/my-team?saved=disabled" if ok else "/my-team?err=not_found",
                            status_code=303)
```
> Примечание: строка `import re as _re_team` не нужна, если `re` уже импортирован в app.py (он используется в `_parse_stopwords`/`triggers_add`). Проверить наличие `import re` вверху app.py; если есть — НЕ добавлять, использовать `re.match`. Эта строка-страховка удаляется при наличии `import re`.

- [ ] **Step 3: Шаблон** `admin-panel/templates/my_team.html`

```html
{% extends "base.html" %}
{% from "_macros.html" import flash %}

{% block title %}ИИ-команда{% endblock %}
{% block body_class %}page-triggers{% endblock %}

{% block content %}
<div class="page-head">
  <h1 class="page-head__title">ИИ-команда</h1>
  <p class="page-head__hint">Соберите команду ИИ-сотрудников по отделам: у каждого своя должность, инструкции и адрес уведомления менеджеров. Какой агент отвечает — выбирается по каналу, а оператор может назначить агента на конкретный диалог. Применяется со следующего сообщения, без перезапуска бота.</p>
</div>

{% if saved %}{{ flash(saved, 'ok') }}{% endif %}
{% if err %}{{ flash(err, 'error') }}{% endif %}

{% if not has_tenant %}
<section class="card">
  <div class="card__title">Кабинет ещё не привязан</div>
  <p class="card__note">Команда настраивается в кабинете клиента. Сейчас к учётной записи кабинет не привязан — напишите в поддержку.</p>
  {% if support_url %}<div class="acct-actions"><a class="btn btn--primary" href="{{ support_url }}" target="_blank" rel="noopener noreferrer nofollow">Написать в поддержку</a></div>{% endif %}
</section>
{% else %}

{% for a in agents %}
<section class="card" aria-label="{{ a.name }}">
  <h2 class="card__title">{{ a.name }}{% if a.is_default %} <span class="pill pill--muted">по умолчанию</span>{% endif %}{% if not a.enabled %} <span class="pill pill--muted">выключен</span>{% endif %}</h2>
  <p class="card__note">Код: <span class="mono">{{ a.slug }}</span>{% if a.role_preset %} · роль: {{ a.role_preset }}{% endif %}{% if a.escalation_chat_id %} · эскалация → <span class="mono">{{ a.escalation_chat_id }}</span>{% endif %}</p>
  <form method="post" action="/my-team/save" autocomplete="off">
    <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
    <input type="hidden" name="slug" value="{{ a.slug }}">
    <label class="field"><span class="field__label">Название</span>
      <input class="field__input" type="text" name="name" maxlength="120" value="{{ a.name|e }}"></label>
    <label class="field"><span class="field__label">Должность (роль)</span>
      <select class="field__input" name="role_preset">
        <option value="">— без пресета —</option>
        {% for p in presets %}<option value="{{ p.key }}"{% if a.role_preset == p.key %} selected{% endif %}>{{ p.label }}</option>{% endfor %}
      </select></label>
    <label class="field"><span class="field__label">Инструкции</span>
      <textarea class="field__input field__textarea" name="system_prompt" rows="4" maxlength="{{ prompt_max }}">{{ a.system_prompt }}</textarea></label>
    <label class="field"><span class="field__label">ID чата менеджеров отдела <span class="field__hint">необязательно</span></span>
      <input class="field__input" type="text" name="escalation_chat_id" inputmode="numeric" value="{{ a.escalation_chat_id|e }}" placeholder="-1002576119452" maxlength="32"></label>
    <label class="field field--check"><input type="checkbox" name="memory_enabled" value="1"{% if a.memory_enabled %} checked{% endif %}><span class="field__label">Долгая память (готовится)</span></label>
    <div class="form-actions">
      <button class="btn btn--primary" type="submit">Сохранить</button>
    </div>
  </form>
  <div class="acct-actions">
    {% if not a.is_default %}
    <form method="post" action="/my-team/default"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><input type="hidden" name="slug" value="{{ a.slug }}"><button class="btn btn--secondary btn--sm" type="submit">Сделать дефолтным</button></form>
    {% endif %}
    {% if a.enabled %}
    <form method="post" action="/my-team/disable" onsubmit="return confirm('Выключить агента?')"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><input type="hidden" name="slug" value="{{ a.slug }}"><button class="btn btn--danger-ghost btn--sm" type="submit">Выключить</button></form>
    {% endif %}
  </div>
</section>
{% endfor %}

<section class="card" aria-label="Новый агент">
  <h2 class="card__title">Добавить агента</h2>
  <form method="post" action="/my-team/save" autocomplete="off">
    <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
    <label class="field"><span class="field__label">Код агента <span class="field__hint">латиница/цифры/дефис, напр. sales</span></span>
      <input class="field__input" type="text" name="slug" maxlength="40" placeholder="sales"></label>
    <label class="field"><span class="field__label">Название</span>
      <input class="field__input" type="text" name="name" maxlength="120" placeholder="Отдел продаж"></label>
    <label class="field"><span class="field__label">Должность (роль)</span>
      <select class="field__input" name="role_preset"><option value="">— без пресета —</option>
        {% for p in presets %}<option value="{{ p.key }}">{{ p.label }}</option>{% endfor %}</select></label>
    <label class="field"><span class="field__label">Инструкции</span>
      <textarea class="field__input field__textarea" name="system_prompt" rows="4" maxlength="{{ prompt_max }}"></textarea></label>
    <label class="field"><span class="field__label">ID чата менеджеров отдела <span class="field__hint">необязательно</span></span>
      <input class="field__input" type="text" name="escalation_chat_id" inputmode="numeric" placeholder="-1002576119452" maxlength="32"></label>
    <div class="form-actions"><button class="btn btn--secondary" type="submit">Добавить</button></div>
  </form>
</section>

{% endif %}
{% endblock %}
```

- [ ] **Step 4: Nav в `base.html`** — три правки

(а) В tenant-ветке `{%- else %}` заменить строку `my_agent` на `my_team` (агентов теперь несколько; легаси `/my-agent` остаётся доступен напрямую, но из меню убран):
```html
      {{ nav_item('my_team',      '/my-team',    'ИИ-команда',       active) }}
      {{ nav_item('triggers',     '/triggers',   'Триггеры',         active) }}
```
(б) `NAV_TITLES` (base.html ~L72): добавить `'my_team': 'ИИ-команда',`.
(в) `nav_icon` (base.html ~L45, перед `data_protection`): добавить ветку (lucide users):
```html
{%- elif name == 'my_team' -%}<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>
```

- [ ] **Step 5: Проверка компиляции + jinja**

```bash
cd /Users/konstantin/Downloads/risuy-ecosystem
python3 -c "import ast; ast.parse(open('admin-panel/app.py').read()); ast.parse(open('admin-panel/db.py').read()); print('py OK')"
./.venv-smoke/bin/python -c "from jinja2 import Environment, FileSystemLoader; e=Environment(loader=FileSystemLoader('admin-panel/templates')); e.get_template('my_team.html'); e.get_template('base.html'); print('jinja OK')"
```
Expected: `py OK` / `jinja OK`.

---

## Task 4: Бэкфилл `scripts/backfill_team_agents.py`

**Files:**
- Create: `scripts/backfill_team_agents.py`
- Test: запуск на `risuy_dev` (idempotent)

**Interfaces:**
- Consumes: таблицы `tenants`, `tenant_settings`, `team_agents`.
- Produces: по одному `team_agents`-агенту (slug=`default`, is_default=true) на тенанта с непустым легаси `ai_system_prompt`.

- [ ] **Step 1: Написать скрипт**

```python
#!/usr/bin/env python3
"""СП-1: бэкфилл команды — из легаси одной-персоны тенанта (tenant_settings.ai_system_prompt/
ai_backend/ai_agent_id) создать дефолтного агента команды в team_agents (slug='default').
Idempotent: повторный запуск обновляет существующего default-агента, дублей не плодит.

🟥 По умолчанию ТОЛЬКО risuy_dev. Прод — лишь при BACKFILL_ALLOW_PROD=yes.
ЗАПУСК:
  BACKFILL_DSN="postgresql://gen_user:<pw>@81.31.246.136:5432/risuy_dev?sslmode=require" \
      ./.venv-smoke/bin/python scripts/backfill_team_agents.py
"""
import asyncio
import os

import asyncpg

DSN = os.environ.get("BACKFILL_DSN")
if not DSN:
    raise SystemExit("Задайте BACKFILL_DSN.")
DBNAME = DSN.split("?")[0].rstrip("/").split("/")[-1]
if DBNAME == "risuy" and os.environ.get("BACKFILL_ALLOW_PROD") != "yes":
    raise SystemExit("ОТКАЗ: боевой risuy. Для прода явно: BACKFILL_ALLOW_PROD=yes.")


async def main() -> None:
    print(f"backfill_team_agents · база={DBNAME}")
    c = await asyncpg.connect(DSN)
    created = 0
    try:
        async with c.transaction():
            rows = await c.fetch(
                "select t.id as tid, s.value as prompt, "
                "  (select value from tenant_settings where tenant_id=t.id and key='ai_backend') as backend, "
                "  (select value from tenant_settings where tenant_id=t.id and key='ai_agent_id') as agent_id "
                "from tenants t join tenant_settings s "
                "  on s.tenant_id=t.id and s.key='ai_system_prompt' "
                "where coalesce(s.value,'') <> '' "
                "  and not exists (select 1 from team_agents a where a.tenant_id=t.id)")
            for r in rows:
                await c.execute(
                    """
                    insert into team_agents
                        (tenant_id, slug, name, system_prompt, backend, agent_id, is_default, position)
                    values ($1, 'default', 'ИИ-сотрудник', $2, $3, $4, true, 0)
                    on conflict (tenant_id, slug) do update set
                        system_prompt = excluded.system_prompt, backend = excluded.backend,
                        agent_id = excluded.agent_id, is_default = true, enabled = true,
                        updated_at = now()
                    """,
                    r["tid"], r["prompt"] or "", (r["backend"] or None), (r["agent_id"] or ""))
                created += 1
        print(f"✅ бэкфилл: обработано тенантов {created}")
    finally:
        await c.close()


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: Прогон на `risuy_dev`** (после Task 1 — таблица существует)

Run: `cd /Users/konstantin/Downloads/risuy-ecosystem && BACKFILL_DSN="postgresql://gen_user:<pw>@81.31.246.136:5432/risuy_dev?sslmode=require" ./.venv-smoke/bin/python scripts/backfill_team_agents.py`
Expected: `✅ бэкфилл: обработано тенантов N` (N ≥ 0); повторный запуск — то же N без дублей (idempotent).

---

## Task 5: БД-смоук + адверсариальное ревью + коммит

**Files:**
- Create: `scripts/team_agents_db_smoke.py`
- Test/Gate: смоук на `risuy_dev` + ревью 3 линзы + локальный коммит

- [ ] **Step 1: Написать БД-смоук** `scripts/team_agents_db_smoke.py` (паттерн `anon_behav_panelrw` — `panel_rw`, RLS enforced; seeding под ctx; чистка по slug тенанта)

```python
#!/usr/bin/env python3
"""БД-смоук СП-1 на risuy_dev (роль panel_rw, RLS ENFORCED): CRUD team_agents под RLS видит только
свой тенант; unique(tenant_id,slug); один is_default; резолвер диалог>канал>дефолт>легаси; agent_memory RLS.
Запуск:
  TEAM_DSN="postgresql://panel_rw:<pw>@81.31.246.136:5432/risuy_dev?sslmode=require" \
  BOT_DSN="postgresql://gen_user:<pw>@81.31.246.136:5432/risuy_dev?sslmode=require" \
  PYTHONPATH=admin-panel:bot-telegram:. ./.venv-smoke/bin/python scripts/team_agents_db_smoke.py
"""
import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "admin-panel"))
os.environ.setdefault("DATABASE_URL", os.environ.get("TEAM_DSN", "postgresql://x/y"))
os.environ.setdefault("SESSION_SECRET", "smoke-session-secret-padding-0123456789-abcdef")
os.environ.setdefault("ADMIN_USERNAME", "smoke")
os.environ.setdefault("ADMIN_PASSWORD_HASH",
                      "$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHQ$aGFzaHN0dWI")
import asyncpg  # noqa: E402
import db as adb  # noqa: E402  (admin-panel/db.py)

TEAM_DSN = os.environ["TEAM_DSN"]
assert "/risuy_dev" in TEAM_DSN.split("?")[0], "только risuy_dev"
FAILS: list[str] = []
SLUG_T = "smoke-team-a"


def check(name, cond, detail=""):
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


async def _cleanup():
    adb.set_active_tenant(None)
    async with adb.pool.acquire() as c:
        ids = await c.fetch("select id from tenants where slug = $1", SLUG_T)
    for r in ids:
        adb.set_active_tenant(str(r["id"]))
        async with adb.pool.acquire() as c:
            await c.execute("delete from agent_memory where tenant_id = $1", r["id"])
            await c.execute("delete from team_agents where tenant_id = $1", r["id"])
            await c.execute("delete from tenant_settings where tenant_id = $1", r["id"])
    adb.set_active_tenant(None)
    async with adb.pool.acquire() as c:
        await c.execute("delete from tenants where slug = $1", SLUG_T)


async def main():
    adb.pool = await asyncpg.create_pool(TEAM_DSN, min_size=1, max_size=4, setup=adb._apply_tenant_guc)
    forced = False
    try:
        adb.set_active_tenant(None)
        async with adb.pool.acquire() as c:
            ta = await c.fetchval("insert into tenants(slug,name,status) values($1,'A','active') returning id", SLUG_T)
            await c.execute("alter table team_agents force row level security")
            await c.execute("alter table agent_memory force row level security")
            forced = True

        adb.set_active_tenant(str(ta))
        await adb.upsert_team_agent(ta, slug="sales", name="Продажи", role_preset="mark",
                                    system_prompt="p-sales", escalation_chat_id="", escalation_topic_id=None,
                                    is_orchestrator=False, memory_enabled=False,
                                    actor="smoke", ip=None, user_agent=None)
        await adb.upsert_team_agent(ta, slug="support", name="Поддержка", role_preset=None,
                                    system_prompt="p-support", escalation_chat_id="", escalation_topic_id=None,
                                    is_orchestrator=False, memory_enabled=False,
                                    actor="smoke", ip=None, user_agent=None)
        rows = await adb.list_team_agents(ta)
        check("CRUD: 2 агента видны под своим тенантом", len(rows) == 2, str(len(rows)))
        ok = await adb.set_default_team_agent(ta, "sales", actor="smoke", ip=None, user_agent=None)
        check("set_default sales", ok)
        # один is_default
        defs = [r for r in await adb.list_team_agents(ta) if r["is_default"]]
        check("ровно один is_default", len(defs) == 1 and defs[0]["slug"] == "sales")
        # upsert идемпотентен (повтор не плодит дубль)
        await adb.upsert_team_agent(ta, slug="sales", name="Продажи2", role_preset="mark",
                                    system_prompt="p2", escalation_chat_id="", escalation_topic_id=None,
                                    is_orchestrator=False, memory_enabled=False,
                                    actor="smoke", ip=None, user_agent=None)
        check("upsert не плодит дубль", len(await adb.list_team_agents(ta)) == 2)
        # soft-delete
        await adb.disable_team_agent(ta, "support", actor="smoke", ip=None, user_agent=None)
        en = [r for r in await adb.list_team_agents(ta) if r["enabled"]]
        check("soft-delete: 1 enabled остался", len(en) == 1 and en[0]["slug"] == "sales")
        # RLS: чужой тенант не видит (ctx None → 0)
        adb.set_active_tenant(None)
        async with adb.pool.acquire() as c:
            seen = await c.fetchval("select count(*) from team_agents")
        check("RLS: ctx None → 0 строк team_agents", seen == 0, str(seen))
    finally:
        try:
            adb.set_active_tenant(None)
            async with adb.pool.acquire() as c:
                if forced:
                    await c.execute("alter table team_agents no force row level security")
                    await c.execute("alter table agent_memory no force row level security")
            await _cleanup()
        finally:
            await adb.pool.close()

    if FAILS:
        print("\n".join("❌ " + f for f in FAILS))
        raise SystemExit(1)
    print("🟢 team_agents_db_smoke зелёный")


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: Прогнать БД-смоук** (после Task 1 DDL на dev)

Run: `cd /Users/konstantin/Downloads/risuy-ecosystem && TEAM_DSN="postgresql://panel_rw:<pw>@81.31.246.136:5432/risuy_dev?sslmode=require" PYTHONPATH=admin-panel:bot-telegram:. ./.venv-smoke/bin/python scripts/team_agents_db_smoke.py`
Expected: `🟢 team_agents_db_smoke зелёный`.

- [ ] **Step 3: Прогнать резолвер-смоук бота на risuy_dev (ручная проверка слоёв)** — опц.: добавить в db-смоук блок, который под owner-DSN (gen_user) зовёт `bot.db.resolve_team_agent_cfg` (бот обходит RLS) и проверяет выбор по slug; либо проверить вживую после деплоя. (Чистый резолвер-смоук Task 2 уже доказал логику слоёв.)

- [ ] **Step 4: Адверсариальное ревью 3 линзы** (Workflow): (1) корректность/RLS-утечки (резолвер фолбэк не ломает легаси; бот фильтрует tenant_id; agent_memory/team_agents RLS); (2) регрессия (t_text слои не сломали ответ; 4 легаси-вызывателя get_tenant_ai_overrides целы; nav my_agent→my_team не порвал /my-agent); (3) панель/152-ФЗ (CRUD под set_config+аудит без ПДн; CSRF; гейт no_tenant). Critical/high — исправить, переcмоучить.

- [ ] **Step 5: Финальная проверка + локальный коммит** (push и прод-DDL — за владельцем)

```bash
cd /Users/konstantin/Downloads/risuy-ecosystem
PYTHONPATH=bot-telegram ./.venv-smoke/bin/python scripts/team_agents_resolver_smoke.py
TEAM_DSN="...risuy_dev..." PYTHONPATH=admin-panel:bot-telegram:. ./.venv-smoke/bin/python scripts/team_agents_db_smoke.py
git add db/schema_team_agents.sql db/panel_role.sql bot-telegram/db.py bot-telegram/multiplex.py \
        admin-panel/db.py admin-panel/app.py admin-panel/templates/my_team.html admin-panel/templates/base.html \
        scripts/backfill_team_agents.py scripts/team_agents_resolver_smoke.py scripts/team_agents_db_smoke.py \
        docs/superpowers/specs/2026-06-29-sp1-tenant-agent-team-design.md \
        docs/superpowers/plans/2026-06-29-sp1-tenant-agent-team.md
git commit -m "feat(panel+bot): СП-1 «ИИ-команда» — per-tenant команда агентов по отделам

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

- [ ] **Step 6: СТОП — за владельцем:** прод-DDL (`twc-migrate.sh ... risuy ...`) + бэкфилл на проде (`BACKFILL_ALLOW_PROD=yes`) + push → редеплой панели/бота → live-проверка (`/my-team`→200; создать агента; тенант-бот отвечает выбранным агентом).

---

## Self-Review
- **Покрытие спеки:** team_agents (Task1) ✓ · agent_memory схема (Task1) ✓ · резолвер слои+легаси-фолбэк (Task2) ✓ · t_text проводка (Task2) ✓ · панель CRUD/раздел/nav (Task3) ✓ · миграция+бэкфилл (Task1/Task4) ✓ · без лимита ✓ (нет cap-проверки) · soft-delete ✓ (disable_team_agent) · флаги is_orchestrator/memory_enabled ✓ · 152-ФЗ (аудит без ПДн, RLS) ✓ · смоуки pure+db ✓ · коллизия имени `tenant_agents` решена (`team_agents`) ✓.
- **Плейсхолдеры:** `<pw>` в командах — это owner/panel_rw-пароль для смоука (inline из Timeweb API при запуске), не плейсхолдер кода. Прод-DDL/push осознанно отложены (gated). `vector(768)` зафиксирован (из schema_kb.sql).
- **Согласованность типов:** `resolve_team_agent_cfg` возвращает контракт `get_tenant_ai_overrides` + extras; `_pick_team_agent` принимает rows-dict с ключами из `_TEAM_AGENT_COLS`; CRUD-сигнатуры (`upsert_team_agent`/`set_default_team_agent`/`set_channel_agent`/`disable_team_agent`) совпадают между db.py и вызовами в app.py/смоуке; slug-валидация `_TEAM_SLUG_RE` едина.
- **Открытый момент реализации:** проверить наличие `import re` вверху `admin-panel/app.py` (Step 2 Task3) — если есть, строку-страховку не добавлять.
