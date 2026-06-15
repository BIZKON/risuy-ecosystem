-- Откат Слоя B (db/schema_tenant_triggers.sql). Дропает таблицу и политику. Применять
-- owner-DSN. Триггеры клиентов будут потеряны — только для отката неудачного релиза.
drop policy if exists tenant_isolation on tenant_triggers;
drop table if exists tenant_triggers;
