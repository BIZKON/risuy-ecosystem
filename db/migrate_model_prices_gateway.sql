-- Токен-биллинг, этап 0 (дыра A): строка model_prices для AI GATEWAY (api.timeweb.ai).
--
-- ЗАЧЕМ. bot-telegram/ai.py:317-322 ищет цену строго по provider='timeweb-ai-gateway' и модели.
-- Строки нет → _capture_gateway_usage выходит ДО charge_usage (ai.py:334), расход НЕ списывается
-- и восстановить его нельзя: списание at-most-once, ключ идемпотентности привязан к request_id
-- конкретного ответа (ai.py:274-284). То есть без этой строки любой тенант на backend='gateway'
-- обслуживается бесплатно и навсегда.
--
-- ⚠️ БЕЗ РЕАЛЬНЫХ ЦЕН НЕ ПРИМЕНЯТЬ. Цены передаются psql-переменными; не задать их — psql упадёт,
-- и это НАМЕРЕННО (guardrail «цены не выдумываем»). Плейсхолдеров-заглушек здесь нет.
--
-- ЧТО ВПИСАТЬ (данные владельца из ЛК Timeweb):
--   price_in  — себестоимость ВХОДНЫХ токенов, µRUB за 1000 токенов (1 ₽ = 1 000 000 µRUB).
--   price_out — себестоимость ВЫХОДНЫХ токенов, µRUB за 1000 токенов.
--   Перевод из «₽ за млн токенов» P:  price = P * 1000.   Пример: 19 ₽/млн → 19000.
--   model_slug — слаг модели В ТОЙ ФОРМЕ, В КАКОЙ ЕГО ПЕЧАТАЕТ ШЛЮЗ в поле "model" ответа
--                (ai.py:267 отдаёт приоритет модели ИЗ ОТВЕТА, а не запрошенной в настройке).
--   ef — момент начала действия, ЯВНОЙ меткой: -v ef="'2026-08-02 12:00+03'".
--
-- 🔴 ПОЧЕМУ ef ЗАДАЁТСЯ ЯВНО, А НЕ now(). Уникальность — по (provider, model, effective_from).
-- При now() повторный прогон даёт ДРУГОЙ момент → on conflict не срабатывает → появляется вторая
-- строка, и она становится действующей (читатель берёт максимальный effective_from). Повтор
-- с опечаткой в цене молча вытеснил бы правильную. Явная метка делает прогон идемпотентным.
--
-- 🔴 БУДУЩУЮ ДАТУ СТАВИТЬ НЕЛЬЗЯ: читатель gateway-цены (ai.py:317-322) берёт последнюю строку
-- БЕЗ фильтра effective_from <= now(), то есть строка «на завтра» начнёт действовать немедленно.
-- Фильтр добавляется отдельным коммитом (план этапа 0, задача A3) — до него ef только прошлое/сейчас.
--
-- НА ЧТО ВЛИЯЕТ ЦИФРА. Для kind='llm' клиент платит по КУРСУ billing_token_rate (1,5 ₽ за 1000
-- токенов, shared/metering.py:173-179), а цена отсюда идёт в cost_microrub — то есть в СЕБЕСТОИМОСТЬ
-- и маржу отчётов, не в счёт клиенту. Ошибка в цифре искажает маржу; отсутствие строки убивает
-- выручку целиком. Наценка для llm — resource_pricing['llm'] = 1.000 (вшита в курс продажи).
--
-- ПРИМЕНЕНИЕ (СНАЧАЛА risuy_dev, прод risuy — за отдельным явным «да» владельца):
--   psql "<owner-dsn>" -v ON_ERROR_STOP=1 \
--        -v model_slug=deepseek/deepseek-v4-flash \
--        -v price_in=<µRUB/1k> -v price_out=<µRUB/1k> \
--        -v ef="'2026-08-02 12:00+03'" \
--        -f db/migrate_model_prices_gateway.sql
-- ⚠️ twc-migrate.sh psql-переменные НЕ передаёт → через него этот файл не применять.

insert into model_prices (provider, model, price_in_microrub_per_1k, price_out_microrub_per_1k, effective_from)
select 'timeweb-ai-gateway', :'model_slug', :price_in, :price_out, :ef::timestamptz
 where not exists (
   select 1 from model_prices
    where provider = 'timeweb-ai-gateway'
      and model = :'model_slug'
      and effective_from = :ef::timestamptz
 );

-- Проверка: ВСЕ строки провайдера, а не одна действующая — оператор обязан видеть, что не наплодил
-- версий и что «действующей» стала именно та, которую он вписал (последняя по effective_from).
select provider, model, price_in_microrub_per_1k, price_out_microrub_per_1k, effective_from,
       (effective_from <= now()) as deystvuet_seychas
  from model_prices
 where provider = 'timeweb-ai-gateway'
 order by model, effective_from desc;
