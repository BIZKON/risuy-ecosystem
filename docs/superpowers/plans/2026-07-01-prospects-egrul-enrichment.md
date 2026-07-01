# План реализации: модуль «База компаний / обогащение по ЕГРЮЛ» — v1

> **Для агентов-исполнителей:** ОБЯЗАТЕЛЬНАЯ СУБ-СКИЛЛ: используй superpowers:subagent-driven-development (рекомендовано) или superpowers:executing-plans для реализации задача-за-задачей. Шаги отмечены чекбоксами (`- [ ]`).

**Спека:** `docs/superpowers/specs/2026-07-01-prospects-egrul-enrichment-design.md`
**Ветка:** `feat/prospects-egrul` (уже создана, спека закоммичена `87e5ee1`).

**Goal:** дать тенанту/оператору по ИНН (или названию) подтянуть карточку юрлица/ИП из ЕГРЮЛ через DaData (per-lookup), сохранить в свою базу и/или привязать к согласившемуся лиду — без телефонов и масс-экспорта.

**Architecture:** tenant-scoped таблица `prospects` (RLS-канон risuy) ← провайдер-адаптер `dadata.py` (find-party по ИНН, suggest по названию, вырезание контактов) ← раздел панели `/companies` (server-rendered, CSP-safe, A1-гейт `active_tenant_id`) + блок «Компания» в карточке лида.

**Tech Stack:** FastAPI 0.115 + Jinja2 3.1 + asyncpg 0.30 (пул с RLS-хуком `app.tenant_id`), Postgres на Timeweb (миграции owner-DSN через `twc-migrate.sh`), stdlib `urllib.request` + `asyncio.to_thread` для DaData (без новых зависимостей).

## Global Constraints

- **Язык:** весь текст (UI, комментарии, коммиты, docstrings) — только русский; латиница — техника.
- **RLS-канон:** новая tenant-scoped таблица — `tenant_id not null → tenants(id) cascade`, политика `tenant_isolation` через `nullif(current_setting('app.tenant_id', true), '')::uuid`, **ENABLE (не FORCE)**; read-хелперы дублируют фильтр `tenant_id` в SQL (in-query backstop к RLS).
- **Гранты `panel_rw`:** `select, insert, update` (без DELETE — soft-архив флагом); грант дублировать в `db/panel_role.sql` (там mass-`revoke all` → иначе реконсиляция Timeweb смоет).
- **Миграции:** `bash ~/.claude/scripts/twc-migrate.sh 4171827 81.31.246.136 <db> gen_user db/<file>.sql`; **СНАЧАЛА `risuy_dev`, потом `risuy` (прод)**, идемпотентно (`if not exists`). Прод-DDL — **только по явному «да» владельца**. owner-DSN на risuy_dev даёт владелец строкой.
- **Комплаенс (в коде):** телефоны/email и закрытые категории (паспорт/ДР/адрес места жительства ИП) **вырезаются провайдером до возврата**; find-party — только для обогащения по ИНН/ОГРН; suggest — только интерактивный ручной поиск по названию (не пакетно, оферта DaData 4.2.1). ИП (`subject_type='individual'`) — режим 152-ФЗ. Данные = аналитика/обогащение, не список для обзвона.
- **CSP-safe:** без клиентского JS (server-round-trip), как онбординг.
- **Источник:** DaData тариф «Лёгкий» (14 000 ₽/год, 50 000/сут); env через API PATCH `/apps/205025` полным набором (UI затирает `run_cmd`).
- **Коммиты:** стейджить файлы явно; НЕ коммитить `CLAUDE.md`/`.claude/`/`.gitignore` (graphify). Трейлер: `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`. Push/прод-деплой — по «да».
- **Смоуки:** `PYTHONPATH=. ./.venv-smoke/bin/python scripts/<name>.py`; db-смоуки гардят `risuy_dev` в скрипте.

---

## Карта файлов

- **Create** `db/migrate_prospects.sql` — DDL таблицы (create + indexes + RLS + grant).
- **Modify** `db/panel_role.sql` — зеркалировать грант `prospects`.
- **Modify** `admin-panel/config.py` — env `DADATA_*`, `PROSPECT_TTL_DAYS`, `DADATA_DAILY_LIMIT`.
- **Create** `admin-panel/dadata.py` — провайдер-адаптер (is_configured, find_party, suggest_party, _parse_party, _sanitize).
- **Modify** `admin-panel/db.py` — хелперы `prospect_*` + `dadata_quota_take`.
- **Modify** `admin-panel/app.py` — маршруты `/companies*` + блок в `_render_dialogs`.
- **Create** `admin-panel/templates/companies.html`, `admin-panel/templates/_company_card.html`.
- **Modify** `admin-panel/templates/base.html` — nav-пункт + иконка `companies`; шаблон карточки лида — блок «Компания».
- **Create** `scripts/dadata_smoke.py` (unit, мок), `scripts/prospects_db_smoke.py` (RLS risuy_dev), `scripts/prospects_ui_smoke.py` (render).

---

### Task 1: Прод-DDL таблицы `prospects` + грант в `panel_role.sql`

**Files:**
- Create: `db/migrate_prospects.sql`
- Modify: `db/panel_role.sql` (дописать грант рядом с прочими)

**Interfaces:**
- Produces: таблица `prospects` (колонки перечислены в спеке §5), политика RLS `tenant_isolation`, грант `panel_rw`.

- [ ] **Шаг 1: Написать миграцию** `db/migrate_prospects.sql`:

```sql
-- ── prospects: карточки компаний ЕГРЮЛ/ЕГРИП (обогащение по ИНН, per-lookup) ──
-- Пишет ПАНЕЛЬ (операторское действие), не бот. Tenant-scoped (RLS по app.tenant_id,
-- как leads/consent_events). Телефоны/email/закрытые категории НЕ хранятся (вырезает
-- провайдер dadata.py до записи; полей под них в схеме нет — defense-in-depth).
-- Источник — DaData find-party; повторный lookup по (tenant_id, inn) обновляет карточку.
--
-- ⚠️ DDL: twc-migrate.sh owner-DSN, СНАЧАЛА risuy_dev, ПЕРЕД деплоем кода. Идемпотентно.
-- Откат: drop table prospects;
--   bash ~/.claude/scripts/twc-migrate.sh 4171827 81.31.246.136 <db> gen_user db/migrate_prospects.sql

create table if not exists prospects (
    id                uuid primary key default gen_random_uuid(),
    tenant_id         uuid not null references tenants(id) on delete cascade,

    inn               text not null,
    kpp               text,
    ogrn              text,
    subject_type      text not null default 'legal'
                      check (subject_type in ('legal','individual')),

    name_short        text,
    name_full         text,
    opf               text,
    okved             text,
    okved_name        text,
    okveds            jsonb,
    address           text,        -- юр.адрес ЮЛ; для ИП — только город (адрес места жительства НЕ храним)
    region            text,
    city              text,
    status            text,        -- ACTIVE|LIQUIDATING|LIQUIDATED|BANKRUPT|REORGANIZING
    registration_date date,
    liquidation_date  date,
    management        jsonb,       -- руководитель ЮЛ (ФИО физлица = ПДн; не для рекламы, маскировать в LLM)

    lead_id           uuid references leads(id) on delete set null,

    source            text not null default 'dadata',   -- dadata|api-fns|manual
    raw               jsonb,       -- САНИТИЗИРОВАННЫЙ ответ (без phones/emails/закрытых категорий)
    fetched_at        timestamptz,
    archived          boolean not null default false,
    created_by        text,
    created_at        timestamptz not null default now(),
    updated_at        timestamptz not null default now(),

    unique (tenant_id, inn)
);

create index if not exists prospects_tenant_lead_idx on prospects (tenant_id, lead_id);
create index if not exists prospects_tenant_city_okved_idx on prospects (tenant_id, city, okved);

alter table prospects enable row level security;
do $$ begin
    if not exists (select 1 from pg_policies where tablename='prospects' and policyname='tenant_isolation') then
        create policy tenant_isolation on prospects
            for all
            using (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
            with check (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);
    end if;
end $$;

do $$ begin
    if exists (select 1 from pg_roles where rolname='panel_rw') then
        grant select, insert, update on prospects to panel_rw;  -- без delete (канон: archived-флаг)
    end if;
end $$;
```

- [ ] **Шаг 2: Зеркалировать грант в `db/panel_role.sql`** — рядом с блоком точечных грантов (после mass-`revoke all`), добавить строку:

```sql
grant select, insert, update on prospects to panel_rw;  -- prospects: карточки ЕГРЮЛ, панель пишет (без delete — archived)
```

- [ ] **Шаг 3: Проверка ДО миграции (ожидаемый FAIL)** — на `risuy_dev` (owner-DSN от владельца):

Run: `psql "<owner-dsn-risuy_dev>" -c "\d prospects"`
Expected: FAIL — `Did not find any relation named "prospects"`.

- [ ] **Шаг 4: Применить миграцию на `risuy_dev`** (owner-DSN от владельца):

Run: `bash ~/.claude/scripts/twc-migrate.sh 4171827 81.31.246.136 risuy_dev gen_user db/migrate_prospects.sql db/panel_role.sql`
Expected: без ошибок; идемпотентно.

- [ ] **Шаг 5: Проверка ПОСЛЕ (ожидаемый PASS)**:

Run: `psql "<owner-dsn-risuy_dev>" -c "select count(*) from pg_policies where tablename='prospects' and policyname='tenant_isolation';"`
Expected: `1`. И `\d prospects` показывает колонки + `unique (tenant_id, inn)`.

- [ ] **Шаг 6: Коммит**

```bash
git add db/migrate_prospects.sql db/panel_role.sql
git commit -m "feat(db): прод-DDL prospects (карточки ЕГРЮЛ, RLS-канон, грант panel_rw)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

> ⚠️ Прод (`risuy`) — НЕ применять здесь. Прод-DDL — отдельный gated-шаг в Task 7 по явному «да».

---

### Task 2: Провайдер-адаптер `dadata.py` + env-конфиг + unit-смоук

**Files:**
- Modify: `admin-panel/config.py` (добавить env-переменные)
- Create: `admin-panel/dadata.py`
- Create: `scripts/dadata_smoke.py`

**Interfaces:**
- Consumes: `config.DADATA_API_KEY`, `config.DADATA_SECRET_KEY`, `config.DADATA_TIMEOUT_SEC`, `config.DADATA_DAILY_LIMIT`.
- Produces: `dadata.is_configured() -> bool`; `dadata.ProspectCard` (dataclass: `inn, subject_type, kpp, ogrn, name_short, name_full, opf, okved, okved_name, okveds, address, region, city, status, registration_date, liquidation_date, management, raw`); `async dadata.find_party(query:str) -> ProspectCard|None`; `async dadata.suggest_party(query:str, count:int=7) -> list[dict]` (элементы `{inn,name,city,status}`); pure `dadata._parse_party(data:dict)->ProspectCard`, `dadata._sanitize(data:dict)->dict`.

- [ ] **Шаг 1: Добавить env в `admin-panel/config.py`** (рядом с блоком SMTP, паттерн `_opt_int`):

```python
# ── DaData (обогащение по ЕГРЮЛ, per-lookup; тариф «Лёгкий») ─────────────────
DADATA_API_KEY = os.environ.get("DADATA_API_KEY", "")
DADATA_SECRET_KEY = os.environ.get("DADATA_SECRET_KEY", "")
DADATA_TIMEOUT_SEC = _opt_int("DADATA_TIMEOUT_SEC", 5)
DADATA_DAILY_LIMIT = _opt_int("DADATA_DAILY_LIMIT", 50000)   # суточный лимит тарифа «Лёгкий»
PROSPECT_TTL_DAYS = _opt_int("PROSPECT_TTL_DAYS", 60)        # TTL карточки (рефреш при показе; задел)
```

- [ ] **Шаг 2: Написать падающий unit-смоук** `scripts/dadata_smoke.py` (чистый, БЕЗ сети):

```python
#!/usr/bin/env python3
"""Unit-смоук провайдера dadata.py: парсинг ЮЛ/ИП + вырезание телефонов/email.
Без сети (проверяет чистые _parse_party/_sanitize на образце ответа DaData).
  PYTHONPATH=. ./.venv-smoke/bin/python scripts/dadata_smoke.py"""
import os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "admin-panel"))
import dadata

FAILS = []
def check(name, cond, detail=""):
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)

# Образец ответа find-party для ЮЛ (с телефонами/email — должны быть вырезаны)
LEGAL = {
    "type": "LEGAL", "inn": "7707083893", "kpp": "770701001", "ogrn": "1027700132195",
    "name": {"short_with_opf": "ООО «РОГА»", "full_with_opf": "ОБЩЕСТВО ... «РОГА»", "short": "РОГА"},
    "opf": {"short": "ООО"}, "okved": "62.01",
    "okveds": [{"main": True, "code": "62.01", "name": "Разработка ПО"}],
    "address": {"value": "г Москва, ул Тверская, 1", "data": {"region": "Москва", "city": "Москва"}},
    "state": {"status": "ACTIVE", "registration_date": 1046649600000, "liquidation_date": None},
    "management": {"name": "Иванов Иван Иванович", "post": "ГЕНДИРЕКТОР"},
    "phones": [{"value": "+7 495 1234567"}], "emails": [{"value": "info@roga.ru"}],
}
# Образец для ИП (ФИО = ПДн; адрес — только город)
INDIVID = {
    "type": "INDIVIDUAL", "inn": "500100732259", "ogrn": "304500116000157",
    "name": {"full": "ПЕТРОВ ПЁТР ПЕТРОВИЧ"}, "fio": {"surname": "Петров", "name": "Пётр", "patronymic": "Петрович"},
    "okved": "47.91", "okveds": [{"main": True, "code": "47.91", "name": "Розница"}],
    "address": {"value": "г Казань", "data": {"region": "Татарстан", "city": "Казань"}},
    "state": {"status": "ACTIVE", "registration_date": 1046649600000},
}

leg = dadata._parse_party(LEGAL)
check("ЮЛ: subject_type=legal", leg.subject_type == "legal")
check("ЮЛ: inn/ogrn/kpp", leg.inn == "7707083893" and leg.ogrn == "1027700132195" and leg.kpp == "770701001")
check("ЮЛ: имя/ОПФ/ОКВЭД", leg.name_short == "ООО «РОГА»" and leg.opf == "ООО" and leg.okved == "62.01")
check("ЮЛ: okved_name основной", leg.okved_name == "Разработка ПО")
check("ЮЛ: город/регион/статус", leg.city == "Москва" and leg.region == "Москва" and leg.status == "ACTIVE")
check("ЮЛ: дата регистрации ISO", leg.registration_date == "2003-03-03")
check("ЮЛ: руководитель сохранён", (leg.management or {}).get("name") == "Иванов Иван Иванович")
check("ЮЛ: телефоны ВЫРЕЗАНЫ из raw", "phones" not in leg.raw)
check("ЮЛ: email ВЫРЕЗАНЫ из raw", "emails" not in leg.raw)
check("ЮЛ: в карточке нет полей-контактов", not hasattr(leg, "phones") and not hasattr(leg, "emails"))

ind = dadata._parse_party(INDIVID)
check("ИП: subject_type=individual", ind.subject_type == "individual")
check("ИП: management НЕ ставим (ПДн)", ind.management is None)
check("ИП: имя из ЕГРИП сохранено", ind.name_full == "ПЕТРОВ ПЁТР ПЕТРОВИЧ")
check("ИП: город есть, полного адреса места жительства нет", ind.city == "Казань")

print(("\nFAIL: " + ", ".join(FAILS)) if FAILS else "\nВсе проверки dadata OK")
sys.exit(1 if FAILS else 0)
```

- [ ] **Шаг 3: Запустить смоук — убедиться, что падает**

Run: `cd /Users/konstantin/Downloads/risuy-ecosystem && PYTHONPATH=. ./.venv-smoke/bin/python scripts/dadata_smoke.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'dadata'`.

- [ ] **Шаг 4: Реализовать `admin-panel/dadata.py`**:

```python
"""DaData-провайдер обогащения по ЕГРЮЛ/ЕГРИП (per-lookup). Паттерн mailer.py:
is_configured() + graceful-degrade. Телефоны/email/закрытые категории вырезаются
ДО возврата (комплаенс §8 спеки). find-party — точный поиск по ИНН/ОГРН; suggest —
интерактивный ручной поиск по названию (оферта DaData 4.2.1 запрещает автообработку suggest).
HTTP — stdlib urllib в to_thread (без новых зависимостей; для per-lookup достаточно)."""
from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone

import config

log = logging.getLogger("dadata")

_FIND_URL = "https://suggestions.dadata.ru/suggestions/api/4_1/rs/findById/party"
_SUGGEST_URL = "https://suggestions.dadata.ru/suggestions/api/4_1/rs/suggest/party"
_STRIP_KEYS = ("phones", "emails")  # контакты — НЕ храним/не показываем


def is_configured() -> bool:
    """DaData настроен для реальных запросов (нужны токен + секрет для find-party)?"""
    return bool(config.DADATA_API_KEY and config.DADATA_SECRET_KEY)


@dataclass
class ProspectCard:
    inn: str
    subject_type: str            # 'legal' | 'individual'
    kpp: str | None = None
    ogrn: str | None = None
    name_short: str | None = None
    name_full: str | None = None
    opf: str | None = None
    okved: str | None = None
    okved_name: str | None = None
    okveds: list | None = None
    address: str | None = None
    region: str | None = None
    city: str | None = None
    status: str | None = None
    registration_date: str | None = None   # ISO 'YYYY-MM-DD' | None
    liquidation_date: str | None = None
    management: dict | None = None
    raw: dict = field(default_factory=dict)  # санитизированный (без контактов)


def _sanitize(data: dict) -> dict:
    """Копия data без контактов/закрытых категорий (защита до записи)."""
    return {k: v for k, v in data.items() if k not in _STRIP_KEYS}


def _epoch_ms_to_date(v) -> str | None:
    if not v:
        return None
    try:
        return datetime.fromtimestamp(int(v) / 1000, tz=timezone.utc).date().isoformat()
    except (ValueError, TypeError, OSError):
        return None


def _main_okved_name(okveds) -> str | None:
    if not okveds:
        return None
    for o in okveds:
        if o.get("main"):
            return o.get("name")
    return okveds[0].get("name")


def _parse_party(data: dict) -> ProspectCard:
    """DaData data-объект → ProspectCard (санитизированный, без телефонов/email)."""
    name = data.get("name") or {}
    opf = data.get("opf") or {}
    addr = data.get("address") if isinstance(data.get("address"), dict) else {}
    addr_data = addr.get("data") or {}
    state = data.get("state") or {}
    stype = "individual" if data.get("type") == "INDIVIDUAL" else "legal"
    return ProspectCard(
        inn=data.get("inn") or "",
        subject_type=stype,
        kpp=data.get("kpp"),
        ogrn=data.get("ogrn"),
        name_short=(name.get("short_with_opf") or name.get("short") or data.get("value")),
        name_full=(name.get("full_with_opf") or name.get("full")),
        opf=opf.get("short"),
        okved=data.get("okved"),
        okved_name=_main_okved_name(data.get("okveds")),
        okveds=data.get("okveds"),
        address=addr.get("value"),
        region=addr_data.get("region"),
        city=addr_data.get("city") or addr_data.get("settlement"),
        status=state.get("status"),
        registration_date=_epoch_ms_to_date(state.get("registration_date")),
        liquidation_date=_epoch_ms_to_date(state.get("liquidation_date")),
        management=(data.get("management") if stype == "legal" else None),
        raw=_sanitize(data),
    )


def _post(url: str, payload: dict) -> dict:
    """Синхронный POST (в to_thread). stdlib — без новых зависимостей."""
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    req.add_header("Authorization", f"Token {config.DADATA_API_KEY}")
    req.add_header("X-Secret", config.DADATA_SECRET_KEY)
    with urllib.request.urlopen(req, timeout=config.DADATA_TIMEOUT_SEC) as resp:
        return json.loads(resp.read().decode("utf-8"))


async def find_party(query: str) -> ProspectCard | None:
    """Точный поиск по ИНН/ОГРН (findById). None — не настроено/не найдено/ошибка."""
    if not is_configured():
        return None
    q = (query or "").strip()
    if not q or len(q) > 300:
        return None
    try:
        resp = await asyncio.to_thread(_post, _FIND_URL, {"query": q})
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as e:
        log.warning("DaData find-party ошибка: %s", e)
        return None
    sugg = (resp or {}).get("suggestions") or []
    return _parse_party(sugg[0].get("data") or {}) if sugg else None


async def suggest_party(query: str, count: int = 7) -> list[dict]:
    """Интерактивный поиск по названию (suggest). Телефоны/email тут и так null."""
    if not is_configured():
        return []
    q = (query or "").strip()
    if not q or len(q) > 300:
        return []
    try:
        resp = await asyncio.to_thread(_post, _SUGGEST_URL, {"query": q, "count": max(1, min(count, 20))})
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as e:
        log.warning("DaData suggest ошибка: %s", e)
        return []
    out = []
    for s in (resp or {}).get("suggestions") or []:
        d = s.get("data") or {}
        addr = d.get("address") if isinstance(d.get("address"), dict) else {}
        out.append({
            "inn": d.get("inn") or "",
            "name": s.get("value") or "",
            "city": (addr.get("data") or {}).get("city") or "",
            "status": (d.get("state") or {}).get("status") or "",
        })
    return out
```

- [ ] **Шаг 5: Запустить смоук — убедиться, что проходит**

Run: `cd /Users/konstantin/Downloads/risuy-ecosystem && PYTHONPATH=. ./.venv-smoke/bin/python scripts/dadata_smoke.py`
Expected: PASS — `Все проверки dadata OK`.

- [ ] **Шаг 6: py_compile**

Run: `cd /Users/konstantin/Downloads/risuy-ecosystem && ./.venv-smoke/bin/python -m py_compile admin-panel/dadata.py admin-panel/config.py`
Expected: без вывода (успех).

- [ ] **Шаг 7: Коммит**

```bash
git add admin-panel/dadata.py admin-panel/config.py scripts/dadata_smoke.py
git commit -m "feat(panel): dadata.py — провайдер обогащения по ЕГРЮЛ (find-party, вырезание контактов)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: db-хелперы `prospect_*` + quota-guard + RLS-смоук на `risuy_dev`

**Files:**
- Modify: `admin-panel/db.py`
- Create: `scripts/prospects_db_smoke.py`

**Interfaces:**
- Consumes: `dadata.ProspectCard`; `pool`, `_insert_audit`, `_json_default` (db.py); таблица `prospects` (Task 1).
- Produces: `async db.prospect_upsert(*, card, tenant_id, actor, ip, user_agent, lead_id=None) -> str`; `async db.prospect_list(include_archived=False) -> list[Record]`; `async db.prospect_get(pid) -> Record|None`; `async db.prospect_for_lead(lead_id) -> Record|None`; `async db.prospect_link_lead(pid, lead_id, *, actor, ip, user_agent) -> bool`; `async db.prospect_archive(pid, *, actor, ip, user_agent) -> bool`; `async db.dadata_quota_take(limit:int) -> bool`.

- [ ] **Шаг 1: Написать падающий RLS-смоук** `scripts/prospects_db_smoke.py` (гард `risuy_dev`):

```python
#!/usr/bin/env python3
"""RLS-смоук prospects на risuy_dev: изоляция A≠B, unique(tenant_id,inn),
lead_id→set null, отсутствие полей-контактов. Пишет/чистит тестовые строки.
  PROSPECTS_SMOKE_DSN="postgresql://gen_user:...@81.31.246.136:5432/risuy_dev?sslmode=require" \
  PYTHONPATH=. ./.venv-smoke/bin/python scripts/prospects_db_smoke.py"""
import asyncio, os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "admin-panel"))
os.environ.setdefault("DATABASE_URL", "postgresql://x/y")
os.environ.setdefault("SESSION_SECRET", "smoke-secret-only-for-import-aaaaaaaa")
os.environ.setdefault("ADMIN_USERNAME", "smoke")
os.environ.setdefault("ADMIN_PASSWORD_HASH", "$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHQ$aGFzaHN0dWI")

import asyncpg
import db
import dadata

DSN = os.environ.get("PROSPECTS_SMOKE_DSN")
if not DSN or "/risuy_dev" not in DSN.split("?")[0]:
    raise SystemExit("Задайте PROSPECTS_SMOKE_DSN на risuy_dev (тест пишет/чистит строки).")

FAILS = []
def check(name, cond, detail=""):
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)

CARD_A = dadata.ProspectCard(inn="7707083893", subject_type="legal", name_short="ООО А",
                             city="Москва", status="ACTIVE", raw={"inn": "7707083893"})
CARD_B = dadata.ProspectCard(inn="7707083893", subject_type="legal", name_short="ООО Б (тенант B)",
                             city="Казань", status="ACTIVE", raw={"inn": "7707083893"})

async def main():
    db.pool = await asyncpg.create_pool(DSN, min_size=1, max_size=4, setup=db._apply_tenant_guc)
    async with db.pool.acquire() as c:
        ta = await c.fetchval("insert into tenants (slug, name, status) values "
                              "('smoke-prospect-a-'||substr(md5(random()::text),1,8),'SMOKE A','active') returning id")
        tb = await c.fetchval("insert into tenants (slug, name, status) values "
                              "('smoke-prospect-b-'||substr(md5(random()::text),1,8),'SMOKE B','active') returning id")
        lead_a = await c.fetchval("insert into leads (tenant_id, name, consent) values ($1,'Лид A',true) returning id", ta)
    try:
        # upsert под тенантом A
        db.set_active_tenant(ta)
        pid_a = await db.prospect_upsert(card=CARD_A, tenant_id=ta, actor="smoke", ip=None,
                                         user_agent=None, lead_id=lead_a)
        rows_a = await db.prospect_list()
        check("A видит свою карточку", len(rows_a) == 1 and rows_a[0]["inn"] == "7707083893")
        p_for_lead = await db.prospect_for_lead(lead_a)
        check("A: карточка привязана к лиду", p_for_lead is not None and str(p_for_lead["id"]) == pid_a)

        # тот же ИНН под тенантом B — отдельная строка (unique per-tenant), А не видит B
        db.set_active_tenant(tb)
        pid_b = await db.prospect_upsert(card=CARD_B, tenant_id=tb, actor="smoke", ip=None, user_agent=None)
        rows_b = await db.prospect_list()
        check("B видит ТОЛЬКО свою карточку", len(rows_b) == 1 and rows_b[0]["name_short"] == "ООО Б (тенант B)")
        check("A≠B: разные id для того же ИНН", pid_a != pid_b)

        # A НЕ видит карточку B (RLS)
        db.set_active_tenant(ta)
        got_b = await db.prospect_get(pid_b)
        check("A НЕ видит карточку B (RLS)", got_b is None)

        # lead_id → set null при удалении лида
        async with db.pool.acquire() as c:
            await c.execute("delete from leads where id=$1", lead_a)
        again = await db.prospect_get(pid_a)
        check("удаление лида → prospect.lead_id = NULL (карточка жива)", again is not None and again["lead_id"] is None)

        # схема не содержит полей-контактов
        async with db.pool.acquire() as c:
            cols = {r["column_name"] for r in await c.fetch(
                "select column_name from information_schema.columns where table_name='prospects'")}
        check("нет колонок под телефоны/email/паспорт", not (cols & {"phones","emails","phone","passport"}))
    finally:
        async with db.pool.acquire() as c:  # чистка (owner обходит RLS)
            await c.execute("delete from prospects where tenant_id = any($1::uuid[])", [ta, tb])
            await c.execute("delete from tenants where id = any($1::uuid[])", [ta, tb])
        await db.pool.close()
    print(("\nFAIL: " + ", ".join(FAILS)) if FAILS else "\nВсе проверки prospects OK")
    sys.exit(1 if FAILS else 0)

asyncio.run(main())
```

- [ ] **Шаг 2: Запустить смоук — убедиться, что падает**

Run: `cd /Users/konstantin/Downloads/risuy-ecosystem && PROSPECTS_SMOKE_DSN="<owner-dsn-risuy_dev>" PYTHONPATH=. ./.venv-smoke/bin/python scripts/prospects_db_smoke.py`
Expected: FAIL — `AttributeError: module 'db' has no attribute 'prospect_upsert'`.

- [ ] **Шаг 3: Реализовать хелперы в `admin-panel/db.py`** (добавить в конец файла; `json`, `_insert_audit`, `_json_default`, `pool` уже есть):

```python
# ── prospects: карточки компаний ЕГРЮЛ (обогащение по ИНН, per-lookup) ───────
def _jsonb(v):
    """Python → jsonb-строка (или None для SQL NULL)."""
    return json.dumps(v, ensure_ascii=False, default=_json_default) if v else None


async def prospect_upsert(*, card, tenant_id, actor, ip, user_agent, lead_id=None) -> str:
    """Upsert карточки по (tenant_id, inn). card — dadata.ProspectCard (контакты уже вырезаны).
    Повторный lookup обновляет реквизиты; ранее привязанный lead_id сохраняется. Возвращает id (str)."""
    async with pool.acquire() as c:
        async with c.transaction():
            pid = await c.fetchval(
                """
                insert into prospects (tenant_id, inn, kpp, ogrn, subject_type, name_short, name_full,
                    opf, okved, okved_name, okveds, address, region, city, status,
                    registration_date, liquidation_date, management, source, raw, fetched_at,
                    lead_id, created_by)
                values ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11::jsonb,$12,$13,$14,$15,$16::date,$17::date,
                    $18::jsonb,'dadata',$19::jsonb, now(), $20, $21)
                on conflict (tenant_id, inn) do update set
                    kpp=excluded.kpp, ogrn=excluded.ogrn, subject_type=excluded.subject_type,
                    name_short=excluded.name_short, name_full=excluded.name_full, opf=excluded.opf,
                    okved=excluded.okved, okved_name=excluded.okved_name, okveds=excluded.okveds,
                    address=excluded.address, region=excluded.region, city=excluded.city,
                    status=excluded.status, registration_date=excluded.registration_date,
                    liquidation_date=excluded.liquidation_date, management=excluded.management,
                    raw=excluded.raw, fetched_at=now(), updated_at=now(),
                    lead_id=coalesce(prospects.lead_id, excluded.lead_id)
                returning id
                """,
                tenant_id, card.inn, card.kpp, card.ogrn, card.subject_type, card.name_short,
                card.name_full, card.opf, card.okved, card.okved_name, _jsonb(card.okveds),
                card.address, card.region, card.city, card.status,
                card.registration_date, card.liquidation_date, _jsonb(card.management),
                _jsonb(card.raw), lead_id, actor,
            )
            await _insert_audit(c, actor=actor, action="prospect_upsert", lead_id=lead_id,
                                ip=ip, user_agent=user_agent,
                                detail={"inn": card.inn, "subject_type": card.subject_type})
    return str(pid)


async def prospect_list(include_archived: bool = False) -> list[asyncpg.Record]:
    async with pool.acquire() as c:
        return await c.fetch(
            """
            select id, inn, subject_type, name_short, okved_name, city, status, lead_id, archived,
                   to_char(fetched_at,'YYYY-MM-DD HH24:MI') as fetched
              from prospects
             where tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid
               and ($1 or not archived)
             order by created_at desc
            """, include_archived)


async def prospect_get(pid) -> asyncpg.Record | None:
    async with pool.acquire() as c:
        return await c.fetchrow(
            "select * from prospects where id = $1 "
            "and tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid", pid)


async def prospect_for_lead(lead_id) -> asyncpg.Record | None:
    async with pool.acquire() as c:
        return await c.fetchrow(
            "select * from prospects where lead_id = $1 "
            "and tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid "
            "and not archived order by updated_at desc limit 1", lead_id)


async def prospect_link_lead(pid, lead_id, *, actor, ip, user_agent) -> bool:
    async with pool.acquire() as c:
        async with c.transaction():
            row = await c.fetchrow(
                "update prospects set lead_id = $2, updated_at = now() where id = $1 "
                "and tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid returning id",
                pid, lead_id)
            if row is None:
                return False
            await _insert_audit(c, actor=actor, action="prospect_link_lead", lead_id=lead_id,
                                ip=ip, user_agent=user_agent, detail={"prospect_id": str(pid)})
            return True


async def prospect_archive(pid, *, actor, ip, user_agent) -> bool:
    async with pool.acquire() as c:
        async with c.transaction():
            row = await c.fetchrow(
                "update prospects set archived = true, updated_at = now() where id = $1 "
                "and tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid returning id", pid)
            if row is None:
                return False
            await _insert_audit(c, actor=actor, action="prospect_archive",
                                ip=ip, user_agent=user_agent, detail={"prospect_id": str(pid)})
            return True


async def dadata_quota_take(limit: int) -> bool:
    """Атомарный инкремент глобального суточного счётчика запросов DaData (app_settings).
    True — в пределах лимита; False — исчерпан. Ключ dadata_quota__<YYYY-MM-DD> (UTC).
    ⚠️ ПЕРЕД реализацией сверить схему: app_settings(key text pk, value text) → value::int корректен."""
    from datetime import datetime, timezone
    key = "dadata_quota__" + datetime.now(timezone.utc).date().isoformat()
    async with pool.acquire() as c:
        cur = await c.fetchval(
            "insert into app_settings (key, value) values ($1, '1') "
            "on conflict (key) do update set value = (app_settings.value::int + 1)::text "
            "returning value::int", key)
        return cur <= limit
```

- [ ] **Шаг 4: Запустить смоук — убедиться, что проходит**

Run: `cd /Users/konstantin/Downloads/risuy-ecosystem && PROSPECTS_SMOKE_DSN="<owner-dsn-risuy_dev>" PYTHONPATH=. ./.venv-smoke/bin/python scripts/prospects_db_smoke.py`
Expected: PASS — `Все проверки prospects OK`.

> ⚠️ Если `dadata_quota_take` упадёт на `value::int` — сверить `\d app_settings` (Шаг 3-комментарий): при `value jsonb` заменить на `(app_settings.value::text::int + 1)` либо хранить счётчик в отдельном ключе-числе.

- [ ] **Шаг 5: py_compile + коммит**

Run: `cd /Users/konstantin/Downloads/risuy-ecosystem && ./.venv-smoke/bin/python -m py_compile admin-panel/db.py`
```bash
git add admin-panel/db.py scripts/prospects_db_smoke.py
git commit -m "feat(panel): db-хелперы prospect_* + quota-guard DaData (RLS-изоляция, upsert, привязка к лиду)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Раздел `/companies` (GET) + шаблоны + навигация + render-смоук

**Files:**
- Modify: `admin-panel/app.py` (маршрут `companies_page` + хелпер `_companies_err_text`)
- Create: `admin-panel/templates/companies.html`, `admin-panel/templates/_company_card.html`
- Modify: `admin-panel/templates/base.html` (nav-пункт + иконка)
- Create: `scripts/prospects_ui_smoke.py`

**Interfaces:**
- Consumes: `require_session`, `templates`, `_help_dismissed`, `_safe_support_url`, `db.prospect_list`, `dadata.is_configured`, `config.SUPPORT_URL`.
- Produces: `GET /companies` → `companies.html`; партиал `_company_card.html`; nav-ключ `companies`.

- [ ] **Шаг 1: Написать падающий render-смоук** `scripts/prospects_ui_smoke.py`:

```python
#!/usr/bin/env python3
"""Render-смоук шаблонов раздела «Компании» (чистый Jinja, без БД/HTTP).
  PYTHONPATH=. ./.venv-smoke/bin/python scripts/prospects_ui_smoke.py"""
import os, sys
from jinja2 import ChainableUndefined, Environment, FileSystemLoader, select_autoescape

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
env = Environment(
    loader=FileSystemLoader(os.path.join(ROOT, "admin-panel", "templates")),
    autoescape=select_autoescape(["html", "xml"]), undefined=ChainableUndefined)

def render(name, **ctx):
    return env.get_template(name).render(**ctx)

FAILS = []
def check(name, cond):
    print(f"  {'OK ' if cond else 'FAIL'} {name}")
    if not cond:
        FAILS.append(name)

# провайдер подключён, есть сохранённые карточки
html = render("companies.html", csrf_token="TESTCSRF", active="companies", has_tenant=True,
              provider_on=True, prospects=[{"id":"1","inn":"7707083893","subject_type":"legal",
              "name_short":"ООО А","okved_name":"Разработка ПО","city":"Москва","status":"ACTIVE",
              "lead_id":None,"archived":False,"fetched":"2026-07-01 10:00"}],
              suggestions=[], search_q="", err="", saved=0, support_url="https://t.me/x", help_dismissed=True)
check("форма поиска по ИНН → /companies/lookup", 'action="/companies/lookup"' in html and 'name="inn"' in html)
check("форма поиска по названию → /companies/search", 'action="/companies/search"' in html)
check("CSRF-поле есть", 'name="csrf_token"' in html and "TESTCSRF" in html)
check("карточка сохранённой компании видна", "ООО А" in html and "7707083893" in html)
check("нет телефона в разметке", "phone" not in html.lower() and "телефон" not in html.lower())

# провайдер не подключён → плашка
html2 = render("companies.html", csrf_token="T", active="companies", has_tenant=True, provider_on=False,
               prospects=[], suggestions=[], search_q="", err="", saved=0, support_url="", help_dismissed=True)
check("provider_off → плашка «источник не подключён»", "не подключён" in html2)

# без тенанта → подсказка выбрать клиента
html3 = render("companies.html", csrf_token="T", active="companies", has_tenant=False, provider_on=True,
               prospects=[], suggestions=[], search_q="", err="", saved=0, support_url="", help_dismissed=True)
check("без тенанта → раздел не даёт поиск", "выберите клиента" in html3.lower() or 'name="inn"' not in html3)

# партиал карточки
card = render("_company_card.html", p={"inn":"7707083893","subject_type":"legal","name_short":"ООО А",
              "name_full":"ОБЩЕСТВО","opf":"ООО","okved":"62.01","okved_name":"Разработка ПО",
              "address":"г Москва","city":"Москва","status":"ACTIVE","registration_date":"2003-03-03",
              "management":{"name":"Иванов И.И.","post":"Директор"}}, csrf_token="T", back="/companies")
check("партиал: реквизиты ЮЛ", "7707083893" in card and "62.01" in card and "ACTIVE" in card)

print(("\nFAIL: " + ", ".join(FAILS)) if FAILS else "\nВсе render-проверки OK")
sys.exit(1 if FAILS else 0)
```

- [ ] **Шаг 2: Запустить — убедиться, что падает**

Run: `cd /Users/konstantin/Downloads/risuy-ecosystem && PYTHONPATH=. ./.venv-smoke/bin/python scripts/prospects_ui_smoke.py`
Expected: FAIL — `jinja2.exceptions.TemplateNotFound: companies.html`.

- [ ] **Шаг 3: Создать `admin-panel/templates/_company_card.html`** (партиал карточки, переиспользуется в диалогах):

```jinja2
{# Карточка компании ЕГРЮЛ. p — строка prospects или ProspectCard. БЕЗ телефонов/email. #}
<div class="company-card">
  <div class="company-card__head">
    <b>{{ p.name_short or p.name_full or "—" }}</b>
    {% if p.status %}<span class="pill pill--{{ 'ok' if p.status == 'ACTIVE' else 'muted' }}">{{ p.status }}</span>{% endif %}
  </div>
  <dl class="company-card__grid">
    <dt>ИНН</dt><dd class="mono">{{ p.inn }}</dd>
    {% if p.kpp %}<dt>КПП</dt><dd class="mono">{{ p.kpp }}</dd>{% endif %}
    {% if p.ogrn %}<dt>ОГРН</dt><dd class="mono">{{ p.ogrn }}</dd>{% endif %}
    {% if p.opf %}<dt>ОПФ</dt><dd>{{ p.opf }}</dd>{% endif %}
    {% if p.okved %}<dt>ОКВЭД</dt><dd>{{ p.okved }}{% if p.okved_name %} — {{ p.okved_name }}{% endif %}</dd>{% endif %}
    {% if p.address %}<dt>Адрес</dt><dd>{{ p.address }}</dd>{% endif %}
    {% if p.registration_date %}<dt>Регистрация</dt><dd>{{ p.registration_date }}</dd>{% endif %}
    {% if p.subject_type == 'legal' and p.management and p.management.name %}
      <dt>Руководитель</dt><dd>{{ p.management.name }}{% if p.management.post %}, {{ p.management.post }}{% endif %}</dd>
    {% endif %}
    {% if p.subject_type == 'individual' %}<dt>Форма</dt><dd>ИП (персональные данные — режим 152-ФЗ)</dd>{% endif %}
  </dl>
</div>
```

- [ ] **Шаг 4: Создать `admin-panel/templates/companies.html`**:

```jinja2
{% extends "base.html" %}
{% from "_macros.html" import help_card %}
{% block content %}
<h1 class="page-title">База компаний</h1>

{% if not has_tenant %}
  <p class="empty-state">Выберите клиента в разделе «Клиенты» — база компаний ведётся в кабинете клиента.</p>
{% else %}

  {% if not help_dismissed %}
    {{ help_card("Зачем «База компаний»",
        "Обогащайте карточки согласившихся лидов данными из ЕГРЮЛ по ИНН: форма, ОКВЭД, адрес, статус. Это аналитика и обогащение — не список для обзвона. Первый контакт с бизнесом — только через воронку согласия.",
        dismiss_key="help_dismissed__companies", csrf_token=csrf_token, back="/companies") }}
  {% endif %}

  {% if not provider_on %}
    <p class="notice notice--muted">Источник ЕГРЮЛ (DaData) не подключён. Задайте DADATA_API_KEY / DADATA_SECRET_KEY.</p>
  {% else %}
    {% if err %}<p class="notice notice--err">{{ err }}</p>{% endif %}
    {% if saved %}<p class="notice notice--ok">Карточка сохранена.</p>{% endif %}

    <form method="post" action="/companies/lookup" class="row">
      <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
      <input type="hidden" name="back" value="/companies">
      <input class="mono" type="text" name="inn" inputmode="numeric" maxlength="15"
             placeholder="ИНН или ОГРН" required>
      <button type="submit">Найти по ИНН</button>
    </form>

    <form method="post" action="/companies/search" class="row">
      <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
      <input type="text" name="q" maxlength="300" value="{{ search_q }}" placeholder="Название компании">
      <button type="submit">Поиск по названию</button>
    </form>

    {% if suggestions %}
      <ul class="suggest-list">
        {% for s in suggestions %}
          <li>
            <span>{{ s.name }} <span class="mono muted">{{ s.inn }}</span>{% if s.city %}, {{ s.city }}{% endif %}</span>
            <form method="post" action="/companies/lookup" class="inline">
              <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
              <input type="hidden" name="inn" value="{{ s.inn }}">
              <input type="hidden" name="back" value="/companies">
              <button type="submit">Подтянуть</button>
            </form>
          </li>
        {% endfor %}
      </ul>
    {% endif %}
  {% endif %}

  <h2>Сохранённые компании</h2>
  {% if prospects %}
    <table class="tbl">
      <thead><tr><th>Компания</th><th>ИНН</th><th>ОКВЭД</th><th>Город</th><th>Статус</th><th>Лид</th><th></th></tr></thead>
      <tbody>
      {% for p in prospects %}
        <tr>
          <td>{{ p.name_short or "—" }}{% if p.subject_type == 'individual' %} <span class="pill pill--muted">ИП</span>{% endif %}</td>
          <td class="mono">{{ p.inn }}</td>
          <td>{{ p.okved_name or "—" }}</td>
          <td>{{ p.city or "—" }}</td>
          <td>{{ p.status or "—" }}</td>
          <td>{{ "да" if p.lead_id else "—" }}</td>
          <td>
            <form method="post" action="/companies/{{ p.id }}/archive" class="inline">
              <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
              <button type="submit" class="btn-link">В архив</button>
            </form>
          </td>
        </tr>
      {% endfor %}
      </tbody>
    </table>
  {% else %}
    <p class="empty-state">Пока нет сохранённых компаний. Найдите по ИНН выше.</p>
  {% endif %}

  {% if support_url %}<p class="muted"><a href="{{ support_url }}">Нужна помощь?</a></p>{% endif %}
{% endif %}
{% endblock %}
```

- [ ] **Шаг 5: Добавить nav-пункт «Компании» в `admin-panel/templates/base.html`** — в `NAV_TITLES` добавить `'companies': 'Компании',`; в навигацию (виден обоим контурам, как «Базы знаний») — в ОБЕИХ ветках (`is_platform` и клиентской) добавить:

```jinja2
{{ nav_item('companies', '/companies', 'Компании', active) }}
```

И в макро `nav_icon` добавить ветку:

```jinja2
{%- elif name == 'companies' -%}<rect x="3" y="3" width="18" height="18" rx="2"/><line x1="9" y1="7" x2="9" y2="17"/><line x1="15" y1="7" x2="15" y2="17"/>
```

- [ ] **Шаг 6: Реализовать маршрут в `admin-panel/app.py`** (рядом с `knowledge_page`; добавить хелпер ошибок):

```python
def _companies_err_text(err: str | None) -> str:
    return {
        "bad_inn": "Неверный ИНН/ОГРН — введите 10, 12, 13 или 15 цифр.",
        "not_found": "Компания по такому ИНН не найдена.",
        "provider_off": "Источник ЕГРЮЛ не подключён.",
        "quota": "Дневной лимит запросов к источнику исчерпан. Попробуйте завтра.",
    }.get(err or "", "")


@app.get("/companies")
async def companies_page(
    request: Request,
    session: auth.Session = Depends(require_session),
    saved: int = 0,
    err: str | None = None,
):
    tid = session.active_tenant_id
    prospects = await db.prospect_list() if tid else []
    return templates.TemplateResponse(
        request, "companies.html",
        {
            "csrf_token": session.csrf_token,
            "session": session,
            "active": "companies",
            "has_tenant": bool(tid),
            "provider_on": dadata.is_configured(),
            "prospects": prospects,
            "suggestions": [],
            "search_q": "",
            "err": _companies_err_text(err),
            "saved": saved,
            "support_url": _safe_support_url(config.SUPPORT_URL),
            "help_dismissed": await _help_dismissed(session, "companies"),
        },
    )
```

И добавить импорт `import dadata` в шапку `app.py` (рядом с `import db`).

- [ ] **Шаг 7: Запустить render-смоук — PASS + py_compile**

Run: `cd /Users/konstantin/Downloads/risuy-ecosystem && PYTHONPATH=. ./.venv-smoke/bin/python scripts/prospects_ui_smoke.py && ./.venv-smoke/bin/python -m py_compile admin-panel/app.py`
Expected: `Все render-проверки OK` + без ошибок компиляции.

- [ ] **Шаг 8: Коммит**

```bash
git add admin-panel/app.py admin-panel/templates/companies.html admin-panel/templates/_company_card.html admin-panel/templates/base.html scripts/prospects_ui_smoke.py
git commit -m "feat(panel): раздел «База компаний» (GET) — поиск по ИНН/названию, список, nav, help_card

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: POST-маршруты `/companies/*` (lookup, search, save-link, archive)

**Files:**
- Modify: `admin-panel/app.py`

**Interfaces:**
- Consumes: `_enforce_csrf`, `_ip`, `_ua`, `_safe_next`, `dadata.find_party/suggest_party/is_configured`, `db.prospect_upsert/prospect_link_lead/prospect_archive/dadata_quota_take`, `config.DADATA_DAILY_LIMIT`.
- Produces: `POST /companies/lookup`, `POST /companies/search`, `POST /companies/{pid}/link-lead`, `POST /companies/{pid}/archive`.

- [ ] **Шаг 1: Расширить render-смоук проверкой ветки поиска** — в `scripts/prospects_ui_smoke.py` добавить перед итогом:

```python
htmls = render("companies.html", csrf_token="T", active="companies", has_tenant=True, provider_on=True,
               prospects=[], search_q="рога",
               suggestions=[{"inn":"7707083893","name":"ООО РОГА","city":"Москва","status":"ACTIVE"}],
               err="", saved=0, support_url="", help_dismissed=True)
check("подсказки: строка с кнопкой «Подтянуть»", "ООО РОГА" in htmls and "Подтянуть" in htmls)
```

Run: `PYTHONPATH=. ./.venv-smoke/bin/python scripts/prospects_ui_smoke.py` → Expected: PASS (шаблон из Task 4 уже рендерит suggestions).

- [ ] **Шаг 2: Реализовать POST-маршруты в `admin-panel/app.py`**:

```python
_INN_LENGTHS = frozenset({10, 12, 13, 15})  # ИНН ЮЛ 10 / ИП 12 / ОГРН 13 / ОГРНИП 15


@app.post("/companies/lookup")
async def companies_lookup(
    request: Request,
    session: auth.Session = Depends(require_session),
    inn: str = Form(""),
    lead_id: str = Form(""),
    back: str = Form("/companies"),
    csrf_token: str = Form(""),
):
    await _enforce_csrf(request, session, csrf_token)
    tid = session.active_tenant_id
    dest = _safe_next(back)
    if not tid:
        return RedirectResponse(url="/companies", status_code=303)
    if not dadata.is_configured():
        return RedirectResponse(url=f"{dest}?err=provider_off", status_code=303)
    q = (inn or "").strip()
    if not q.isdigit() or len(q) not in _INN_LENGTHS:
        return RedirectResponse(url=f"{dest}?err=bad_inn", status_code=303)
    if not await db.dadata_quota_take(config.DADATA_DAILY_LIMIT):
        return RedirectResponse(url=f"{dest}?err=quota", status_code=303)
    card = await dadata.find_party(q)
    if card is None:
        return RedirectResponse(url=f"{dest}?err=not_found", status_code=303)
    lid = lead_id.strip() or None
    await db.prospect_upsert(card=card, tenant_id=tid, actor=session.actor,
                             ip=_ip(request), user_agent=_ua(request), lead_id=lid)
    return RedirectResponse(url=f"{dest}?saved=1", status_code=303)


@app.post("/companies/search")
async def companies_search(
    request: Request,
    session: auth.Session = Depends(require_session),
    q: str = Form(""),
    csrf_token: str = Form(""),
):
    await _enforce_csrf(request, session, csrf_token)
    tid = session.active_tenant_id
    suggestions = []
    if tid and dadata.is_configured() and (q or "").strip():
        if await db.dadata_quota_take(config.DADATA_DAILY_LIMIT):
            suggestions = await dadata.suggest_party(q)
    prospects = await db.prospect_list() if tid else []
    return templates.TemplateResponse(
        request, "companies.html",
        {
            "csrf_token": session.csrf_token, "session": session, "active": "companies",
            "has_tenant": bool(tid), "provider_on": dadata.is_configured(),
            "prospects": prospects, "suggestions": suggestions, "search_q": q,
            "err": "", "saved": 0, "support_url": _safe_support_url(config.SUPPORT_URL),
            "help_dismissed": await _help_dismissed(session, "companies"),
        },
    )


@app.post("/companies/{pid}/link-lead")
async def companies_link_lead(
    pid: str,
    request: Request,
    session: auth.Session = Depends(require_session),
    lead_id: str = Form(""),
    back: str = Form("/companies"),
    csrf_token: str = Form(""),
):
    await _enforce_csrf(request, session, csrf_token)
    if not session.active_tenant_id:
        return RedirectResponse(url="/companies", status_code=303)
    lid = lead_id.strip() or None
    if lid:
        await db.prospect_link_lead(pid, lid, actor=session.actor, ip=_ip(request), user_agent=_ua(request))
    return RedirectResponse(url=f"{_safe_next(back)}?saved=1", status_code=303)


@app.post("/companies/{pid}/archive")
async def companies_archive(
    pid: str,
    request: Request,
    session: auth.Session = Depends(require_session),
    csrf_token: str = Form(""),
):
    await _enforce_csrf(request, session, csrf_token)
    if session.active_tenant_id:
        await db.prospect_archive(pid, actor=session.actor, ip=_ip(request), user_agent=_ua(request))
    return RedirectResponse(url="/companies", status_code=303)
```

- [ ] **Шаг 3: py_compile + render-смоук**

Run: `cd /Users/konstantin/Downloads/risuy-ecosystem && ./.venv-smoke/bin/python -m py_compile admin-panel/app.py && PYTHONPATH=. ./.venv-smoke/bin/python scripts/prospects_ui_smoke.py`
Expected: без ошибок + `Все render-проверки OK`.

- [ ] **Шаг 4: Коммит**

```bash
git add admin-panel/app.py scripts/prospects_ui_smoke.py
git commit -m "feat(panel): POST-маршруты /companies (lookup по ИНН, поиск по названию, привязка к лиду, архив)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 6: Блок «Компания» в карточке лида (`_render_dialogs`)

**Files:**
- Modify: `admin-panel/app.py` (в `_render_dialogs` подмешать привязанный prospect в контекст)
- Modify: шаблон диалогов (найти по `templates.TemplateResponse(... "dialogs...")` в `_render_dialogs`)

**Interfaces:**
- Consumes: `db.prospect_for_lead(lead_id)`, партиал `_company_card.html`, маршруты Task 5.

- [ ] **Шаг 1: Найти точку рендера карточки лида** — `_render_dialogs` (`admin-panel/app.py:1150`). Определить имя шаблона и переменную открытого лида.

Run: `cd /Users/konstantin/Downloads/risuy-ecosystem && graphify query "_render_dialogs lead card template context selected lead"` затем прочитать нужный фрагмент `app.py:1150`+ и шаблон.

- [ ] **Шаг 2: Подмешать prospect в контекст `_render_dialogs`** — там, где формируется контекст открытого лида, добавить (если есть выбранный лид `lead`):

```python
        lead_company = await db.prospect_for_lead(lead["id"]) if lead else None
        # ... в dict контекста шаблона:
        "lead_company": lead_company,
```

- [ ] **Шаг 3: В шаблоне диалогов добавить блок «Компания»** (в панели открытого лида; `{% from "_company_card.html" import ... %}` не нужен — это include):

```jinja2
<section class="lead-company">
  <h3>Компания</h3>
  {% if lead_company %}
    {% include "_company_card.html" with context %}{# ожидает p; см. ниже #}
  {% else %}
    <form method="post" action="/companies/lookup" class="row">
      <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
      <input type="hidden" name="lead_id" value="{{ lead.id }}">
      <input type="hidden" name="back" value="/dialogs">
      <input class="mono" type="text" name="inn" inputmode="numeric" maxlength="15" placeholder="ИНН компании" required>
      <button type="submit">Привязать по ИНН</button>
    </form>
    <p class="muted">Данные ЕГРЮЛ — для обогащения карточки, не для обзвона.</p>
  {% endif %}
</section>
```

> ⚠️ `_company_card.html` использует переменную `p`. Для `include ... with context` задать `p` перед include: обернуть `{% set p = lead_company %}` перед `{% include %}`, либо в Шаге 2 класть prospect в контекст под именем `p_company` и адаптировать. Проще: `{% with p = lead_company %}{% include "_company_card.html" %}{% endwith %}`.

Итоговый блок с `{% with %}`:

```jinja2
  {% if lead_company %}
    {% with p = lead_company %}{% include "_company_card.html" %}{% endwith %}
  {% else %}
    ...форма...
  {% endif %}
```

- [ ] **Шаг 4: Расширить render-смоук диалогов** — добавить кейс в `scripts/prospects_ui_smoke.py` (или в существующий dialogs-смоук, если есть): отрендерить блок с `lead_company=None` (форма привязки) и с заполненным (карточка). Если рендер целого dialogs-шаблона сложен, ограничиться проверкой партиала `_company_card.html` (уже есть в Task 4) + ручной проверкой на dev-сервере.

- [ ] **Шаг 5: py_compile + смоук + коммит**

Run: `cd /Users/konstantin/Downloads/risuy-ecosystem && ./.venv-smoke/bin/python -m py_compile admin-panel/app.py && PYTHONPATH=. ./.venv-smoke/bin/python scripts/prospects_ui_smoke.py`
```bash
git add admin-panel/app.py admin-panel/templates/<dialogs-template>.html
git commit -m "feat(panel): блок «Компания» в карточке лида — привязка/показ prospect

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 7: Адверсариальное ревью + выкатка (gated)

**Files:** —

- [ ] **Шаг 1: Прогнать все смоуки на `risuy_dev`**:

```bash
cd /Users/konstantin/Downloads/risuy-ecosystem
PYTHONPATH=. ./.venv-smoke/bin/python scripts/dadata_smoke.py
PROSPECTS_SMOKE_DSN="<owner-dsn-risuy_dev>" PYTHONPATH=. ./.venv-smoke/bin/python scripts/prospects_db_smoke.py
PYTHONPATH=. ./.venv-smoke/bin/python scripts/prospects_ui_smoke.py
./.venv-smoke/bin/python -m py_compile admin-panel/dadata.py admin-panel/db.py admin-panel/config.py admin-panel/app.py
```
Expected: все PASS.

- [ ] **Шаг 2: 3-линзовое адверсариальное ревью (Workflow)** — 3 линзы: (1) correctness (upsert/on-conflict, quota-гонки, парсинг DaData); (2) изоляция RLS+tenant (in-query backstop, кросс-тенант); (3) комплаенс-ПДн (вырезание телефонов/email, ИП-гейт, отсутствие рекламных путей). Реальные находки — исправить, смоуки перегнать.

- [ ] **Шаг 3: Прод-DDL `risuy` (ТОЛЬКО по явному «да» владельца)**:

```bash
bash ~/.claude/scripts/twc-migrate.sh 4171827 81.31.246.136 risuy gen_user db/migrate_prospects.sql db/panel_role.sql
```
Проверить: `psql "<owner-dsn-risuy>" -c "select 1 from pg_policies where tablename='prospects';"` → `1`.

- [ ] **Шаг 4: env панели** — владелец заводит аккаунт DaData «Лёгкий» + токен/секрет. Проставить через **API PATCH `/apps/205025` ПОЛНЫМ набором** (UI затирает `run_cmd`): добавить `DADATA_API_KEY`, `DADATA_SECRET_KEY` (+ опц. `DADATA_DAILY_LIMIT`, `PROSPECT_TTL_DAYS`).

- [ ] **Шаг 5: Push + деплой (по «да»)**:

```bash
git push -u origin feat/prospects-egrul   # затем PR/merge в main по решению владельца
```
После merge в `main` — авто-редеплой App Platform; поллить по `app.commit_sha` (`twc apps get 205025 -o json` → `.app.commit_sha`) до совпадения с `git rev-parse HEAD` + status=active. ⚠️ НЕ коммитить graphify-файлы.

- [ ] **Шаг 6: Живая проверка**: `GET /companies` → 200; провайдер подключён; тест-lookup по реальному ИНН (например 7707083893) → карточка без телефонов; привязка к тест-лиду.

- [ ] **Шаг 7: Pre-launch legal gate** (перед боевым включением тенантам, из спеки §14): юр-заключение (основание п.11 ч.1 ст.6 152-ФЗ для ИП; исключения ч.4 ст.18; риск ст.1335.1 п.3), подтверждение канала DaData (письмо поддержки о хранении), политика ПДн + уведомление ст.18 ч.3.

---

## Self-Review (проверка плана против спеки)

**Покрытие спеки:**
- §4 источник DaData → Task 2 (dadata.py) + Task 7 (env, тариф). ✓
- §5 DDL prospects → Task 1. ✓
- §6 компоненты (dadata/config/db/маршруты/шаблоны) → Tasks 2–6. ✓
- §7 поток (обогащение лида + карточка по запросу) → Task 5 (lookup) + Task 6 (лид). ✓
- §8 комплаенс-гейты: вырезание телефонов/email → Task 2 (_sanitize) + смоук; ИП-гейт → _parse_party (management None) + партиал; per-lookup/quota → Task 3/5; UI-текст → шаблоны. ✓
- §9 деградация → dadata.is_configured + плашка (Task 4). ✓
- §10 тесты → 3 смоука (Tasks 2–4). ✓
- §11 выкатка → Task 1 (dev-DDL) + Task 7 (прод-DDL/env/push). ✓
- §14 pre-launch gate → Task 7 Шаг 7. ✓

**Плейсхолдеры:** код есть во всех кодовых шагах. Открытые «найти хук/шаблон диалогов» (Task 6 Шаг 1) — легитимный discovery-шаг (точное имя dialogs-шаблона неизвестно без чтения; закреплён graphify-запросом). `dadata_quota_take` — помечена сверка схемы `app_settings` (Task 3 Шаг 3/4).

**Согласованность типов:** `ProspectCard` (Task 2) → потребляется `prospect_upsert(card=...)` (Task 3) — поля совпадают. `prospect_for_lead` (Task 3) → `lead_company`/`p` в шаблоне (Task 6). Маршруты (Task 5) зовут хелперы Task 3 с точными сигнатурами. `back`/`_safe_next` единообразны. ✓

**Открытые вопросы (в спеку §15 / к владельцу):** TTL карточки (дефолт 60 дней задан в config); per-tenant soft-cap квоты — не в v1 (глобальный достаточно); HTTP-клиент — stdlib urllib (если предпочтёшь httpx — пиненный свап в requirements.txt + замена `_post`).
