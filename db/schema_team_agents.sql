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

-- СП-2a: per-agent тумблер базы знаний (симметрично memory_enabled). default true. Идемпотентно.
alter table team_agents add column if not exists kb_enabled boolean not null default true;
