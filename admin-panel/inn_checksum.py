"""Контрольные суммы ИНН/ОГРН/ОГРНИП (алгоритмы ФНС) для B2B-гейта лендинга (T-1F-3b).

В проекте раньше была только проверка по длине (app.py:_INN_LENGTHS, leadmagnet._INN_RE) —
БЕЗ контрольной суммы. Решение владельца #5 требует валидацию с контрольными суммами
(ИНН-10/12, ОГРНИП-15). ⚠️ subject_type — техническая маршрутизация по длине; ПРАВОВОЕ
основание B2B = чек-бокс agree_entrepreneur, а не длина ИНН (ревью M-20).
"""
from __future__ import annotations

_INN10_W = (2, 4, 10, 3, 5, 9, 4, 6, 8)
_INN12_W11 = (7, 2, 4, 10, 3, 5, 9, 4, 6, 8)
_INN12_W12 = (3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8)


def _inn10_ok(d: list[int]) -> bool:
    return (sum(w * x for w, x in zip(_INN10_W, d[:9])) % 11) % 10 == d[9]


def _inn12_ok(d: list[int]) -> bool:
    c11 = (sum(w * x for w, x in zip(_INN12_W11, d[:10])) % 11) % 10
    c12 = (sum(w * x for w, x in zip(_INN12_W12, d[:11])) % 11) % 10
    return c11 == d[10] and c12 == d[11]


def _ogrn_ok(digits: str, mod: int) -> bool:
    """ОГРН-13 (mod 11) / ОГРНИП-15 (mod 13): контрольная = (число из первых n-1 цифр) mod → % 10."""
    body, control = digits[:-1], int(digits[-1])
    return (int(body) % mod) % 10 == control


def classify(value: str | None) -> tuple[str, str] | None:
    """Валидирует ИНН/ОГРН/ОГРНИП по контрольной сумме. Возвращает (kind, subject_type):
      kind ∈ {inn10, inn12, ogrn13, ogrnip15}; subject_type ∈ {legal, individual}.
    None — не цифры / неверная длина / битая контрольная сумма. ⚠️ длина ≠ статус ИП
    (12-значный ИНН — у любого физлица); правовое основание B2B — чек-бокс agree_entrepreneur.
    """
    q = (value or "").strip()
    if not q.isdigit():
        return None
    n = len(q)
    d = [int(ch) for ch in q]
    if n == 10 and _inn10_ok(d):
        return ("inn10", "legal")
    if n == 12 and _inn12_ok(d):
        return ("inn12", "individual")
    if n == 13 and _ogrn_ok(q, 11):
        return ("ogrn13", "legal")
    if n == 15 and _ogrn_ok(q, 13):
        return ("ogrnip15", "individual")
    return None


def is_valid(value: str | None) -> bool:
    return classify(value) is not None
