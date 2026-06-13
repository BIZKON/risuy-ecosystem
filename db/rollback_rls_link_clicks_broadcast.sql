-- ОТКАТ follow-up харденинга №2 (мгновенный): снять RLS с link_clicks / broadcast_recipients.
-- Применять, ЕСЛИ панель ослепла под RLS (раздел «Рассылки»: список адресатов/статусы пуст,
-- ИЛИ метрика кликов «Каналов» обнулилась) из-за непокрытого app.tenant_id пути. Бот (owner)
-- RLS и так обходил → на трекинг-редирект /r и материализацию рассылок Школы (§8.7) не влияет.
-- Политику tenant_isolation НЕ дропаем (она безвредна без enable RLS) — при повторном
-- включении RLS заработает снова.
--
-- Запуск: twc-migrate.sh 4171827 81.31.246.136 risuy gen_user db/rollback_rls_link_clicks_broadcast.sql
alter table link_clicks          disable row level security;
alter table broadcast_recipients disable row level security;
