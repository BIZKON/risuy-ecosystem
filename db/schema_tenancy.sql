-- Reseller-платформа, Wave 0 (ТЗ docs/reseller-platform-tz.md §4.1): TENANCY.
-- Тенант = клиент платформы «ИИ-Агент Про» (свой бот, свои данные, свой кошелёк).
--
-- ПОРЯДОК ПРИМЕНЕНИЯ (строго, владелец-DSN, СНАЧАЛА risuy_dev, потом прод):
--   schema_tenancy.sql → schema_billing.sql → schema_metering.sql →
--   schema_vault.sql → migrate_tenant_scope.sql → panel_role.sql
--   bash ~/.claude/scripts/twc-migrate.sh 4171827 81.31.246.136 <db> gen_user db/schema_tenancy.sql ...
-- Идемпотентно (IF NOT EXISTS) — применять можно повторно.
--
-- RLS-модель (решение DECISIONS.md 2026-06-12 п.7):
--   • tenant-scoped таблицы → policy по current_setting('app.tenant_id', true);
--     без выставленного контекста panel_rw НЕ видит строк (deny-by-default).
--   • tenants и memberships — БЕЗ RLS: панель резолвит «мои тенанты» по
--     memberships ДО установки контекста (доступ ограничен грантами; чувствительных
--     данных в tenants нет — slug/name/status).
--   • Бот ходит под owner (gen_user) — owner обходит RLS (доверенный код,
--     контекст тенанта в мультиплексе — Wave 3).

create extension if not exists "pgcrypto";  -- gen_random_uuid()

-- ── tenants: один клиент = один изолированный «ИИ-Агент Про» ─────────────────
create table if not exists tenants (
    id          uuid primary key default gen_random_uuid(),
    slug        text unique not null,            -- идентификатор white-label
    name        text not null,                   -- «Школа Лесова», ...
    status      text not null default 'provisioning'
                check (status in ('provisioning','active','suspended','canceled')),
    plan_id     uuid,                            -- FK добавляет schema_billing.sql (plans создаются там)
    created_at  timestamptz not null default now()
);
create index if not exists tenants_status_idx on tenants (status);

-- ── memberships: RBAC внутри тенанта поверх СУЩЕСТВУЮЩЕЙ admin_users ─────────
-- env-админ платформы в admin_users НЕ хранится (bootstrap-суперюзер мимо БД,
-- см. schema_team.sql) — он видит все тенанты без membership.
create table if not exists memberships (
    id          uuid primary key default gen_random_uuid(),
    tenant_id   uuid not null references tenants(id) on delete cascade,
    username    text not null references admin_users(username) on delete cascade,
    role        text not null check (role in ('owner','admin','operator')),
    created_at  timestamptz not null default now(),
    unique (tenant_id, username)
);
create index if not exists memberships_username_idx on memberships (username);

-- ── tenant_settings: tenant-scoped замена app_settings ───────────────────────
-- Та же модель «панель пишет / бот читает». app_settings остаётся для
-- платформенных ключей и как legacy-фолбэк Школы до конца переезда (Wave 3).
create table if not exists tenant_settings (
    tenant_id  uuid not null references tenants(id) on delete cascade,
    key        text not null,
    value      text not null default '',
    updated_at timestamptz not null default now(),
    primary key (tenant_id, key)
);

alter table tenant_settings enable row level security;
do $$ begin
    if not exists (select 1 from pg_policies
                   where tablename = 'tenant_settings' and policyname = 'tenant_isolation') then
        create policy tenant_isolation on tenant_settings
            for all
            using (tenant_id = current_setting('app.tenant_id', true)::uuid)
            with check (tenant_id = current_setting('app.tenant_id', true)::uuid);
    end if;
end $$;

-- ── Wave 1: активный тенант сессии (селектор тенантов в панели) ──────────────
-- Хранится в строке сессии (НЕ в cookie): выбирается при логине (дефолт = первый
-- доступный по memberships; env-админ — первый активный), меняется POST /tenant/switch.
alter table admin_sessions add column if not exists active_tenant_id uuid references tenants(id);

-- ── Гранты panel_rw (зеркалятся в panel_role.sql) ────────────────────────────
do $$ begin
    if exists (select 1 from pg_roles where rolname = 'panel_rw') then
        grant select, insert, update on tenants         to panel_rw;  -- delete НЕ выдан (канон: деактивация статусом)
        grant select, insert, update, delete on memberships to panel_rw;  -- отзыв доступа = delete membership
        grant select, insert, update on tenant_settings to panel_rw;
    end if;
end $$;
