"""Матчинг «Клуб предпринимателей» по цепочке потребления (Фаза 1, Уровень 1).

Чистые функции — БЕЗ БД/сети (тестируется unit-смоуком scripts/club_match_smoke.py).
Матчинг ТОЛЬКО среди участников ОДНОГО тенанта — вызывающий код (app.py) обязан
передавать candidates, уже отфильтрованные по active_tenant_id (Уровень 2 —
cross-tenant биржа — вне Фазы 1, здесь не реализуется).

Ожидаемые поля участника (dict, из club_members + club_profiles):
    display_name, city, okved, offering, seeking, chain_position, okved_seek.
Любое поле может отсутствовать/быть None — считается «неизвестно», не участвует в скоре.
"""

# Веса скоринга (сумма компонентов ограничена до 100 в score_match).
_CHAIN_COMPLEMENT_SCORE = 45   # комплементарная позиция в цепочке (before<->after, both<->*)
_OKVED_CROSS_SCORE = 20        # за каждое направление пересечения okved_seek <-> okved (макс 2 = 40)
_CITY_BONUS_SCORE = 15         # тот же город


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


def _okved_cross_matches(me: dict, other: dict) -> tuple[bool, bool]:
    """Возвращает (я_ищу_его_сферу, он_ищет_мою_сферу) — подстрочное совпадение
    (okved_seek — свободный текст, не обязан быть точным кодом ОКВЭД)."""
    me_seek = _norm(me.get("okved_seek"))
    other_seek = _norm(other.get("okved_seek"))
    me_okved = _norm(me.get("okved"))
    other_okved = _norm(other.get("okved"))

    i_seek_them = bool(me_seek and other_okved) and (me_seek in other_okved or other_okved in me_seek)
    they_seek_me = bool(other_seek and me_okved) and (other_seek in me_okved or me_okved in other_seek)
    return i_seek_them, they_seek_me


def score_match(me: dict, other: dict) -> tuple[int, str]:
    """Скор 0..100 совместимости `other` как партнёра для `me` + человекочитаемая причина
    на русском. Факторы: комплементарность цепочки потребления, пересечение ОКВЭД/окведа-
    поиска (в любую сторону), один город."""
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

    i_seek_them, they_seek_me = _okved_cross_matches(me, other)
    if i_seek_them and they_seek_me:
        score += _OKVED_CROSS_SCORE * 2
        reasons.append("встречный комплементарный ОКВЭД (вы ищете его сферу, он — вашу)")
    elif i_seek_them:
        score += _OKVED_CROSS_SCORE
        reasons.append("вы ищете именно его сферу (ОКВЭД)")
    elif they_seek_me:
        score += _OKVED_CROSS_SCORE
        reasons.append("он ищет именно вашу сферу (ОКВЭД)")

    same_city = bool(_norm(me.get("city"))) and _norm(me.get("city")) == _norm(other.get("city"))
    if same_city:
        score += _CITY_BONUS_SCORE
        reasons.append("тот же город")

    score = max(0, min(100, score))

    if reasons:
        reason = ", ".join(reasons)
        reason = reason[0].upper() + reason[1:]
    else:
        reason = "Совпадений по цепочке, ОКВЭД и городу не найдено"

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
