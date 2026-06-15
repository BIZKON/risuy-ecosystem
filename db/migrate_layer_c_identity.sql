-- Слой C, Фаза C0: обобщение идентичности под каналы VK/MAX (docs/layer-c-vk-max-channels.md).
-- Снимает «блокер №1» (messages.tg_user_id NOT NULL — Telegram-специфичен) и доводит уникальность
-- max/vk-лидов до tenant-scope (как уже сделано для tg). Гибрид-модель: колонки на канал
-- (tg_user_id/max_user_id/vk_user_id), вызовы — через helper resolve_lead (код, Фаза C0-шаг2).
--
-- ПРИМЕНЕНИЕ (expand, ДО кода): twc-migrate.sh owner-DSN, СНАЧАЛА risuy_dev, потом прод.
--   bash ~/.claude/scripts/twc-migrate.sh 4171827 81.31.246.136 <db> gen_user /abs/.../db/migrate_layer_c_identity.sql
-- Идемпотентно. Telegram-путь НЕ меняется (tg_user_id у tg по-прежнему заполняется).

-- ── leads: VK-идентичность + tenant-scoped уникальность max/vk (зеркало tg) ──────────
-- Колонка под VK from_id (положительный bigint). NULL не конфликтует (как tg/max).
alter table leads add column if not exists vk_user_id bigint;

-- Уникальность лида в пределах ТЕНАНТА (изоляция воронок), как leads_tenant_tg_user_id_key.
-- NULL считаются разными → лиды без max/vk не конфликтуют.
create unique index if not exists leads_tenant_max_user_id_key on leads (tenant_id, max_user_id);
create unique index if not exists leads_tenant_vk_user_id_key  on leads (tenant_id, vk_user_id);

-- Снять ГЛОБАЛЬНЫЙ unique на max_user_id: он кросс-тенантный (один MAX-человек не мог бы быть
-- лидом у двух тенантов — нарушение изоляции, которое для tg уже починено). Безопасно сейчас:
-- MAX-канал ещё не активен (нет данных и кода `on conflict (max_user_id)`). VK-глобального нет.
drop index if exists leads_max_user_id_key;

-- ── messages: снять Telegram-привязку (БЛОКЕР №1) ────────────────────────────────────
-- tg_user_id → nullable: VK/MAX-сообщения пишутся с tg_user_id=NULL (адрес лида = lead_id, FK уже
-- есть; история/метеринг/счётчик переводятся на lead_id в коде). messenger — для фильтра/витрины.
alter table messages alter column tg_user_id drop not null;
alter table messages add column if not exists messenger text not null default 'tg';
