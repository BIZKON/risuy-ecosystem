-- Reseller-платформа, Wave 0 (ТЗ §4.4): METERING — потребление НАРУЖУ (×3). Ядро.
-- Применять ПОСЛЕ schema_tenancy.sql. Идемпотентно.
--
-- Три гвоздя конструкции (ТЗ §5.1): SELECT ... FOR UPDATE на кошельке (овердрафт),
-- unique(idempotence_key) (двойное списание), целые µRUB + ceil_mul (дрейф float).
-- Списание делает ТОЛЬКО shared/metering.py::charge_usage() (Wave 3) одной транзакцией.

create extension if not exists "pgcrypto";

-- ── credit_wallets: баланс кошелька, АВТОРИТЕТНЫЙ источник ───────────────────
create table if not exists credit_wallets (
    tenant_id        uuid primary key references tenants(id),
    balance_microrub bigint not null default 0,  -- минус — только для postpaid-планов
    updated_at       timestamptz not null default now()
);

-- ── usage_ledger: APPEND-ONLY, каждая строка = одно списание ─────────────────
-- Append-only закреплён ГРАНТАМИ: panel_rw получает только select+insert
-- (update/delete НЕ выдаются никогда — история списаний неизменяема).
create table if not exists usage_ledger (
    id                     bigint generated always as identity primary key,
    tenant_id              uuid not null references tenants(id),
    occurred_at            timestamptz not null default now(),
    kind                   text not null check (kind in ('llm','embedding','message','other')),
    provider               text,                  -- 'timeweb-cloud-ai' | 'timeweb-ai-gateway'
    model                  text,                  -- 'deepseek-v4-pro-thinking', ...
    units                  jsonb not null default '{}'::jsonb, -- {tokens_in,tokens_out,tokens_total,messages}
    cost_microrub          bigint not null,       -- НАША себестоимость (до наценки)
    multiplier             numeric(4,2) not null, -- снимок множителя на момент списания
    charged_microrub       bigint not null,       -- ceil_mul(cost, multiplier) | per_message плана
    balance_after_microrub bigint not null,
    request_id             text,
    idempotence_key        text unique not null   -- ← защита от двойного списания
);
create index if not exists usage_ledger_tenant_idx on usage_ledger (tenant_id, occurred_at desc);

-- ── model_prices: НАШИ закупочные тарифы Timeweb (µRUB за 1k токенов) ────────
-- ПЕРЕД вписыванием новых цен — проверка актуальных тарифов в ЛК Timeweb
-- (guardrail ТЗ §10: цены дрейфуют, из памяти не вписывать).
create table if not exists model_prices (
    id                        bigint generated always as identity primary key,
    provider                  text not null,
    model                     text not null,
    price_in_microrub_per_1k  bigint not null,
    price_out_microrub_per_1k bigint not null,
    effective_from            timestamptz not null default now(),
    unique (provider, model, effective_from)
);

-- seed: DeepSeek V4 Pro Thinking — тариф проверен владельцем в ЛК 2026-06-12:
-- вход 234,9 ₽/млн = 234_900 µRUB/1k; выход 469,8 ₽/млн = 469_800 µRUB/1k.
-- Агент Лии (180177) переведён на эту модель 2026-06-12 (model_id 135).
insert into model_prices (provider, model, price_in_microrub_per_1k, price_out_microrub_per_1k)
select 'timeweb-cloud-ai', 'deepseek-v4-pro-thinking', 234900, 469800
where not exists (select 1 from model_prices
                  where provider = 'timeweb-cloud-ai' and model = 'deepseek-v4-pro-thinking');

-- ── agent_token_snapshots: основа метеринга по дельте used_tokens (ТЗ §5.2) ──
-- cloud-ai /call НЕ отдаёт usage per-call (факт прода 2026-06-12); агент
-- принадлежит ровно одному тенанту → дельта used_tokens между снапшотами = его
-- точный расход. Снимает фоновый воркер бота (Wave 3).
create table if not exists agent_token_snapshots (
    agent_id    bigint not null,                  -- числовой id агента Timeweb
    tenant_id   uuid not null references tenants(id),
    used_tokens bigint not null,
    taken_at    timestamptz not null default now(),
    primary key (agent_id, taken_at)
);
create index if not exists agent_snapshots_tenant_idx on agent_token_snapshots (tenant_id, taken_at desc);

-- ── RLS ──────────────────────────────────────────────────────────────────────
alter table credit_wallets        enable row level security;
alter table usage_ledger          enable row level security;
alter table agent_token_snapshots enable row level security;
do $$ begin
    if not exists (select 1 from pg_policies
                   where tablename = 'credit_wallets' and policyname = 'tenant_isolation') then
        create policy tenant_isolation on credit_wallets
            for all
            using (tenant_id = current_setting('app.tenant_id', true)::uuid)
            with check (tenant_id = current_setting('app.tenant_id', true)::uuid);
    end if;
    if not exists (select 1 from pg_policies
                   where tablename = 'usage_ledger' and policyname = 'tenant_isolation') then
        create policy tenant_isolation on usage_ledger
            for all
            using (tenant_id = current_setting('app.tenant_id', true)::uuid)
            with check (tenant_id = current_setting('app.tenant_id', true)::uuid);
    end if;
    if not exists (select 1 from pg_policies
                   where tablename = 'agent_token_snapshots' and policyname = 'tenant_isolation') then
        create policy tenant_isolation on agent_token_snapshots
            for all
            using (tenant_id = current_setting('app.tenant_id', true)::uuid)
            with check (tenant_id = current_setting('app.tenant_id', true)::uuid);
    end if;
end $$;
-- model_prices — платформенный справочник (себестоимость!): клиентскому коду
-- панели НЕ отдаётся в шаблоны; грант select нужен платформенным экранам.

-- ── Гранты panel_rw (зеркалятся в panel_role.sql) ────────────────────────────
do $$ begin
    if exists (select 1 from pg_roles where rolname = 'panel_rw') then
        grant select, insert, update on credit_wallets        to panel_rw;  -- топап из вебхука панели
        grant select, insert         on usage_ledger          to panel_rw;  -- APPEND-ONLY: без update/delete
        grant select, insert         on model_prices          to panel_rw;  -- цены добавляются, не правятся
        grant select, insert         on agent_token_snapshots to panel_rw;
    end if;
end $$;
