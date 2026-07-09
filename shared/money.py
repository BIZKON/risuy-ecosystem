"""Деньги платформы (ТЗ §2, DECISIONS п.1–3): ТОЛЬКО целые микро-рубли.

1 RUB = 1_000_000 µRUB (bigint в БД). Никаких float в денежной арифметике.
ЕДИНСТВЕННАЯ точка округления — ceil_mul(): всегда ВВЕРХ (в пользу платформы).
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_HALF_UP

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


def parse_price(raw) -> tuple[Decimal | None, bool]:
    """Цена (строка формы / число из JSON) → (Decimal | None, ok). None/пусто → (None, True)
    — цена опциональна. Запятая = десятичный разделитель, пробелы-разделители тысяч убираются.
    Отрицательную/нечисловую → (None, False). numeric(12,2): целая часть ≤ 10 цифр."""
    if raw is None:
        return None, True
    s = str(raw).strip()
    if not s:
        return None, True
    s = s.replace(" ", "").replace(" ", "").replace(",", ".")
    try:
        val = Decimal(s)
    except (InvalidOperation, ValueError):
        return None, False
    if val < 0:
        return None, False
    val = val.quantize(Decimal("0.01"))
    if val >= Decimal("10000000000"):
        return None, False
    return val, True
