-- Reseller-платформа, Wave 0 (ТЗ §4.5): VAULT — секреты тенанта.
-- BOT_TOKEN клиента, ключи его кассы (shop_yookassa_*), VK-токен и т.п.
-- Применять ПОСЛЕ schema_tenancy.sql. Идемпотентно.
--
-- Шифрование: AES-256-GCM (shared/vault.py, Wave 1), мастер-ключ VAULT_MASTER_KEY —
-- ТОЛЬКО в env приложений (через twc-set-env.sh; в репо/логах/чате не живёт).
-- В БД — НИКОГДА не plaintext. UI — write-only («задан/не задан» + last_used_at).

create extension if not exists "pgcrypto";

create table if not exists tenant_secrets (
    id           uuid primary key default gen_random_uuid(),
    tenant_id    uuid not null references tenants(id) on delete cascade,
    key_name     text not null,    -- 'telegram_bot_token','shop_yookassa_shop_id',
                                   -- 'shop_yookassa_secret_key','vk_token', ...
    ciphertext   bytea not null,   -- AES-GCM envelope, никогда не plaintext
    nonce        bytea not null,
    key_version  int not null default 1,   -- для ротации мастер-ключа
    created_at   timestamptz not null default now(),
    last_used_at timestamptz,
    unique (tenant_id, key_name)
);

alter table tenant_secrets enable row level security;
do $$ begin
    if not exists (select 1 from pg_policies
                   where tablename = 'tenant_secrets' and policyname = 'tenant_isolation') then
        create policy tenant_isolation on tenant_secrets
            for all
            using (tenant_id = current_setting('app.tenant_id', true)::uuid)
            with check (tenant_id = current_setting('app.tenant_id', true)::uuid);
    end if;
end $$;

-- Гранты panel_rw (зеркалятся в panel_role.sql): ввод/ротация/удаление ключа из
-- кабинета. select отдаёт только ciphertext — расшифровка возможна лишь процессом
-- с VAULT_MASTER_KEY в env.
do $$ begin
    if exists (select 1 from pg_roles where rolname = 'panel_rw') then
        grant select, insert, update, delete on tenant_secrets to panel_rw;
    end if;
end $$;
