"""Деньги партнёрской программы (спека 2026-08-16).

🔴 Вынесено в чистые функции намеренно: это чужие деньги. Ошибка здесь не падает с
трассировкой, а тихо занижает или завышает выплату — и обнаруживается, когда партнёр
пересчитает вручную и придёт спорить. Всё, что можно проверить без базы, проверяется
без базы: scripts/partner_money_calc_smoke.py.
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

import config

CENT = Decimal("0.01")


@dataclass(frozen=True)
class PartnerNode:
    """Партнёр в дереве. rate_percent — его текущая ставка; в начисление она попадает
    КОПИЕЙ, чтобы правка настроек не переписала прошлое."""

    id: str
    parent_id: str | None
    joined_at: datetime
    rate_percent: Decimal


@dataclass(frozen=True)
class Accrual:
    """Одна строка начисления. level: 0 — продавец, 1 — наставник."""

    partner_id: str
    level: int
    rate_percent: Decimal
    amount_rub: Decimal
    reason: str


def round_rub(value: Decimal) -> Decimal:
    """Рубли с копейками: доли процентов не должны копиться в невидимых хвостах."""
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def _add_months(moment: datetime, months: int) -> datetime:
    """Прибавить месяцы, не съезжая на конец месяца (31 января + 1 месяц = 28/29 февраля)."""
    year, month = divmod(moment.month - 1 + months, 12)
    year += moment.year
    month += 1
    day = min(moment.day, calendar.monthrange(year, month)[1])
    return moment.replace(year=year, month=month, day=day)


def mentor_still_earns(seller_joined_at: datetime, at: datetime) -> bool:
    """Идут ли ещё наставнические с продаж этого партнёра.

    🔴 Срок считается от регистрации ПРИВЕДЁННОГО партнёра, а не от даты продажи: год
    даётся на то, чтобы наставник окупил своё участие, а не на каждую сделку заново.
    День в день срок ещё действует — граница трактуется в пользу партнёра.
    """
    return at <= _add_months(seller_joined_at, config.MENTOR_BONUS_MONTHS)


def accruals_for_payment(
    *,
    amount_rub: Decimal,
    seller: PartnerNode,
    mentor: PartnerNode | None,
    at: datetime,
    mentors_enabled: bool = True,
) -> list[Accrual]:
    """Что начисляется с ОДНОГО платежа клиента.

    Продавцу — доля по ЕГО ставке. Наставнику — сверх, из нашей маржи, и только пока не
    истёк срок. Возвращает список строк, а не сумму: у платежа несколько получателей, и
    каждому нужна своя запись с зафиксированной ставкой.
    """
    amount = Decimal(amount_rub)
    rows = [
        Accrual(
            partner_id=seller.id,
            level=0,
            rate_percent=seller.rate_percent,
            amount_rub=round_rub(amount * seller.rate_percent / 100),
            reason="sale",
        )
    ]
    if not mentors_enabled or mentor is None:
        return rows
    # Наставник — ровно один уровень вверх. Наставник наставника не получает ничего:
    # дерево без дна мы строить не договаривались.
    if seller.parent_id != mentor.id:
        return rows
    if not mentor_still_earns(seller.joined_at, at):
        return rows
    rate = Decimal(config.MENTOR_RATE_PERCENT)
    rows.append(
        Accrual(
            partner_id=mentor.id,
            level=1,
            rate_percent=rate,
            amount_rub=round_rub(amount * rate / 100),
            reason="mentor",
        )
    )
    return rows


def partner_balance(accrued_rows, payout_rows) -> tuple[Decimal, Decimal, Decimal]:
    """Начислено, выплачено, к выплате.

    Колонки «баланс» в базе нет: сохранённое вычисляемое значение расходится с фактом при
    первой же правке задним числом. Сторно приходит ОТРИЦАТЕЛЬНОЙ строкой начисления —
    поэтому суммируем как есть, без фильтров по знаку.
    """
    accrued = round_rub(sum((Decimal(v) for v in accrued_rows), Decimal(0)))
    paid = round_rub(sum((Decimal(v) for v in payout_rows), Decimal(0)))
    return accrued, paid, round_rub(accrued - paid)


def would_create_cycle(partner_id: str, parent_id: str | None, parent_of: dict) -> bool:
    """A привёл B — B не может стать наставником A.

    Без этой проверки два партнёра при глубине 1 начисляли бы друг другу с каждой продажи,
    а дерево перестало бы иметь корень.
    """
    if parent_id is None:
        return False
    seen: set[str] = set()
    cursor: str | None = parent_id
    while cursor is not None and cursor not in seen:
        if cursor == partner_id:
            return True
        seen.add(cursor)
        cursor = parent_of.get(cursor)
    return False
