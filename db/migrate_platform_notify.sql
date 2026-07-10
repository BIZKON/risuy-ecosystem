-- platform_notify: очередь НЕ-лидовых уведомлений (владелец платформы; в Спеке 2 — партнёры).
-- outbox lead-scoped (lead_id NOT NULL) → отдельная таблица. БЕЗ RLS (платформенный артефакт).
-- Применение: twc-migrate.sh 4171827 81.31.246.136 risuy_dev gen_user db/migrate_platform_notify.sql
create table if not exists platform_notify (
    id         bigserial primary key,
    chat_id    bigint not null,
    text       text   not null,
    status     text   not null default 'queued',
    attempts   int    not null default 0,
    last_error text,
    created_at timestamptz not null default now(),
    sent_at    timestamptz,
    constraint platform_notify_status_chk check (status in ('queued','sending','sent','failed'))
);
create index if not exists platform_notify_queued_idx on platform_notify (created_at) where status='queued';

do $$ begin
    if exists (select 1 from pg_roles where rolname='panel_rw') then
        grant select, insert, update on platform_notify to panel_rw;
        grant usage, select on sequence platform_notify_id_seq to panel_rw;
    end if;
end $$;
