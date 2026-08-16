-- Внедрение как платёжный объект (спека §15, поправка владельца 16.08.2026).
-- До этой миграции система умела принимать от клиента только абонплату; разовая продажа
-- внедрения за 60 000 ₽ нигде не учитывалась и шла мимо кода — а именно с неё и идёт
-- партнёрская доля.
--
-- Аддитивно и идемпотентно. Применять ПЕРЕД деплоем кода (старый код таблицу игнорирует).
--   bash ~/.claude/scripts/twc-migrate.sh 4171827 81.31.246.136 risuy_dev gen_user \
--       /Users/konstantin/Downloads/risuy-wt-landing/db/migrate_implementations.sql

-- Платформенный артефакт БЕЗ RLS — как tenants: внедрения продаём только мы, у тенанта
-- своих внедрений не бывает. Партнёр в строке НЕ хранится: резолвится из
-- tenants.partner_id той же функцией partner_pair_for_client. Дублировать привязку
-- значило бы завести второй источник истины о том, чей это клиент.
create table if not exists implementations (
    id                  uuid          primary key default gen_random_uuid(),
    tenant_id           uuid          not null references tenants(id),
    title               text          not null,
    amount_rub          numeric(12,2) not null check (amount_rub > 0),
    status              text          not null default 'pending',
    yookassa_payment_id text,
    payment_url         text,
    paid_at             timestamptz,
    note                text,
    created_by          text          not null,
    created_at          timestamptz   not null default now(),
    constraint implementations_status_chk check (status in ('pending','paid','canceled','refunded'))
);

-- Один платёж — одно внедрение. Матч вебхука идёт по этому полю.
create unique index if not exists implementations_ykid_uq
    on implementations (yookassa_payment_id) where yookassa_payment_id is not null;
create index if not exists implementations_tenant_idx on implementations (tenant_id, created_at desc);
create index if not exists implementations_status_idx on implementations (status);

-- Новый источник начисления. Check пересоздаётся свободно (это не enum).
alter table partner_accruals drop constraint if exists partner_accruals_skind_chk;
alter table partner_accruals add constraint partner_accruals_skind_chk
    check (source_kind in ('service_invoice','order','implementation'));

do $$ begin
    if exists (select 1 from pg_roles where rolname='panel_rw') then
        grant select, insert, update on implementations to panel_rw;
    end if;
end $$;
