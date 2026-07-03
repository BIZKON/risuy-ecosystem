"""Матчинг «Клуб предпринимателей» по цепочке потребления (Фаза 1, Уровень 1).

Чистые функции — БЕЗ БД/сети (тестируется unit-смоуком scripts/club_match_smoke.py).
Матчинг ТОЛЬКО среди участников ОДНОГО тенанта — вызывающий код (app.py) обязан
передавать candidates, уже отфильтрованные по active_tenant_id (Уровень 2 —
cross-tenant биржа — вне Фазы 1, здесь не реализуется).

Ожидаемые поля участника (dict, из club_members + club_profiles):
    display_name, city, okved, offering, seeking, chain_position.
Любое поле может отсутствовать/быть None — считается «неизвестно», не участвует в скоре.

Скоринг — 2 фактора (Фаза 1): комплементарность позиции в цепочке потребления
(chain_position) + один город. okved_seek НЕ участвует в скоринге: поле не
заполняется воронкой (FSM пишет okved_seek="" при регистрации, панель
club_profile_upsert его не собирает) — фактор был мёртвым и обманывал скор
(начислял баллы за пустое совпадение). См. финал-ревью security-audit.
Заполнение okved_seek оператором/повторное включение фактора — Фаза 2.
"""

# Веса скоринга (сумма компонентов ограничена до 100 в score_match).
_CHAIN_COMPLEMENT_SCORE = 75   # комплементарная позиция в цепочке (before<->after, both<->*)
_CITY_BONUS_SCORE = 25         # тот же город


def _norm(value) -> str:
    """Нормализует строковое поле для сравнения (пусто/None -> "")."""
    return (value or "").strip().lower()


def _chain_complementary(me_pos: str, other_pos: str) -> bool:
    """before<->after комплементарны в обе стороны; 'both' совместим с любой известной
    позицией партнёра (открыт для обеих ролей цепочки)."""
    me_pos = _norm(me_pos)
    other_pos = _norm(other_pos)
    if not me_pos or not other_pos:
        return False
    if me_pos == "both" or other_pos == "both":
        return True
    return {me_pos, other_pos} == {"before", "after"}


def score_match(me: dict, other: dict) -> tuple[int, str]:
    """Скор 0..100 совместимости `other` как партнёра для `me` + человекочитаемая причина
    на русском. Факторы (Фаза 1, только 2): комплементарность цепочки потребления,
    один город. okved_seek намеренно не участвует — см. docstring модуля."""
    reasons: list[str] = []
    score = 0

    me_pos = _norm(me.get("chain_position"))
    other_pos = _norm(other.get("chain_position"))
    if _chain_complementary(me_pos, other_pos):
        score += _CHAIN_COMPLEMENT_SCORE
        if other_pos == "before":
            reasons.append("он до вас в цепочке потребления")
        elif other_pos == "after":
            reasons.append("он после вас в цепочке потребления")
        else:
            reasons.append("комплементарная позиция в цепочке потребления")

    same_city = bool(_norm(me.get("city"))) and _norm(me.get("city")) == _norm(other.get("city"))
    if same_city:
        score += _CITY_BONUS_SCORE
        reasons.append("тот же город")

    score = max(0, min(100, score))

    if reasons:
        reason = ", ".join(reasons)
        reason = reason[0].upper() + reason[1:]
    else:
        reason = "Совпадений по цепочке потребления и городу не найдено"

    return score, reason


def rank_matches(me: dict, candidates: list[dict]) -> list[dict]:
    """Ранжирует candidates по убыванию скора совместимости с me (лучший первым).
    Каждый элемент результата — копия исходных полей кандидата + match_score/match_reason.
    Не мутирует переданные candidates."""
    ranked: list[dict] = []
    for other in candidates:
        candidate_score, reason = score_match(me, other)
        enriched = dict(other)
        enriched["match_score"] = candidate_score
        enriched["match_reason"] = reason
        ranked.append(enriched)
    ranked.sort(key=lambda c: c["match_score"], reverse=True)
    return ranked
