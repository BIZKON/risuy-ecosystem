-- Reseller-платформа, Wave 0 (ТЗ §4.6): tenant-scope СУЩЕСТВУЮЩИХ таблиц.
-- Существующий пайплайн не переписывается — он скоупится: tenant_id во все
-- tenant-scoped таблицы + backfill на первого тенанта «Школа Лесова».
-- Применять ПОСЛЕ schema_tenancy.sql. Идемпотентно, прод не останавливается.
--
-- ⚠️ RLS на существующих таблицах в Wave 0 НЕ включается (DECISIONS 2026-06-12):
-- текущая панель ещё не выставляет app.tenant_id — включение RLS сейчас спрятало
-- бы данные Школы (слом §8.7). Включение — Wave 1 отдельной миграцией, когда
-- tenant-context middleware панели задеплоен.

-- 1) Первый тенант = Школа Лесова (текущий прод). Идемпотентно по slug.
insert into tenants (slug, name, status)
values ('lesov-school', 'Школа Лесова', 'active')
on conflict (slug) do nothing;

-- 2) tenant_id + backfill + NOT NULL + FK + индекс — на каждую tenant-scoped
--    таблицу. Один plpgsql-цикл вместо 12 копий (EXECUTE format).
do $$
declare
    school uuid;
    t text;
    tables constant text[] := array[
        'leads', 'messages', 'orders', 'products',
        'broadcasts', 'broadcast_recipients', 'broadcast_files',
        'link_tokens', 'link_clicks', 'outbox',
        'kb_documents', 'kb_chunks'
    ];
begin
    select id into school from tenants where slug = 'lesov-school';
    if school is null then
        raise exception 'тенант lesov-school не создан — backfill невозможен';
    end if;

    foreach t in array tables loop
        execute format('alter table %I add column if not exists tenant_id uuid', t);
        execute format('update %I set tenant_id = $1 where tenant_id is null', t) using school;
        -- DEFAULT = тенант Школы: ЖИВОЙ код (бот/панель) вставляет без tenant_id
        -- до Wave 3 — без default его INSERT упал бы на NOT NULL (слом §8.7).
        -- Когда мультиплекс начнёт писать tenant_id явно, default снимается
        -- отдельной миграцией Wave 3.
        execute format('alter table %I alter column tenant_id set default %L::uuid', t, school);
        execute format('alter table %I alter column tenant_id set not null', t);
        if not exists (select 1 from pg_constraint
                       where conname = t || '_tenant_id_fkey') then
            execute format(
                'alter table %I add constraint %I foreign key (tenant_id) references tenants(id)',
                t, t || '_tenant_id_fkey');
        end if;
        execute format(
            'create index if not exists %I on %I (tenant_id, %s)',
            t || '_tenant_idx', t,
            case t                                     -- вторая колонка = естественный доступ таблицы
                when 'kb_chunks'            then 'document_id'
                when 'link_clicks'          then 'clicked_at'   -- created_at у неё нет
                when 'broadcast_recipients' then 'broadcast_id' -- временной колонки нет
                else 'created_at'
            end);
    end loop;
end $$;

-- 3) Контрольный смоук: все 12 таблиц заскоуплены, NULL-ов нет.
select 'tenant-scope готов' as итог,
       (select count(*) from tenants)                          as тенантов,
       (select count(*) from leads      where tenant_id is null) as leads_null,
       (select count(*) from messages   where tenant_id is null) as messages_null,
       (select count(*) from kb_chunks  where tenant_id is null) as kb_chunks_null;
