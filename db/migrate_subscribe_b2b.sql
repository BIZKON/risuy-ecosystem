-- Токен-биллинг v2, T-1F-3b: лендинг-провижининг + B2B-гейт. EXPAND-first, идемпотентно.
-- Применять twc-migrate.sh owner-DSN: СНАЧАЛА risuy_dev, прод risuy — за явным «да» (ПЕРЕД деплоем кода).
--
-- Решения #5/#6 + адверс-ревью дизайна:
--   • identity покупателя → ОТДЕЛЬНАЯ tenant-scoped RLS-таблица (НЕ колонки tenants: tenants без
--     RLS, ИНН ИП = ПДн физлица — 152-ФЗ; ревью H-7).
--   • согласие → append-only consent_events (уже есть; payments НЕ трогаем — ревью M-10).
--   • pending_service_purchase — серверная pre-tenant покупка (ПДн НЕ в metadata ЮKassa; TTL-purge).
--   • admin_users.password_set — гейт claim-письма (ревью M-9).
-- Гранты panel_rw живут ЗДЕСЬ (панель подключается как panel_rw; panel_role.sql применяется ВРУЧНУЮ).
-- ⚠️ FOLLOW-UP: при следующей ревизии db/panel_role.sql зеркалировать эти гранты в guarded
--    to_regclass do-блоке (иначе его mass-revoke их снимет). Сейчас не трогаем — файл параллельной задачи.

-- ── 1. identity покупателя (стабильная орг-идентичность), tenant-scoped RLS ──
create table if not exists tenant_billing_identity (
    tenant_id          uuid primary key references tenants(id) on delete cascade,
    buyer_inn          text not null,
    buyer_ogrnip       text,
    buyer_subject_type text not null check (buyer_subject_type in ('legal', 'individual')),
    updated_at         timestamptz not null default now()
);
alter table tenant_billing_identity enable row level security;
do $$ begin
    if not exists (select 1 from pg_policies
                   where tablename = 'tenant_billing_identity' and policyname = 'tenant_isolation') then
        create policy tenant_isolation on tenant_billing_identity
            for all
            using (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
            with check (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);
    end if;
end $$;
grant select, insert, update on tenant_billing_identity to panel_rw;

-- ── 2. признак «пароль задан пользователем» (гейт claim; ревью M-9) ──
alter table admin_users add column if not exists password_set boolean not null default false;

-- ── 3. серверная pre-tenant покупка (ПДн вне metadata ЮKassa; TTL-purge) ──
create table if not exists pending_service_purchase (
    id                 uuid primary key default gen_random_uuid(),   -- purchase_ref (не-ПДн) в metadata
    email              text not null,
    buyer_inn          text not null,                                -- чистый B2B: ИНН обязателен (#5)
    buyer_ogrnip       text,
    buyer_subject_type text not null check (buyer_subject_type in ('legal', 'individual')),
    is_entrepreneur    boolean not null,
    plan_code          text not null,
    offer_version      text,
    offer_text_hash    text,
    agree_pdn          boolean not null,
    consent_at         timestamptz not null,
    idempotence_key    text not null unique,                         -- детерминированный, == ЮKassa idem-key
    yookassa_payment_id text,
    status             text not null default 'pending'
                       check (status in ('pending', 'claimed', 'failed', 'expired')),
    tenant_id          uuid,                                         -- заполняется после провижининга
    created_at         timestamptz not null default now(),
    claimed_at         timestamptz
);
create index if not exists pending_service_purchase_ykid_idx
    on pending_service_purchase (yookassa_payment_id);
-- Гонко-безопасный дедуп (ревью H-2 concurrent-first-submit): не более ОДНОГО живого pending
-- на (email, plan) → второй одновременный INSERT упрётся в конфликт → reuse_or_create_pending
-- ловит UniqueViolation и переиспользует. Claimed/failed/expired снимают ограничение (повтор покупки).
create unique index if not exists pending_service_purchase_one_open_idx
    on pending_service_purchase (lower(email), plan_code) where status = 'pending';
-- Платформенная (pre-tenant) → БЕЗ RLS. Операционная (purgeable) → panel_rw с delete.
grant select, insert, update, delete on pending_service_purchase to panel_rw;
