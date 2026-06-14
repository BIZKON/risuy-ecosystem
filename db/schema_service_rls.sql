-- CONTRACT-шаг tenant-изоляции service_invoices: включаем RLS + политику.
-- Применять ТОЛЬКО ПОСЛЕ деплоя кода, который (а) пишет tenant_id в create_period_invoice,
-- (б) ставит app.tenant_id в вебхуке (mark_service_invoice_paid_by_payment) и
-- (в) скан по тенантам в service_revenue_total. Иначе вставка/вебхук упрутся в RLS.
--
-- Применение (owner-DSN, сперва risuy_dev, затем прод ПОСЛЕ деплоя кода):
--   ~/.claude/scripts/twc-migrate.sh 4171827 81.31.246.136 risuy_dev gen_user \
--       db/schema_service_rls.sql
-- Откат (мгновенный, если витрина «ослепнет»): db/rollback_rls_service_invoices.sql
--
-- Идиома политики — зеркало leads/messages/link_clicks (nullif → '' не ломает каст ::uuid).
-- ENABLE (НЕ FORCE): owner-роль (бот/миграции) RLS обходит; panel_rw (без bypassrls) — нет.

-- Подметание NULL-tenant строк перед NOT NULL: повторяем бэкфилл (идемпотентно) на случай
-- строк, вставленных СТАРЫМ кодом в окне деплоя (expand применён, новый код ещё не выехал —
-- старый INSERT не писал tenant_id). Зеркало шагов 2/3 из schema_service_tenant.sql.
update service_invoices si set tenant_id = m.tenant_id from memberships m
where si.tenant_id is null and m.username = si.created_by;
update service_invoices si
set tenant_id = (select id from tenants where status in ('provisioning','active')
                 order by created_at limit 1)
where si.tenant_id is null
  and exists (select 1 from tenants where status in ('provisioning','active'));

-- NOT NULL: каждый счёт привязан к тенанту (create_period_invoice всегда пишет tenant_id;
-- вебхук только UPDATE статуса). Структурно исключает «осиротевшие» строки → платформенная
-- выручка (service_revenue_total, скан по тенантам) ничего не теряет. На проде таблица пуста
-- → безопасно; если осталась NULL-строка (нет ни одного живого тенанта) — ALTER упадёт ЯВНО
-- (лучше громкий сбой миграции, чем тихая потеря выручки).
alter table service_invoices alter column tenant_id set not null;

alter table service_invoices enable row level security;

do $$ begin
    if not exists (select 1 from pg_policies
                   where tablename = 'service_invoices' and policyname = 'tenant_isolation') then
        create policy tenant_isolation on service_invoices
            for all
            using (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
            with check (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);
    end if;
end $$;
