-- Харденинг мультитенантности — ШАГ 1 (АДДИТИВНЫЙ, expand).
-- Лид уникален в пределах ТЕНАНТА, а не глобально: добавляем составной unique
-- (tenant_id, tg_user_id). Глобальный leads_tg_user_id_key пока ОСТАЁТСЯ — старый код
-- (on conflict (tg_user_id)) продолжает работать без сбоев. tenant_id уже NOT NULL
-- (Wave 3d), поэтому индекс корректен и однозначен. Идемпотентно.
--
-- Порядок выкатки (expand-contract, zero-downtime):
--   1) ЭТОТ файл → risuy_dev, затем прод (аддитивно, до деплоя кода);
--   2) деплой кода: upsert → on conflict (tenant_id, tg_user_id) + tenant-скоуп чтений лида;
--   3) migrate_lead_drop_global_tg_unique.sql → risuy_dev, затем прод (ПОСЛЕ деплоя кода).
create unique index if not exists leads_tenant_tg_user_id_key
    on leads (tenant_id, tg_user_id);
