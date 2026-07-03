-- Клуб предпринимателей — домен (Фаза 1, Уровень 1). RLS tenant_isolation по app.tenant_id
-- (как migrate_consent_events.sql). network_opt_in заводится, но Уровень 2 — вне Фазы 1.
create table if not exists club_members (
    id             uuid primary key default gen_random_uuid(),
    tenant_id      uuid not null references tenants(id) on delete cascade,
    lead_id        uuid references leads(id) on delete set null,   -- если промоушен лида
    inn            text,                                           -- связь с prospects (ЕГРЮЛ), опц.
    display_name   text not null,
    city           text,
    okved          text,
    status         text not null default 'active' check (status in ('active','paused','left')),
    network_opt_in boolean not null default false,                 -- Уровень 2 (вне Фазы 1)
    created_at     timestamptz not null default now()
);
create index if not exists club_members_tenant_idx     on club_members (tenant_id, status);
create index if not exists club_members_tenant_city_idx on club_members (tenant_id, city, okved);
create index if not exists club_members_tenant_lead_idx on club_members (tenant_id, lead_id);

create table if not exists club_profiles (
    member_id      uuid primary key references club_members(id) on delete cascade,
    tenant_id      uuid not null references tenants(id) on delete cascade,   -- для RLS
    offering       text,
    avg_check      integer,
    seeking        text,
    chain_position text check (chain_position in ('before','after','both')),
    okved_seek     text,
    description    text
);

create table if not exists club_intros (
    id           uuid primary key default gen_random_uuid(),
    tenant_id    uuid not null references tenants(id) on delete cascade,   -- RLS = инициатор
    from_member  uuid not null references club_members(id) on delete cascade,
    to_member    uuid not null references club_members(id) on delete cascade,
    to_tenant_id uuid references tenants(id) on delete set null,           -- Ур.2 (в Фазе 1 = tenant_id)
    status       text not null default 'requested'
                 check (status in ('requested','accepted','declined','cancelled')),
    message      text,
    created_at   timestamptz not null default now(),
    decided_at   timestamptz
);
create index if not exists club_intros_tenant_idx on club_intros (tenant_id, status, created_at desc);

-- Двусторонний accept знакомства (красная линия 152-ФЗ: intro_accept с ОБЕИХ сторон).
-- Каждая сторона (инициатор from_member И получатель to_member) принимает СВОИМ действием;
-- status='accepted' и раскрытие контактов — только когда ОБЕ accepted_at проставлены.
alter table club_intros add column if not exists from_accepted_at timestamptz;
alter table club_intros add column if not exists to_accepted_at   timestamptz;

alter table consent_events add column if not exists member_id uuid references club_members(id) on delete set null;

-- RLS tenant_isolation (паттерн migrate_consent_events.sql)
do $$
declare t text;
begin
  foreach t in array array['club_members','club_profiles','club_intros'] loop
    execute format('alter table %I enable row level security', t);
    if not exists (select 1 from pg_policies where tablename=t and policyname='tenant_isolation') then
      execute format($f$create policy tenant_isolation on %I for all
        using (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
        with check (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)$f$, t);
    end if;
  end loop;
end $$;
