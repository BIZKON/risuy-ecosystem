-- Схема данных движка лидогенерации (S1-RAW). Идемпотентна, expand-first.
-- Применяется в db-init ПОСЛЕ снапшота(public) + roles_bootstrap(роли+схема engine).
-- Все engine-таблицы владеет engine_rw (Вариант A): owner обходит RLS на engine,
-- изоляция движка — на явном tenant_id + in-query backstop (engine/common/db.py::set_tenant);
-- RLS на tenant-scoped engine-таблицах защищает ЧТЕНИЯ panel_rw (панель).
-- Порядок операторов соблюдает топологию FK: raw_messages/search_profiles/identities → matching.
create extension if not exists vector;

-- panel_rw ходит в engine.sources/search_profiles (table-гранты ниже) → нужен usage на схему.
-- НЕ даёт доступа к таблицам без явного table-гранта (raw_messages/accounts/identities/matching — без него).
grant usage on schema engine to panel_rw;

-- ─────────────────────────────────────────────────────────────────────────────
-- engine.raw_messages — SHARED firehose. Глобальный дедуп (source_kind, external_id)
-- БЕЗ tenant_id (одно публичное сообщение = одна строка; тенант появляется в matching).
-- drop cascade суперсёдит S0M-заглушку (и её FK-иждивенцев, если есть — они пересоздаются ниже).
-- ─────────────────────────────────────────────────────────────────────────────
drop table if exists engine.raw_messages cascade;
create table engine.raw_messages (
    id           bigint generated always as identity primary key,
    created_at   timestamptz not null default now(),
    source_kind  text not null,                  -- telegram|vk|boards|tenders
    external_id  text not null,                  -- композит (контракт S2): TG chat_id:message_id, VK owner_id:post_id
    chat_ref     text,                            -- публичная ссылка на чат/канал (first-touch source_url)
    author_ref   text,                            -- публичный id автора (не ПДн-контакт)
    posted_at    timestamptz,                     -- время публикации в источнике (свежесть <5 мин)
    body         text,                            -- сырой текст; до внешнего LLM → Presidio-RU (S5)
    lang         text,
    embedding    vector(768),                     -- e5-base passage:; NULL пока не проиндексирован
    metadata     jsonb not null default '{}'::jsonb,
    unique (source_kind, external_id)
);
alter table engine.raw_messages owner to engine_rw;
create index if not exists raw_messages_embedding_hnsw on engine.raw_messages
    using hnsw (embedding vector_cosine_ops) where embedding is not null;
create index if not exists raw_messages_created_idx on engine.raw_messages (created_at);
create index if not exists raw_messages_metadata_gin on engine.raw_messages
    using gin (metadata jsonb_path_ops);

-- ─────────────────────────────────────────────────────────────────────────────
-- engine.accounts — PLATFORM пул userbot-сессий (vault-шифр., без tenant_id, без RLS).
-- ─────────────────────────────────────────────────────────────────────────────
create table if not exists engine.accounts (
    id            uuid primary key default gen_random_uuid(),
    channel       text not null,                  -- telegram|vk
    label         text,
    phone_masked  text,
    ciphertext    bytea not null,                 -- AES-256-GCM конверт session-string (shared/vault.py)
    nonce         bytea not null,
    key_version   int not null default 1,         -- задел ротации → OpenBao P3 без смены DDL
    status        text not null default 'warmup'
                  check (status in ('warmup','active','floodwait','banned')),
    floodwait_until timestamptz,
    proxy_ref     text,
    warmup_since  timestamptz,
    last_used_at  timestamptz,
    last_error    text,                            -- НЕ логировать plaintext/session
    created_at    timestamptz not null default now(),
    unique (channel, label)
);
alter table engine.accounts owner to engine_rw;
create index if not exists accounts_channel_status_idx on engine.accounts (channel, status);
create index if not exists accounts_status_floodwait_idx on engine.accounts (status, floodwait_until);

-- ─────────────────────────────────────────────────────────────────────────────
-- engine.sources — TENANT-SCOPED (что читать). RLS ENABLE + tenant_isolation + panel_rw CRUD.
-- ─────────────────────────────────────────────────────────────────────────────
create table if not exists engine.sources (
    id             uuid primary key default gen_random_uuid(),
    tenant_id      uuid not null references public.tenants(id) on delete cascade,
    source_kind    text not null,
    kind           text,                           -- chat|channel|group|board|tender_region
    external_ref   text not null,
    title          text,
    enabled        boolean not null default true,
    last_polled_at timestamptz,
    cursor         text,
    created_at     timestamptz not null default now(),
    updated_at     timestamptz not null default now(),
    unique (tenant_id, source_kind, external_ref)
);
alter table engine.sources owner to engine_rw;
alter table engine.sources enable row level security;
drop policy if exists tenant_isolation on engine.sources;
create policy tenant_isolation on engine.sources for all
    using (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    with check (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);
grant select, insert, update, delete on engine.sources to panel_rw;
create index if not exists sources_tenant_kind_idx on engine.sources (tenant_id, source_kind);
create index if not exists sources_tenant_enabled_idx on engine.sources (tenant_id, enabled);

-- ─────────────────────────────────────────────────────────────────────────────
-- engine.search_profiles — TENANT-SCOPED (что искать). RLS + panel_rw CRUD.
-- ─────────────────────────────────────────────────────────────────────────────
create table if not exists engine.search_profiles (
    id             uuid primary key default gen_random_uuid(),
    tenant_id      uuid not null references public.tenants(id) on delete cascade,
    name           text not null,
    intent_keywords jsonb not null default '[]'::jsonb,
    industry       text,
    geo            jsonb,
    min_intent_score numeric,
    min_urgency    numeric,
    enabled        boolean not null default true,
    created_at     timestamptz not null default now(),
    updated_at     timestamptz not null default now(),
    unique (tenant_id, name)
);
alter table engine.search_profiles owner to engine_rw;
alter table engine.search_profiles enable row level security;
drop policy if exists tenant_isolation on engine.search_profiles;
create policy tenant_isolation on engine.search_profiles for all
    using (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    with check (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);
grant select, insert, update, delete on engine.search_profiles to panel_rw;
create index if not exists profiles_tenant_enabled_idx on engine.search_profiles (tenant_id, enabled);
create index if not exists profiles_tenant_created_idx on engine.search_profiles (tenant_id, created_at);

-- ─────────────────────────────────────────────────────────────────────────────
-- engine.identities + identity_edges — SHARED граф личности (S15/P4, семантическая надстройка).
-- Exact-match идентичность остаётся на public.leads (Layer-C) — НЕ дублировать.
-- ─────────────────────────────────────────────────────────────────────────────
create table if not exists engine.identities (
    id          uuid primary key default gen_random_uuid(),
    channel     text,                              -- telegram|vk|max (форма account_identities, её НЕ трогать)
    external_id text,
    phone_hash  text,                              -- shared/hashing (S15), тот же алгоритм что funnel/handlers/панель
    embedding   vector(768),                       -- e5-base passage:, семантическая близость узлов
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now(),
    unique (channel, external_id)
);
alter table engine.identities owner to engine_rw;
create index if not exists identities_phone_idx on engine.identities (phone_hash) where phone_hash is not null;
create index if not exists identities_embedding_hnsw on engine.identities
    using hnsw (embedding vector_cosine_ops) where embedding is not null;

create table if not exists engine.identity_edges (
    id          bigint generated always as identity primary key,
    identity_a  uuid not null references engine.identities(id) on delete cascade,
    identity_b  uuid not null references engine.identities(id) on delete cascade,
    edge_kind   text,                              -- phone|semantic|manual
    confidence  numeric,
    evidence    jsonb,
    created_at  timestamptz not null default now(),
    unique (identity_a, identity_b, edge_kind)
);
alter table engine.identity_edges owner to engine_rw;

-- ─────────────────────────────────────────────────────────────────────────────
-- engine.matching — ENGINE-INTERNAL join (search_profile→raw_message→lead) + тенант-атрибуция.
-- Зависит от search_profiles + raw_messages (выше) + public.tenants/leads (снапшот).
-- ─────────────────────────────────────────────────────────────────────────────
create table if not exists engine.matching (
    id                bigint generated always as identity primary key,
    search_profile_id uuid not null references engine.search_profiles(id) on delete cascade,
    raw_message_id    bigint not null references engine.raw_messages(id) on delete cascade,
    tenant_id         uuid not null references public.tenants(id) on delete cascade,
    lead_id           uuid references public.leads(id) on delete set null,
    intent_score      numeric,
    urgency           numeric,
    status            text not null default 'matched'
                      check (status in ('matched','forwarded','skipped')),
    matched_at        timestamptz not null default now(),
    unique (search_profile_id, raw_message_id)
);
alter table engine.matching owner to engine_rw;
create index if not exists matching_tenant_status_idx on engine.matching (tenant_id, status);
create index if not exists matching_raw_idx on engine.matching (raw_message_id);
create index if not exists matching_lead_idx on engine.matching (lead_id) where lead_id is not null;

-- ─────────────────────────────────────────────────────────────────────────────
-- public.lead_feedback — TENANT-SCOPED append-only фидбек → ScoreAgent (B-SCORE).
-- В public (не engine): писатель = тенант через panel_rw, FK на public.leads, канон consent_events.
-- Owner подхватит owner_reassign public-loop (gen_user). RLS защищает panel_rw.
-- ─────────────────────────────────────────────────────────────────────────────
create table if not exists public.lead_feedback (
    id          bigint generated always as identity primary key,
    tenant_id   uuid not null references public.tenants(id) on delete cascade,
    lead_id     uuid references public.leads(id) on delete set null,
    verdict     text not null check (verdict in ('bought','junk','wrong_geo','wrong_budget')),
    actor       text,
    provenance  text,
    occurred_at timestamptz not null default now()
);
alter table public.lead_feedback enable row level security;
drop policy if exists tenant_isolation on public.lead_feedback;
create policy tenant_isolation on public.lead_feedback for all
    using (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    with check (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);
grant select, insert on public.lead_feedback to panel_rw;
revoke update, delete on public.lead_feedback from panel_rw;   -- append-only
create index if not exists lead_feedback_tenant_lead_idx on public.lead_feedback (tenant_id, lead_id);
create index if not exists lead_feedback_tenant_time_idx on public.lead_feedback (tenant_id, occurred_at desc);
