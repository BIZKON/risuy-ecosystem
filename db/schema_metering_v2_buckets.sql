-- Токен-биллинг v2, под-этап 1B (спека 2026-07-20 §6, §7.2): ДВА БАКЕТА кошелька.
-- credit_wallets получает ПУЛ ТАРИФА (included_microrub + included_period_end, сгорает —
-- абонплата D1) ОТДЕЛЬНО от КОШЕЛЬКА-АВАНСА (topup_microrub, возвратный по 782). Порядок
-- списания (T-1B-2): пул → кошелёк. balance_microrub ОСТАВЛЯЕМ (депрекейт в T-1C).
-- Наследует RLS tenant_isolation credit_wallets (новые колонки под той же политикой).
-- Грант panel_rw на credit_wallets — table-level (select,insert,update), новые колонки
-- покрыты → panel_role.sql НЕ трогаем. Применять ПОСЛЕ schema_metering.sql. Идемпотентно.
-- Expand-first: СНАЧАЛА risuy_dev, потом risuy (за явным «да» владельца).

alter table credit_wallets
    add column if not exists included_microrub   bigint not null default 0,  -- пул тарифа (сгорает по period_end)
    add column if not exists included_period_end timestamptz,                -- конец оплаченного периода пула
    add column if not exists topup_microrub      bigint not null default 0;  -- кошелёк-аванс (возвратный)

-- Бэкфилл: текущий единый balance_microrub = накопленные пополнения (аванс) → в кошелёк.
-- Идемпотентно (только где topup ещё 0), повторный прогон не задваивает.
update credit_wallets
   set topup_microrub = balance_microrub
 where balance_microrub <> 0 and topup_microrub = 0;
