-- Токен-биллинг v2, T-1F-2 (CONTRACT): ИНВАРИАНТ «ровно одна ЖИВАЯ подписка на тенанта».
-- Живая = status in ('trialing','active','past_due') — как в get_tenant_plan/activate.
--
-- Порядок (expand-first): СНАЧАЛА деплой кода activate_subscription_from_payment (он уже
-- collapse-ит лишние живые при активации), ПОТОМ эта миграция — она закрывает ИСТОРИЧЕСКИЕ
-- дубли (легаси-баг §9: прежний безусловный INSERT active плодил живые) + ставит partial
-- unique index, который физически не даёт появиться второй живой строке.
-- Идемпотентно (повторный прогон безопасен). risuy_dev — сейчас; прод (risuy) — ТОЛЬКО за «да».
-- Грантов не нужно (индекс; аддитивно к subscriptions_period_end_idx / subscriptions_renewal_idx).

-- ── Часть 1. Дедуп исторических живых: оставить НОВЕЙШУЮ, остальные → canceled ──
-- Новейшая = created_at desc, id desc (детерминированный tie-break). На проде с 0 дублей — no-op.
with ranked as (
    select id,
           row_number() over (partition by tenant_id
                              order by created_at desc, id desc) as rn
    from subscriptions
    where status in ('trialing', 'active', 'past_due')
)
update subscriptions s
   set status = 'canceled'
  from ranked r
 where s.id = r.id
   and r.rn > 1;

-- ── Часть 2. Partial unique index: инвариант «одна живая» на уровне БД ──
create unique index if not exists subscriptions_one_live_idx
    on subscriptions (tenant_id)
    where status in ('trialing', 'active', 'past_due');
