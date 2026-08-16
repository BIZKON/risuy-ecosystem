-- ИСПРАВЛЕНИЕ политики партнёрских таблиц: владелец платформы обязан видеть СВОИ строки.
--
-- 🔴 ЧТО БЫЛО НЕ ТАК. Первая редакция политики (migrate_partner_money_rls.sql) различала
-- контуры ТОЛЬКО по app.tenant_id и считала, что у платформенной сессии этот GUC пуст.
-- Это неверно: auth.create_session ставит env-админу активным «первый живой» тенант, а
-- пул проставляет его в app.tenant_id на каждом соединении. В итоге после включения RLS
-- владелец платформы переставал видеть собственных партнёров (раздел «Партнёры» — пусто),
-- а «Создать партнёра» падал бы на with check: owner_tenant_id null против GUC-тенанта.
-- Найдено живым прогоном на проде 16.08.2026; политика заменена этой миграцией.
--
-- РЕШЕНИЕ: второй GUC app.is_platform, который панель ставит из Session.is_platform
-- (то есть только env-админу). Тенантская сессия платформенной не бывает по определению
-- — is_platform == actor равен config.ADMIN_USERNAME, — поэтому утечки между клиентами нет.
--
-- ⚠️ ПОРЯДОК: сначала ДЕПЛОЙ кода, который ставит app.is_platform, потом эта миграция.
-- Наоборот — политика будет ссылаться на GUC, которого никто не выставляет, и владелец
-- по-прежнему не увидит своих партнёров.
--
--   bash ~/.claude/scripts/twc-migrate.sh 4171827 81.31.246.136 risuy_dev gen_user \
--       /Users/konstantin/Downloads/risuy-wt-landing/db/migrate_partner_money_rls_platform.sql
--
-- ОТКАТ: переприменить db/migrate_partner_money_rls.sql (там прежние определения).

drop policy if exists partner_scope on partners;
create policy partner_scope on partners
    using (
        owner_tenant_id is not distinct from nullif(current_setting('app.tenant_id', true), '')::uuid
        or (owner_tenant_id is null and current_setting('app.is_platform', true) = 'on')
    )
    with check (
        owner_tenant_id is not distinct from nullif(current_setting('app.tenant_id', true), '')::uuid
        or (owner_tenant_id is null and current_setting('app.is_platform', true) = 'on')
    );

drop policy if exists partner_accruals_scope on partner_accruals;
create policy partner_accruals_scope on partner_accruals
    using (
        owner_tenant_id is not distinct from nullif(current_setting('app.tenant_id', true), '')::uuid
        or (owner_tenant_id is null and current_setting('app.is_platform', true) = 'on')
    )
    with check (
        owner_tenant_id is not distinct from nullif(current_setting('app.tenant_id', true), '')::uuid
        or (owner_tenant_id is null and current_setting('app.is_platform', true) = 'on')
    );

drop policy if exists partner_payouts_scope on partner_payouts;
create policy partner_payouts_scope on partner_payouts
    using (
        owner_tenant_id is not distinct from nullif(current_setting('app.tenant_id', true), '')::uuid
        or (owner_tenant_id is null and current_setting('app.is_platform', true) = 'on')
    )
    with check (
        owner_tenant_id is not distinct from nullif(current_setting('app.tenant_id', true), '')::uuid
        or (owner_tenant_id is null and current_setting('app.is_platform', true) = 'on')
    );
