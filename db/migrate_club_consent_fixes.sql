-- Клуб — 152-ФЗ выверка (СЛОЙ 2: отзыв согласия члена + retention + ФЗ-38 opt-in).
-- Аддитивно и идемпотентно поверх migrate_club.sql. RLS уже включён на club_members.
-- НАКАТ: СНАЧАЛА risuy_dev (twc-migrate.sh 4171827 81.31.246.136 risuy_dev gen_user
-- db/migrate_club_consent_fixes.sql), прод risuy — ТОЛЬКО по явному «да» владельца.

-- Канал идентичности члена клуба: чистый клуб-член входит через ?start=club с lead_id=NULL,
-- поэтому у него НЕТ обратной связи «внешний id канала → член». Без этого отзыв согласия
-- (cmd_revoke → request_erase по leads) для такого члена = no-op (L4-revoke-noop-club).
-- Зеркалит идентичность-модель leads (tg/vk/max_user_id).
alter table club_members add column if not exists tg_user_id  bigint;
alter table club_members add column if not exists vk_user_id  bigint;
alter table club_members add column if not exists max_user_id bigint;

-- Отзыв согласия члена (152-ФЗ ст.9 ч.2) + признак для retention-обезличивания клуб-ПДн
-- (L4-retention-club-tables). Coalesce-семантика как leads.erase_requested_at.
alter table club_members add column if not exists erase_requested_at timestamptz;

-- ФЗ-38 ст.18: отделимое согласие на «предложения о партнёрстве». Отказ от предложений
-- не должен требовать выхода из клуба (L2-no-offer-optout). Гейт рекламного потока —
-- по этому флагу, НЕ по leads.unsubscribed_at. Дефолт true = согласие на этапе вступления.
alter table club_members add column if not exists offers_opt_in boolean not null default true;

-- Lookup члена по каналу идентичности при отзыве (cmd_revoke) и retention-выборке.
create index if not exists club_members_tenant_tg_idx  on club_members (tenant_id, tg_user_id);
create index if not exists club_members_erase_idx      on club_members (tenant_id, erase_requested_at);
