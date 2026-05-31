-- Схема базы «Рисуй с душой» для Timeweb Managed PostgreSQL.
-- Одна таблица leads — общая для Telegram-бота, MAX-бота (позже) и панели.
-- Применить один раз: psql "$DATABASE_URL" -f db/schema.sql

create extension if not exists "pgcrypto";  -- gen_random_uuid()

create table if not exists leads (
    id              uuid primary key default gen_random_uuid(),
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now(),

    messenger       text not null default 'tg',     -- tg | max
    source          text not null default 'other',  -- reels|dzen|youtube|vk|max|other (метка площадки)

    name            text,
    phone           text,
    phone_hash      text,                            -- sha256(только цифры) — для склейки TG и MAX

    consent         boolean not null default false,  -- согласие на обработку ПДн (152-ФЗ)
    subscribed      boolean not null default false,  -- прошёл гейт подписки на канал

    status          text not null default 'new',     -- new|guide_sent|nurturing|converted|lost

    guide_sent_at   timestamptz,                     -- когда выдан гайд
    follow_up_1_at  timestamptz,                     -- ФАКТ. время отправки касания (null = ещё не отправлено)
    follow_up_2_at  timestamptz,
    follow_up_3_at  timestamptz,

    tg_user_id      bigint,
    max_user_id     bigint,

    notes           text,                            -- заметки Насти (ручные, из панели)
    survey          jsonb                            -- задел на опрос (Фаза 3), пока не используется
);

-- NULL в уникальном индексе считаются разными → у MAX-лидов tg_user_id=NULL не конфликтует, и наоборот.
create unique index if not exists leads_tg_user_id_key  on leads (tg_user_id);
create unique index if not exists leads_max_user_id_key on leads (max_user_id);

create index if not exists leads_phone_hash_idx on leads (phone_hash);
create index if not exists leads_created_at_idx on leads (created_at desc);
create index if not exists leads_source_idx     on leads (source);
create index if not exists leads_status_idx     on leads (status);
create index if not exists leads_messenger_idx  on leads (messenger);

-- updated_at автоматически
create or replace function set_updated_at() returns trigger as $$
begin
    new.updated_at = now();
    return new;
end;
$$ language plpgsql;

drop trigger if exists trg_leads_updated_at on leads;
create trigger trg_leads_updated_at
    before update on leads
    for each row execute function set_updated_at();
