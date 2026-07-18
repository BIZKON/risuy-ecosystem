-- SL: провенанс-шов public.leads (152-ФЗ). EXPAND, аддитивно, идемпотентно.
-- provenance = text + CHECK + app-allow-list (config.PROVENANCES), НЕ native enum.
-- NOT NULL DEFAULT 'inbound_optin' → fast-default мгновенно бэкфиллит существующие строки
-- и авто-тегирует все инбаунд-INSERT (upsert_start не правится). Движок B-FWD override'ит на
-- 'outbound_signal'. Прод-накат: сначала risuy_dev, затем прод risuy (за явным «да»).
alter table public.leads add column if not exists provenance    text not null default 'inbound_optin';
alter table public.leads add column if not exists consent_basis text;
alter table public.leads add column if not exists intent_score  integer;
alter table public.leads add column if not exists urgency       text;
alter table public.leads add column if not exists industry      text;
alter table public.leads add column if not exists geo           text;
alter table public.leads add column if not exists extracted     jsonb;
alter table public.leads add column if not exists source_url    text;
alter table public.leads add column if not exists over_quota    boolean default false;

do $$ begin
  if not exists (select 1 from pg_constraint where conname = 'leads_provenance_chk') then
    alter table public.leads add constraint leads_provenance_chk
      check (provenance in ('inbound_optin', 'outbound_signal', 'distributed_from_t0'));
  end if;
  if not exists (select 1 from pg_constraint where conname = 'leads_consent_basis_chk') then
    alter table public.leads add constraint leads_consent_basis_chk
      check (consent_basis is null or consent_basis = 'public_source');
  end if;
  -- Жёсткий 152-ФЗ-инвариант: outbound-строка НИКОГДА не несёт consent=true (защита от
  -- бага merge/dedup/ручной правки, повышающего согласие на спарсенном лице).
  if not exists (select 1 from pg_constraint where conname = 'leads_outbound_no_consent_chk') then
    alter table public.leads add constraint leads_outbound_no_consent_chk
      check (provenance <> 'outbound_signal' or consent = false);
  end if;
end $$;

create index if not exists leads_tenant_provenance_idx on public.leads (tenant_id, provenance);
