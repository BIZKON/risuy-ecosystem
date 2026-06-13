-- Reseller-платформа, Wave 3d (ТЗ §4.6, §7): СНЯТИЕ переходника DEFAULT tenant_id.
-- Применять ПОСЛЕ деплоя кода Wave 3b/3c (бот И панель пишут tenant_id ЯВНО во все
-- вставки 12 tenant-scoped таблиц) и ПОСЛЕ доказательства полного INSERT-покрытия
-- (workflow verify-tenant-insert-coverage — ноль подтверждённых дыр).
-- СНАЧАЛА risuy_dev, потом прод. Идемпотентно.
--
-- ⚠️ НЕОБРАТИМОСТЬ ПО ПРОДУ: после снятия DEFAULT любой INSERT без явного tenant_id
-- упадёт на NOT NULL. Поэтому порядок строгий: код-в-проде → проверка покрытия →
-- эта миграция. Откат (вернуть DEFAULT) — на случай регресса, командой в конце файла.
--
-- DECISIONS п.12: DEFAULT=uuid Школы был переходником, пока живой код вставлял без
-- tenant_id. Wave 3b/3c сделал все вставки явными → переходник снимается.

do $$
declare
    t text;
    tables constant text[] := array[
        'leads', 'messages', 'orders', 'products',
        'broadcasts', 'broadcast_recipients', 'broadcast_files',
        'link_tokens', 'link_clicks', 'outbox',
        'kb_documents', 'kb_chunks'
    ];
begin
    foreach t in array tables loop
        -- NOT NULL и FK НЕ трогаем — остаются (целостность tenant-scope сохраняется).
        -- Снимаем ТОЛЬКО default: теперь значение обязан передавать вызывающий код.
        execute format('alter table %I alter column tenant_id drop default', t);
    end loop;
end $$;

-- Контрольный смоук: ни на одной из 12 таблиц не должно остаться column-default
-- на tenant_id (ожидаем 0 строк).
select c.relname as таблица, d.adsrc_is_set as есть_дефолт
from pg_attribute a
join pg_class c on c.oid = a.attrelid
left join lateral (
    select true as adsrc_is_set
    from pg_attrdef ad
    where ad.adrelid = a.attrelid and ad.adnum = a.attnum
) d on true
where a.attname = 'tenant_id'
  and c.relname in ('leads','messages','orders','products','broadcasts',
                    'broadcast_recipients','broadcast_files','link_tokens',
                    'link_clicks','outbox','kb_documents','kb_chunks')
  and d.adsrc_is_set is true;

-- ── ОТКАТ (если регресс — вернуть DEFAULT=uuid Школы, выполнять вручную) ──────
-- do $$
-- declare t text; school uuid;
--     tables constant text[] := array['leads','messages','orders','products',
--         'broadcasts','broadcast_recipients','broadcast_files','link_tokens',
--         'link_clicks','outbox','kb_documents','kb_chunks'];
-- begin
--     select id into school from tenants where slug = 'lesov-school';
--     foreach t in array tables loop
--         execute format('alter table %I alter column tenant_id set default %L::uuid', t, school);
--     end loop;
-- end $$;
