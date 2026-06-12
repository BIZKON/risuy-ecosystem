"""Деньги платформы (ТЗ §2, DECISIONS п.1–3): ТОЛЬКО целые микро-рубли.

1 RUB = 1_000_000 µRUB (bigint в БД). Никаких float в денежной арифметике.
ЕДИНСТВЕННАЯ точка округления — ceil_mul(): всегда ВВЕРХ (в пользу платформы).
"""
from __future__ import annotations

from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP

MICRO = 1_000_000


def rub_to_micro(value: str | int | Decimal) -> int:
    """'350' / '350.50' ₽ → µRUB (int). Бросает на мусоре — валидируй до вызова."""
    d = Decimal(str(value).replace(",", ".").strip())
    return int((d * MICRO).to_integral_value(rounding=ROUND_HALF_UP))


def micro_to_rub_str(micro: int) -> str:
    """µRUB → строка '1 234,56' (для UI; копейки скрываются, если их нет)."""
    d = (Decimal(micro) / MICRO).quantize(Decimal("0.01"))
    whole, cents = divmod(int(d * 100), 100)
    s = f"{whole:,}".replace(",", " ")
    return s if cents == 0 else f"{s},{cents:02d}"


def micro_to_amount_str(micro: int) -> str:
    """µRUB → строка ЮKassa '1234.56' (ровно 2 знака, точка)."""
    return str((Decimal(micro) / MICRO).quantize(Decimal("0.01")))


def ceil_mul(cost_micro: int, multiplier) -> int:
    """charged = ceil(cost × multiplier) в µRUB. Округление ВСЕГДА вверх."""
    return int((Decimal(cost_micro) * Decimal(str(multiplier)))
               .to_integral_value(rounding=ROUND_CEILING))
