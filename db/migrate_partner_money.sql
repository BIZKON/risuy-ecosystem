-- Партнёрский кабинет: начисления, выплаты, приглашения (спека 2026-08-16).
-- EXPAND-шаг. Аддитивно и идемпотентно: старый код новые объекты игнорирует.
-- CONTRACT (включение RLS) — db/migrate_partner_money_rls.sql, ПОСЛЕ деплоя кода.
--
-- ПРИМЕНЕНИЕ (сначала risuy_dev!):
--   bash ~/.claude/scripts/twc-migrate.sh 4171827 81.31.246.136 risuy_dev gen_user \
--       /Users/konstantin/Downloads/risuy-wt-landing/db/migrate_partner_money.sql
--
-- ⚠️ Гранты panel_rw выданы в конце этого файла. Зеркалирование в db/panel_role.sql
-- (канон прав для пересоздания роли с нуля) отложено: файл в момент написания правился
-- другой сессией, и добавление своего блока утащило бы её работу в чужой коммит.

-- ── 1. Расширение реестра партнёров ──────────────────────────────────────────────
-- owner_tenant_id: null = НАША платформенная программа, не-null = программа тенанта.
-- Все существующие строки платформенные, бэкфилл не нужен.
alter table partners add column if not exists owner_tenant_id uuid references tenants(id);
-- parent_id: наставник. Фиксируется при регистрации и НЕ перепривязывается — смена
-- наставника задним числом переписала бы историю начислений.
alter table partners add column if not exists parent_id    uuid references partners(id);
alter table partners add column if not exists rate_percent numeric(5,2) not null default 20;
alter table partners add column if not exists joined_at    timestamptz not null default now();
alter table partners add column if not exists login_actor  text;
alter table partners add column if not exists email        text;
alter table partners add column if not exists tax_status   text;

-- Атрибуция для ТЕНАНТСКОГО контура: партнёр тенанта приводит лида, как партнёр
-- платформы приводит тенанта (tenants.partner_id, миграция migrate_partners.sql).
-- Колонка нужна уже сейчас: без неё partner_pair_for_client не соберётся.
alter table leads add column if not exists partner_id uuid references partners(id);
create index if not exists leads_partner_idx on leads (partner_id) where partner_id is not null;

create index if not exists partners_owner_idx  on partners (owner_tenant_id);
create index if not exists partners_parent_idx on partners (parent_id) where parent_id is not null;
create unique index if not exists partners_login_actor_uq
    on partners (login_actor) where login_actor is not null;

-- ── 2. Начисления ────────────────────────────────────────────────────────────────
create table if not exists partner_accruals (
    id              uuid primary key default gen_random_uuid(),
    partner_id      uuid not null references partners(id),
    owner_tenant_id uuid references tenants(id),   -- копия контура: для RLS и отчётов
    source_kind     text not null,                 -- service_invoice | order
    source_id       uuid not null,
    client_kind     text not null,                 -- tenant | lead
    client_id       uuid not null,
    level           smallint not null,             -- 0 продавец, 1 наставник
    rate_percent    numeric(5,2) not null,         -- КОПИЯ ставки на момент начисления
    amount_rub      numeric(12,2) not null,        -- отрицательная при сторно
    reason          text not null,                 -- sale | mentor | refund
    created_at      timestamptz not null default now(),
    constraint partner_accruals_level_chk  check (level in (0,1)),
    constraint partner_accruals_reason_chk check (reason in ('sale','mentor','refund')),
    constraint partner_accruals_skind_chk  check (source_kind in ('service_invoice','order')),
    constraint partner_accruals_ckind_chk  check (client_kind in ('tenant','lead'))
);

-- Повторный вебхук не начислит дважды.
create unique index if not exists partner_accruals_source_uq
    on partner_accruals (source_kind, source_id, partner_id, level)
    where reason <> 'refund';

-- 🔴 «Только первый платёж» — правило в БАЗЕ, а не в коде. Проверка «а не начисляли ли
-- мы уже» в Python была бы правдой ровно до первой параллельной оплаты.
create unique index if not exists partner_accruals_first_sale_uq
    on partner_accruals (client_kind, client_id, level)
    where reason in ('sale','mentor');

create index if not exists partner_accruals_partner_idx
    on partner_accruals (partner_id, created_at desc);

-- ── 3. Выплаты ───────────────────────────────────────────────────────────────────
-- Колонки «баланс» нет намеренно: сохранённое вычисляемое значение расходится с фактом
-- при первой правке задним числом. К выплате = сумма начислений − сумма выплат.
create table if not exists partner_payouts (
    id              uuid primary key default gen_random_uuid(),
    partner_id      uuid not null references partners(id),
    owner_tenant_id uuid references tenants(id),
    amount_rub      numeric(12,2) not null check (amount_rub > 0),
    paid_at         timestamptz not null default now(),
    method          text,
    note            text,
    created_by      text not null,
    created_at      timestamptz not null default now()
);
create index if not exists partner_payouts_partner_idx on partner_payouts (partner_id, paid_at desc);

-- ── 4. Одноразовые приглашения (образец: password_reset_tokens) ──────────────────
-- В базе лежит ХЕШ токена, не токен: утечка дампа не должна давать вход в кабинет.
create table if not exists partner_invites (
    token_hash text primary key,
    partner_id uuid not null references partners(id),
    expires_at timestamptz not null,
    used_at    timestamptz,
    created_by text not null,
    created_at timestamptz not null default now()
);
create index if not exists partner_invites_partner_idx on partner_invites (partner_id);

-- ── 5. 🔴 SECURITY DEFINER: без них платформенный контур мёртв ───────────────────
-- Тенант X платит абонплату → вебхук ставит app.tenant_id = X. Партнёр, приведший X, —
-- ПЛАТФОРМЕННЫЙ (owner_tenant_id is null). После включения RLS политика его не покажет и
-- не даст записать начисление: партнёр не получит ничего, ошибки в логах не будет.
-- Та же грабля, что чинили на orders (db/migrate_rls_discovery_fns.sql).
-- Функции исполняются под владельцем таблиц; владелец не подчиняется RLS, пока стоит
-- ENABLE, а не FORCE. EXECUTE выдаём ТОЛЬКО panel_rw.
--
-- Утечки между тенантами нет: функция скоупится своими аргументами — партнёр берётся по
-- атрибуции ЭТОГО клиента, а не поиском по таблице.

create or replace function partner_pair_for_client(p_client_kind text, p_client_id uuid)
returns table (
    role_level      smallint,
    partner_id      uuid,
    parent_id       uuid,
    joined_at       timestamptz,
    rate_percent    numeric,
    status          text,
    owner_tenant_id uuid
)
language sql
security definer
set search_path = public
as $$
    with seller as (
        select p.* from partners p
        where p.id = case
            when p_client_kind = 'tenant' then (select t.partner_id from tenants t where t.id = p_client_id)
            when p_client_kind = 'lead'   then (select l.partner_id from leads   l where l.id = p_client_id)
        end
    )
    select 0::smallint, s.id, s.parent_id, s.joined_at, s.rate_percent, s.status, s.owner_tenant_id
      from seller s
    union all
    select 1::smallint, m.id, m.parent_id, m.joined_at, m.rate_percent, m.status, m.owner_tenant_id
      from seller s join partners m on m.id = s.parent_id
$$;

create or replace function insert_partner_accrual(
    p_partner_id      uuid,
    p_owner_tenant_id uuid,
    p_source_kind     text,
    p_source_id       uuid,
    p_client_kind     text,
    p_client_id       uuid,
    p_level           smallint,
    p_rate            numeric,
    p_amount          numeric,
    p_reason          text
) returns uuid
language sql
security definer
set search_path = public
as $$
    insert into partner_accruals (partner_id, owner_tenant_id, source_kind, source_id,
                                  client_kind, client_id, level, rate_percent, amount_rub, reason)
    values (p_partner_id, p_owner_tenant_id, p_source_kind, p_source_id,
            p_client_kind, p_client_id, p_level, p_rate, p_amount, p_reason)
    on conflict do nothing
    returning id
$$;

-- Сторно по возврату: отражает ФАКТИЧЕСКИЕ строки начисления этого источника.
-- 🔴 Не пересчитывает от текущей ставки: ставка партнёра могла измениться между продажей
-- и возвратом, и пересчёт оставил бы на балансе разницу, которой никто не заметит.
-- Баланс обязан сходиться в ноль.
-- Идемпотентно: строка, по которой сторно уже есть, пропускается (not exists), поэтому
-- повторный возврат не уводит партнёра в минус. Уникальным индексом это не закрыть —
-- возврат бывает частичным и не один.
create or replace function storno_partner_accruals(p_source_kind text, p_source_id uuid)
returns integer
language sql
security definer
set search_path = public
as $$
    with inserted as (
        insert into partner_accruals (partner_id, owner_tenant_id, source_kind, source_id,
                                      client_kind, client_id, level, rate_percent, amount_rub, reason)
        select a.partner_id, a.owner_tenant_id, a.source_kind, a.source_id,
               a.client_kind, a.client_id, a.level, a.rate_percent, -a.amount_rub, 'refund'
          from partner_accruals a
         where a.source_kind = p_source_kind
           and a.source_id   = p_source_id
           and a.reason in ('sale','mentor')
           and not exists (
               select 1 from partner_accruals r
                where r.source_kind = a.source_kind and r.source_id = a.source_id
                  and r.partner_id  = a.partner_id  and r.level     = a.level
                  and r.reason = 'refund')
        returning 1
    )
    select count(*)::integer from inserted
$$;

-- ── 6. Гранты ────────────────────────────────────────────────────────────────────
-- delete не даём нигде: начисления и выплаты не удаляются, а сторнируются.
do $$ begin
    if exists (select 1 from pg_roles where rolname='panel_rw') then
        grant select, insert, update on partner_accruals, partner_payouts, partner_invites to panel_rw;
        grant execute on function partner_pair_for_client(text, uuid) to panel_rw;
        grant execute on function insert_partner_accrual(uuid, uuid, text, uuid, text, uuid, smallint, numeric, numeric, text) to panel_rw;
        grant execute on function storno_partner_accruals(text, uuid) to panel_rw;
    end if;
end $$;
