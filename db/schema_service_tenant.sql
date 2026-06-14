-- Tenant-изоляция биллинга подписки (service_invoices). Исторически таблица —
-- ГЛОБАЛЬНЫЙ леджер (допущение «одна школа платит агентству»): без tenant_id и RLS.
-- Self-serve мультитенантность (account_identities/create_client_account) → любой
-- клиент видел тариф/факт оплаты ЛЮБОГО тенанта на /subscription. Чиним: tenant_id + RLS.
--
-- EXPAND-CONTRACT (handoff-правило: DDL СНАЧАЛА risuy_dev, expand ПЕРЕД кодом):
--   • ЭТОТ файл = EXPAND: колонка nullable + бэкфилл + индекс. Безопасен до кода
--     (старый код колонку игнорирует).
--   • db/schema_service_rls.sql = CONTRACT (enable RLS + политика): ПОСЛЕ деплоя кода,
--     который пишет tenant_id (create_period_invoice) и ставит app.tenant_id в вебхуке.
--
-- ПОРЯДОК / ПРИМЕНЕНИЕ (owner-DSN, сперва risuy_dev):
--   ~/.claude/scripts/twc-migrate.sh 4171827 81.31.246.136 risuy_dev gen_user \
--       db/schema_service_tenant.sql
--   (после деплоя кода: db/schema_service_rls.sql). Идемпотентно.

-- 1) Колонка тенанта (nullable на время expand). FK на tenants.
alter table service_invoices
    add column if not exists tenant_id uuid references tenants(id);

-- 2) Бэкфилл существующих строк: created_by (actor) → его тенант по membership.
--    Self-serve клиент: created_by = client_<token>, membership(owner) → его tenant.
update service_invoices si
set tenant_id = m.tenant_id
from memberships m
where si.tenant_id is null
  and m.username = si.created_by;

-- 3) Фолбэк для строк, чей created_by вне memberships (env-админ / ручные / legacy):
--    привязываем к старейшему ЖИВОМУ тенанту. Фильтр status in ('provisioning','active')
--    ОБЯЗАТЕЛЕН — он совпадает с критерием читателей (service_revenue_total / env-админ
--    default-tenant / tenant_accessible): иначе при старейшем suspended/canceled тенанте
--    строки осели бы на невидимом тенанте и выпали из витрины/выручки. Нет живых тенантов —
--    строка останется NULL и под RLS невидима (безопасно, физически не теряется).
update service_invoices si
set tenant_id = (select id from tenants where status in ('provisioning','active')
                 order by created_at limit 1)
where si.tenant_id is null
  and exists (select 1 from tenants where status in ('provisioning','active'));

-- 4) Индекс под per-tenant выборку «последний оплаченный / история».
create index if not exists service_invoices_tenant_period_idx
    on service_invoices (tenant_id, period_end desc);
