-- Биллинг СЕРВИСА (раздел «Подписка»): школа платит агентству за экосистему по
-- ТАРИФАМ (модель НЕЙРОАГЕНТОВ). Каждый счёт = один оплаченный период тарифа со
-- снимком квоты/превышения. Текущий тариф/период выводятся из последнего ОПЛАЧЕННОГО
-- счёта; флаг отмены — в app_settings (отдельной таблицы подписки нет). Отдельно от
-- orders (там — продажи школы своим лидам, B2C). Идемпотентно (IF NOT EXISTS).
--
-- ПОРЯДОК ПРИМЕНЕНИЯ (строго):
--   schema.sql → schema_admin.sql → schema_panel_ext.sql → schema_products.sql →
--   schema_orders.sql → schema_service.sql → panel_role.sql
-- Применить ОДИН РАЗ owner-DSN ПЕРЕД деплоем кода:
--   psql "$OWNER_DATABASE_URL" -f db/schema_service.sql
--
-- ── Граница доступа ───────────────────────────────────────────────────────────
-- Панель (panel_rw): INSERT счёта при выборе тарифа/«Оплатить»; UPDATE статуса/карты
-- из ВЕБХУКА ЮKassa (вебхук в процессе панели перепроверяет платёж через API ЮKassa).
-- Метрика «сообщения ИИ» считается на лету по messages (source='liya'); квота/overage
-- снимаются в счёт на момент выставления. gen_random_uuid() → pgcrypto.

create extension if not exists "pgcrypto";

-- Счёт за период тарифа. plan_key/plan_name/quota — СНИМОК тарифа на момент выставления
-- (тариф в коде мог измениться — история не должна плыть). overage_count/amount —
-- превышение ПРЕДЫДУЩЕГО периода, доначисленное в этот счёт. amount = plan_amount +
-- overage_amount (фактически списано). card_last4 — последние 4 цифры карты из платежа.
create table if not exists service_invoices (
    id                  uuid          primary key default gen_random_uuid(),
    period_start        date          not null,
    period_end          date          not null,
    plan_key            text          not null,
    plan_name           text          not null,
    quota               integer,                                  -- снимок квоты сообщений ИИ (null = безлимит/договорная)
    plan_amount         numeric(12,2) not null,                   -- базовая цена тарифа
    overage_count       integer       not null default 0,         -- превышение прошлого периода (сообщений)
    overage_amount      numeric(12,2) not null default 0,         -- доначислено за превышение
    amount              numeric(12,2) not null,                   -- ИТОГО к оплате (plan_amount + overage_amount)
    currency            text          not null default 'RUB',
    status              text          not null default 'pending', -- pending|paid|canceled
    yookassa_payment_id text,
    card_last4          text,
    paid_at             timestamptz,
    created_by          text,
    created_at          timestamptz   not null default now(),
    constraint service_invoices_status_chk check (status in ('pending','paid','canceled')),
    constraint service_invoices_amount_chk check (amount >= 0)
);
create unique index if not exists service_invoices_ykid_idx
    on service_invoices (yookassa_payment_id) where yookassa_payment_id is not null;
create index if not exists service_invoices_status_idx on service_invoices (status);
create index if not exists service_invoices_period_idx on service_invoices (period_end desc);
-- UUID-PK с default gen_random_uuid() → секвенса нет, грант usage on sequence не нужен.
