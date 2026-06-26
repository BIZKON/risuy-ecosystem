-- ── Реестр согласий субъектов ПДн (152-ФЗ ст. 9) ─────────────────────────────
-- Доказательная база «кто / когда / на какую редакцию текста / каким каналом» дал или
-- отозвал согласие на обработку ПДн. Append-only. Tenant-scoped (RLS по app.tenant_id, как
-- tenant_settings / leads). ОТДЕЛЬНО от admin_audit: тот — журнал ОПЕРАТОРОВ (actor=сотрудник),
-- а согласие даёт СУБЪЕКТ. Бот пишет (owner-роль, RLS обходит, фильтрует tenant_id явно);
-- панель читает реестр и может писать revoked при отзыве из кабинета.
--
-- ⚠️ DDL: применять twc-migrate.sh owner-DSN, СНАЧАЛА risuy_dev, ПЕРЕД деплоем кода
-- (set_consent начнёт писать сюда). Идемпотентно (if not exists).

create table if not exists consent_events (
    id           bigserial    primary key,
    tenant_id    uuid         not null references tenants(id) on delete cascade,
    lead_id      uuid         references leads(id) on delete set null,  -- запись переживает обезличивание лида
    doc_type     text         not null default 'consent',               -- consent | privacy (на будущее)
    doc_version  integer      not null default 1,                        -- версия редакции документа
    text_hash    text,                                                   -- sha256 текста, на который дано согласие
    action       text         not null check (action in ('granted', 'revoked')),
    channel      text,                                                   -- tg | vk | max | web
    occurred_at  timestamptz  not null default now(),
    ip           inet,
    user_agent   text
);

create index if not exists consent_events_tenant_lead_idx
    on consent_events (tenant_id, lead_id);
create index if not exists consent_events_tenant_action_idx
    on consent_events (tenant_id, action, occurred_at desc);

-- RLS: tenant_isolation по app.tenant_id (вариант nullif — пустой GUC фоновых задач → 0 строк, без ошибки).
alter table consent_events enable row level security;
do $$ begin
    if not exists (select 1 from pg_policies
                   where tablename = 'consent_events' and policyname = 'tenant_isolation') then
        create policy tenant_isolation on consent_events
            for all
            using (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
            with check (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);
    end if;
end $$;

-- panel_rw: append-only (как admin_audit) — читает реестр + пишет revoked из панели; без update/delete.
grant select, insert on consent_events to panel_rw;
revoke update, delete on consent_events from panel_rw;
grant usage on sequence consent_events_id_seq to panel_rw;
