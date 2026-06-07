-- Расширение схемы панели «Школа Лесова»: переписка / перехват / рассылки / трекинг.
-- Дополняет db/schema.sql + db/schema_admin.sql — таблицу leads НЕ пересоздаёт,
-- только ДОБАВЛЯЕТ колонки (bot_paused / unsubscribed_at). Идемпотентно (IF NOT EXISTS) —
-- применять можно повторно без ошибок.
--
-- ПОРЯДОК ПРИМЕНЕНИЯ (строго, см. §2 плана):
--   db/schema.sql → db/schema_admin.sql → db/schema_panel_ext.sql → db/panel_role.sql
-- Этот файл — ПОСЛЕ schema_admin.sql, ДО panel_role.sql (гранты panel_rw на новые
-- объекты живут в panel_role.sql и ссылаются на созданные здесь sequence).
--
-- Применить ОДИН РАЗ owner-DSN (роль-владелец БД, не panel_rw — у неё нет прав на DDL):
--   psql "$OWNER_DATABASE_URL" -f db/schema_panel_ext.sql
--
-- ⚠️ Деплой DDL ПЕРЕД кодом: сначала owner-DSN накатывает этот файл на РАБОТАЮЩЕМ боте
-- (все if not exists; старый код новые колонки игнорирует), и ТОЛЬКО потом выкатывается
-- новый образ. Иначе get_due_followups с новым WHERE упадёт UndefinedColumn каждый тик.
--
-- ── Граница доступа (несущий инвариант) ───────────────────────────────────────
-- Панель ходит под panel_rw, БЕЗ BOT_TOKEN. Бот — под owner-ролью (gen_user).
--   • messages              — пишет БОТ (вход через middleware, исход через messaging-слой);
--                             панель только SELECT.
--   • outbox                — панель кладёт 'queued'; статусы ведёт БОТ.
--   • broadcasts            — панель создаёт заявку (draft→queued→canceled); старт/итоги — БОТ.
--   • broadcast_recipients  — материализует и ведёт статусы БОТ (единый WHERE «кому можно»);
--                             панель только SELECT.
--   • broadcast_files       — панель кладёт байты; tg_file_id/обнуление bytes — БОТ.
--   • link_tokens           — регистрирует панель при создании рассылки.
--   • link_clicks           — пишет обработчик /r/<token> в БОТЕ (owner).
--
-- ПДн-потоки (retention-cron §6 плана, обезличивание/TTL обязательны):
--   messages.text — высокочувствительный диалоговый ПДн; broadcast_recipients/link_clicks
--   несут lead_id. Чистка при erase_requested_at + ERASE_AFTER_DAYS и по абсолютному TTL.

create extension if not exists "pgcrypto";

-- ── Блок 2: перехват + отписка (флаги на leads) ──────────────────────────────
-- bot_paused      — оператор взял «ручное управление»: бот перестаёт авто-отвечать
--                   (глушится Лия + nurture; транзакционная воронка consent→phone→
--                   gate→deliver НЕ трогается). Семантика — db.is_bot_paused.
-- unsubscribed_at — отписка субъекта от рассылок/касаний (152-ФЗ); ставит САМ субъект
--                   через бота (/stop или inline 'unsub'), панель только видит.
--                   `is null` = подписан (отдельной булевы unsubscribed НЕТ).
--                   ≠ erase_requested_at (отзыв согласия на ПДн из панели) — разные сущности.
alter table leads add column if not exists bot_paused      boolean     not null default false;
alter table leads add column if not exists unsubscribed_at timestamptz;
create index if not exists leads_bot_paused_idx   on leads (bot_paused)      where bot_paused = true;
create index if not exists leads_unsubscribed_idx on leads (unsubscribed_at) where unsubscribed_at is not null;

-- ── Блок 1: переписка (пишет БОТ; панель только SELECT) ──────────────────────
-- lead_id nullable: резолв по tg_user_id может опоздать (входящее логируется ДО роутинга,
-- лида может ещё не быть). tg_user_id есть ВСЕГДА. direction in|out. kind — реальные типы
-- Telegram. source — классификатор исходящих: funnel|liya|nurture|manual|broadcast|system.
create table if not exists messages (
    id            bigserial   primary key,
    lead_id       uuid        references leads(id) on delete cascade,   -- nullable: резолв по tg_user_id может опоздать
    tg_user_id    bigint      not null,
    tg_message_id bigint,
    direction     text        not null,                                 -- in | out
    kind          text        not null default 'text',
    text          text,                                                 -- ПДн → retention-cron, §6 плана
    file_id       text,
    source        text,                                                 -- funnel|liya|nurture|manual|broadcast|system
    created_at    timestamptz not null default now(),
    constraint messages_direction_chk check (direction in ('in','out')),
    constraint messages_kind_chk check (kind in ('text','photo','document','video','voice','video_note','audio','animation','sticker','other'))
);
create index if not exists messages_lead_created_idx on messages (lead_id, created_at);
create index if not exists messages_tg_created_idx   on messages (tg_user_id, created_at);

-- ── Блок 3a: outbox (панель INSERT 'queued'; бот дренаж) ─────────────────────
-- tg_user_id денормализуется при постановке (панель знает адрес из лида). status-машина
-- queued→sending→sent|failed. claimed_at — для reclaim застрявших 'sending' после краша (§3 плана).
create table if not exists outbox (
    id         bigserial   primary key,
    lead_id    uuid        not null references leads(id) on delete cascade,
    tg_user_id bigint      not null,                                    -- денорм при постановке (панель знает из лида)
    kind       text        not null default 'text',
    text       text,
    file_id    text,
    status     text        not null default 'queued',                  -- queued|sending|sent|failed
    attempts   int         not null default 0,
    claimed_at timestamptz,                                             -- для reclaim после краша, §3 плана
    last_error text,
    created_by text        not null,
    created_at timestamptz not null default now(),
    sent_at    timestamptz,
    constraint outbox_status_chk check (status in ('queued','sending','sent','failed'))
);
create index if not exists outbox_queued_idx on outbox (created_at) where status = 'queued';

-- ── Блок 3b: broadcasts (панель INSERT заявку; бот ведёт) ────────────────────
-- id bigserial (не uuid: проще sequence-грант + сортировка/URL). messenger tg|max (max — задел,
-- disabled в композере). body_template несёт плейсхолдер {link} для /r (§5.7). audience_filter —
-- подмножество build_filters (jsonb, НЕ сырой SQL). recipient_count материализуется ДО старта.
-- totals — {sent,failed,skipped} сводка. status-машина draft→queued→sending→paused→done|canceled.
create table if not exists broadcasts (
    id              bigserial   primary key,
    title           text,
    messenger       text        not null default 'tg',                 -- tg | max (max — задел)
    kind            text        not null default 'text',
    body_template   text        not null,                              -- с плейсхолдером {link} для /r, §5.7 плана
    audience_filter jsonb       not null default '{}'::jsonb,          -- подмножество build_filters, НЕ сырой SQL
    status          text        not null default 'draft',              -- draft|queued|sending|paused|done|canceled
    recipient_count int,                                               -- материализуется ДО старта, §6/§7 плана
    created_by      text        not null,
    created_at      timestamptz not null default now(),
    started_at      timestamptz,
    finished_at     timestamptz,
    totals          jsonb       not null default '{}'::jsonb,          -- {sent,failed,skipped} сводка
    constraint broadcasts_status_chk check (status in ('draft','queued','sending','paused','done','canceled'))
);
create index if not exists broadcasts_created_idx on broadcasts (created_at desc);

-- ── Блок 3c/4: получатели (пишет БОТ; панель SELECT) ─────────────────────────
-- Материализует БОТ одним INSERT…SELECT FROM leads WHERE <§5.1> (детерминированный snapshot до
-- первой отправки), on conflict (broadcast_id,lead_id) do nothing. click_token — per-recipient
-- токен для атрибуции (§5). status-машина pending→sending→sent|failed|skipped (skipped — отписался/
-- erase между материализацией и send, §5.1 TOCTOU). unique(broadcast_id,lead_id) — идемпотентность.
create table if not exists broadcast_recipients (
    id           bigserial   primary key,
    broadcast_id bigint      not null references broadcasts(id) on delete cascade,
    lead_id      uuid        not null references leads(id)      on delete cascade,
    tg_user_id   bigint      not null,
    status       text        not null default 'pending',              -- pending|sending|sent|failed|skipped
    click_token  text,                                                -- per-recipient, §5 плана
    attempts     int         not null default 0,
    claimed_at   timestamptz,
    error        text,
    sent_at      timestamptz,
    unique (broadcast_id, lead_id),
    constraint br_status_chk check (status in ('pending','sending','sent','failed','skipped'))
);
create index if not exists br_pending_idx on broadcast_recipients (broadcast_id) where status = 'pending';
create index if not exists br_status_idx  on broadcast_recipients (broadcast_id, status);

-- ── Файл рассылки (один на рассылку, переиспускаемый file_id) ─────────────────
-- Панель кладёт bytes; БОТ первичной заливкой в СЛУЖЕБНЫЙ чат (OPS_CHAT_ID, §5.6 — НЕ на
-- первом получателе) получает tg_file_id, проставляет его и ОБНУЛЯЕТ bytes. Дальше рассылка по file_id.
create table if not exists broadcast_files (
    id           bigserial   primary key,
    broadcast_id bigint      not null references broadcasts(id) on delete cascade,
    filename     text,
    mime         text,
    bytes        bytea,                                                -- обнуляется ботом после получения file_id
    tg_file_id   text,                                                 -- бот проставляет после первой заливки в служебный чат
    created_at   timestamptz not null default now()
);

-- ── Блок 4: трекинг ссылок /r/<token> (токены — панель; клики — бот) ─────────
-- token — secrets.token_urlsafe(16). target_url — заранее зарегистрированный, allow-list http/https
-- (проверка И на записи в панели, И на чтении в /r бота — defence-in-depth, §6.3). Редирект /r/<token>
-- живёт в БОТЕ (его aiohttp _start_health на BOT_PUBLIC_BASE_URL), НЕ в панели.
create table if not exists link_tokens (
    token        text        primary key,                             -- secrets.token_urlsafe(16)
    target_url   text        not null,                                -- allow-list http/https, см. §6.3 плана
    broadcast_id bigint      references broadcasts(id) on delete cascade,
    lead_id      uuid        references leads(id) on delete set null,
    created_at   timestamptz not null default now()
);
create index if not exists link_tokens_broadcast_idx on link_tokens (broadcast_id);

-- link_clicks — пишет обработчик /r в БОТЕ (owner). ua обрезается [:512], НЕ в текст логов.
-- Клик = «переход по ссылке», НЕ доказанное действие лида (форвард/превью-бот/прокси), §6.3.
create table if not exists link_clicks (
    id           bigserial   primary key,
    token        text        not null references link_tokens(token) on delete cascade,
    broadcast_id bigint,
    lead_id      uuid        references leads(id) on delete set null,
    clicked_at   timestamptz not null default now(),
    ua           text,                                                -- обрезанный [:512], НЕ в текст логов
    ip           inet
);
create index if not exists link_clicks_token_idx     on link_clicks (token);
create index if not exists link_clicks_broadcast_idx on link_clicks (broadcast_id);
