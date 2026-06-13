-- Харденинг №2: RLS на legacy-таблицах leads / messages / outbox.
-- (DECISIONS п.13 — включение было отложено до tenant-context панели; теперь он есть:
-- centralized-хук admin-panel/db.py ставит app.tenant_id на каждый acquire из тенанта
-- сессии (require_session), а вебхук заказов ставит его явно из тенанта заказа.)
--
-- panel_rw (БЕЗ bypassrls) теперь видит/пишет строки этих таблиц ТОЛЬКО своего тенанта —
-- изоляция лидов/переписки enforced БАЗОЙ, а не только фильтром в коде. Политика зеркалит
-- schema_metering/billing (tenant_id = current_setting('app.tenant_id')).
--
-- ⚠️ ENABLE, НЕ FORCE: владелец таблиц (роль бота) RLS ОБХОДИТ → бот пишет воронку как
-- раньше, БЕЗ app.tenant_id (§8.7 не затронут). Идемпотентно.
-- ⚠️ ПОРЯДОК: применять ТОЛЬКО ПОСЛЕ деплоя кода (centralized-хук + вебхук), иначе панель
-- (и вебхук конвертации) ослепнут под deny-by-default. Откат мгновенный:
--   alter table leads/messages/outbox disable row level security;
--
-- link_clicks / broadcast_recipients (трекинг, tenant_id есть) — НЕ трогаем; параллельный
-- follow-up при необходимости (ниже PII-чувствительность).

alter table leads    enable row level security;
alter table messages enable row level security;
alter table outbox   enable row level security;

-- nullif(..., '')::uuid: app.tenant_id может быть ПУСТОЙ строкой (asyncpg на release делает
-- RESET → кастомный GUC возвращается к дефолту ''), а ''::uuid падает с ошибкой. nullif
-- превращает '' в NULL → tenant_id = NULL = false → 0 строк, БЕЗ ошибки (фон/без сессии).
-- drop+create — идемпотентный апдейт политики при повторном применении.
drop policy if exists tenant_isolation on leads;
create policy tenant_isolation on leads
    for all
    using (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    with check (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

drop policy if exists tenant_isolation on messages;
create policy tenant_isolation on messages
    for all
    using (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    with check (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

drop policy if exists tenant_isolation on outbox;
create policy tenant_isolation on outbox
    for all
    using (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    with check (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

-- Гранты panel_rw на leads/messages/outbox уже выданы (panel_role.sql / schema_panel_ext) —
-- RLS лишь фильтрует строки поверх существующих привилегий.
