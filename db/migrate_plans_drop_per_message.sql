-- Токен-биллинг v2, под-этап 1C (T-1C-1): депрекейт billing_mode='per_message'.
-- Планы econom/start → cost_multiplier (per_message_microrub обнуляется); снимаем CHECK
-- plans_per_message_chk (проверял «per_message ⇒ есть цена сообщения» — больше не нужен).
-- Идемпотентно. Expand-first: СНАЧАЛА risuy_dev, потом risuy (за явным «да» владельца).
-- Данные (dev+prod, 2026-07-22): per_message только у econom/start; 0 живых подписок,
-- 0 tenants.plan_id на этих планах → миграция никого не перетарифицирует.
-- markup_multiplier у econom/start уже = 3.00 → after cost_multiplier get_tenant_plan
-- вернёт валидный множитель (крэша нет). CHECK plans_billing_mode_check оставляем
-- ('per_message' — мёртвое, но допустимое значение; ужесточение — CONTRACT после cutover 1C-3).

update plans
   set billing_mode = 'cost_multiplier', per_message_microrub = null
 where code in ('econom', 'start') and billing_mode = 'per_message';

alter table plans drop constraint if exists plans_per_message_chk;
