# Клуб-UX над членами — фильтры + CSV + дашборд — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended)
> or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Дать оператору тенанта фильтры каталога клуба, CSV-выгрузку бизнес-полей и дашборд-аналитику
над opt-in членами — без нового прод-DDL и без утечки intro-gated контактов.

**Architecture:** Чистый модуль `shared/club_analytics.py` (тип субъекта, нормализация города, сводка,
CSV-строки, фильтрация) — юнит-тест без БД. Панель `admin-panel/db.py` даёт tenant-scoped выборку с
ЕГРЮЛ-обогащением (`club_member_list_enriched`) + SQL-агрегаты роста/воронки. Три поверхности в
`admin-panel/app.py`: `/club` (фильтры+KPI), `/club/dashboard` (полный дашборд), `POST /club/export.csv`
(CSRF+аудит, потоковый CSV). Фильтры `status`/`okved` — в SQL, `city`/`type` — в Python (чистые функции).

**Tech Stack:** Python 3 (asyncpg, FastAPI, Jinja2), смоук-скрипты (`.venv-smoke`), Postgres (Neon/Timeweb).

**Спека:** `docs/superpowers/specs/2026-07-03-club-ux-dashboard-design.md`.

## Global Constraints

- 🇷🇺 **Только русский** в комментариях/докстрингах/UI/коммитах (латиница — только идентификаторы/SQL/ключи).
- **Ветка** `docs/security-audit` (= main). Коммиты **явными файлами** (НЕ `CLAUDE.md`/`.claude/`/`.gitignore`/
  `graphify-out/`/`.superpowers/`). Коммиттеры-субагенты — строго **ПОСЛЕДОВАТЕЛЬНО** (гонка git-индекса).
- **Прод-DDL НЕ требуется** — все колонки уже есть (`club_members.city/okved/inn/status`,
  `prospects.opf/subject_type/name_short/okved_name`, `club_intros.status/from_accepted_at/to_accepted_at`).
- **in-query backstop:** во ВСЕХ новых `club_*`-запросах — явный `tenant_id` в каждом `where`/`join`
  (owner-DSN обходит RLS). Паттерн `club_member_list_with_profile`.
- **db-смоуки гонит КОНТРОЛЛЕР** inline с `TEAM_DSN` (risuy_dev): owner-DSN **НЕ передаётся субагентам**.
  Субагент пишет db-смоук, но **не запускает** его (нет DSN) — помечает «ожидает контроллера».
- **CSV/дашборд БЕЗ контактов и ПДн:** никогда не включать `tg/vk/max_user_id`, контакты лида
  (intro-gated), `prospects.management` (ФИО руководителя), адрес ИП. Formula-guard — через `anon.csv_safe`.
- **Экспорт = POST + CSRF + аудит** (`db.audit`), как `export_masked`/`export_full` (уточнение к спеке §7,
  где был GET — приводим к домовому паттерну CSV-экспорта: CSRF-защита + фиксация факта выгрузки).
- **graphify query до grep** при исследовании (для субагентов-имплементеров — включать в промпт).
- **Раннеры смоуков:** unit/ui — `PYTHONPATH=. ./.venv-smoke/bin/python scripts/<name>.py`;
  db — `TEAM_DSN=... PYTHONPATH=admin-panel:. ./.venv-smoke/bin/python scripts/<name>.py`.
- **Деплой** = `git push origin docs/security-audit:main` → авто-редеплой панели 205025 (бот НЕ трогаем).
  Только по явному «да» владельца (auth-классификатор). HTTP-live из среды агента недоступен → сверка
  `twc apps get 205025 -o json | grep commit_sha` + `status=active`; владелец глазами `cabinet.pro-agent-ai.ru`.

---

### Task 1: Чистый модуль аналитики `shared/club_analytics.py`

**Files:**
- Create: `shared/club_analytics.py`
- Test: `scripts/club_analytics_smoke.py`

**Interfaces:**
- Consumes: `shared.anon.csv_safe` (formula-guard, уже есть).
- Produces (для Task 3/4/5):
  - `entity_type(inn: str|None, opf: str|None) -> str` ∈ {`'ИП'`,`'ЮЛ'`,`'Гос'`,`'не указан'`}
  - `normalize_city(raw: str|None) -> str` (пусто → `'Не указан'`)
  - `summarize(rows: list[dict]) -> dict` (ключи: `kpi`, `by_city`, `by_okved`, `by_type`, `chain`, `avg_check`)
  - `csv_business_rows(rows: list[dict]) -> Iterable[list[str]]` (ТОЛЬКО data-строки, без заголовка)
  - `CSV_HEADERS: list[str]` (13 бизнес-колонок, без контактов)
  - `filter_members(rows: list[dict], *, city='', etype='') -> list[dict]`

- [ ] **Step 1: Написать смоук (падает — модуля нет)**

Create `scripts/club_analytics_smoke.py`:

```python
#!/usr/bin/env python3
"""Юнит-смоук чистого модуля club_analytics (без БД/сети):
entity_type (ИП/ЮЛ/Гос/не указан), normalize_city (алиасы/префиксы), summarize
(KPI/распределения/чек None-safe), csv_business_rows (колонки + ОТСУТСТВИЕ контактов),
filter_members (город/тип). Formula-guard проверяем через anon.csv_safe.
  PYTHONPATH=. ./.venv-smoke/bin/python scripts/club_analytics_smoke.py
"""
import sys

from shared import club_analytics as ca
from shared.anon import csv_safe

FAILS = []


def check(name, cond, detail=""):
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


# ── entity_type ───────────────────────────────────────────────────────────────
check("ИНН 12 → ИП", ca.entity_type("165000000000", None) == "ИП")
check("ИНН 10 + ООО → ЮЛ", ca.entity_type("7700000000", "ООО") == "ЮЛ")
check("ИНН 10 без ОПФ → ЮЛ", ca.entity_type("7700000000", None) == "ЮЛ")
check("ИНН 10 + ГБУ → Гос", ca.entity_type("7700000000", "ГБУ") == "Гос")
check("ИНН 10 + 'муниципальное учреждение' → Гос",
      ca.entity_type("7700000000", "муниципальное автономное учреждение") == "Гос")
check("пустой ИНН → не указан", ca.entity_type("", "ООО") == "не указан")
check("мусорный ИНН → не указан", ca.entity_type("123", None) == "не указан")
check("ИНН с дефисами 10 цифр → ЮЛ", ca.entity_type("77-000-000-00", "АО") == "ЮЛ")

# ── normalize_city ────────────────────────────────────────────────────────────
check("мск → Москва", ca.normalize_city("мск") == "Москва")
check("'г. Москва' → Москва", ca.normalize_city("г. Москва") == "Москва")
check("СПб → Санкт-Петербург", ca.normalize_city("СПб") == "Санкт-Петербург")
check("пусто → Не указан", ca.normalize_city("") == "Не указан")
check("None → Не указан", ca.normalize_city(None) == "Не указан")
check("'старый оскол' → Старый Оскол", ca.normalize_city("старый оскол") == "Старый Оскол")
check("'КАЗАНЬ' → Казань", ca.normalize_city("КАЗАНЬ") == "Казань")

# ── summarize ─────────────────────────────────────────────────────────────────
ROWS = [
    {"status": "active", "city": "мск", "okved": "62.01", "inn": "165000000000",
     "chain_position": "before", "avg_check": 100, "offering": "x", "prospect": None},
    {"status": "active", "city": "Москва", "okved": "62.01", "inn": "7700000000",
     "chain_position": "after", "avg_check": 300, "prospect": {"opf": "ООО", "name_short": "ООО Ромашка"}},
    {"status": "paused", "city": "Казань", "okved": "41.20", "inn": "7800000000",
     "chain_position": None, "avg_check": None, "prospect": {"opf": "ГБУ"}},
]
s = ca.summarize(ROWS)
check("summarize.kpi.total == 3", s["kpi"]["total"] == 3)
check("summarize.kpi.active == 2", s["kpi"]["active"] == 2)
check("summarize.kpi.paused == 1", s["kpi"]["paused"] == 1)
check("summarize.kpi.with_egrul == 2", s["kpi"]["with_egrul"] == 2)
check("summarize.by_type ИП=1 ЮЛ=1 Гос=1",
      s["by_type"]["ИП"] == 1 and s["by_type"]["ЮЛ"] == 1 and s["by_type"]["Гос"] == 1)
check("summarize.chain before=1 after=1 'нет профиля'=1",
      s["chain"]["before"] == 1 and s["chain"]["after"] == 1 and s["chain"]["нет профиля"] == 1)
check("summarize город 'мск' и 'Москва' схлопнулись в Москва=2",
      dict(s["by_city"]).get("Москва") == 2)
check("summarize.avg_check.count == 2 (None пропущен)", s["avg_check"]["count"] == 2)
check("summarize.avg_check.median == 200", s["avg_check"]["median"] == 200)
empty = ca.summarize([])
check("summarize([]) не падает, total=0", empty["kpi"]["total"] == 0)
check("summarize([]).avg_check.median == 0 (None-safe)", empty["avg_check"]["median"] == 0)

# ── filter_members ────────────────────────────────────────────────────────────
check("filter_members city='Москва' ловит и 'мск' → 2",
      len(ca.filter_members(ROWS, city="Москва")) == 2)
check("filter_members etype='Гос' → 1", len(ca.filter_members(ROWS, etype="Гос")) == 1)
check("filter_members без фильтров → все", len(ca.filter_members(ROWS)) == 3)

# ── csv_business_rows: колонки, отсутствие контактов, formula-guard ───────────
check("CSV_HEADERS = 13 колонок", len(ca.CSV_HEADERS) == 13)
_hdr_join = " ".join(ca.CSV_HEADERS).lower()
for banned in ("телефон", "phone", "tg", "vk", "email", "контакт", "руковод", "адрес"):
    check(f"в заголовках CSV нет '{banned}'", banned not in _hdr_join)
data = list(ca.csv_business_rows(ROWS))
check("csv_business_rows отдаёт только data (3 строки, без заголовка)", len(data) == 3)
check("каждая CSV-строка = 13 полей", all(len(r) == 13 for r in data))
# formula-guard применяется на слое _csv_line (anon.csv_safe) — проверяем сам guard:
check("anon.csv_safe нейтрализует ведущий '='", csv_safe("=SUM(A1)").startswith("'"))

print(("\nFAIL: " + ", ".join(FAILS)) if FAILS else "\nOK: club_analytics_smoke")
sys.exit(1 if FAILS else 0)
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `cd ~/Downloads/risuy-ecosystem && PYTHONPATH=. ./.venv-smoke/bin/python scripts/club_analytics_smoke.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'shared.club_analytics'`.

- [ ] **Step 3: Реализовать модуль**

Create `shared/club_analytics.py`:

```python
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
```

- [ ] **Step 4: Запустить смоук — зелёный**

Run: `cd ~/Downloads/risuy-ecosystem && PYTHONPATH=. ./.venv-smoke/bin/python scripts/club_analytics_smoke.py`
Expected: PASS — `OK: club_analytics_smoke`, exit 0.

- [ ] **Step 5: Коммит**

```bash
cd ~/Downloads/risuy-ecosystem
git add shared/club_analytics.py scripts/club_analytics_smoke.py
git commit -m "feat(club): чистый модуль аналитики — тип субъекта/город/сводка/CSV + юнит-смоук

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: DB-хелперы `admin-panel/db.py` — обогащённая выборка + рост + воронка

**Files:**
- Modify: `admin-panel/db.py` (добавить три функции рядом с `club_member_list_with_profile:4759`)
- Test: `scripts/club_dashboard_db_smoke.py` (**гонит КОНТРОЛЛЕР** на risuy_dev; субагент НЕ запускает)

**Interfaces:**
- Consumes: `pool`, `db.set_active_tenant`, `db._apply_tenant_guc` (есть).
- Produces (для Task 3/4/5):
  - `club_member_list_enriched(tenant_id, *, status=None, okved=None) -> list[dict]` — строки со всеми
    колонками `club_members` + `offering/seeking/chain_position/okved_seek/avg_check/description` +
    `prospect_opf/prospect_subject_type/prospect_name_short/prospect_okved_name/prospect_status`.
  - `club_growth(tenant_id, period='month') -> list[dict]` — `[{'bucket':'YYYY-MM-DD','count':int}]` по возр.
  - `club_intro_funnel(tenant_id) -> dict` — ключи `requested/accepted/declined/cancelled/both_accepted/total`.

- [ ] **Step 1: Написать db-смоук (падает — функций нет)**

Create `scripts/club_dashboard_db_smoke.py`:

```python
#!/usr/bin/env python3
"""db-смоук новых клуб-аналитических хелперов на risuy_dev: club_member_list_enriched
(ЕГРЮЛ-join по inn + фильтры status/okved), club_growth (week/month), club_intro_funnel.
Гоняет КОНТРОЛЛЕР (нужен TEAM_DSN — owner-DSN risuy_dev). Пишет/чистит свои тенанты.
  TEAM_DSN="postgresql://gen_user:...@81.31.246.136:5432/risuy_dev?sslmode=require" \\
  PYTHONPATH=admin-panel:. ./.venv-smoke/bin/python scripts/club_dashboard_db_smoke.py
"""
import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "admin-panel"))
os.environ.setdefault("DATABASE_URL", "postgresql://x/y")
os.environ.setdefault("SESSION_SECRET", "smoke-secret-only-for-import-aaaaaaaa")
os.environ.setdefault("ADMIN_USERNAME", "smoke")
os.environ.setdefault("ADMIN_PASSWORD_HASH", "$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHQ$aGFzaHN0dWI")

import asyncpg  # noqa: E402
import db  # admin-panel/db.py  # noqa: E402

DSN = os.environ.get("TEAM_DSN", "")
if "/risuy_dev" not in DSN.split("?")[0]:
    print("SKIP: нужен TEAM_DSN на risuy_dev")
    sys.exit(0)

FAILS = []


def check(name, cond, detail=""):
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


async def main():
    db.pool = await asyncpg.create_pool(DSN, min_size=1, max_size=4, setup=db._apply_tenant_guc)
    ta = tb = None
    try:
        async with db.pool.acquire() as c:
            ta = await c.fetchval(
                "insert into tenants (slug, name, status) values "
                "('smoke-dash-a-'||substr(md5(random()::text),1,8),'SMOKE DASH A','active') returning id")
            tb = await c.fetchval(
                "insert into tenants (slug, name, status) values "
                "('smoke-dash-b-'||substr(md5(random()::text),1,8),'SMOKE DASH B','active') returning id")
            # члены A: ИП (Казань), ЮЛ с ЕГРЮЛ (Москва), ещё один ЮЛ (Москва, okved 41.20)
            m1 = await c.fetchval(
                "insert into club_members (tenant_id, display_name, city, okved, inn, status, created_at) "
                "values ($1,'ИП Соколова','Казань','62.01','165000000000','active', now() - interval '40 days') returning id", ta)
            m2 = await c.fetchval(
                "insert into club_members (tenant_id, display_name, city, okved, inn, status, created_at) "
                "values ($1,'ООО Ромашка','Москва','62.01','7700000001','active', now()) returning id", ta)
            m3 = await c.fetchval(
                "insert into club_members (tenant_id, display_name, city, okved, inn, status) "
                "values ($1,'ООО Строй','Москва','41.20','7700000002','paused') returning id", ta)
            await c.execute(
                "insert into club_profiles (member_id, tenant_id, offering, seeking, chain_position, avg_check) "
                "values ($1,$2,'разработка','дизайн','before',300)", m2, ta)
            # ЕГРЮЛ-карточка для m2 (join по inn)
            await c.execute(
                "insert into prospects (tenant_id, inn, subject_type, name_short, opf, okved, okved_name) "
                "values ($1,'7700000001','legal','ООО Ромашка','ООО','62.01','Разработка ПО')", ta)
            # знакомства: одно accepted-обоюдное, одно requested, одно declined
            i1 = await c.fetchval(
                "insert into club_intros (tenant_id, from_member, to_member, status, from_accepted_at, to_accepted_at) "
                "values ($1,$2,$3,'accepted', now(), now()) returning id", ta, m2, m3)
            await c.execute(
                "insert into club_intros (tenant_id, from_member, to_member, status) values ($1,$2,$3,'requested')", ta, m2, m1)
            await c.execute(
                "insert into club_intros (tenant_id, from_member, to_member, status) values ($1,$2,$3,'declined')", ta, m3, m1)

        db.set_active_tenant(ta)

        # ── enriched: все члены A, ЕГРЮЛ подмешан для m2 ──────────────────────
        rows = await db.club_member_list_enriched(ta)
        check("enriched(A) вернул 3 членов", len(rows) == 3, f"n={len(rows)}")
        by_name = {r["display_name"]: r for r in rows}
        check("enriched: у ООО Ромашка подмешан ЕГРЮЛ-opf=ООО",
              by_name["ООО Ромашка"]["prospect_opf"] == "ООО")
        check("enriched: у ООО Ромашка profile avg_check=300",
              by_name["ООО Ромашка"]["avg_check"] == 300)
        check("enriched: у ИП Соколова ЕГРЮЛ отсутствует (prospect_opf=NULL)",
              by_name["ИП Соколова"]["prospect_opf"] is None)

        # ── enriched: фильтры status/okved в SQL ──────────────────────────────
        act = await db.club_member_list_enriched(ta, status="active")
        check("enriched(status=active) → 2", len(act) == 2, f"n={len(act)}")
        ok = await db.club_member_list_enriched(ta, okved="41.20")
        check("enriched(okved=41.20) → 1 (ООО Строй)",
              len(ok) == 1 and ok[0]["display_name"] == "ООО Строй")

        # ── изоляция: B не видит членов A ────────────────────────────────────
        db.set_active_tenant(tb)
        rows_b = await db.club_member_list_enriched(tb)
        check("изоляция: enriched(B) пуст (нет членов B)", len(rows_b) == 0)

        # ── рост: месяц/неделя ────────────────────────────────────────────────
        db.set_active_tenant(ta)
        gm = await db.club_growth(ta, "month")
        check("growth(month): ≥2 бакета (40 дней назад + сейчас)", len(gm) >= 2, f"buckets={len(gm)}")
        check("growth(month): сумма count == 3", sum(b["count"] for b in gm) == 3)
        check("growth: bucket — строка YYYY-MM-DD", all(isinstance(b["bucket"], str) for b in gm))
        gw = await db.club_growth(ta, "week")
        check("growth(week): сумма count == 3", sum(b["count"] for b in gw) == 3)

        # ── воронка знакомств ─────────────────────────────────────────────────
        f = await db.club_intro_funnel(ta)
        check("funnel.accepted == 1", f["accepted"] == 1, f"f={f}")
        check("funnel.requested == 1", f["requested"] == 1)
        check("funnel.declined == 1", f["declined"] == 1)
        check("funnel.both_accepted == 1", f["both_accepted"] == 1)
        check("funnel.total == 3", f["total"] == 3)

        # ── пустой тенант: воронка/рост не падают ─────────────────────────────
        db.set_active_tenant(tb)
        fb = await db.club_intro_funnel(tb)
        check("funnel(пустой) total=0 both_accepted=0", fb["total"] == 0 and fb["both_accepted"] == 0)
        check("growth(пустой) == []", await db.club_growth(tb, "month") == [])

    finally:
        async with db.pool.acquire() as c:
            for t in (ta, tb):
                if not t:
                    continue
                await c.execute("delete from club_intros where tenant_id=$1", t)
                await c.execute("delete from club_profiles where tenant_id=$1", t)
                await c.execute("delete from prospects where tenant_id=$1", t)
                await c.execute("delete from club_members where tenant_id=$1", t)
                await c.execute("delete from tenants where id=$1", t)
        await db.pool.close()

    print(("\nFAIL: " + ", ".join(FAILS)) if FAILS else "\nOK: club_dashboard_db_smoke")
    sys.exit(1 if FAILS else 0)


asyncio.run(main())
```

- [ ] **Step 2: Пометить, что смоук ожидает контроллера**

Субагент **НЕ запускает** db-смоук (нет TEAM_DSN). В отчёте написать: «`club_dashboard_db_smoke.py`
написан, ожидает прогона контроллером на risuy_dev». Контроллер прогонит после реализации Step 3.

- [ ] **Step 3: Реализовать три хелпера в `admin-panel/db.py`**

Вставить сразу после `club_member_list_with_profile` (после строки ~4780):

```python
async def club_member_list_enriched(tenant_id, *, status: str | None = None,
                                    okved: str | None = None) -> list[dict]:
    """Члены клуба + профиль + ЕГРЮЛ-обогащение (prospects по inn) одним запросом.
    Явный tenant_id во ВСЕХ join/where = in-query backstop (owner-DSN обходит RLS),
    как club_member_list_with_profile. Фильтры status/okved — в SQL (ложатся на индексы
    (tenant_id,status)/(tenant_id,city,okved)); city/тип — в Python на стороне роута
    (club_analytics.filter_members). prospects — LEFT JOIN по (inn, tenant_id): у члена
    без inn/без сохранённой карточки prospect_*-поля = NULL."""
    clauses = ["m.tenant_id = $1"]
    args: list = [tenant_id]
    if status:
        args.append(status)
        clauses.append(f"m.status = ${len(args)}")
    if okved:
        args.append(okved)
        clauses.append(f"m.okved = ${len(args)}")
    where = " and ".join(clauses)
    async with pool.acquire() as c:
        rows = await c.fetch(
            f"""
            select m.*,
                   p.offering, p.seeking, p.chain_position, p.okved_seek,
                   p.avg_check, p.description,
                   pr.opf          as prospect_opf,
                   pr.subject_type as prospect_subject_type,
                   pr.name_short   as prospect_name_short,
                   pr.okved_name   as prospect_okved_name,
                   pr.status       as prospect_status
            from club_members m
            left join club_profiles p on p.member_id = m.id and p.tenant_id = $1
            left join prospects    pr on pr.inn = m.inn and pr.tenant_id = $1
            where {where}
            order by m.created_at desc
            """,
            *args,
        )
    return [dict(r) for r in rows]


async def club_growth(tenant_id, period: str = "month") -> list[dict]:
    """Рост клуба: число вступлений по бакетам (period ∈ {'week','month'}, вайтлист —
    безопасно инлайнить в date_trunc). Явный tenant_id-фильтр (backstop). Возвращает
    [{'bucket':'YYYY-MM-DD','count':int}] по возрастанию даты."""
    per = "week" if period == "week" else "month"
    async with pool.acquire() as c:
        rows = await c.fetch(
            f"""
            select date_trunc('{per}', created_at)::date as bucket, count(*) as count
            from club_members where tenant_id = $1
            group by bucket order by bucket
            """,
            tenant_id,
        )
    return [{"bucket": r["bucket"].isoformat(), "count": r["count"]} for r in rows]


async def club_intro_funnel(tenant_id) -> dict:
    """Воронка знакомств: счётчики по статусам club_intros + обоюдно принятые
    (from_accepted_at И to_accepted_at NOT NULL). Явный tenant_id-фильтр (backstop)."""
    async with pool.acquire() as c:
        rows = await c.fetch(
            "select status, count(*) as count from club_intros where tenant_id = $1 group by status",
            tenant_id,
        )
        both = await c.fetchval(
            "select count(*) from club_intros where tenant_id = $1 "
            "and from_accepted_at is not null and to_accepted_at is not null",
            tenant_id,
        )
    d = {"requested": 0, "accepted": 0, "declined": 0, "cancelled": 0}
    for r in rows:
        if r["status"] in d:
            d[r["status"]] = r["count"]
    d["both_accepted"] = int(both or 0)
    d["total"] = d["requested"] + d["accepted"] + d["declined"] + d["cancelled"]
    return d
```

- [ ] **Step 4: Контроллер прогоняет db-смоук на risuy_dev**

Run (контроллер, DSN из scratchpad):
`TEAM_DSN="...risuy_dev..." PYTHONPATH=admin-panel:. ./.venv-smoke/bin/python scripts/club_dashboard_db_smoke.py`
Expected: PASS — `OK: club_dashboard_db_smoke`, exit 0.

- [ ] **Step 5: Коммит**

```bash
cd ~/Downloads/risuy-ecosystem
git add admin-panel/db.py scripts/club_dashboard_db_smoke.py
git commit -m "feat(club): db-хелперы аналитики — enriched-выборка + рост + воронка знакомств

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Роут `/club` — фильтры (тип/статус) + KPI-полоска + шаблон

**Files:**
- Modify: `admin-panel/app.py` (`club_page:4228`; добавить хелперы `_club_prospect`, применение фильтров/KPI)
- Modify: `admin-panel/templates/club.html` (форма фильтров +тип/статус, KPI-полоска, ссылка «Дашборд», кнопка «Выгрузить CSV»)
- Test: `scripts/club_catalog_ui_smoke.py` (расширить)

**Interfaces:**
- Consumes: `club_analytics.filter_members/summarize/entity_type` (Task 1), `db.club_member_list_enriched` (Task 2).
- Produces (для Task 5): `_club_prospect(m: dict) -> dict|None` (сборка вложенного `prospect` из `prospect_*`-колонок).

- [ ] **Step 1: Расширить ui-смоук (падает на новых проверках)**

В `scripts/club_catalog_ui_smoke.py` в `BASE_CTX` добавить ключи и новые проверки. Добавить в контекст:

```python
    filter_type="",
    filter_status="",
    types=["ИП", "ЮЛ", "Гос"],
    kpi={"total": 2, "active": 1, "paused": 1, "left": 0, "with_egrul": 1, "with_profile": 1, "cities": 2},
```

И в конец файла (перед итоговым `print`) добавить проверки рендера:

```python
html = render()
check("KPI-полоска: показан total", "Всего" in html or "kpi" in html.lower())
check("фильтр по типу присутствует (name=type)", 'name="type"' in html)
check("фильтр по статусу присутствует (name=status)", 'name="status"' in html)
check("ссылка на дашборд есть", "/club/dashboard" in html)
check("кнопка выгрузки CSV (форма POST /club/export.csv)", "/club/export.csv" in html)
```

- [ ] **Step 2: Запустить — падает**

Run: `cd ~/Downloads/risuy-ecosystem && PYTHONPATH=. ./.venv-smoke/bin/python scripts/club_catalog_ui_smoke.py`
Expected: FAIL — новые проверки (`name="type"`, `/club/dashboard`, `/club/export.csv`, KPI) не проходят.

- [ ] **Step 3a: Импорт + хелпер `_club_prospect` в `app.py`**

В шапке `app.py` (рядом с `import club_match` на строке 43) добавить:

```python
from shared import club_analytics
```

Рядом с `club_page` (перед функцией, ~строка 4227) добавить хелпер:

```python
def _club_prospect(m: dict) -> dict | None:
    """Собирает вложенную ЕГРЮЛ-карточку prospect из плоских prospect_*-колонок
    club_member_list_enriched (для шаблона club.html и analytics). None — если ЕГРЮЛ
    не подмешан (member без inn/без сохранённой карточки)."""
    if not (m.get("prospect_name_short") or m.get("prospect_opf") or m.get("prospect_subject_type")):
        return None
    return {
        "opf": m.get("prospect_opf"),
        "subject_type": m.get("prospect_subject_type"),
        "name_short": m.get("prospect_name_short"),
        "okved_name": m.get("prospect_okved_name"),
        "status": m.get("prospect_status"),
        "inn": m.get("inn"),
        "okved": m.get("okved"),
    }
```

- [ ] **Step 3b: Переписать тело `club_page` на enriched-выборку + фильтры тип/статус + KPI**

Заменить сигнатуру и начало `club_page` (строки 4228–4281) — блоки выборки, фильтрации, ЕГРЮЛ,
опций фильтра. Новый вид (matches/intros/invite/резолв контактов НИЖЕ — НЕ трогаем):

```python
async def club_page(
    request: Request,
    session: auth.Session = Depends(require_session),
    city: str = "",
    okved: str = "",
    status: str = "",
):
    """Каталог «Клуб предпринимателей» (Уровень 1, tenant-scoped) + фильтры + KPI.
    Фильтры status/okved — в SQL (club_member_list_enriched), city/тип — в Python
    (club_analytics.filter_members: normalize_city/entity_type). ЕГРЮЛ-обогащение —
    LEFT JOIN prospects по inn (уже сохранённые карточки, без платного lookup)."""
    tid = session.active_tenant_id
    etype = (request.query_params.get("type") or "").strip()
    status_q = (status or "").strip()
    okved_q = (okved or "").strip()
    city_q = (city or "").strip()

    all_members = await db.club_member_list_enriched(tid) if tid else []
    for m in all_members:
        m["prospect"] = _club_prospect(m)

    # Рекомендации — по всему клубу тенанта (до фильтрации город/ОКВЭД/тип/статус).
    matches_by_member: dict[str, list[dict]] = {}
    for m in all_members:
        others = [o for o in all_members if o.get("id") != m.get("id")]
        matches_by_member[str(m.get("id"))] = club_match.rank_matches(m, others)[:3]

    # Фильтрация основного списка: status/okved — по колонкам, city/тип — нормализованно.
    members = all_members
    if status_q:
        members = [m for m in members if (m.get("status") or "") == status_q]
    if okved_q:
        members = [m for m in members if (m.get("okved") or "").strip() == okved_q]
    members = club_analytics.filter_members(members, city=city_q, etype=etype)
    for m in members:
        m["matches"] = matches_by_member.get(str(m.get("id")), [])

    # Опции фильтров (из всего клуба) + KPI по отфильтрованному набору.
    cities = sorted({club_analytics.normalize_city(m.get("city")) for m in all_members
                     if (m.get("city") or "").strip()})
    okveds = sorted({(m.get("okved") or "").strip() for m in all_members if m.get("okved")})
    types = ["ИП", "ЮЛ", "Гос", "не указан"]
    kpi = club_analytics.summarize(members)["kpi"]
```

Затем в существующем `templates.TemplateResponse(request, "club.html", {...})` (строки ~4321–4339)
добавить ключи (рядом с `filter_city`/`filter_okved`):

```python
            "filter_status": status_q,
            "filter_type": etype,
            "types": types,
            "kpi": kpi,
```

⚠️ Удалить старую строку ЕГРЮЛ-обогащения через `db.prospect_list()` (строки ~4269–4276) и старый
Python-фильтр город/ОКВЭД (строки ~4253–4258) и старые `cities`/`okveds` (строки ~4280–4281) — их
заменяет код выше. Блоки `intros`/`invite`/`Cache-Control` (строки 4283–4346) остаются без изменений.

- [ ] **Step 3c: Обновить `admin-panel/templates/club.html`**

Найти существующую GET-форму фильтров (поля `city`/`okved`) и добавить рядом селекты типа и статуса
(значения из `types` и фиксированного списка статусов), пометив выбранные `filter_type`/`filter_status`:

```html
    <select name="type">
      <option value="">Тип: любой</option>
      {% for t in types %}
      <option value="{{ t }}" {% if filter_type == t %}selected{% endif %}>{{ t }}</option>
      {% endfor %}
    </select>
    <select name="status">
      <option value="">Статус: любой</option>
      <option value="active" {% if filter_status == 'active' %}selected{% endif %}>активен</option>
      <option value="paused" {% if filter_status == 'paused' %}selected{% endif %}>на паузе</option>
      <option value="left"   {% if filter_status == 'left'   %}selected{% endif %}>вышел</option>
    </select>
```

Над списком карточек добавить KPI-полоску:

```html
    {% if has_tenant %}
    <div class="club-kpi">
      <span>Всего: <b>{{ kpi.total }}</b></span>
      <span>Активных: <b>{{ kpi.active }}</b></span>
      <span>С ЕГРЮЛ: <b>{{ kpi.with_egrul }}</b></span>
      <span>Городов: <b>{{ kpi.cities }}</b></span>
      <a href="/club/dashboard">Дашборд →</a>
    </div>
    {% endif %}
```

Рядом с фильтром — форма выгрузки CSV (POST, CSRF + текущие фильтры как hidden):

```html
    <form method="post" action="/club/export.csv" style="display:inline">
      <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
      <input type="hidden" name="city" value="{{ filter_city }}">
      <input type="hidden" name="okved" value="{{ filter_okved }}">
      <input type="hidden" name="type" value="{{ filter_type }}">
      <input type="hidden" name="status" value="{{ filter_status }}">
      <button type="submit">Выгрузить CSV</button>
    </form>
```

⚠️ Форма экспорта POST шлёт фильтры телом; роут (Task 5) читает их из query ИЛИ формы — в Task 5 роут
читает из `Form(...)`. Значит здесь фильтры идут как `name=...` в теле формы — соответствует Task 5.

- [ ] **Step 4: Запустить ui-смоук — зелёный**

Run: `cd ~/Downloads/risuy-ecosystem && PYTHONPATH=. ./.venv-smoke/bin/python scripts/club_catalog_ui_smoke.py`
Expected: PASS — `OK: club_catalog_ui_smoke`.

- [ ] **Step 5: Коммит**

```bash
cd ~/Downloads/risuy-ecosystem
git add admin-panel/app.py admin-panel/templates/club.html scripts/club_catalog_ui_smoke.py
git commit -m "feat(club): каталог /club — фильтры тип/статус + KPI-полоска + enriched-выборка

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Вкладка `/club/dashboard` — полный дашборд

**Files:**
- Modify: `admin-panel/app.py` (новый роут `club_dashboard` рядом с `club_page`)
- Create: `admin-panel/templates/club_dashboard.html`
- Test: `scripts/club_dashboard_ui_smoke.py`

**Interfaces:**
- Consumes: `db.club_member_list_enriched/club_growth/club_intro_funnel` (Task 2), `club_analytics.summarize` (Task 1), `_club_prospect` (Task 3).

- [ ] **Step 1: Написать ui-смоук (падает — шаблона нет)**

Create `scripts/club_dashboard_ui_smoke.py`:

```python
#!/usr/bin/env python3
"""Render-смоук вкладки /club/dashboard (club_dashboard.html): KPI-плитки,
распределения (город/ОКВЭД/тип), покрытие цепочки, средний чек, рост, воронка знакомств;
empty-state без active_tenant. Чистый Jinja (без БД/HTTP).
  PYTHONPATH=. ./.venv-smoke/bin/python scripts/club_dashboard_ui_smoke.py
"""
import os
import sys

from jinja2 import ChainableUndefined, Environment, FileSystemLoader, select_autoescape

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
env = Environment(
    loader=FileSystemLoader(os.path.join(ROOT, "admin-panel", "templates")),
    autoescape=select_autoescape(["html", "xml"]),
    undefined=ChainableUndefined,
)
env.globals["asset_version"] = ""
env.globals["service_site_url"] = ""

FAILS = []


def check(name, cond, detail=""):
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


SUMMARY = {
    "kpi": {"total": 3, "active": 2, "paused": 1, "left": 0, "with_egrul": 2, "with_profile": 1, "cities": 2},
    "by_city": [("Москва", 2), ("Казань", 1)],
    "by_okved": [("62.01", 2), ("41.20", 1)],
    "by_type": {"ИП": 1, "ЮЛ": 1, "Гос": 1, "не указан": 0},
    "chain": {"before": 1, "after": 1, "both": 0, "нет профиля": 1},
    "avg_check": {"count": 2, "min": 100, "median": 200, "max": 300},
}
CTX = dict(
    csrf_token="csrf",
    session={"is_platform": False, "active_tenant_name": "Тестовый клиент", "actor": "o@e.com"},
    active="club", has_tenant=True, support_url="",
    summary=SUMMARY,
    growth_month=[{"bucket": "2026-06-01", "count": 1}, {"bucket": "2026-07-01", "count": 2}],
    growth_week=[{"bucket": "2026-06-29", "count": 3}],
    funnel={"requested": 1, "accepted": 1, "declined": 1, "cancelled": 0, "both_accepted": 1, "total": 3},
)


def render(**over):
    ctx = dict(CTX); ctx.update(over)
    return env.get_template("club_dashboard.html").render(**ctx)


html = render()
check("KPI total рендерится", "3" in html and ("Всего" in html or "kpi" in html.lower()))
check("распределение по типу (ИП/ЮЛ/Гос)", "ИП" in html and "ЮЛ" in html and "Гос" in html)
check("распределение по городу (Москва)", "Москва" in html)
check("покрытие цепочки (до вас/после вас или before/after)", "цепоч" in html.lower() or "before" in html)
check("средний чек (медиана 200)", "200" in html)
check("рост клуба (бакет-дата)", "2026-07" in html)
check("воронка знакомств (предложено/принято)", "знаком" in html.lower() or "воронк" in html.lower())

empty = render(has_tenant=False, summary={"kpi": {"total": 0, "active": 0, "paused": 0, "left": 0,
    "with_egrul": 0, "with_profile": 0, "cities": 0}, "by_city": [], "by_okved": [],
    "by_type": {"ИП": 0, "ЮЛ": 0, "Гос": 0, "не указан": 0},
    "chain": {"before": 0, "after": 0, "both": 0, "нет профиля": 0},
    "avg_check": {"count": 0, "min": 0, "median": 0, "max": 0}},
    growth_month=[], growth_week=[], funnel={"requested": 0, "accepted": 0, "declined": 0,
    "cancelled": 0, "both_accepted": 0, "total": 0})
check("empty-state без active_tenant не падает", "Выберите клиента" in empty or "клиент" in empty.lower())

print(("\nFAIL: " + ", ".join(FAILS)) if FAILS else "\nOK: club_dashboard_ui_smoke")
sys.exit(1 if FAILS else 0)
```

- [ ] **Step 2: Запустить — падает**

Run: `cd ~/Downloads/risuy-ecosystem && PYTHONPATH=. ./.venv-smoke/bin/python scripts/club_dashboard_ui_smoke.py`
Expected: FAIL — `jinja2.exceptions.TemplateNotFound: club_dashboard.html`.

- [ ] **Step 3a: Создать `admin-panel/templates/club_dashboard.html`**

Расширить `base.html` тем же способом, что `club.html` (посмотреть его первые строки на `{% extends %}`
и имя блока контента; здесь предполагается `{% extends "base.html" %}` + `{% block content %}`). Шаблон:

```html
{% extends "base.html" %}
{% block content %}
<div class="club-dashboard">
  <div class="club-tabs">
    <a href="/club">Каталог</a>
    <a href="/club/dashboard" class="active">Дашборд</a>
  </div>

  {% if not has_tenant %}
    <p class="empty">Выберите клиента, чтобы увидеть аналитику клуба.</p>
  {% else %}
  <section class="kpi-tiles">
    <div class="tile"><span class="num">{{ summary.kpi.total }}</span><span>Всего членов</span></div>
    <div class="tile"><span class="num">{{ summary.kpi.active }}</span><span>Активных</span></div>
    <div class="tile"><span class="num">{{ summary.kpi.paused }}</span><span>На паузе</span></div>
    <div class="tile"><span class="num">{{ summary.kpi.with_egrul }}</span><span>С ЕГРЮЛ</span></div>
    <div class="tile"><span class="num">{{ summary.kpi.cities }}</span><span>Городов</span></div>
    <div class="tile"><span class="num">{{ summary.kpi.with_profile }}</span><span>С профилем</span></div>
  </section>

  <section class="dist">
    <h3>По типу</h3>
    <ul>
      {% for t, n in summary.by_type.items() %}<li>{{ t }}: <b>{{ n }}</b></li>{% endfor %}
    </ul>
    <h3>По городу</h3>
    <ul>
      {% for city, n in summary.by_city %}<li>{{ city }}: <b>{{ n }}</b></li>{% endfor %}
    </ul>
    <h3>По ОКВЭД</h3>
    <ul>
      {% for ok, n in summary.by_okved %}<li>{{ ok }}: <b>{{ n }}</b></li>{% endfor %}
    </ul>
    <h3>Покрытие цепочки потребления</h3>
    <ul>
      <li>До вас: <b>{{ summary.chain.before }}</b></li>
      <li>После вас: <b>{{ summary.chain.after }}</b></li>
      <li>Оба направления: <b>{{ summary.chain.both }}</b></li>
      <li>Нет профиля: <b>{{ summary.chain['нет профиля'] }}</b></li>
    </ul>
    <h3>Средний чек</h3>
    <p>по {{ summary.avg_check.count }} профилям: мин {{ summary.avg_check.min }} ·
       медиана <b>{{ summary.avg_check.median }}</b> · макс {{ summary.avg_check.max }}</p>
  </section>

  <section class="growth">
    <h3>Рост клуба (по месяцам)</h3>
    {% if growth_month %}
    <ul>{% for b in growth_month %}<li>{{ b.bucket }}: <b>{{ b.count }}</b></li>{% endfor %}</ul>
    {% else %}<p class="empty">Пока нет вступлений.</p>{% endif %}
  </section>

  <section class="funnel">
    <h3>Воронка знакомств</h3>
    <ul>
      <li>Предложено: <b>{{ funnel.requested + funnel.accepted + funnel.declined + funnel.cancelled }}</b></li>
      <li>Принято (обоюдно): <b>{{ funnel.both_accepted }}</b></li>
      <li>Отклонено: <b>{{ funnel.declined }}</b></li>
      <li>В ожидании: <b>{{ funnel.requested }}</b></li>
    </ul>
  </section>
  {% endif %}
</div>
{% endblock %}
```

⚠️ Перед реализацией имплементер обязан открыть `admin-panel/templates/club.html` и `base.html`, свериться
с реальными именами `{% extends %}`/`{% block %}` и заголовочной навигацией, и привести шаблон к ним
(имя блока может быть не `content`). Смоук проверяет только наличие текста — структуру берём из base.html.

- [ ] **Step 3b: Добавить роут `club_dashboard` в `app.py`**

Рядом с `club_page` (после её тела, до `_club_intro_err_text`) добавить:

```python
@app.get("/club/dashboard")
async def club_dashboard(
    request: Request,
    session: auth.Session = Depends(require_session),
):
    """Дашборд клуба по ВСЕМУ клубу тенанта (без фильтра): KPI + распределения +
    покрытие цепочки + средний чек + рост + воронка знакомств."""
    tid = session.active_tenant_id
    members = await db.club_member_list_enriched(tid) if tid else []
    for m in members:
        m["prospect"] = _club_prospect(m)
    summary = club_analytics.summarize(members)
    growth_month = await db.club_growth(tid, "month") if tid else []
    growth_week = await db.club_growth(tid, "week") if tid else []
    funnel = await db.club_intro_funnel(tid) if tid else {
        "requested": 0, "accepted": 0, "declined": 0, "cancelled": 0, "both_accepted": 0, "total": 0}
    return templates.TemplateResponse(
        request, "club_dashboard.html",
        {
            "csrf_token": session.csrf_token,
            "session": session,
            "active": "club",
            "has_tenant": bool(tid),
            "summary": summary,
            "growth_month": growth_month,
            "growth_week": growth_week,
            "funnel": funnel,
            "support_url": _safe_support_url(config.SUPPORT_URL),
        },
    )
```

- [ ] **Step 4: Запустить ui-смоук — зелёный**

Run: `cd ~/Downloads/risuy-ecosystem && PYTHONPATH=. ./.venv-smoke/bin/python scripts/club_dashboard_ui_smoke.py`
Expected: PASS — `OK: club_dashboard_ui_smoke`.

- [ ] **Step 5: Коммит**

```bash
cd ~/Downloads/risuy-ecosystem
git add admin-panel/app.py admin-panel/templates/club_dashboard.html scripts/club_dashboard_ui_smoke.py
git commit -m "feat(club): вкладка /club/dashboard — KPI, распределения, рост, воронка знакомств

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: Экспорт `POST /club/export.csv` — бизнес-поля, CSRF, аудит

**Files:**
- Modify: `admin-panel/app.py` (роут `club_export` рядом с другими `/export*`; генератор строк)
- Test: `scripts/club_export_csv_smoke.py`

**Interfaces:**
- Consumes: `club_analytics.csv_business_rows/CSV_HEADERS/filter_members` (Task 1),
  `db.club_member_list_enriched` (Task 2), `_club_prospect` (Task 3), `_csv_line/_csv_headers` (есть),
  `_enforce_csrf/db.audit/_ip/_ua` (есть).

- [ ] **Step 1: Написать смоук экспорта (падает)**

Create `scripts/club_export_csv_smoke.py`:

```python
#!/usr/bin/env python3
"""Смоук CSV-экспорта клуба: csv_business_rows отдаёт ровно бизнес-колонки (CSV_HEADERS),
НЕ содержит контактных/ПДн-полей, formula-guard нейтрализует инъекцию. Без БД/HTTP.
  PYTHONPATH=. ./.venv-smoke/bin/python scripts/club_export_csv_smoke.py
"""
import sys

from shared import club_analytics as ca
from shared.anon import csv_safe

FAILS = []


def check(name, cond, detail=""):
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


ROWS = [
    {"display_name": "ИП Соколова", "city": "мск", "inn": "165000000000", "okved": "62.01",
     "offering": "разработка", "seeking": "дизайн", "chain_position": "before",
     "avg_check": 150, "status": "active", "created_at": None,
     # контактные/ПДн поля НАМЕРЕННО присутствуют во входе — не должны попасть в CSV:
     "tg_user_id": 12345, "prospect": {"opf": "ООО", "name_short": "ООО Р", "okved_name": "ПО",
                                       "management": "Иванов Иван Иванович", "address": "секрет"}},
]
rows = list(ca.csv_business_rows(ROWS))
check("одна data-строка", len(rows) == 1)
check("строка = 13 полей (= CSV_HEADERS)", len(rows[0]) == len(ca.CSV_HEADERS) == 13)
flat = " | ".join(rows[0])
check("НЕТ tg_user_id в строке", "12345" not in flat)
check("НЕТ ФИО руководителя (management)", "Иванов" not in flat)
check("НЕТ адреса ИП", "секрет" not in flat)
check("город нормализован (мск→Москва)", "Москва" in flat)
check("тип ИП определён", "ИП" in rows[0])
check("ЕГРЮЛ name_short попал", "ООО Р" in flat)

# formula-guard на слое _csv_line (anon.csv_safe):
mal = list(ca.csv_business_rows([{"display_name": "=cmd()", "inn": "7700000000", "prospect": {"opf": "ООО"}}]))
guarded = [csv_safe(v) for v in mal[0]]
check("formula-guard нейтрализует '=cmd()' в названии", guarded[0].startswith("'"))

print(("\nFAIL: " + ", ".join(FAILS)) if FAILS else "\nOK: club_export_csv_smoke")
sys.exit(1 if FAILS else 0)
```

- [ ] **Step 2: Запустить — падает** (если Task 1 уже готов, смоук пройдёт сразу — тогда это
  контрольная проверка контракта; если `csv_business_rows` ещё не финальна, увидим FAIL).

Run: `cd ~/Downloads/risuy-ecosystem && PYTHONPATH=. ./.venv-smoke/bin/python scripts/club_export_csv_smoke.py`
Expected: PASS (контракт Task 1 уже удовлетворяет смоук). Если FAIL — чинить `csv_business_rows`.

- [ ] **Step 3: Добавить роут `club_export` в `app.py`**

Рядом с `export_consents` (после ~строки 1948) добавить:

```python
# ---- /club/export.csv — POST, CSRF, аудит, ТОЛЬКО бизнес-поля (без контактов) ---- #
@app.post("/club/export.csv")
async def club_export(
    request: Request,
    session: auth.Session = Depends(require_session),
    csrf_token: str = Form(""),
    city: str = Form(""),
    okved: str = Form(""),
    type: str = Form(""),
    status: str = Form(""),
):
    await _enforce_csrf(request, session, csrf_token)
    tid = session.active_tenant_id
    if not tid:
        raise StarletteHTTPException(status_code=400, detail="Выберите клиента")

    status_q = (status or "").strip() or None
    okved_q = (okved or "").strip() or None
    members = await db.club_member_list_enriched(tid, status=status_q, okved=okved_q)
    for m in members:
        m["prospect"] = _club_prospect(m)
    members = club_analytics.filter_members(members, city=(city or "").strip(), etype=(type or "").strip())

    # Аудит ДО стрима (fail-closed): факт выгрузки фиксируем; ПДн в лог не пишем.
    await db.audit(actor=session.actor, action="club_export", ip=_ip(request), user_agent=_ua(request),
                   detail={"filters": {"city": (city or "").strip(), "okved": okved_q or "",
                                       "type": (type or "").strip(), "status": status_q or ""},
                           "matched": len(members)})

    def _rows():
        yield _csv_line(club_analytics.CSV_HEADERS, bom=True)
        for row in club_analytics.csv_business_rows(members):
            yield _csv_line(row)

    return StreamingResponse(
        _rows(),
        media_type="text/csv; charset=utf-8",
        headers=_csv_headers("club_members"),
    )
```

⚠️ Параметр `type: str = Form("")` — имя телом формы (в шаблоне Task 3 `name="type"`). `type` как имя
аргумента затеняет builtin в области функции — допустимо (внутри не используем `type()`).

- [ ] **Step 4: Перезапустить смоук + проверить импорт роутера**

Run: `cd ~/Downloads/risuy-ecosystem && PYTHONPATH=. ./.venv-smoke/bin/python scripts/club_export_csv_smoke.py`
Expected: PASS — `OK: club_export_csv_smoke`.

Смоук импорта приложения (роут регистрируется без ошибок):
`PYTHONPATH=admin-panel:. DATABASE_URL=postgresql://x/y SESSION_SECRET=aaaaaaaaaaaaaaaa ADMIN_USERNAME=x ADMIN_PASSWORD_HASH='$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHQ$aGFzaHN0dWI' ./.venv-smoke/bin/python -c "import app; print('routes ok', any(getattr(r,'path','')=='/club/export.csv' for r in app.app.routes))"`
Expected: `routes ok True`.

- [ ] **Step 5: Коммит**

```bash
cd ~/Downloads/risuy-ecosystem
git add admin-panel/app.py scripts/club_export_csv_smoke.py
git commit -m "feat(club): POST /club/export.csv — CSV бизнес-полей (без контактов) + CSRF + аудит

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 6: Верификация + адверсариальное ревью + деплой-гейт

**Files:** нет изменений кода (кроме фиксов по ревью).

- [ ] **Step 1: Прогнать ВСЕ смоуки (unit/ui — субагент/контроллер; db — контроллер)**

```bash
cd ~/Downloads/risuy-ecosystem
PYTHONPATH=. ./.venv-smoke/bin/python scripts/club_analytics_smoke.py
PYTHONPATH=. ./.venv-smoke/bin/python scripts/club_catalog_ui_smoke.py
PYTHONPATH=. ./.venv-smoke/bin/python scripts/club_dashboard_ui_smoke.py
PYTHONPATH=. ./.venv-smoke/bin/python scripts/club_export_csv_smoke.py
# db (контроллер, DSN из scratchpad):
TEAM_DSN="...risuy_dev..." PYTHONPATH=admin-panel:. ./.venv-smoke/bin/python scripts/club_dashboard_db_smoke.py
```
Expected: все `OK: ...`, exit 0. Регрессия: `club_catalog_ui_smoke`, `club_db_smoke` (существующий) зелёные.

- [ ] **Step 2: Адверсариальное ревью (свежий ревьюер, read-only)**

Проверить: (1) нет утечки контактов/ПДн в CSV/дашборд (grep роутов на `tg_user_id`/`management`/`address`);
(2) in-query backstop — `tenant_id` в каждом новом `where`/`join`; (3) `date_trunc`-инлайн только из
вайтлиста week/month; (4) фильтры city/type консистентны между `/club`, дашбордом (без фильтра) и
экспортом; (5) `type`-затенение builtin безвредно; (6) шаблон `club_dashboard.html` реально расширяет
`base.html` (имя блока). Найденное — фикс-волна, повторный смоук.

- [ ] **Step 3: Деплой-гейт**

Собрать сводку (файлы, смоуки, что НЕ делали — прод-DDL нет, бот не трогали). **Спросить у владельца
явное «да» на деплой** (`git push origin docs/security-audit:main` → редеплой панели 205025).
После push — сверка `twc apps get 205025 -o json | grep commit_sha` == HEAD + `status=active`; попросить
владельца открыть `cabinet.pro-agent-ai.ru` → `/club` (фильтры, KPI, кнопка CSV) и `/club/dashboard`
(HTTP-live из среды агента недоступен).

---

## Self-Review (автор плана)

**Spec coverage:**
- §2 фильтры city/okved/type/status → Task 2 (SQL status/okved) + Task 1 `filter_members` (city/type) + Task 3 (UI). ✓
- §2 CSV бизнес-поля → Task 1 `csv_business_rows`/`CSV_HEADERS` + Task 5 роут. ✓
- §2 дашборд полный → Task 4 (+ Task 2 growth/funnel, Task 1 summarize). ✓
- §3 гибрид-агрегации / read-time города / всё сразу → Task 1+2. ✓
- §5 модуль club_analytics (entity_type/normalize_city/summarize/csv_business_rows) → Task 1. ✓
- §6 db-хелперы → Task 2 (enriched/growth/funnel). ✓ (city/type фильтр в Python — уточнено: `filter_members`.)
- §7 поверхности /club, /club/dashboard, /club/export.csv → Task 3/4/5. (Экспорт GET→POST — уточнение, см. Global Constraints.) ✓
- §8 152-ФЗ без контактов/ПДн → Task 1 (колонки) + Task 5 (аудит) + смоук на отсутствие контактов. ✓
- §9 деградация (нет ЕГРЮЛ/пустой клуб/None чек) → Task 1 summarize None-safe + Task 4 empty-state + db-смоук пустого тенанта. ✓
- §10 смоуки (unit+db+ui) → Task 1/2/4/5. ✓
- §11 порядок выкатки, DDL нет → Task 6. ✓

**Placeholder scan:** плейсхолдеров нет — код полный в каждом шаге.

**Type consistency:** `club_member_list_enriched(tenant_id, *, status, okved)`, `club_growth(tid, period)`,
`club_intro_funnel(tid)`, `_club_prospect(m)`, `club_analytics.{entity_type,normalize_city,summarize,
csv_business_rows,CSV_HEADERS,filter_members}` — имена и сигнатуры совпадают между Task 1→6.
Ключи `summary`: `kpi/by_city/by_okved/by_type/chain/avg_check` — согласованы между Task 1 (summarize),
Task 4 (шаблон+смоук). `funnel`: `requested/accepted/declined/cancelled/both_accepted/total` — Task 2↔4↔смоук. ✓
