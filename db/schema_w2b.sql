-- Reseller-платформа, Wave 2b (ТЗ §5.3): автосписания рекуррента ЮKassa.
-- Применять ПОСЛЕ schema_billing.sql. Идемпотентно. СНАЧАЛА risuy_dev, потом прод.
--
-- Расширяет subscriptions полями для безакцептных автосписаний:
--   • receipt_email — email клиента (с первой оплаты) для чека 54-ФЗ безакцептных
--     платежей: магазин подписки 1378536 фискальный → каждый платёж требует receipt;
--   • last_charge_attempt_at — для backoff-ретраев cron (не долбить ЮKassa каждый тик);
--   • charge_attempts — счётчик неудачных попыток автосписания (потолок → canceled + алерт).
-- Сохранение payment_method_id (для безакцепта) уже есть: subscriptions.yookassa_payment_method_id
-- (schema_billing.sql) — в Wave 2b начинаем его ЗАПОЛНЯТЬ (save_payment_method при оплате).

alter table subscriptions add column if not exists receipt_email          text;
alter table subscriptions add column if not exists last_charge_attempt_at timestamptz;
alter table subscriptions add column if not exists charge_attempts        int not null default 0;

-- Скан автосписаний cron'а: живые подписки с сохранённой картой и истёкшим периодом.
-- Частичный индекс — дёшево (подписок мало, кандидатов на списание ещё меньше).
create index if not exists subscriptions_renewal_idx on subscriptions (current_period_end)
    where status in ('active', 'past_due') and yookassa_payment_method_id is not null;

select 'w2b subscriptions расширена' as итог,
       count(*) filter (where yookassa_payment_method_id is not null) as с_сохранённой_картой
from subscriptions;
