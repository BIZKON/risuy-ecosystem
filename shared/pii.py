"""In-process PII-маскировка перед ВНЕШНИМ ИИ (152-ФЗ): mask → LLM → unmask.

Зачем: запросы к Timeweb AI (Gateway / cloud-ai) по первоисточникам НЕ гарантируют, что инференс и
хранение остаются в РФ (док-проверка: место инференса не указано, есть чекбокс согласия на трансгран,
«единый прокси»). Чтобы сырые ПДн лида не уходили во внешний ИИ-контур, маскируем СТРУКТУРНЫЕ ПДн
ДЕТЕРМИНИРОВАННО перед отправкой и восстанавливаем в ответе.

Что маскируем (низкий false-positive, без NER): РФ-телефон, e-mail, ИНН (по контексту «ИНН»),
СНИЛС (по контексту «СНИЛС»), паспорт (по контексту «паспорт» в любом падеже, серия+номер).
Структурные ПДн без ключевого слова НЕ трогаем (иначе портим номера заказов/треков). Свободные ФИО/адреса
и паспорт БЕЗ ключевого слова regex НЕ ловит — это зона NER-сервиса (см. скилл masker-pii-redaction,
отдельный инкремент, требует self-hosted Masker). Поэтому Политику формулировать строго:
«идентифицирующие контакты (телефон/e-mail/ИНН/СНИЛС/паспорт) обезличиваются до передачи во внешний ИИ».

Свойства:
- Консистентные плейсхолдеры в рамках ОДНОГО вызова: один и тот же телефон → один [PHONE_1] в system/
  истории/текущем сообщении (модель сохраняет контекст).
- Маппинг (placeholder→оригинал) живёт ТОЛЬКО в памяти на время вызова и НЕ логируется.
- fail-closed: при сбое маскировки вызывающий обязан НЕ отправлять сырой текст (см. ai.py).
"""
import re

# РФ-телефон: +7/7/8 + ещё 10 цифр, между цифрами одиночные разделители. Lookbehind отрицает ТОЛЬКО
# цифру слева (чтобы не схватить хвост более длинного числа), но НЕ букву — лид часто пишет номер слитно
# со словом ('телефон89111234567'); блокировка по \w давала утечку сырого номера (ревью).
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?7|8)[\s\-().]{0,3}\d(?:[\s\-().]{0,3}\d){9}(?!\d)")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
# ИНН маскируем ТОЛЬКО по контексту (после слова «ИНН»): голая контрольная сумма даёт ~10% ложных
# срабатываний на номерах заказов/счетов (ревью) → портила бы данные, уходящие в ИИ. Цифры ИНН — group(1).
_INN_RE = re.compile(r"ИНН[\s:№/.\-]*(\d{12}|\d{10})(?!\d)", re.IGNORECASE)
# СНИЛС (формат XXX-XXX-XXX YY) — ТОЛЬКО по контексту (после «СНИЛС»), как ИНН/паспорт: сам по
# себе формат 3-3-3-2 встречается и у номеров договоров/треков/заказов → без ключевого слова
# не трогаем (иначе портим данные для ИИ ложным срабатыванием). Цифры СНИЛС — group(1).
_SNILS_RE = re.compile(r"СНИЛС[а-яё]*[\s:№/.\-]*(\d{3}-\d{3}-\d{3}[ \-]\d{2})(?!\d)", re.IGNORECASE)
# Паспорт РФ (серия+номер, 4+6 цифр) — ТОЛЬКО по контексту слова «паспорт» в ЛЮБОМ падеже
# («паспорт», «паспорта», «по паспорту», «в паспорте», «пасп.»), как ИНН: голый 10-значный прогон =
# номер заказа/счёта → без ключевого слова не трогаем (иначе портим данные для ИИ). Свободный
# паспорт без слова — зона NER-сервиса (Masker). пасп[а-яё]* покрывает все падежи. Цифры — group(1).
_PASSPORT_RE = re.compile(r"пасп[а-яё]*[\s:№/.\-]*(\d{4}\s?\d{6})(?!\d)", re.IGNORECASE)
# Осиротевшие плейсхолдеры (чужой/прошлый серверный контекст нативного /call, галлюцинация модели) —
# срезаем при unmask, чтобы клиент НИКОГДА не увидел служебный токен.
_ORPHAN_RE = re.compile(r"\[(?:PHONE|EMAIL|INN|SNILS|PASSPORT)_\d+\]")


def _phone_key(raw: str) -> str:
    """Ключ консистентности телефона: только цифры, лидирующая 8 → 7 (8XXX… и +7XXX… — один номер)."""
    d = "".join(ch for ch in raw if ch.isdigit())
    if len(d) == 11 and d[0] == "8":
        d = "7" + d[1:]
    return "p:" + d


class Mapping:
    """Маппинг плейсхолдер↔оригинал для ОДНОГО вызова LLM. Только в памяти, не логируется."""

    def __init__(self) -> None:
        self._fwd: dict[str, str] = {}   # ключ-консистентности → плейсхолдер
        self.back: dict[str, str] = {}   # плейсхолдер → что подставить обратно
        self._counts: dict[str, int] = {}

    def placeholder(self, kind: str, key: str, restore: str) -> str:
        """Вернуть стабильный плейсхолдер для значения. key — ключ консистентности (один key → один
        плейсхолдер за вызов); restore — что подставится при unmask (первое встреченное написание)."""
        if key in self._fwd:
            return self._fwd[key]
        n = self._counts.get(kind, 0) + 1
        self._counts[kind] = n
        ph = f"[{kind}_{n}]"
        self._fwd[key] = ph
        self.back[ph] = restore
        return ph

    def empty(self) -> bool:
        return not self.back


def mask_text(text: str, mapping: Mapping) -> str:
    """Маскировать СТРУКТУРНЫЕ ПДн в тексте, наполняя mapping. Порядок: email → телефон → ИНН
    (каждый следующий проход видит уже замаскированный текст → цифры email/телефона не путаются с ИНН)."""
    if not text:
        return text or ""
    s = str(text)
    s = _EMAIL_RE.sub(lambda m: mapping.placeholder("EMAIL", "e:" + m.group(0).lower(), m.group(0)), s)
    s = _PHONE_RE.sub(lambda m: mapping.placeholder("PHONE", _phone_key(m.group(0)), m.group(0)), s)

    def _inn_repl(m: "re.Match[str]") -> str:
        digits = m.group(1)  # маскируем только цифры ИНН, слово «ИНН» оставляем как контекст для модели
        ph = mapping.placeholder("INN", "i:" + digits, digits)
        return m.group(0).replace(digits, ph, 1)

    s = _INN_RE.sub(_inn_repl, s)

    def _snils_repl(m: "re.Match[str]") -> str:
        digits = m.group(1)  # маскируем только цифры СНИЛС, слово «СНИЛС» — контекст для модели
        ph = mapping.placeholder("SNILS", "s:" + re.sub(r"\D", "", digits), digits)
        return m.group(0).replace(digits, ph, 1)

    s = _SNILS_RE.sub(_snils_repl, s)

    def _passport_repl(m: "re.Match[str]") -> str:
        digits = m.group(1)  # маскируем только серию+номер, слово «паспорт» — контекст для модели
        ph = mapping.placeholder("PASSPORT", "pp:" + re.sub(r"\s", "", digits), digits)
        return m.group(0).replace(digits, ph, 1)

    s = _PASSPORT_RE.sub(_passport_repl, s)
    return s


def unmask_text(text: str, mapping: Mapping) -> str:
    """Восстановить оригиналы из плейсхолдеров (ответ LLM мог их вернуть), затем СРЕЗАТЬ осиротевшие
    плейсхолдеры (не из текущего mapping — прошлый серверный контекст /call или галлюцинация модели),
    чтобы клиент никогда не увидел служебный токен. Срез делаем всегда, даже при пустом mapping."""
    if not text:
        return text or ""
    s = str(text)
    for ph, original in mapping.back.items():
        if ph in s:
            s = s.replace(ph, original)
    return _ORPHAN_RE.sub("", s)


def mask_messages(messages: list[dict], mapping: Mapping) -> list[dict]:
    """Маскировать поле content во ВСЕХ сообщениях одним mapping (консистентность в рамках вызова).
    Возвращает НОВЫЙ список (исходные dict не мутируем)."""
    out: list[dict] = []
    for m in messages or []:
        nm = dict(m)
        if isinstance(nm.get("content"), str):
            nm["content"] = mask_text(nm["content"], mapping)
        out.append(nm)
    return out


def redact_text(text: str) -> tuple[str, Mapping]:
    """Удобный враппер: (масированный_текст, mapping) для одиночного текста (нативный /call)."""
    mp = Mapping()
    return mask_text(text, mp), mp


def redact_messages(messages: list[dict]) -> tuple[list[dict], Mapping]:
    """Удобный враппер: (масированные_messages, mapping) для OpenAI-формата."""
    mp = Mapping()
    return mask_messages(messages, mp), mp
