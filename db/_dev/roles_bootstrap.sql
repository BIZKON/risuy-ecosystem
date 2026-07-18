-- Локальный bootstrap ролей и схемы engine для эфемерного PG (docker-compose.dev).
-- В ПРОДЕ роли panel_rw/engine_rw уже есть; здесь — только для локали. НЕ для прода.
-- Пароли — локальные, фиктивные (совпадают с docker-compose.dev.yml).

-- panel_rw (как в проде: не-owner, без bypassrls) — нужен для rls_leads_messages_smoke.
do $$ begin
  if not exists (select 1 from pg_roles where rolname = 'panel_rw') then
    create role panel_rw login password 'panel_rw_local';
  end if;
  if not exists (select 1 from pg_roles where rolname = 'engine_rw') then
    create role engine_rw login password 'engine_rw_local';
  end if;
end $$;

-- Схема сырья движка (решение Q10 — отдельная схема, не инстанс).
create schema if not exists engine authorization engine_rw;

-- Заглушка raw_messages ТОЛЬКО под walking-skeleton S0M. Финальный DDL — S1-RAW.
create table if not exists engine.raw_messages (
    id          bigserial primary key,
    created_at  timestamptz not null default now(),
    tenant_id   text not null,
    source_kind text not null,
    external_id text not null,
    text        text,
    unique (source_kind, external_id)
);

-- Гранты engine_rw на схему engine.
grant usage on schema engine to engine_rw;
grant select, insert, update on all tables in schema engine to engine_rw;
grant usage, select on all sequences in schema engine to engine_rw;
alter default privileges in schema engine grant select, insert, update on tables to engine_rw;

-- Гранты engine_rw на public.leads (forward-совместимо под B-FWD; RLS применяется — не owner).
grant usage on schema public to engine_rw;
grant select, insert, update on public.leads to engine_rw;
