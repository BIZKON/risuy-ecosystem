#!/usr/bin/env python3
"""Unit-смоук: матчинг по цепочке потребления (club_match.py). Чистые функции,
без БД/сети. Фаза 1 — 2 фактора скоринга (okved_seek НЕ участвует, мёртвый
фактор убран — см. финал-ревью security-audit):
- score_match: комплементарный партнёр (before/after) в ТОМ ЖЕ городе > комплементарный
  партнёр в ДРУГОМ городе >> некомплементарный партнёр; причина человекочитаема
  (по-русски, отражает факторы совпадения — цепочка/город, без ОКВЭД);
- rank_matches: сортирует кандидатов по убыванию скора, лучший первым.
Запуск:
  PYTHONPATH=. ./.venv-smoke/bin/python scripts/club_match_smoke.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "admin-panel"))

from club_match import rank_matches, score_match  # noqa: E402

FAILS: list[str] = []


def check(name, cond, detail=""):
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


ME = {
    "id": "me",
    "display_name": "ИП Соколова Анна",
    "city": "Казань",
    "okved": "62.01",
    "offering": "разработка сайтов",
    "seeking": "подрядчик по стройке",
    "chain_position": "after",
}

# Комплементарный партнёр (before), тот же город.
PARTNER_SAME_CITY = {
    "id": "same_city",
    "display_name": "ООО «Стройсервис»",
    "city": "Казань",
    "okved": "41.20",
    "offering": "строительство",
    "seeking": "разработка сайтов",
    "chain_position": "before",
}

# Тот же комплементарный партнёр, но в другом городе.
PARTNER_OTHER_CITY = {
    "id": "other_city",
    "display_name": "ООО «Стройсервис-Москва»",
    "city": "Москва",
    "okved": "41.20",
    "offering": "строительство",
    "seeking": "разработка сайтов",
    "chain_position": "before",
}

# Некомплементарный партнёр (тоже after, не смежная сфера, другой город) — нулевой скор.
PARTNER_WEAK = {
    "id": "weak",
    "display_name": "ИП Петров Пётр",
    "city": "Новосибирск",
    "okved": "47.11",
    "offering": "розница",
    "seeking": "поставщик рыбы",
    "chain_position": "after",
}

# 1. score_match: комплементарный партнёр в том же городе > комплементарный в другом городе.
score_same, reason_same = score_match(ME, PARTNER_SAME_CITY)
score_other, reason_other = score_match(ME, PARTNER_OTHER_CITY)
check(
    "тот же город даёт более высокий скор при равной комплементарности",
    score_same > score_other,
    f"same={score_same} other={score_other}",
)
check("скор в диапазоне 0..100 (same)", 0 <= score_same <= 100, str(score_same))
check("скор в диапазоне 0..100 (other)", 0 <= score_other <= 100, str(score_other))

# 2. Причина человекочитаема — непустая строка на русском, отражает факторы совпадения
# (цепочка/город — БЕЗ ОКВЭД, фактор okved_seek убран как мёртвый).
check("причина (same) — непустая строка", isinstance(reason_same, str) and len(reason_same) > 0)
check(
    "причина (same) упоминает цепочку/город",
    any(kw in reason_same.lower() for kw in ["цепоч", "город"]),
    reason_same,
)
check(
    "причина (same) НЕ упоминает ОКВЭД (мёртвый фактор убран)",
    "оквэд" not in reason_same.lower(),
    reason_same,
)
check("причина (other) — непустая строка", isinstance(reason_other, str) and len(reason_other) > 0)

# Комплементарный партнёр должен явно превосходить некомплементарного слабого (нулевой скор).
score_weak, reason_weak = score_match(ME, PARTNER_WEAK)
check(
    "комплементарный партнёр сильно превосходит некомплементарного",
    score_same > score_weak,
    f"same={score_same} weak={score_weak}",
)
check("некомплементарный партнёр без совпадений города — нулевой скор", score_weak == 0, str(score_weak))

# 3. rank_matches — сортировка по убыванию скора, лучший первым.
ranked = rank_matches(ME, [PARTNER_WEAK, PARTNER_OTHER_CITY, PARTNER_SAME_CITY])
check("rank_matches вернул все 3 кандидата", len(ranked) == 3, str(len(ranked)))
check(
    "rank_matches ставит лучшего (same_city) первым",
    ranked[0]["id"] == "same_city",
    str([r["id"] for r in ranked]),
)
check(
    "rank_matches ставит слабого (weak) последним",
    ranked[-1]["id"] == "weak",
    str([r["id"] for r in ranked]),
)
check(
    "порядок скоров по убыванию",
    ranked[0]["match_score"] >= ranked[1]["match_score"] >= ranked[2]["match_score"],
    str([r["match_score"] for r in ranked]),
)
check(
    "исходные поля кандидата сохранены (display_name)",
    ranked[0]["display_name"] == "ООО «Стройсервис»",
    ranked[0].get("display_name"),
)
check(
    "match_reason присутствует в результате rank_matches",
    all(isinstance(r.get("match_reason"), str) and r["match_reason"] for r in ranked),
)


def _summary():
    print(f"\n{'ВСЕ ОК' if not FAILS else 'ПРОВАЛЫ: ' + ', '.join(FAILS)}")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    _summary()
