-- Токен-биллинг, этап 0 (гейт бэкфилла дыры B): цена модели, на которой создаются АГЕНТЫ тенантов.
--
-- ЗАЧЕМ. metering_worker ищет цену по provider='timeweb-cloud-ai' и слагу модели агента
-- (bot-telegram/metering_worker.py:194). Слаг строится из public_name каталога Timeweb:
--   _slug('DeepSeek V4 Pro') → 'deepseek-v4-pro'   (metering_worker.py:60)
-- В базе на 07.08 есть только 'deepseek-v4-pro-thinking' (модель 135, агент Школы 180177), а
-- PERSONA_AGENT_MODEL_ID = 133 = «DeepSeek V4 Pro» (admin-panel/config.py:584) → слаг другой,
-- цены под него НЕТ.
--
-- 🔴 БЕЗ ЭТОЙ СТРОКИ БЭКФИЛЛ РЕЕСТРА tenant_agents ЗАПУСКАТЬ НЕЛЬЗЯ: на ветке «нет цены» воркер
-- не пишет даже снапшот, дельта копится, ops-алерт летит раз в час (metering_worker.py:198-208),
-- а когда цена появится — весь накопленный объём спишется одним ударом и уронит кошелёк в минус.
-- Поэтому порядок строгий: сначала эта миграция, потом scripts/backfill_tenant_agents.py.
--
-- ЦЕНЫ (ЛК Timeweb, снято владельцем 07.08.2026) — DeepSeek V4 Pro:
--   вход  234,9 ₽ за 1 млн токенов → 234 900 µRUB за 1 000 токенов
--   выход 469,8 ₽ за 1 млн токенов → 469 800 µRUB за 1 000 токенов
-- Совпадает с уже вписанной строкой 'deepseek-v4-pro-thinking' — то есть Pro и Pro Thinking у
-- Timeweb стоят одинаково, и прежняя цена подтверждена независимым источником.
-- Себестоимость blended ≈ 352,35 ₽/млн против курса продажи 1 500 ₽/млн → маржа 76,5 %
-- (ровно та цифра, на которой построен docs/superpowers/notes/tarify-worksheet.md).
--
-- ПРИМЕНЕНИЕ (сначала risuy_dev, прод risuy — за явным «да» владельца):
--   psql "<owner-dsn>" -v ON_ERROR_STOP=1 -f db/migrate_model_prices_persona_agent.sql

insert into model_prices (provider, model, price_in_microrub_per_1k, price_out_microrub_per_1k, effective_from)
select 'timeweb-cloud-ai', 'deepseek-v4-pro', 234900::bigint, 469800::bigint, timestamptz '2026-08-07 12:00+03'
 where not exists (
   select 1 from model_prices
    where provider = 'timeweb-cloud-ai' and model = 'deepseek-v4-pro'
      and effective_from = timestamptz '2026-08-07 12:00+03'
 );

-- Проверка: все строки cloud-ai (должно стать две модели — pro и pro-thinking).
select provider, model, price_in_microrub_per_1k, price_out_microrub_per_1k, effective_from,
       (effective_from <= now()) as deystvuet_seychas
  from model_prices
 where provider = 'timeweb-cloud-ai'
 order by model, effective_from desc;
