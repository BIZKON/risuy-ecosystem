-- Схема раздела «Команда» (мульти-оператор + роли) для админ-панели «Школа Лесова».
-- Дополняет db/schema_admin.sql. Идемпотентно (IF NOT EXISTS) — применять можно повторно.
--
-- Применить ОДИН РАЗ owner-DSN (роль-владелец БД, не panel_rw — у неё нет прав на DDL),
-- ПЕРЕД деплоем кода (schema-first). Порядок: schema.sql → schema_admin.sql → … →
-- schema_team.sql (этот) → db/panel_role.sql (гранты panel_rw на admin_users).
--   ~/.claude/scripts/twc-migrate.sh 4171827 81.31.246.136 risuy gen_user \
--       db/schema_team.sql db/panel_role.sql
--
-- АРХИТЕКТУРА АУТЕНТИФИКАЦИИ (аддитивно, lockout невозможен):
--   • env-админ (config.ADMIN_USERNAME/ADMIN_PASSWORD_HASH) остаётся bootstrap-СУПЕРЮЗЕРОМ,
--     который работает ВСЕГДА, мимо БД и этой таблицы. Его роль ВСЕГДА вычисляется как
--     'admin' в коде (auth.py), в admin_users он НЕ хранится.
--   • admin_users — дополнительные именованные операторы поверх. Если таблица пуста/
--     сломана/все деактивированы — вход через env-админа цел.
--   • actor (логин) совпадает с admin_sessions.actor и admin_audit.actor → аудит
--     автоматически атрибутируется по конкретному пользователю.

create extension if not exists "pgcrypto";  -- на случай применения в изоляции

-- ── Пользователи панели (операторы) ──────────────────────────────────────────
-- username — PK и АКТОР (тот же ключ, что в admin_sessions.actor/admin_audit.actor).
--   Логин лоуэркейснутый и валидируется в app.py (alnum + _-, длина); env-админ сюда
--   НЕ попадает (он отдельный bootstrap-суперюзер) — коллизия имени с ним отвергается кодом.
-- password_hash — argon2id PHC-строка (как ADMIN_PASSWORD_HASH); генерит панель (auth._ph.hash),
--   plain-пароль в БД НЕ хранится.
-- role — admin|operator. admin = полный доступ (вкл. раздел «Команда»); operator = всё
--   операционное, КРОМЕ управления пользователями (решение владельца, v1). Гейт роли — в коде.
-- active — деактивация вместо удаления (append-only-философия; deactivated не входит).
create table if not exists admin_users (
    username      text        primary key,
    password_hash text        not null,
    role          text        not null default 'operator',
    active        boolean     not null default true,
    created_at    timestamptz not null default now(),
    created_by    text,                                   -- кто завёл (актор-админ)
    updated_at    timestamptz not null default now(),     -- бампается панелью при правке
    constraint admin_users_role_chk check (role in ('admin', 'operator'))
);

-- Список команды в /team обычно «свежие сверху» / фильтр по активности.
create index if not exists admin_users_active_idx on admin_users (active, created_at desc);

-- ── Роль на серверной сессии ──────────────────────────────────────────────────
-- Роль фиксируется в момент логина (auth.create_session) и читается в load_session —
-- без джойна к admin_users на каждом запросе. Для env-админа роль ВСЕГДА 'admin'
-- вычисляется в коде поверх хранимого значения (он суперюзер вне БД). Дефолт 'operator'
-- безопасен для старых строк (докатка ниже): реальную роль env-админа код всё равно
-- поднимет до 'admin'. NB: расхождение имени колонки ⇒ ошибка на КАЖДОМ load_session.
alter table admin_sessions add column if not exists role text not null default 'operator';
