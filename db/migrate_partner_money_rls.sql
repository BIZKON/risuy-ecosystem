-- CONTRACT-шаг партнёрского кабинета: включение RLS (спека §5.5).
--
-- 🔴 ПРИМЕНЯТЬ ТОЛЬКО ПОСЛЕ деплоя кода, который ходит за начислениями через
-- SECURITY DEFINER (partner_pair_for_client / insert_partner_accrual /
-- storno_partner_accruals). До этого политика отрежет вебхуку платформенного партнёра, и
-- начисления молча перестанут появляться — без ошибки в логах.
--
-- ENABLE, а не FORCE: владелец таблиц (gen_user) обязан обходить политику, иначе
-- SECURITY DEFINER-функции перестанут работать — ровно как на orders.
--
-- ПРИМЕНЕНИЕ (сначала risuy_dev!):
--   bash ~/.claude/scripts/twc-migrate.sh 4171827 81.31.246.136 risuy_dev gen_user \
--       /Users/konstantin/Downloads/risuy-wt-landing/db/migrate_partner_money_rls.sql
--
-- ── СНИМОК ДЛЯ ОТКАТА ────────────────────────────────────────────────────────────
-- До этой миграции RLS на partners / partner_accruals / partner_payouts /
-- partner_invites ВЫКЛЮЧЕН, политик нет. Откат:
--   drop policy if exists partner_scope           on partners;
--   drop policy if exists partner_accruals_scope  on partner_accruals;
--   drop policy if exists partner_payouts_scope   on partner_payouts;
--   drop policy if exists partner_invites_open    on partner_invites;
--   alter table partners         disable row level security;
--   alter table partner_accruals disable row level security;
--   alter table partner_payouts  disable row level security;
--   alter table partner_invites  disable row level security;
-- ─────────────────────────────────────────────────────────────────────────────────

alter table partners          enable row level security;
alter table partner_accruals  enable row level security;
alter table partner_payouts   enable row level security;
alter table partner_invites   enable row level security;

-- nullif: пустой app.tenant_id не должен давать 22P02 (урок tenant_agents, 03.08).
-- is not distinct from: платформенные строки (owner_tenant_id is null) видны только при
-- ПУСТОМ GUC, то есть платформенному владельцу; тенант видит ровно свои.
drop policy if exists partner_scope on partners;
create policy partner_scope on partners
    using      (owner_tenant_id is not distinct from nullif(current_setting('app.tenant_id', true), '')::uuid)
    with check (owner_tenant_id is not distinct from nullif(current_setting('app.tenant_id', true), '')::uuid);

drop policy if exists partner_accruals_scope on partner_accruals;
create policy partner_accruals_scope on partner_accruals
    using      (owner_tenant_id is not distinct from nullif(current_setting('app.tenant_id', true), '')::uuid)
    with check (owner_tenant_id is not distinct from nullif(current_setting('app.tenant_id', true), '')::uuid);

drop policy if exists partner_payouts_scope on partner_payouts;
create policy partner_payouts_scope on partner_payouts
    using      (owner_tenant_id is not distinct from nullif(current_setting('app.tenant_id', true), '')::uuid)
    with check (owner_tenant_id is not distinct from nullif(current_setting('app.tenant_id', true), '')::uuid);

-- Приглашения контура не имеют: они гасятся ДО появления сессии, когда GUC пуст в
-- принципе. Изоляция здесь — секретность самого токена (в базе только sha256), а не
-- политика; закрывать их по тенанту значило бы сломать единственный сценарий их работы.
drop policy if exists partner_invites_open on partner_invites;
create policy partner_invites_open on partner_invites using (true) with check (true);
