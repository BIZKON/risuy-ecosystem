"""Клуб-аналитика: чистые функции над членами клуба — тип субъекта, нормализация
города, сводка для дашборда, CSV бизнес-полей, фильтрация. Без БД/сети (юнит-тест).
Стиль зеркалит shared/anon.py. Контакты членов intro-gated → в CSV/сводку НЕ попадают."""
from __future__ import annotations

from statistics import median
from typing import Any, Iterable

# ── Тип субъекта: ИП / ЮЛ / Гос / не указан ──────────────────────────────────
# Короткие ОПФ гос/муниципальных форм (prospects.opf = opf.short из DaData).
GOV_OPF_SHORT: frozenset[str] = frozenset({
    "ГУП", "МУП", "ФГУП", "ГКУ", "МКУ", "ФКУ", "ГБУ", "МБУ", "ФГБУ", "ФГКУ",
    "ГАУ", "МАУ", "ФГАУ", "ГКОУ", "МКОУ", "ГБОУ", "МБОУ", "ФКП",
})
# Подстроки для полного ОПФ/наименования (регистронезависимо).
GOV_OPF_SUBSTR: tuple[str, ...] = (
    "государственн", "муниципальн", "казённое", "казенное",
    "бюджетн", "автономн", "администрация", "департамент", "министерство",
)


def _digits(s: str | None) -> str:
    return "".join(ch for ch in (s or "") if ch.isdigit())


def entity_type(inn: str | None, opf: str | None) -> str:
    """ИП (ИНН 12 цифр), ЮЛ (ИНН 10), Гос (ЮЛ с гос/муниц. ОПФ), 'не указан' иначе."""
    d = _digits(inn)
    if len(d) == 12:
        return "ИП"
    if len(d) == 10:
        o = (opf or "").strip()
        if o and (o.upper() in GOV_OPF_SHORT or any(s in o.lower() for s in GOV_OPF_SUBSTR)):
            return "Гос"
        return "ЮЛ"
    return "не указан"


# ── Нормализация города ──────────────────────────────────────────────────────
_CITY_PREFIXES = ("г.", "гор.", "город ", "г ")
CITY_ALIASES: dict[str, str] = {
    "москва": "Москва", "мск": "Москва",
    "санкт-петербург": "Санкт-Петербург", "санкт петербург": "Санкт-Петербург",
    "спб": "Санкт-Петербург", "с-петербург": "Санкт-Петербург", "питер": "Санкт-Петербург",
    "нижний новгород": "Нижний Новгород", "нижний-новгород": "Нижний Новгород", "нн": "Нижний Новгород",
    "екатеринбург": "Екатеринбург", "екб": "Екатеринбург", "ект": "Екатеринбург",
}


def normalize_city(raw: str | None) -> str:
    s = (raw or "").strip()
    if not s:
        return "Не указан"
    low = " ".join(s.lower().split())
    for p in _CITY_PREFIXES:
        if low.startswith(p):
            low = low[len(p):].strip()
            break
    if low in CITY_ALIASES:
        return CITY_ALIASES[low]
    return " ".join(w[:1].upper() + w[1:] for w in low.split())


# ── Фильтрация (city/тип — в Python; status/okved — в SQL на стороне db.py) ───
def _opf_of(m: dict[str, Any]) -> str | None:
    return (m.get("prospect") or {}).get("opf") or m.get("opf")


def filter_members(rows: list[dict[str, Any]], *, city: str = "", etype: str = "") -> list[dict[str, Any]]:
    out = rows
    if city:
        target = normalize_city(city)
        out = [m for m in out if normalize_city(m.get("city")) == target]
    if etype:
        out = [m for m in out if entity_type(m.get("inn"), _opf_of(m)) == etype]
    return out


# ── Сводка для дашборда ──────────────────────────────────────────────────────
def _top(d: dict[str, int], n: int = 10) -> list[tuple[str, int]]:
    items = sorted(d.items(), key=lambda kv: (-kv[1], kv[0]))
    if len(items) <= n:
        return items
    rest = sum(v for _, v in items[n:])
    return items[:n] + [("прочие", rest)]


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_status: dict[str, int] = {}
    by_city: dict[str, int] = {}
    by_okved: dict[str, int] = {}
    by_type = {"ИП": 0, "ЮЛ": 0, "Гос": 0, "не указан": 0}
    chain = {"before": 0, "after": 0, "both": 0, "нет профиля": 0}
    checks: list[int] = []
    with_egrul = 0
    with_profile = 0
    for r in rows:
        st = (r.get("status") or "").strip() or "active"
        by_status[st] = by_status.get(st, 0) + 1
        city = normalize_city(r.get("city"))
        by_city[city] = by_city.get(city, 0) + 1
        okved = (r.get("okved") or "").strip() or "Не указан"
        by_okved[okved] = by_okved.get(okved, 0) + 1
        by_type[entity_type(r.get("inn"), _opf_of(r))] += 1
        cp = (r.get("chain_position") or "").strip()
        chain[cp if cp in ("before", "after", "both") else "нет профиля"] += 1
        ac = r.get("avg_check")
        if isinstance(ac, int) and ac > 0:
            checks.append(ac)
        if r.get("prospect"):
            with_egrul += 1
        if r.get("offering") or r.get("seeking") or r.get("chain_position"):
            with_profile += 1
    return {
        "kpi": {
            "total": len(rows),
            "active": by_status.get("active", 0),
            "paused": by_status.get("paused", 0),
            "left": by_status.get("left", 0),
            "with_egrul": with_egrul,
            "with_profile": with_profile,
            "cities": len([c for c in by_city if c != "Не указан"]),
        },
        "by_city": _top(by_city),
        "by_okved": _top(by_okved),
        "by_type": by_type,
        "chain": chain,
        "avg_check": {
            "count": len(checks),
            "min": min(checks) if checks else 0,
            "median": int(median(checks)) if checks else 0,
            "max": max(checks) if checks else 0,
        },
    }


# ── CSV бизнес-полей (только data-строки; заголовок пишет роут из CSV_HEADERS) ─
CSV_HEADERS: list[str] = [
    "Название", "Город", "Тип", "ИНН", "ОКВЭД", "ОКВЭД (название)",
    "Наименование ЕГРЮЛ", "Что предлагает", "Средний чек",
    "Что ищет", "Позиция в цепочке", "Статус", "Дата регистрации",
]
_CHAIN_RU = {"before": "до вас", "after": "после вас", "both": "оба направления"}
_STATUS_RU = {"active": "активен", "paused": "на паузе", "left": "вышел"}


def csv_business_rows(rows: list[dict[str, Any]]) -> Iterable[list[str]]:
    """Только бизнес-поля (13 колонок = CSV_HEADERS). Контакты (tg/vk/max, лид),
    ФИО руководителя, адрес ИП — НЕ включаются (intro-gated / ПДн). Значения сырые;
    formula-guard применяет роут через _csv_line (anon.csv_safe)."""
    for r in rows:
        prospect = r.get("prospect") or {}
        created = r.get("created_at")
        created_s = created.date().isoformat() if hasattr(created, "date") else (str(created) if created else "")
        ac = r.get("avg_check")
        yield [
            r.get("display_name") or "",
            normalize_city(r.get("city")),
            entity_type(r.get("inn"), _opf_of(r)),
            r.get("inn") or "",
            r.get("okved") or "",
            prospect.get("okved_name") or r.get("okved_name") or "",
            prospect.get("name_short") or r.get("name_short") or "",
            r.get("offering") or "",
            str(ac) if isinstance(ac, int) and ac > 0 else "",
            r.get("seeking") or "",
            _CHAIN_RU.get((r.get("chain_position") or "").strip(), ""),
            _STATUS_RU.get((r.get("status") or "").strip(), r.get("status") or ""),
            created_s,
        ]
