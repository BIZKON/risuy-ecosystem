-- ОТКАТ (мгновенный): снять RLS с service_invoices. Применять, ЕСЛИ раздел «Подписка»
-- ослеп под RLS (текущий тариф/История пусты у клиента, у которого счета ЕСТЬ) или вебхук
-- перестал отмечать оплату из-за непокрытого app.tenant_id пути. Колонку tenant_id и
-- бэкфилл НЕ трогаем (безвредны), политику tenant_isolation НЕ дропаем (без enable RLS
-- бездействует) — при повторном включении заработает снова. Бот (owner) RLS и так обходит.
--
-- Запуск: twc-migrate.sh 4171827 81.31.246.136 risuy gen_user db/rollback_rls_service_invoices.sql
alter table service_invoices disable row level security;
