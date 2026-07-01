-- ── Токены сброса пароля оператора (self-service восстановление по email) ─────
-- Одноразовые токены. В БД хранится sha256(hex) токена; сам токен только в письме
-- (утечка БД не даёт рабочих ссылок). Глобальный реестр по username (как admin_users),
-- НЕ tenant-scoped → RLS не нужен. panel_rw: select/insert/update (update — used_at).
--
-- ⚠️ DDL: применять owner-DSN, СНАЧАЛА risuy_dev, ПЕРЕД деплоем кода. Идемпотентно.

create table if not exists password_reset_tokens (
    token_hash  text        primary key,
    username    text        not null references admin_users(username) on delete cascade,
    created_at  timestamptz not null default now(),
    expires_at  timestamptz not null,
    used_at     timestamptz,
    request_ip  text
);

create index if not exists password_reset_tokens_username_idx
    on password_reset_tokens (username, created_at desc);
create index if not exists password_reset_tokens_expires_idx
    on password_reset_tokens (expires_at);

grant select, insert, update on password_reset_tokens to panel_rw;
-- sequence грант НЕ нужен: PK = token_hash (text), не serial.
