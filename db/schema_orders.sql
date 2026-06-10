-- Платежи / заказы — раздел «Платежи» панели (Phase 1A: ручной учёт продаж).
-- Дополняет db/schema_products.sql (products) и db/schema.sql (leads). Идемпотентно
-- (IF NOT EXISTS) — применять можно повторно без ошибок.
--
-- ПОРЯДОК ПРИМЕНЕНИЯ (строго, см. §2 плана):
--   schema.sql → schema_admin.sql → schema_panel_ext.sql → schema_products.sql →
--   schema_orders.sql → panel_role.sql
-- Этот файл — ПОСЛЕ schema_products.sql (FK на products), ДО panel_role.sql
-- (гранты panel_rw на orders живут там).
--
-- Применить ОДИН РАЗ owner-DSN (роль-владелец БД, не panel_rw — у неё нет прав на DDL):
--   psql "$OWNER_DATABASE_URL" -f db/schema_orders.sql
--
-- ⚠️ Деплой DDL ПЕРЕД кодом: сначала owner-DSN накатывает этот файл на РАБОТАЮЩЕМ
-- стэке (all if not exists; старый код таблицу orders просто игнорирует), и ТОЛЬКО
-- потом выкатывается новый образ панели с разделом «Платежи».
--
-- ── Граница доступа (несущий инвариант) ───────────────────────────────────────
-- Phase 1A: панель INSERT/UPDATE 'manual'-заказы (оператор фиксирует продажу руками),
-- панель SELECT для дашборда. Phase 1B (онлайн-оплата): строки orders с source
-- 'yookassa'/'telegram_stars' пишет БОТ (owner) из вебхука провайдера — без отдельных
-- грантов панели. gen_random_uuid() требует pgcrypto (создан в schema_panel_ext.sql).

create extension if not exists "pgcrypto";

-- lead_id/product_id nullable + on delete set null: заказ может быть без привязки к
-- лиду (продажа вне воронки) или к оферу (разовая позиция), и переживает удаление
-- лида (152-ФЗ обезличивание) / архивацию офера — финансовая история не теряется.
-- amount — сумма позиции; currency по умолчанию RUB. status-машина pending→paid|
-- failed|refunded. source — канал оплаты (manual в 1A; провайдеры в 1B).
create table if not exists orders (
    id                  uuid          primary key default gen_random_uuid(),
    lead_id             uuid          references leads(id)    on delete set null,
    product_id          bigint        references products(id) on delete set null,
    amount              numeric(12,2) not null,
    currency            text          not null default 'RUB',
    status              text          not null default 'paid',     -- pending|paid|failed|refunded
    source              text          not null default 'manual',   -- manual|yookassa|telegram_stars
    provider_payment_id text,                                       -- id транзакции провайдера (1B)
    note                text,
    created_by          text,                                       -- actor панели (или 'bot' в 1B)
    created_at          timestamptz   not null default now(),
    paid_at             timestamptz,                                -- проставляется при status='paid'
    constraint orders_status_chk   check (status in ('pending','paid','failed','refunded')),
    constraint orders_currency_chk check (currency in ('RUB','USD','EUR')),
    constraint orders_amount_chk   check (amount >= 0)
);
create index if not exists orders_created_idx on orders (created_at desc);
create index if not exists orders_lead_idx    on orders (lead_id);
create index if not exists orders_status_idx  on orders (status);
-- UUID-PK с default gen_random_uuid() → секвенса нет, грант usage on sequence не нужен.

-- ── Phase 1B: онлайн-оплата ЮKassa (магазин ШКОЛЫ, отдельный от магазина подписки) ──
-- payment_url — confirmation_url платежа: повторный клик «Купить» в течение TTL
-- переиспользует ТОТ ЖЕ заказ и ссылку (анти-двойное списание). Пишут бот (owner,
-- кнопка в рассылке) И панель («выставить счёт» из диалога — грант в panel_role.sql).
alter table orders add column if not exists payment_url text;
-- Вебхук панели матчит заказ ПО id платежа провайдера (телу вебхука не доверяем).
create index if not exists orders_provider_payment_idx on orders (provider_payment_id)
    where provider_payment_id is not null;
