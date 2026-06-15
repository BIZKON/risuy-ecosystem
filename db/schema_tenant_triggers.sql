-- Слой B движка триггеров (docs/tenant-triggers-escalation.md): клиент САМ создаёт триггеры
-- (стоп-слова / намерение / кол-во сообщений / документы) → действие (уведомить менеджеров в
-- свою группу через бот-нотификатор + готовый ответ клиенту). Эталон UX — конкурент «Нейроагенты».
--
-- tenant-scoped, RLS deny-by-default (как tenant_settings): панель пишет ПОСЛЕ
-- set_config('app.tenant_id'); бот (owner) обходит RLS, фильтрует tenant_id явно.
-- Дедуп — НЕТ (решение владельца «каждый раз»): триггер срабатывает на каждое подходящее
-- сообщение. Уведомитель — ЕДИНЫЙ сервис-бот (NOTIFIER_BOT_TOKEN), клиент добавляет его в группу.
--
-- ПРИМЕНЕНИЕ: bash ~/.claude/scripts/twc-migrate.sh 4171827 81.31.246.136 <db> gen_user \
--   /abs/.../db/schema_tenant_triggers.sql   — СНАЧАЛА risuy_dev. Идемпотентно (IF NOT EXISTS).
-- Новая таблица → RLS включаем сразу (нет существующих читателей/данных → expand-contract не нужен).
-- Гранты panel_rw — TABLE-level (как tenant_settings; column-INSERT-грабли №8 нет).

create extension if not exists "pgcrypto";  -- gen_random_uuid()

create table if not exists tenant_triggers (
    id              uuid primary key default gen_random_uuid(),
    tenant_id       uuid not null references tenants(id) on delete cascade,
    channel         text not null default 'telegram',     -- канал РАЗГОВОРА (telegram|vk|max — Слой C)
    type            text not null
                    check (type in ('stopwords','intent','message_count','documents')),
    action          text not null default 'notify_reply_continue'
                    check (action in ('notify_reply_continue','notify_reply_pause','notify_only')),
    -- условие (по типу; незадействованные поля — дефолт):
    stopwords       text[]  not null default '{}',         -- type=stopwords
    intent_desc     text    not null default '',           -- type=intent (описание «когда сработать»)
    msg_count       int,                                   -- type=message_count (порог N)
    -- уведомление + ответ:
    notify_chat_id  text    not null default '',           -- -100… (через бот-нотификатор)
    notify_topic_id int,                                   -- опц. тема форума
    reply_text      text    not null default '',           -- готовый ответ клиенту при срабатывании
    enabled         boolean not null default true,
    position        int     not null default 0,            -- порядок в списке/приоритет
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now()
);

create index if not exists tenant_triggers_lookup_idx
    on tenant_triggers (tenant_id, enabled, type);

alter table tenant_triggers enable row level security;
do $$ begin
    if not exists (select 1 from pg_policies
                   where tablename = 'tenant_triggers' and policyname = 'tenant_isolation') then
        create policy tenant_isolation on tenant_triggers
            for all
            using (tenant_id = current_setting('app.tenant_id', true)::uuid)
            with check (tenant_id = current_setting('app.tenant_id', true)::uuid);
    end if;
end $$;

do $$ begin
    if exists (select 1 from pg_roles where rolname = 'panel_rw') then
        grant select, insert, update, delete on tenant_triggers to panel_rw;
    end if;
end $$;
