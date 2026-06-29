-- RF-RAG: своя база знаний на pgvector (НЕ managed-KB Timeweb, НЕ OpenAI).
-- Вектор-стор — расширение pgvector на ТОМ ЖЕ кластере, где leads/orders (общий,
-- персистентный, в бэкапах). Эмбеддер — self-host TEI (intfloat/multilingual-e5-base,
-- 768-dim) на отдельной VM; в БД попадают только готовые векторы. Контент в OpenAI/США
-- НЕ уходит — всё в РФ-инфре (152-ФЗ ок для справки и для запросов).
--
-- ⚠️ ПОРЯДОК (строго):
--   0) Включить расширение pgvector в UI Timeweb DBaaS → Конфигурация → Расширения
--      (CREATE EXTENSION без этого падает — managed-PG не даёт включать из SQL).
--   1) Применить этот файл owner-DSN ПЕРЕД деплоем кода:
--        bash ~/.claude/scripts/twc-migrate.sh 4171827 81.31.246.136 risuy gen_user db/schema_kb.sql
--   2) db/panel_role.sql (гранты panel_rw на kb_* зеркалятся там же).
-- Идемпотентно (IF NOT EXISTS) — применять можно повторно без ошибок.
--
-- Модель доступа:
--   • БОТ ходит под owner-ролью → читает kb_chunks без отдельного гранта (retrieval).
--   • ПАНЕЛЬ/ингест под panel_rw → пишет документы и чанки (гранты ниже + panel_role.sql).
-- role_tag: NULL/'' = общая справка (видна ВСЕМ ролям); конкретный слаг персоны
--   (config.PERSONA_PRESETS) = чанк только для этой роли. Фильтр retrieval — по role_tag.

create extension if not exists "vector";   -- pgvector (включить в UI DBaaS, см. шаг 0)
create extension if not exists "pgcrypto"; -- gen_random_uuid() (уже есть из schema.sql)

-- Исходные документы базы знаний (для аудита и пере-чанкинга).
create table if not exists kb_documents (
    id          uuid primary key default gen_random_uuid(),
    title       text not null,
    source      text,                      -- происхождение: имя файла / URL / «панель»
    role_tag    text,                      -- слаг персоны или NULL = общая справка
    content     text not null,             -- исходный текст целиком
    created_by  text,                      -- актор панели / «script»
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now()
);

-- Чанки + эмбеддинги. embedding NULL = ещё не проиндексирован (ингест проставит вектор).
create table if not exists kb_chunks (
    id           bigint generated always as identity primary key,
    document_id  uuid not null references kb_documents(id) on delete cascade,
    chunk_index  int  not null,
    content      text not null,
    embedding    vector(768),              -- intfloat/multilingual-e5-base
    metadata     jsonb not null default '{}'::jsonb,  -- {role_tag,title,source} для фильтра
    created_at   timestamptz not null default now()
);

create index if not exists kb_chunks_doc_idx on kb_chunks (document_id);
-- HNSW по косинусу: основной индекс retrieval (top-k ближайших по `embedding <=> $q`).
create index if not exists kb_chunks_embedding_idx
    on kb_chunks using hnsw (embedding vector_cosine_ops);
-- GIN по metadata: быстрый фильтр по role_tag/темам в гибридном поиске.
create index if not exists kb_chunks_meta_idx
    on kb_chunks using gin (metadata jsonb_path_ops);

-- ── Гранты panel_rw (least-privilege). Бот (owner) грантов не требует. ──
-- Зеркалятся в db/panel_role.sql (перевыдаются при реконсиляции Timeweb).
-- identity-колонка kb_chunks.id отдельного гранта на sequence НЕ требует (в отличие от serial).
do $$ begin
    if exists (select 1 from pg_roles where rolname = 'panel_rw') then
        grant select, insert, update, delete on kb_documents to panel_rw;
        grant select, insert, update, delete on kb_chunks    to panel_rw;
    end if;
end $$;

-- ── СП-2a: per-tenant изоляция базы знаний ──────────────────────────────────────
-- tenant_id NULL = платформенная/School-справка (видна owner-боту School-пути; в панели —
-- только при ОТСУТСТВИИ активного клиента). Не-NULL = знание конкретного тенанта.
-- Бэкфилл не нужен: существующие School-строки остаются NULL. Идемпотентно.
alter table kb_documents add column if not exists tenant_id uuid;
alter table kb_chunks    add column if not exists tenant_id uuid;
-- FK с on delete cascade — ЯВНЫМ ADD CONSTRAINT: inline `references ... on delete cascade` в
-- ADD COLUMN НЕ проставляет cascade-action (confdeltype остаётся 'a'/no-action). Идемпотентно по имени.
do $$ begin
  if not exists (select 1 from pg_constraint where conname='kb_documents_tenant_id_fkey') then
    alter table kb_documents add constraint kb_documents_tenant_id_fkey
      foreign key (tenant_id) references tenants(id) on delete cascade;
  end if;
  if not exists (select 1 from pg_constraint where conname='kb_chunks_tenant_id_fkey') then
    alter table kb_chunks add constraint kb_chunks_tenant_id_fkey
      foreign key (tenant_id) references tenants(id) on delete cascade;
  end if;
end $$;
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
