-- Токен-биллинг v2, T-1D-1: строка model_prices для ЭМБЕДДЕРА (self-host TEI).
--
-- ⚠️ ЭТОТ ФАЙЛ НЕЛЬЗЯ ПРИМЕНИТЬ БЕЗ РЕАЛЬНОЙ ЦЕНЫ. Себестоимость передаётся psql-переменной
-- :price_in — если её не задать, psql упадёт («unrecognized variable»), и это НАМЕРЕННО:
-- guardrail ТЗ §10 «цены не выдумываем». Плейсхолдера-заглушки здесь нет специально —
-- фальшивая цена начала бы списывать деньги тенантов по выдуманному тарифу.
--
-- ЧТО ВПИСАТЬ (данные владельца #3, из ЛК Timeweb):
--   price_in — СЕБЕСТОИМОСТЬ в µRUB за 1000 токенов (1 ₽ = 1_000_000 µRUB).
--   • если известна managed-цена «₽ за млн токенов» P:      price_in = P * 1000
--       (пример: 45 ₽/млн → 45000)
--   • если известна аренда VM+сторедж R ₽/мес и ожидаемый объём V млн токенов/мес:
--       price_in = R * 1000 / V      (амортизация; пример: 3000 ₽/мес ÷ 10 млн → 300000)
--   price_out НЕ передаётся и ставится 0: у эмбеддингов выхода нет, units считаются
--   только по входу (shared/embed_metering.py читает исключительно price_in_microrub_per_1k).
--
-- Наценка НЕ здесь: resource_pricing['embedding'] = 3.000 (засеяна в schema_billing_v2_pricing.sql,
-- на проде есть). Клиент заплатит price_in × 3.
--
-- ПРИМЕНЕНИЕ (сначала risuy_dev, прод risuy — за явным «да»):
--   psql "<owner-dsn>" -v ON_ERROR_STOP=1 -v price_in=45000 -f db/migrate_model_prices_embedder.sql
-- ⚠️ twc-migrate.sh переменные НЕ передаёт → через него этот файл не применять.
--
-- Версионируемость: model_prices уникальна по (provider, model, effective_from) — смена цены
-- делается НОВОЙ строкой с новым effective_from, старая остаётся историей (читатель берёт
-- последнюю с effective_from <= now()). Повторный прогон в ту же миллисекунду — no-op.

insert into model_prices (provider, model, price_in_microrub_per_1k, price_out_microrub_per_1k)
values ('timeweb-tei', 'multilingual-e5-base', :price_in, 0)
on conflict (provider, model, effective_from) do nothing;

-- Проверка: что реально применится (последняя действующая строка эмбеддера).
select provider, model, price_in_microrub_per_1k, price_out_microrub_per_1k, effective_from
  from model_prices
 where provider = 'timeweb-tei' and model = 'multilingual-e5-base'
   and effective_from <= now()
 order by effective_from desc
 limit 1;
