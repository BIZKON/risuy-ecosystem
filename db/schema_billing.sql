-- Reseller-платформа, Wave 0 (ТЗ §4.3): BILLING — деньги ВНУТРЬ платформы.
-- Подписки тенантов и пополнения кошелька. Применять ПОСЛЕ schema_tenancy.sql.
-- Идемпотентно.
--
-- ДЕНЬГИ: только целые микро-рубли (bigint, 1 RUB = 1_000_000 µRUB) — DECISIONS п.1.
-- Существующие service_invoices/orders (numeric) — legacy, не трогаем здесь.

create extension if not exists "pgcrypto";

-- ── plans: тарифы платформы. multiplier/цена сообщения — ТОЛЬКО здесь (сервер) ─
create table if not exists plans (
    id                        uuid primary key default gen_random_uuid(),
    code                      text unique not null,        -- 'econom','start','custom'
    name                      text not null,
    price_microrub            bigint not null,             -- цена подписки за период
    "interval"                text not null default 'month'
                              check ("interval" in ('month','year')),
    included_credits_microrub bigint not null default 0,   -- кредиты, входящие в период
    billing_mode              text not null default 'cost_multiplier'
                              check (billing_mode in ('cost_multiplier','per_message')),
    markup_multiplier         numeric(4,2) not null default 3.00,
    per_message_microrub      bigint,                      -- для billing_mode='per_message'
    features                  jsonb not null default '{}'::jsonb,
    constraint plans_per_message_chk
        check (billing_mode <> 'per_message' or per_message_microrub is not null)
);

-- FK tenants.plan_id → plans (tenants создан раньше plans, поэтому FK здесь)
do $$ begin
    if not exists (select 1 from pg_constraint where conname = 'tenants_plan_id_fkey') then
        alter table tenants add constraint tenants_plan_id_fkey
            foreign key (plan_id) references plans(id);
    end if;
end $$;

-- seed: перенос действующего прайса «ИИ-Агент Про» (admin-panel/config.py
-- SERVICE_PLANS) в БД. Режим per_message = совместимость с витриной
-- («500 сообщений ИИ, 7,5 ₽ сверх»); новые планы по умолчанию — cost_multiplier ×3.
-- included_credits = квота × цена сообщения (кредитный эквивалент квоты).
insert into plans (code, name, price_microrub, "interval", included_credits_microrub,
                   billing_mode, markup_multiplier, per_message_microrub, features)
values
    ('econom', 'Эконом',         3750000000, 'month', 3750000000,
     'per_message', 3.00, 7500000,
     '{"payable": true,  "quota_messages": 500,  "marketing_price": "2 250 ₽ / 3 750 ₽"}'::jsonb),
    ('start',  'Стартовый',      7500000000, 'month', 7500000000,
     'per_message', 3.00, 5000000,
     '{"payable": true,  "quota_messages": 1500, "marketing_price": "7 500 ₽ в месяц"}'::jsonb),
    ('custom', 'Индивидуальный', 0,          'month', 0,
     'cost_multiplier', 3.00, null,
     '{"payable": false, "by_request": true}'::jsonb)
on conflict (code) do nothing;

-- ── subscriptions: подписка тенанта (ЮKassa не имеет объектов-подписок — сами) ─
create table if not exists subscriptions (
    id                         uuid primary key default gen_random_uuid(),
    tenant_id                  uuid not null references tenants(id),
    plan_id                    uuid not null references plans(id),
    status                     text not null default 'trialing'
                               check (status in ('trialing','active','past_due','canceled')),
    current_period_start       timestamptz not null,
    current_period_end         timestamptz not null,
    yookassa_payment_method_id text,         -- сохранённый метод для автосписаний
    created_at                 timestamptz not null default now()
);
create index if not exists subscriptions_tenant_idx on subscriptions (tenant_id, created_at desc);
create index if not exists subscriptions_period_end_idx on subscriptions (current_period_end)
    where status in ('trialing','active');   -- скан автосписаний

-- ── payments: входящие платежи платформы (подписка | топап кошелька) ─────────
create table if not exists payments (
    id                  uuid primary key default gen_random_uuid(),
    tenant_id           uuid not null references tenants(id),
    type                text not null check (type in ('subscription','topup')),
    yookassa_payment_id text unique,
    idempotence_key     text unique not null,
    amount_microrub     bigint not null check (amount_microrub > 0),
    status              text not null default 'pending'
                        check (status in ('pending','waiting_for_capture','succeeded','canceled')),
    captured_at         timestamptz,
    raw                 jsonb,               -- сырой ответ ЮKassa для аудита
    created_at          timestamptz not null default now()
);
create index if not exists payments_tenant_idx on payments (tenant_id, created_at desc);

-- ── webhook_events: журнал идемпотентности входящих уведомлений ──────────────
-- Вебхук ЮKassa повторяет доставку; перепроверка платежа по id (re-fetch) уже
-- реализована в панели и СОХРАНЯЕТСЯ — журнал добавляет дедуп повторов.
-- Платформенный (без tenant_id): событие матчится к платежу ПОСЛЕ дедупа.
create table if not exists webhook_events (
    id           uuid primary key default gen_random_uuid(),
    provider     text not null default 'yookassa',
    external_id  text unique not null,        -- id события/платежа от провайдера
    event_type   text,
    payload      jsonb,
    status       text not null default 'received'
                 check (status in ('received','processed','failed')),
    processed_at timestamptz
);

-- ── RLS: subscriptions/payments — tenant-scoped ──────────────────────────────
alter table subscriptions enable row level security;
alter table payments      enable row level security;
do $$ begin
    if not exists (select 1 from pg_policies
                   where tablename = 'subscriptions' and policyname = 'tenant_isolation') then
        create policy tenant_isolation on subscriptions
            for all
            using (tenant_id = current_setting('app.tenant_id', true)::uuid)
            with check (tenant_id = current_setting('app.tenant_id', true)::uuid);
    end if;
    if not exists (select 1 from pg_policies
                   where tablename = 'payments' and policyname = 'tenant_isolation') then
        create policy tenant_isolation on payments
            for all
            using (tenant_id = current_setting('app.tenant_id', true)::uuid)
            with check (tenant_id = current_setting('app.tenant_id', true)::uuid);
    end if;
end $$;
-- plans и webhook_events — платформенные справочник/журнал, без RLS (гранты ниже).

-- ── Гранты panel_rw (зеркалятся в panel_role.sql) ────────────────────────────
do $$ begin
    if exists (select 1 from pg_roles where rolname = 'panel_rw') then
        grant select, insert, update on plans          to panel_rw;  -- правит платформа-админ из панели
        grant select, insert, update on subscriptions  to panel_rw;
        grant select, insert, update on payments       to panel_rw;
        grant select, insert, update on webhook_events to panel_rw;
    end if;
end $$;
