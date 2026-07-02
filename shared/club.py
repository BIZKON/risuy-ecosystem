"""Клуб предпринимателей: тексты согласия (152-ФЗ ст.9 + ФЗ-38 — согласие на матчинг/
партнёрские предложения) и константы полей профиля.
⚠️ ШАБЛОН: финально выверяет юрист оператора."""

# Позиция в цепочке поставки: значение колонки club_profiles.chain_position → человекочитаемая подпись.
CLUB_CHAIN_POSITIONS: list[tuple[str, str]] = [
    ("before", "До меня в цепочке (даёт трафик)"),
    ("after", "После меня (даёт допродажу)"),
    ("both", "И до, и после"),
]

_CHAIN_POSITION_VALUES = {code for code, _ in CLUB_CHAIN_POSITIONS}


def _truthy(v) -> bool:
    return bool(str(v or "").strip())


def build_club_consent_text(kind: str, operator_name: str) -> str:
    """Текст согласия для consent_events.text_hash (кладём sha256 этого текста).
    kind: 'club_join' — вступление в клуб; 'network_join' — видимость в общей бирже
    (Уровень 2, вне Фазы 1, текст готовим заранее); 'intro' — обмен контактами при знакомстве.
    Неизвестный kind → пустая строка (вызывающий код должен сам решить, падать или нет)."""
    if kind == "club_join":
        return ("Вступая в клуб предпринимателей оператора " + operator_name + ", вы соглашаетесь на "
                "обработку данных вашего бизнеса для подбора комплементарных партнёров и получение "
                "предложений о партнёрстве в рамках клуба. Отозвать согласие можно в любой момент.")
    if kind == "network_join":
        return ("Дополнительно вы соглашаетесь сделать профиль вашего бизнеса видимым в общей бирже "
                "предпринимателей и получать предложения о партнёрстве от участников из других кабинетов.")
    if kind == "intro":
        return ("Принимая знакомство, вы соглашаетесь на обмен контактами со вторым участником для "
                "обсуждения партнёрства.")
    return ""


def validate_club_registration(d: dict) -> list[str]:
    """Проверка полей регистрации в клуб (чистая, без сети/БД). Возвращает список
    человекочитаемых ошибок (пусто = ок). Обязательные поля — название бизнеса, город,
    ОКВЭД; chain_position — необязателен, но если задан — должен быть одним из
    CLUB_CHAIN_POSITIONS."""
    errs: list[str] = []
    if not _truthy(d.get("display_name")):
        errs.append("Укажите название бизнеса.")
    if not _truthy(d.get("city")):
        errs.append("Укажите город.")
    if not _truthy(d.get("okved")):
        errs.append("Укажите ОКВЭД.")

    chain_position = d.get("chain_position")
    if chain_position is not None and chain_position not in _CHAIN_POSITION_VALUES:
        errs.append("Позиция в цепочке поставки указана некорректно.")

    return errs
