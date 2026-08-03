-- Этап 0, дыра B: политика RLS `tenant_agents` — пустой GUC не должен ронять запрос.
--
-- ПРОБЛЕМА. Политика создана без nullif (db/schema_metering_w3.sql:20-29):
--     using (tenant_id = current_setting('app.tenant_id', true)::uuid)
-- При НЕзаданном GUC current_setting(..., true) даёт NULL → сравнение ложно → «0 строк», это ок.
-- Но после `RESET ALL` (или явного set_config(...,'')) значение становится ПУСТОЙ СТРОКОЙ,
-- а ''::uuid — это ошибка 22P02 `invalid input syntax for type uuid`, то есть транзакция
-- падает посреди работы вместо пустой выборки. Соседние политики этого класса написаны
-- через nullif (db/schema_team_agents.sql:60-67, db/migrate_consent_events.sql:30-40) —
-- приводим tenant_agents к тому же канону.
--
-- Воспроизведение (оно же приёмка) — scripts/tenant_agents_registry_smoke.py, блок 6:
--   begin; select set_config('app.tenant_id','',true); select count(*) from tenant_agents;
--   ДО миграции → asyncpg.InvalidTextRepresentationError (22P02)
--   ПОСЛЕ      → 0 строк без исключения
--
-- ФОРМА — `alter policy`, НЕ drop+create: drop+create оставляет окно, в котором RLS включён,
-- а политик нет, то есть для panel_rw таблица на мгновение становится deny-all.
--
-- ПРИОРИТЕТ (честно): миграция НЕ является предусловием самого регистратора — он всегда
-- задаёт app.tenant_id явно (admin-panel/db.py::register_tenant_agent), а снапшот-воркер
-- ходит под owner и RLS обходит. Она страхует будущие панельные чтения реестра и любой код,
-- который выполнит RESET ALL на соединении из пула.
--
-- ПРИМЕНЕНИЕ (СНАЧАЛА risuy_dev, прод risuy — за отдельным явным «да»):
--   psql "<owner-dsn>" -v ON_ERROR_STOP=1 -f db/migrate_tenant_agents_rls_nullif.sql
-- Идемпотентно: повторный прогон переписывает политику тем же определением.

alter policy tenant_isolation on tenant_agents
  using      (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
  with check (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

-- Проверка: определение политики после применения (оба выражения должны содержать nullif).
select polname as политика,
       pg_get_expr(polqual, polrelid)      as using_выражение,
       pg_get_expr(polwithcheck, polrelid) as with_check_выражение
  from pg_policy
 where polrelid = 'tenant_agents'::regclass;
