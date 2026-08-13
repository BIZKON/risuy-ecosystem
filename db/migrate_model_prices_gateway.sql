-- Токен-биллинг, этап 0 (дыра A): цены модели AI GATEWAY (api.timeweb.ai).
--
-- ЗАЧЕМ. bot-telegram/ai.py:317-322 ищет цену по provider='timeweb-ai-gateway' и ТОЧНОМУ слагу
-- модели. Строки нет → _capture_gateway_usage выходит ДО charge_usage (ai.py:334): расход не
-- списывается и не восстанавливается (списание at-most-once, ключ привязан к request_id ответа).
--
-- ЦЕНЫ (ЛК Timeweb, снято владельцем 07.08.2026) — DeepSeek V4 Flash:
--   вход  18,9 ₽ за 1 млн токенов → 18 900 µRUB за 1 000 токенов
--   выход 37,8 ₽ за 1 млн токенов → 37 800 µRUB за 1 000 токенов
-- Себестоимость blended ≈ 28,35 ₽/млн против курса продажи 1 500 ₽/млн (billing_token_rate) —
-- маржа ≈ 98 %. Для сравнения: на V4 Pro (352,35 ₽/млн) маржа 76,5 %.
--
-- 🔴 ДВЕ СТРОКИ НА ОДНУ МОДЕЛЬ — ЭТО НЕ ОШИБКА. Читатель берёт слаг из поля "model" ОТВЕТА шлюза
-- (ai.py:267), а в настройке тенанта записан запрошенный 'deepseek/deepseek-v4-flash'. Какую из
-- форм вернёт шлюз — на 07.08 не проверено (в логах бота за неделю нет ни одного capture: путь
-- ни разу не вызывался). Поэтому покрываем ОБЕ формы одной ценой: поиск идёт по точному model,
-- строки друг с другом не конфликтуют. Когда в usage_ledger появится фактический слаг — лишнюю
-- строку можно не трогать, она просто не будет выбираться.
--
-- effective_from задан ЯВНОЙ меткой, а не now(): при now() повторный прогон дал бы другой момент,
-- guard `where not exists` не сработал бы, появилась вторая строка и стала бы действующей.
-- Метка в прошлом — читатель gateway-цены фильтра `<= now()` НЕ имеет (правится отдельно, A3).
--
-- ПРИМЕНЕНИЕ (сначала risuy_dev, прод risuy — за явным «да» владельца):
--   psql "<owner-dsn>" -v ON_ERROR_STOP=1 -f db/migrate_model_prices_gateway.sql

insert into model_prices (provider, model, price_in_microrub_per_1k, price_out_microrub_per_1k, effective_from)
select v.provider, v.model, v.pin, v.pout, v.ef
  from (values
          ('timeweb-ai-gateway', 'deepseek/deepseek-v4-flash', 18900::bigint, 37800::bigint, timestamptz '2026-08-07 12:00+03'),
          ('timeweb-ai-gateway', 'deepseek-v4-flash',          18900::bigint, 37800::bigint, timestamptz '2026-08-07 12:00+03')
       ) as v(provider, model, pin, pout, ef)
 where not exists (
   select 1 from model_prices m
    where m.provider = v.provider and m.model = v.model and m.effective_from = v.ef
 );

-- Проверка: ВСЕ строки провайдера — оператор должен видеть, что не наплодил версий.
select provider, model, price_in_microrub_per_1k, price_out_microrub_per_1k, effective_from,
       (effective_from <= now()) as deystvuet_seychas
  from model_prices
 where provider = 'timeweb-ai-gateway'
 order by model, effective_from desc;
