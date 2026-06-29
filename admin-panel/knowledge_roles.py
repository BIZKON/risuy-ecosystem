"""СП-2b: нормализация role_tag (отдела) для базы знаний. Чистая логика, без БД.

role_tag в KB = '' (общая справка тенанта, видна всем его агентам) ИЛИ slug отдела
(team-агент тенанта; для School-тенанта — slug персоны PERSONA_PRESETS). Ретрив бота
(kb_search) матчит role_tag ТОЧНЫМ равенством slug, поэтому опечатка/чужой slug сделает
чанк невидимым для всех агентов — такие значения сбрасываем в '' (общая справка)."""


def normalize_role(role: str, allowed: set[str]) -> str:
    """'' (общая) либо slug из allowed; иначе → '' (не тегируем мусором)."""
    role = (role or "").strip()
    return role if (role and role in allowed) else ""
