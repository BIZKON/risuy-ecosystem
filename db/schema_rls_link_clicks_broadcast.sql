-- Харденинг №2 (follow-up): RLS на трекинг-таблицах link_clicks / broadcast_recipients.
-- Продолжение schema_rls_leads_messages.sql — закрываем оставшиеся две tenant-scoped
-- таблицы с ПДн (link_clicks: ip/ua; broadcast_recipients: tg_user_id адресата).
--
-- ⚠️ КОД УЖЕ В ПРОДЕ → expand-contract соблюдён БЕЗ нового деплоя: центральный tenant-хук
-- панели (admin-panel/db.py::_apply_tenant_guc, setup пула) ставит app.tenant_id на КАЖДОМ
-- acquire из активного тенанта сессии. Панель обе таблицы ТОЛЬКО SELECT-ит (panel_role.sql:
-- grant select on broadcast_recipients/link_clicks to panel_rw) и только из authed-роутов
-- (total_link_clicks → дашборд; list_broadcast_recipients/статусы → страница рассылки). Ни
-- одного sessionless-пути панели к ним нет (вебхук ЮKassa их не читает). Поэтому отдельный
-- код-деплой ПЕРЕД RLS не требуется — хук покрывает их так же, как leads/messages.
--
-- ⚠️ ENABLE, НЕ FORCE: владелец таблиц (роль бота) RLS ОБХОДИТ → обработчик /r (bot.py,
-- log_link_click) и материализация/статусы рассылки (worker.py, claim_broadcast_recipients)
-- пишут как раньше, БЕЗ app.tenant_id (§8.7 не затронут). Вставки уже несут tenant_id
-- (NOT NULL, DEFAULT снят в Wave 3d) — RLS для owner ничего не меняет.
--
-- Откат мгновенный (если панель ослепнет на разделе «Рассылки»/«Каналы»):
--   db/rollback_rls_link_clicks_broadcast.sql
--
-- Не входят в эту волну (app-фильтр tenant_id остаётся): broadcasts / link_tokens /
-- broadcast_files — меньший приоритет (нет прямых ПДн), консистентный follow-up при нужде.

alter table link_clicks          enable row level security;
alter table broadcast_recipients enable row level security;

-- nullif(..., '')::uuid: после asyncpg RESET ALL кастомный GUC app.tenant_id = '' (пусто);
-- ''::uuid падает с ошибкой → nullif превращает '' в NULL → tenant_id = NULL = false →
-- 0 строк БЕЗ ошибки касту (фон/без сессии). Зеркалит политику leads/messages/outbox.
-- drop+create — идемпотентный апдейт политики при повторном применении.
drop policy if exists tenant_isolation on link_clicks;
create policy tenant_isolation on link_clicks
    for all
    using (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    with check (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

drop policy if exists tenant_isolation on broadcast_recipients;
create policy tenant_isolation on broadcast_recipients
    for all
    using (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    with check (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

-- Гранты panel_rw (select) на обе таблицы уже выданы (panel_role.sql:115,124) —
-- RLS лишь фильтрует строки поверх существующих привилегий.
