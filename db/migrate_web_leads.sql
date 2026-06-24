-- Веб-чат демо как лиды (раздел «Демо-монитор» панели): идентичность веб-сессии — как
-- vk_user_id/max_user_id в Слое C. Виджет сайта шлёт стабильный session_id (localStorage),
-- бот upsert'ит лид по нему и пишет переписку → веб-чат виден в Диалогах/Демо-мониторе.
-- Expand-миграция (nullable, без бэкафилла) — безопасна, накатывается ДО кода.
alter table leads add column if not exists web_session_id text;

-- Уникальность веб-сессии в рамках тенанта — НЕ частичная (как leads_tenant_vk_user_id_key):
-- NULL и так различны → tg/vk/max-лиды (web_session_id=NULL) не конфликтуют, а upsert_start
-- делает ON CONFLICT (tenant_id, web_session_id) БЕЗ предиката — частичный индекс он не матчит.
drop index if exists leads_web_session_id_key;  -- ранее ошибочно создавали частичным
create unique index if not exists leads_tenant_web_session_id_key
    on leads (tenant_id, web_session_id);
