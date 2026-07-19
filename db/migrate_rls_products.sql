-- Ревизия (адверсариально верифицировано): products — ЕДИНСТВЕННАЯ из tenant-scoped таблиц
-- (migrate_tenant_scope.sql добавил tenant_id + FK + backfill), пропущенная при включении RLS:
-- в migrate_rls_orders_kb_broadcasts.sql её нет, отдельного файла тоже → панель под panel_rw
-- видела и правила каталог оферов ВСЕХ тенантов. Тот же паттерн, что остальные таблицы
-- (schema_rls_leads_messages.sql): политика по app.tenant_id; владелец (бот gen_user) RLS
-- ОБХОДИТ (не FORCE), панель (panel_rw) — подчиняется (app.tenant_id ставит pool-хук).
--
-- Код-слой панели уже несёт defence-in-depth тем же предикатом (_PRODUCTS_TENANT_SQL,
-- admin-panel/db.py) — миграция замыкает изоляцию на уровне БД.
--
-- ⚠️ ПОРЯДОК: СНАЧАЛА risuy_dev + проверка под ролью panel_rw, потом прод. Идемпотентно
-- (enable повторно безопасен; drop+create policy).
--
-- ПРИМЕНЕНИЕ: bash ~/.claude/scripts/twc-migrate.sh 4171827 81.31.246.136 <db> gen_user /abs/.../db/migrate_rls_products.sql

do $$
begin
    -- Guard предусловия: понятный raise, если Wave-0 (tenant_id) не накатан на эту БД.
    if not exists (
        select 1 from information_schema.columns
        where table_schema = 'public' and table_name = 'products' and column_name = 'tenant_id'
    ) then
        raise exception 'RLS-миграция: на таблице "products" нет колонки tenant_id — сначала накатите migrate_tenant_scope.sql (Wave 0)';
    end if;
    execute 'alter table products enable row level security';
    execute 'drop policy if exists tenant_isolation on products';
    -- nullif(...,'')::uuid: пустой app.tenant_id (asyncpg RESET на release → '') → NULL → 0 строк
    -- БЕЗ ошибки (как в schema_rls_leads_messages.sql). FORCE НЕ ставим → владелец-бот обходит RLS.
    execute
        'create policy tenant_isolation on products for all '
        'using (tenant_id = nullif(current_setting(''app.tenant_id'', true), '''')::uuid) '
        'with check (tenant_id = nullif(current_setting(''app.tenant_id'', true), '''')::uuid)';
end $$;

-- Контроль-ASSERT: rls_on=true И forced=false; raise при отклонении, чтобы тихий неверный
-- результат не проехал в прод незамеченным.
do $$
declare
    r record;
begin
    select c.relrowsecurity as rls_on, c.relforcerowsecurity as forced into r
    from pg_class c join pg_namespace n on n.oid = c.relnamespace
    where n.nspname = 'public' and c.relname = 'products';
    if r is null or (not r.rls_on) or r.forced then
        raise exception 'RLS-инвариант нарушен на products: rls_on=% forced=% (ожидалось on=true, forced=false)',
            r.rls_on, r.forced;
    end if;
    raise notice 'OK: RLS tenant_isolation включён на products (без FORCE)';
end $$;
