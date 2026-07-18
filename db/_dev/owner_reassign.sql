-- Не-суперюзер owner для ЧЕСТНОЙ проверки RLS локально. НЕ для прода.
--
-- Прод: owner схемы = gen_user (НЕ суперюзер) → FORCE ROW LEVEL SECURITY к нему применяется,
-- и rls_leads_messages_smoke.py доказывает изоляцию. Локально POSTGRES_USER = gen_user_local —
-- СУПЕРЮЗЕР, а суперюзер обходит RLS ДАЖЕ при FORCE (иначе смоук ложно «проваливается»,
-- показывая чужие строки). Поэтому заводим не-суперюзер gen_user и передаём ему владение
-- снапшот-объектами, чтобы локаль воспроизводила прод-семантику владельца.
-- Гоняется db-init ПОСЛЕ снапшота+roles_bootstrap, только когда снапшот применён.
do $$ begin
  if not exists (select 1 from pg_roles where rolname = 'gen_user') then
    create role gen_user login password 'gen_user_local';
  end if;
end $$;

grant usage on schema public to gen_user;

-- Передаём gen_user владение таблицами+последовательностями public (НЕ системными
-- объектами — иначе REASSIGN OWNED падает «required by the database system»).
do $$
declare r record;
begin
  for r in select tablename from pg_tables where schemaname = 'public' loop
    execute format('alter table public.%I owner to gen_user', r.tablename);
  end loop;
  for r in select sequencename from pg_sequences where schemaname = 'public' loop
    execute format('alter sequence public.%I owner to gen_user', r.sequencename);
  end loop;
end $$;
