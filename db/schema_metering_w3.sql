-- Reseller-платформа, Wave 3 (ТЗ §5.2; DECISIONS п.5): реестр агентов тенантов.
-- Применять ПОСЛЕ schema_metering.sql. Идемпотентно. СНАЧАЛА risuy_dev, потом прод.
--
-- tenant_agents: какой cloud-ai агент принадлежит какому тенанту. Основа метеринга
-- по дельте used_tokens: снапшот-воркер бота метрирует ТОЛЬКО агентов из реестра.
-- Агент аккаунта БЕЗ строки в реестре никому не списывается (воркер логирует и
-- пропускает) — чужой/служебный агент не может разорить тенанта по ошибке.

create table if not exists tenant_agents (
    agent_id   bigint primary key,               -- числовой id агента Timeweb (для PATCH/учёта)
    tenant_id  uuid not null references tenants(id),
    access_id  text,                             -- UUID для /call (справочно, бот зовёт по нему)
    note       text,
    created_at timestamptz not null default now()
);
create index if not exists tenant_agents_tenant_idx on tenant_agents (tenant_id);

alter table tenant_agents enable row level security;
do $$ begin
    if not exists (select 1 from pg_policies
                   where tablename = 'tenant_agents' and policyname = 'tenant_isolation') then
        create policy tenant_isolation on tenant_agents
            for all
            using (tenant_id = current_setting('app.tenant_id', true)::uuid)
            with check (tenant_id = current_setting('app.tenant_id', true)::uuid);
    end if;
end $$;

-- seed: Лия (агент 180177) принадлежит Школе Лесова — факт прода 2026-06-12
-- (GET /cloud-ai/agents: id=180177, access_id ниже, model_id=135 V4 Pro Thinking).
insert into tenant_agents (agent_id, tenant_id, access_id, note)
select 180177, t.id, '0c6e804f-ca0a-4739-932e-0625928763f1', 'Лия — первый агент Школы'
from tenants t
where t.slug = 'lesov-school'
on conflict (agent_id) do nothing;

-- Гранты panel_rw (зеркалятся в panel_role.sql): панель ведёт реестр при
-- провижининге агентов; снапшот-воркер бота ходит под owner (RLS обходит).
do $$ begin
    if exists (select 1 from pg_roles where rolname = 'panel_rw') then
        grant select, insert, update, delete on tenant_agents to panel_rw;
    end if;
end $$;

-- Контрольный смоук.
select 'w3 tenant_agents готов' as итог,
       (select count(*) from tenant_agents) as агентов_в_реестре;
