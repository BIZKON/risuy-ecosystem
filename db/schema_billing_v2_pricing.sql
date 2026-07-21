-- Токен-биллинг v2, под-этап 1A (спека 2026-07-20 §5, §7.1): ПРАЙС-СЛОЙ.
-- Ценовой инвариант §5: КУРС продажи токена хранится ОТДЕЛЬНО от НАЦЕНКИ ресурса,
--   курс × наценка_ресурса = целевая наценка (LLM ×4,26; DaData ×3; голос ×2).
-- Решение владельца #1 (2026-07-21): курс — ВЕРСИОНИРУЕМАЯ строка в БД (effective_from),
--   НЕ const: курс = цена оферты → нужен период действия + аудит + снимок в usage_ledger.
-- Применять ПОСЛЕ schema_metering.sql. Идемпотентно. Платформенные справочники (без RLS,
--   как model_prices): себестоимость/цены клиентскому коду в шаблоны не отдаются.
-- Expand-first: СНАЧАЛА risuy_dev, потом risuy (за явным «да» владельца).

-- ── resource_pricing: НАЦЕНКА НА РЕСУРС (не единый множитель плана) ────────────
-- LLM=1.000 (наценка вшита в КУРС продажи, не в множитель); DaData=3.000; голос=2.000;
-- эмбеддинг=3.000. Правится UPDATE (стабильный справочник, не версионируется).
create table if not exists resource_pricing (
    resource          text primary key,          -- 'llm' | 'dadata' | 'voice' | 'embedding'
    markup_multiplier numeric(6,3) not null,      -- наценка ресурса (курс × наценка = целевая)
    note              text
);

insert into resource_pricing (resource, markup_multiplier, note) values
    ('llm',       1.000, 'наценка вшита в курс продажи токена (×4,26 над себестоимостью LLM)'),
    ('dadata',    3.000, 'проверка/поиск контрагента DaData — маржа 66,7%'),
    ('voice',     2.000, 'голос-минута EL-стек — пол маржи 50%'),
    ('embedding', 3.000, 'эмбеддинги/индексация БЗ')
on conflict (resource) do nothing;

-- ── billing_token_rate: КУРС ПРОДАЖИ токена, ВЕРСИОНИРУЕМЫЙ (решение #1) ────────
-- Читать текущий: where effective_from <= now() order by effective_from desc limit 1
-- (допускает будущий курс = запланированную смену цены оферты). Смена курса = НОВАЯ
-- строка (аудит), НЕ update: курс — цена оферты (D1), смена = новая редакция + уведомление.
create table if not exists billing_token_rate (
    id                   bigint generated always as identity primary key,
    effective_from       timestamptz not null default now(),
    rate_microrub_per_1k bigint not null,          -- 1_500_000 = 0,0015 ₽/токен (1500 ₽/млн)
    note                 text,
    unique (effective_from)
);

insert into billing_token_rate (rate_microrub_per_1k, note)
select 1500000, 'старт токен-модели: 0,0015 ₽/токен (×4,26 над LLM-себест. 0,000352 ₽)'
where not exists (select 1 from billing_token_rate);

-- ── usage_ledger: снимок КУРСА на момент LLM-списания (аудит смены цены оферты) ─
-- Заполняется только для kind='llm' (charged = tokens × курс); для DaData/голос/
-- эмбеддинг остаётся NULL (там charged = cost × наценка, снимок множителя — в multiplier).
alter table usage_ledger add column if not exists token_rate_microrub_per_1k bigint;

-- ── Гранты panel_rw (зеркалятся в db/panel_role.sql) ──────────────────────────
do $$ begin
    if exists (select 1 from pg_roles where rolname = 'panel_rw') then
        grant select, insert, update on resource_pricing   to panel_rw;  -- наценки правятся UPDATE
        grant select, insert         on billing_token_rate to panel_rw;  -- курс — новой строкой, не правится
        -- ⚠️ Timeweb ALTER DEFAULT PRIVILEGES авто-выдаёт panel_rw ВСЕ права на новые
        -- owner-таблицы → append-only курса держится ТОЛЬКО явным REVOKE (как consent_events).
        revoke update, delete on billing_token_rate from panel_rw;  -- курс неизменяем (аудит цены оферты)
    end if;
end $$;
