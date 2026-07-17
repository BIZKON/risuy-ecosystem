-- partners: реестр партнёров реферальной программы (платформенный артефакт, БЕЗ RLS, как tenants).
-- Применение: apply_migration.py (APPLY_EXPECT_DB=risuy_dev|risuy). Аддитивно, идемпотентно.
create table if not exists partners (
    id         uuid primary key default gen_random_uuid(),
    name       text not null,
    ref_code   text not null unique,             -- авто secrets.token_hex(4)
    tg_chat_id text,                             -- для уведомлений партнёру (может быть пустым)
    status     text not null default 'active',
    created_at timestamptz not null default now(),
    constraint partners_status_chk check (status in ('active','disabled'))
);
-- Атрибуция тенанта партнёру + кто создал (дедуп/rate-limit реф-потока).
alter table tenants add column if not exists partner_id     uuid references partners(id);
alter table tenants add column if not exists ref_tg_user_id bigint;
create index if not exists tenants_partner_idx on tenants (partner_id) where partner_id is not null;

do $$ begin
    if exists (select 1 from pg_roles where rolname='panel_rw') then
        grant select, insert, update on partners to panel_rw;
    end if;
end $$;
