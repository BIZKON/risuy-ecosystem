-- B7 (аудит #5), ШАГ 1 (EXPAND, безопасно/аддитивно): SECURITY DEFINER-функции discovery для
-- вебхука ЮKassa, ЧТОБЫ включить RLS на orders НЕ сломав приём оплаты.
--
-- ПРОБЛЕМА: вебхук работает в процессе ПАНЕЛИ (роль panel_rw, НЕ owner) и читает orders СЕССИОННО-
-- БЕЗ app.tenant_id (он там только УЗНАЁТ тенанта заказа). После включения RLS deny-by-default такие
-- чтения вернут 0 строк → заказ не найдётся → оплата зависнет в pending (прод-простой платежей).
--
-- РЕШЕНИЕ: discovery-чтения orders выносим в SECURITY DEFINER-функции. Они исполняются под
-- ВЛАДЕЛЬЦЕМ (gen_user — владелец orders), а владелец таблицы НЕ подчиняется RLS, пока на таблице
-- НЕ выставлен FORCE ROW LEVEL SECURITY (мы ставим обычный ENABLE). → функции читают orders в обход
-- RLS. EXECUTE выдаём ТОЛЬКО panel_rw. Сами функции тенант-СКОУПЯТ по своему аргументу (payment_id/
-- order_id уникальны), утечки между тенантами нет: вернётся ровно заказ этого платежа/id.
--
-- ⚠️ ПОРЯДОК (expand-contract): (1) эта миграция dev+прод; (2) деплой кода панели, который ВЫЗЫВАЕТ
-- эти функции (push владельца); (3) ТОЛЬКО ПОТОМ migrate_rls_orders_kb_broadcasts.sql (enable RLS).
-- До шага 3 функции просто дублируют прямые SELECT — поведение не меняется. Идемпотентно.
--
-- ПРИМЕНЕНИЕ: bash ~/.claude/scripts/twc-migrate.sh 4171827 81.31.246.136 <db> gen_user /abs/.../db/migrate_rls_discovery_fns.sql

-- tenant_id заказа по id платежа ЮKassa (основной матч вебхука).
create or replace function order_tenant_for_payment(p_payment_id text)
returns uuid
language sql
security definer
set search_path = public
as $$
    select tenant_id from orders where provider_payment_id = p_payment_id
$$;

-- tenant_id заказа по ЕГО id (фолбэк #9 — из metadata.order_id платежа). Невалидный uuid обрабатывает
-- вызывающий (passes text; кривой uuid → ошибка каста ловится в коде панели как «не наш заказ»).
create or replace function order_tenant_by_id(p_order_id uuid)
returns uuid
language sql
security definer
set search_path = public
as $$
    select tenant_id from orders where id = p_order_id
$$;

-- Есть ли заказ с таким платежом (ветка вебхука: заказ vs счёт подписки).
create or replace function order_exists_for_payment(p_payment_id text)
returns boolean
language sql
security definer
set search_path = public
as $$
    select exists (select 1 from orders where provider_payment_id = p_payment_id)
$$;

-- Только panel_rw (вебхук в процессе панели). PUBLIC — отозвать (least-privilege).
revoke all on function order_tenant_for_payment(text)  from public;
revoke all on function order_tenant_by_id(uuid)        from public;
revoke all on function order_exists_for_payment(text)  from public;
grant execute on function order_tenant_for_payment(text) to panel_rw;
grant execute on function order_tenant_by_id(uuid)       to panel_rw;
grant execute on function order_exists_for_payment(text) to panel_rw;
