-- ОТКАТ харденинга №2 (мгновенный): снять RLS с leads/messages/outbox.
-- Применять, ЕСЛИ панель ослепла под RLS (раздел «Лиды»/«Диалоги» пуст из-за непокрытого
-- app.tenant_id пути). Бот (owner) RLS и так обходил → на воронку Школы (§8.7) не влияет.
-- Политику tenant_isolation НЕ дропаем (она безвредна без enable RLS) — при повторном
-- включении RLS заработает снова.
--
-- Запуск: twc-migrate.sh 4171827 81.31.246.136 risuy gen_user db/rollback_rls_leads_messages.sql
alter table leads    disable row level security;
alter table messages disable row level security;
alter table outbox   disable row level security;
