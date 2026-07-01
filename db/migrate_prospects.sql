-- ── prospects: карточки компаний ЕГРЮЛ/ЕГРИП (обогащение по ИНН, per-lookup) ──
-- Пишет ПАНЕЛЬ (операторское действие), не бот. Tenant-scoped (RLS по app.tenant_id,
-- как leads/consent_events). Телефоны/email/закрытые категории НЕ хранятся (вырезает
-- провайдер dadata.py до записи; полей под них в схеме нет — defense-in-depth).
-- Источник — DaData find-party; повторный lookup по (tenant_id, inn) обновляет карточку.
--
-- ⚠️ DDL: twc-migrate.sh owner-DSN, СНАЧАЛА risuy_dev, ПЕРЕД деплоем кода. Идемпотентно.
-- Откат: drop table prospects;
--   bash ~/.claude/scripts/twc-migrate.sh 4171827 81.31.246.136 <db> gen_user db/migrate_prospects.sql

create table if not exists prospects (
    id                uuid primary key default gen_random_uuid(),
    tenant_id         uuid not null references tenants(id) on delete cascade,

    inn               text not null,
    kpp               text,
    ogrn              text,
    subject_type      text not null default 'legal'
                      check (subject_type in ('legal','individual')),

    name_short        text,
    name_full         text,
    opf               text,
    okved             text,
    okved_name        text,
    okveds            jsonb,
    address           text,        -- юр.адрес ЮЛ; для ИП — только город (адрес места жительства НЕ храним)
    region            text,
    city              text,
    status            text,        -- ACTIVE|LIQUIDATING|LIQUIDATED|BANKRUPT|REORGANIZING
    registration_date date,
    liquidation_date  date,
    management        jsonb,       -- руководитель ЮЛ (ФИО физлица = ПДн; не для рекламы, маскировать в LLM)

    lead_id           uuid references leads(id) on delete set null,

    source            text not null default 'dadata',   -- dadata|api-fns|manual
    raw               jsonb,       -- САНИТИЗИРОВАННЫЙ ответ (без phones/emails/закрытых категорий)
    fetched_at        timestamptz,
    archived          boolean not null default false,
    created_by        text,
    created_at        timestamptz not null default now(),
    updated_at        timestamptz not null default now(),

    unique (tenant_id, inn)
);

create index if not exists prospects_tenant_lead_idx on prospects (tenant_id, lead_id);
create index if not exists prospects_tenant_city_okved_idx on prospects (tenant_id, city, okved);

alter table prospects enable row level security;
do $$ begin
    if not exists (select 1 from pg_policies where tablename='prospects' and policyname='tenant_isolation') then
        create policy tenant_isolation on prospects
            for all
            using (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
            with check (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);
    end if;
end $$;

do $$ begin
    if exists (select 1 from pg_roles where rolname='panel_rw') then
        grant select, insert, update on prospects to panel_rw;  -- без delete (канон: archived-флаг)
    end if;
end $$;
