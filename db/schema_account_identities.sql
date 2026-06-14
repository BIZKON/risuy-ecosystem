-- Парадная «ИИ-Агент Про»: внешние идентичности клиентских учёток (email/телефон/ВК/ТГ).
-- Self-serve регистрация переиспользует admin_users(username PK)+memberships+tenants(provisioning);
-- эта таблица — лишь МАППИНГ способа входа (provider+external_id) → username для резолва логина.
--
-- Применять owner-DSN СНАЧАЛА risuy_dev, потом прод (DDL аддитивный — можно до кода). Порядок:
-- ПОСЛЕ schema_team.sql (FK на admin_users) и schema_tenancy.sql. Идемпотентно.
--   ~/.claude/scripts/twc-migrate.sh 4171827 81.31.246.136 risuy_dev gen_user \
--       db/schema_account_identities.sql db/panel_role.sql
--
-- ⚠️ БЕЗ RLS — резолв идентичности происходит ДО сессии (нет app.tenant_id), как у
-- admin_users/tenants/admin_sessions. Изоляция данных тенанта остаётся на leads/messages/…
-- (там RLS уже включён). panel_rw читает/пишет эту таблицу (грант — db/panel_role.sql).

create table if not exists account_identities (
    id            bigint generated always as identity primary key,
    provider      text not null check (provider in ('email','phone','vk','telegram')),
    external_id   text not null,        -- email(lower) | phone(E.164) | vk user id | tg user id
    username      text not null references admin_users(username) on delete cascade,
    verified      boolean not null default false,
    display_name  text,
    created_at    timestamptz not null default now(),
    last_login_at timestamptz,
    -- один внешний идентификатор провайдера → ровно одна учётка (анти-дубль/анти-захват).
    unique (provider, external_id)
);

create index if not exists account_identities_username_idx on account_identities (username);
