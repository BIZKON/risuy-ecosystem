"""Метеринг потребления ИИ (ТЗ §5.1–§5.2; DECISIONS п.2–5, 16–17): списание из кошелька.

charge_usage() — ЕДИНСТВЕННАЯ точка списания кредитов тенанта. Одна транзакция,
три гвоздя конструкции (ТЗ §5.1):
  • unique(idempotence_key)        — двойное списание на ретрае исключено;
  • SELECT … FOR UPDATE кошелька   — овердрафт на параллельных списаниях исключён;
  • целые µRUB + ceil_mul          — дрейф float исключён (округление ВСЕГДА вверх,
                                     единая точка — shared/money.py).

Все рабочие списания Wave 3 — ПОСТФАКТУМ (токены у Timeweb уже потрачены), поэтому
вызываются с allow_negative=True: отклонять уже потреблённое нельзя — кошелёк может
уйти в минус (клиент увидит «пополните» в панели). allow_negative=False (дефолт) —
prepaid-отказ для pre-check-путей и приёмки §8.4: при нехватке средств поднимает
InsufficientCreditsError, НЕ трогая ни кошелёк, ни леджер.

Множитель наценки и цена сообщения — ТОЛЬКО с сервера (план тенанта из БД,
DECISIONS п.2); из клиентского запроса не принимаются никогда. Тенант без живой
подписки (Школа Лесова до Wave 4) — переходник: cost_multiplier ×3, prepaid=False,
никаких блокировок — критерий §8.7 «Школа не сломана» держится при любом балансе.
"""
from __future__ import annotations

import json
from decimal import Decimal

import asyncpg

from shared.money import ceil_mul

# Дефолт реселлера для тенанта без плана (ТЗ §1: канон — cost_multiplier ×3).
DEFAULT_MULTIPLIER = Decimal("3.00")


class InsufficientCreditsError(Exception):
    """Кошелька не хватает на списание (prepaid-режим, allow_negative=False)."""

    def __init__(self, tenant_id, balance: int, charged: int):
        super().__init__(
            f"кошелёк тенанта {tenant_id}: {balance} µRUB < списания {charged} µRUB"
        )
        self.tenant_id = tenant_id
        self.balance = balance
        self.charged = charged


async def get_tenant_plan(conn: asyncpg.Connection, tenant_id) -> dict:
    """Тарифный контекст тенанта С СЕРВЕРА (план никогда не приходит из запроса).

    Приоритет: живая подписка (trialing/active/past_due) → tenants.plan_id →
    дефолт «без плана» (cost_multiplier ×3, prepaid=False — переходник Школы до
    Wave 4). prepaid=True ⇔ план найден: postpaid-планов в v1 нет (DECISIONS п.16).
    """
    row = await conn.fetchrow(
        """
        select p.code, p.billing_mode, p.markup_multiplier, p.per_message_microrub
        from subscriptions s join plans p on p.id = s.plan_id
        where s.tenant_id = $1 and s.status in ('trialing','active','past_due')
        order by s.created_at desc limit 1
        """,
        tenant_id,
    )
    if row is None:
        row = await conn.fetchrow(
            "select p.code, p.billing_mode, p.markup_multiplier, p.per_message_microrub "
            "from tenants t join plans p on p.id = t.plan_id where t.id = $1",
            tenant_id,
        )
    if row is None:
        return {
            "code": None,
            "billing_mode": "cost_multiplier",
            "markup_multiplier": DEFAULT_MULTIPLIER,
            "per_message_microrub": None,
            "prepaid": False,
        }
    return {
        "code": row["code"],
        "billing_mode": row["billing_mode"],
        "markup_multiplier": Decimal(row["markup_multiplier"]),
        "per_message_microrub": row["per_message_microrub"],
        "prepaid": True,
    }


def blended_price_per_token(price_in_per_1k: int, price_out_per_1k: int, out_share) -> Decimal:
    """µRUB за ОДИН токен по смешанной цене. used_tokens cloud-ai не делит
    вход/выход (DECISIONS п.5) → (1−share)·вход + share·выход, делённые на 1000.
    Результат — Decimal: округлится ОДИН раз, в ceil_mul на итоговой сумме."""
    share = Decimal(str(out_share))
    return (Decimal(price_in_per_1k) * (1 - share) + Decimal(price_out_per_1k) * share) / 1000


async def _current_token_rate(conn: asyncpg.Connection) -> int:
    """Текущий курс продажи токена (µRUB за 1k) из billing_token_rate — версионируемый
    (решение #1: курс = цена оферты, читается снимком в транзакции списания). Допускает
    будущий курс (effective_from <= now). Пустая таблица = конфиг-ошибка, не молча 0."""
    rate = await conn.fetchval(
        "select rate_microrub_per_1k from billing_token_rate "
        "where effective_from <= now() order by effective_from desc limit 1"
    )
    if rate is None:
        raise RuntimeError("billing_token_rate пуст — курс продажи токена не задан")
    return int(rate)


async def _resource_markup(conn: asyncpg.Connection, resource: str, *, default) -> Decimal:
    """Наценка ресурса из resource_pricing (per-resource прайс, §7.1). Неизвестный
    ресурс → default (множитель плана; для llm — 1.00, наценка вшита в курс)."""
    mk = await conn.fetchval(
        "select markup_multiplier from resource_pricing where resource = $1", resource
    )
    return Decimal(mk) if mk is not None else Decimal(str(default))


async def charge_usage(
    conn: asyncpg.Connection,
    tenant_id,
    cost_microrub: int,
    meta: dict,
    idempotence_key: str,
    *,
    allow_negative: bool = False,
) -> asyncpg.Record:
    """Транзакционное списание (ТЗ §5.1). Возвращает строку usage_ledger — новую
    или существующую (повтор idempotence_key = ровно одно списание, ретрай безопасен).

    conn — asyncpg-соединение; можно звать ВНУТРИ внешней транзакции (вложенный
    transaction() станет savepoint'ом — снапшот+списание атомарны у вызывающего).

    meta: kind ('llm'|'embedding'|'message'|'other'), provider, model,
          units (dict: tokens_*/messages), request_id. charged считается ЗДЕСЬ:
          • billing_mode='per_message' и kind='message' → цена сообщения плана;
          • иначе → ceil_mul(cost, множитель плана). Оба значения — из БД (сервер).
    """
    if cost_microrub < 0:
        raise ValueError("cost_microrub не может быть отрицательным")
    try:
        async with conn.transaction():
            dup = await conn.fetchrow(
                "select * from usage_ledger where idempotence_key = $1", idempotence_key
            )
            if dup:
                return dup

            # Кошелька может ещё не быть (тенант без топапов) — создаём нулевой,
            # затем блокируем строку (FOR UPDATE) до конца транзакции.
            await conn.execute(
                "insert into credit_wallets (tenant_id) values ($1) on conflict do nothing",
                tenant_id,
            )
            wallet = await conn.fetchrow(
                "select balance_microrub from credit_wallets where tenant_id = $1 for update",
                tenant_id,
            )

            plan = await get_tenant_plan(conn, tenant_id)
            # T-1A-2: per-resource прайс + курс из БД (решение #1). resource определяет ставку.
            resource = meta.get("resource") or meta.get("kind") or "other"
            token_rate = None  # снимок курса в usage_ledger — заполняется только для LLM
            if plan["billing_mode"] == "per_message" and meta.get("kind") == "message":
                charged = int(plan["per_message_microrub"])
                multiplier = plan["markup_multiplier"]
            elif resource == "llm":
                # LLM тарифицируется по КУРСУ продажи токена из БД (billing_token_rate,
                # версионируемый); наценка вшита в курс → множитель-снимок = 1.00.
                token_rate = await _current_token_rate(conn)
                multiplier = await _resource_markup(conn, "llm", default=Decimal("1.00"))
                tokens_total = int((meta.get("units") or {}).get("tokens_total") or 0)
                charged = ceil_mul(tokens_total, Decimal(token_rate) / 1000)
            else:
                # Не-LLM (dadata/voice/embedding/…): себестоимость × наценка РЕСУРСА
                # (per-resource, не единый множитель плана). Неизвестный ресурс → множитель плана.
                multiplier = await _resource_markup(conn, resource, default=plan["markup_multiplier"])
                charged = ceil_mul(cost_microrub, multiplier)

            balance = int(wallet["balance_microrub"])
            if not allow_negative and balance < charged:
                raise InsufficientCreditsError(tenant_id, balance, charged)

            balance_after = balance - charged
            await conn.execute(
                "update credit_wallets set balance_microrub = $2, updated_at = now() "
                "where tenant_id = $1",
                tenant_id, balance_after,
            )
            return await conn.fetchrow(
                """
                insert into usage_ledger
                    (tenant_id, kind, provider, model, units, cost_microrub,
                     multiplier, charged_microrub, balance_after_microrub,
                     request_id, idempotence_key, token_rate_microrub_per_1k)
                values ($1, $2, $3, $4, $5::jsonb, $6, $7, $8, $9, $10, $11, $12)
                returning *
                """,
                tenant_id, meta.get("kind", "other"), meta.get("provider"),
                meta.get("model"), json.dumps(meta.get("units") or {}),
                cost_microrub, multiplier, charged, balance_after,
                meta.get("request_id"), idempotence_key, token_rate,
            )
    except asyncpg.UniqueViolationError:
        # Гонка двух списаний с одним ключом: проигравшая транзакция откатилась
        # целиком (кошелёк не тронут) — возвращаем строку победителя.
        return await conn.fetchrow(
            "select * from usage_ledger where idempotence_key = $1", idempotence_key
        )
