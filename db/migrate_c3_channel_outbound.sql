-- Слой C, Фаза C3: канал-агностичная ИСХОДЯЩАЯ доставка (ручной ответ оператора + рассылки)
-- для VK/MAX (docs/layer-c-vk-max-channels.md §7). Обобщает очередь исходящих (outbox) и
-- материализацию получателей рассылки (broadcast_recipients) с TG-адреса на любой канал, и
-- персистит адрес ответа MAX (chat_id ≠ user_id в личке) в leads.
--
-- ПРИМЕНЕНИЕ (expand, ДО кода): twc-migrate.sh owner-DSN, СНАЧАЛА risuy_dev, потом прод.
--   bash ~/.claude/scripts/twc-migrate.sh 4171827 81.31.246.136 <db> gen_user /abs/.../db/migrate_c3_channel_outbound.sql
-- Идемпотентно. Telegram-путь НЕ меняется (tg-строки по-прежнему несут tg_user_id; messenger='tg'
-- по умолчанию; для tg reply_address не используется). Гранты НЕ нужны: panel_rw имеет TABLE-level
-- grant на outbox/broadcasts, новые колонки покрываются автоматически (см. db/panel_role.sql).

-- ── leads: адрес ответа MAX ───────────────────────────────────────────────────────────
-- В личке MAX отвечать надо на recipient.chat_id (≠ max_user_id). Сохраняем его при ВХОДЯЩЕМ
-- (multiplex._max_respond/_max_callback), чтобы панель/воркер могли ответить лиду. VK не требует:
-- в личке peer_id == from_id == vk_user_id (уже есть). NULL = ещё не писал / не из лички.
alter table leads add column if not exists max_chat_id bigint;

-- ── outbox: канал точечного ответа оператора ──────────────────────────────────────────
-- messenger — каким драйвером слать (tg|vk|max). tg_user_id → nullable: для vk/max адрес ответа
-- резолвится воркером из leads по (lead_id, messenger) — vk_user_id / max_chat_id (lead_id NOT NULL
-- в outbox уже есть). tg-путь без изменений (tg_user_id заполняется, messenger='tg').
alter table outbox add column if not exists messenger text not null default 'tg';
alter table outbox alter column tg_user_id drop not null;

-- ── broadcast_recipients: канал и денорм-адрес материализованного получателя ───────────
-- messenger денормализуется из broadcasts.messenger при материализации. reply_address — адрес
-- доставки для не-TG каналов (vk → vk_user_id, max → max_chat_id); для tg остаётся tg_user_id
-- (reply_address NULL). tg_user_id → nullable (vk/max-получатель его не имеет).
alter table broadcast_recipients add column if not exists messenger text not null default 'tg';
alter table broadcast_recipients add column if not exists reply_address bigint;
alter table broadcast_recipients alter column tg_user_id drop not null;
