-- B7 (аудит #5), ШАГ 2 (CONTRACT): включить RLS tenant_isolation на оставшихся tenant-scoped
-- таблицах с ПДн/финданными. Тот же паттерн, что leads/messages/outbox/link_clicks/broadcast_recipients
-- (schema_rls_leads_messages.sql): политика по app.tenant_id; владелец (бот gen_user) RLS ОБХОДИТ
-- (не FORCE), панель (panel_rw) — подчиняется и ставит app.tenant_id из сессии/явно.
--
-- ⚠️ ПОРЯДОК (СТРОГО): применять ТОЛЬКО ПОСЛЕ (1) migrate_rls_discovery_fns.sql И (2) деплоя кода
-- панели, где вебхук читает orders через SECURITY DEFINER-функции (order_tenant_for_payment и др.)
-- и mark_order_paid_* ставят app.tenant_id ДО select. Иначе вебхук ЮKassa перестанет находить заказы
-- → онлайн-оплаты зависнут в pending. СНАЧАЛА risuy_dev + проверка под ролью panel_rw, потом прод.
-- Идемпотентно (enable повторно безопасен; drop+create policy).
--
-- ПРИМЕНЕНИЕ: bash ~/.claude/scripts/twc-migrate.sh 4171827 81.31.246.136 <db> gen_user /abs/.../db/migrate_rls_orders_kb_broadcasts.sql

do $$
declare
    t text;
    -- orders — discovery вебхука уже переведён на SECURITY DEFINER (шаг 1). Остальные: панель пишет
    -- с сессией (app.tenant_id из centralized-хука), бот читает как owner (обход), вебхук их не трогает.
    tables constant text[] := array[
        'orders', 'broadcasts', 'broadcast_files', 'link_tokens', 'kb_documents', 'kb_chunks'
    ];
begin
    foreach t in array tables loop
        execute format('alter table %I enable row level security', t);
        execute format('drop policy if exists tenant_isolation on %I', t);
        -- nullif(...,'')::uuid: пустой app.tenant_id (asyncpg RESET на release → '') → NULL → 0 строк
        -- БЕЗ ошибки (как в schema_rls_leads_messages.sql). FORCE НЕ ставим → владелец-бот обходит RLS.
        execute format(
            'create policy tenant_isolation on %I for all '
            'using (tenant_id = nullif(current_setting(''app.tenant_id'', true), '''')::uuid) '
            'with check (tenant_id = nullif(current_setting(''app.tenant_id'', true), '''')::uuid)', t);
    end loop;
end $$;

-- Контроль: ожидаем relrowsecurity=true и relforcerowsecurity=false на всех шести.
select c.relname, c.relrowsecurity as rls_on, c.relforcerowsecurity as forced
from pg_class c join pg_namespace n on n.oid = c.relnamespace
where n.nspname = 'public'
  and c.relname in ('orders','broadcasts','broadcast_files','link_tokens','kb_documents','kb_chunks')
order by c.relname;
