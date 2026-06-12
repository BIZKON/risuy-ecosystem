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
