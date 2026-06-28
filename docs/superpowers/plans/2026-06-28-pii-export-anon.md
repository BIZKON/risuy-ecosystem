# Обезличенная выгрузка базы тенанта (Приказ №140) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Дать тенанту в кабинете раздел «Защита данных (152-ФЗ)» с двумя действиями — скачать обезличенную базу лидов (псевдонимизация по №140) и отдельно gated-справочник соответствия — без DDL, в скоупе активного тенанта (RLS).

**Architecture:** Чистая логика псевдонимизации/CSV-санитайза — в новом self-contained модуле `shared/anon.py` (юнит-тестируется без БД/импорта app). Два курсорных RLS-стрима в `admin-panel/db.py` отдают whitelist-колонки. Слой `admin-panel/app.py` собирает CSV (BOM, как существующие экспорты), маршруты POST с CSRF + аудит-до-стрима (fail-closed). UI — новый шаблон + nav-пункт, видимый тенанту.

**Tech Stack:** Python 3 / FastAPI / asyncpg / Jinja2 (admin-panel); смоук-скрипты (idiom репо, не pytest) на `risuy_dev`.

**Спека:** `docs/superpowers/specs/2026-06-28-pii-export-anon-design.md` (прошла spec-review, 3 линзы).

## Global Constraints

- **Только русский** во всём (UI/комменты/докстринги/коммиты); латиница — лишь идентификаторы/пути/SQL.
- **БЕЗ DDL.** Все колонки уже существуют (проверено по `db/schema.sql` + `db/*.sql` ALTER).
- **TG-путь и существующие экспорты по поведению не трогаем** (кроме осознанного добавления formula-guard в `_csv_line` — это улучшение безопасности, покрывает и старые экспорты).
- **RLS-скоуп активного тенанта** обязателен; новые стримы дополнительно ставят `set_config('app.tenant_id', …, true)` и падают, если тенант не установлен (defence-in-depth).
- **Прямые идентификаторы НЕ попадают в обезличенный `anon.csv`**: `name, phone, phone_hash, tg_user_id, vk_user_id, max_user_id, max_chat_id, web_session_id`. Служебные `id`/`tenant_id`/`survey` — не в CSV (`id` только вход для псевдонима). Сырой `notes` — НЕ в `anon.csv` (нет NER), только `has_notes`; raw-`notes` — лишь в gated `map.csv`.
- **Псевдоним:** `subject_code = "СУБЪЕКТ-" + sha256(str(lead.id)).hexdigest()[:16]` (канонический uuid в нижнем регистре с дефисами; 16 hex = 64 бита; без соли).
- **Смоуки** — на `risuy_dev` (owner-DSN inline из Timeweb API, **VPN выключить**); чистка throwaway по порядку `leads`→`tenants` (FK без cascade).
- **Перед коммитом** (рабочий ритм владельца): адверсариальное ревью 3 линзы + зелёные смоуки. **Push — только по явному «да» владельца** (деплой = редеплой панели). Частые коммиты из writing-plans переопределены этим ритмом: код Tasks 1–3 пишем без коммита, единый коммит — в Task 4 после ревью.
- **Ориентация по коду:** `graphify query` до grep/чтения исходников.

---

## File Structure

- **Create** `shared/anon.py` — `subject_code`, `csv_safe`, `valid_persona`, `anon_row`, `map_row`, `ANON_HEADER`, `MAP_HEADER` (всё чистое, без БД/импорта app/config).
- **Create** `scripts/pii_anon_smoke.py` — чистый смоук модуля `shared/anon`.
- **Modify** `admin-panel/db.py` — `stream_leads_anon`, `stream_leads_map` (рядом со `stream_export_full`, ~L562).
- **Create** `scripts/pii_anon_db_smoke.py` — БД-смоук на `risuy_dev` (FORCE RLS, корректность, отзыв, guard).
- **Modify** `admin-panel/app.py` — import `anon`; formula-guard в `_csv_line`; генераторы `_csv_anon_rows`/`_csv_map_rows`; маршруты `GET /data-protection`, `POST /data-protection/anon.csv`, `POST /data-protection/map.csv`.
- **Create** `admin-panel/templates/data_protection.html` — страница раздела (две CSRF-формы).
- **Modify** `admin-panel/templates/base.html` — `nav_icon` ветка + `NAV_TITLES` метка + `nav_item` (вне `is_platform`).

---

## Task 1: Чистый модуль псевдонимизации `shared/anon.py`

**Files:**
- Create: `shared/anon.py`
- Test: `scripts/pii_anon_smoke.py`

**Interfaces:**
- Produces:
  - `subject_code(lead_id) -> str`
  - `csv_safe(value) -> str`
  - `valid_persona(slug, allowed) -> str`
  - `anon_row(rec: dict, allowed_personas) -> list[str]`
  - `map_row(rec: dict) -> list[str]`
  - `ANON_HEADER: list[str]`, `MAP_HEADER: list[str]`

- [ ] **Step 1: Написать падающий смоук** `scripts/pii_anon_smoke.py`

```python
#!/usr/bin/env python3
"""Чистый смоук shared/anon: псевдоним, formula-guard, валидация persona, сборка строк anon/map.
Запуск: PYTHONPATH=. ./.venv-smoke/bin/python scripts/pii_anon_smoke.py  (БД не нужна)"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from shared import anon  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


UID1 = "11111111-1111-4111-8111-111111111111"
UID2 = "22222222-2222-4222-8222-222222222222"


def main() -> None:
    # --- subject_code: детерминизм, префикс, длина, различимость ---
    c1 = anon.subject_code(UID1)
    check("subject_code детерминирован", c1 == anon.subject_code(UID1), c1)
    check("subject_code префикс", c1.startswith("СУБЪЕКТ-"))
    check("subject_code длина 16 hex", len(c1) == len("СУБЪЕКТ-") + 16, str(len(c1)))
    check("subject_code различимость", anon.subject_code(UID2) != c1)

    # --- csv_safe: formula-guard ---
    for raw, exp in [("=cmd", "'=cmd"), ("+1", "'+1"), ("-1", "'-1"), ("@x", "'@x"),
                     ("\tx", "'\tx"), ("\rx", "'\rx"), ("ok", "ok"), ("", ""), (None, "")]:
        check(f"csv_safe({raw!r})", anon.csv_safe(raw) == exp, repr(anon.csv_safe(raw)))

    # --- valid_persona ---
    allowed = {"liya", "mark"}
    check("valid_persona known", anon.valid_persona("liya", allowed) == "liya")
    check("valid_persona unknown→пусто", anon.valid_persona("evil", allowed) == "")
    check("valid_persona None→пусто", anon.valid_persona(None, allowed) == "")

    # --- anon_row: нет прямых идентификаторов, длина = заголовку, has_notes/persona ---
    rec = {
        "id": UID1, "messenger": "tg", "source": "vk", "consent": True, "subscribed": False,
        "status": "new", "created_at": None, "updated_at": None, "guide_sent_at": None,
        "follow_up_1_at": None, "follow_up_2_at": None, "follow_up_3_at": None,
        "unsubscribed_at": None, "erase_requested_at": None, "ai_persona": "evil",
        "bot_paused": False, "escalated_at": None, "has_notes": True,
    }
    row = anon.anon_row(rec, allowed)
    check("anon_row длина = ANON_HEADER", len(row) == len(anon.ANON_HEADER), str(len(row)))
    check("anon_row[0] = subject_code", row[0] == c1)
    check("anon_row has_notes=да", row[anon.ANON_HEADER.index("has_notes")] == "да")
    check("anon_row невалидный persona→пусто", row[anon.ANON_HEADER.index("ai_persona")] == "")
    # структурная гарантия: anon_row не читает ключи прямых идентификаторов (иначе KeyError выше)
    check("anon_row не требует name/phone", "name" not in rec and "phone" not in rec)

    # --- map_row: обычный лид vs отзыв ---
    mrec = {"id": UID1, "name": "Иван", "phone": "+79001112233", "tg_user_id": 42,
            "vk_user_id": None, "max_user_id": None, "max_chat_id": None,
            "web_session_id": None, "notes": "живёт на Тверской", "erase_requested_at": None}
    mrow = anon.map_row(mrec)
    check("map_row длина = MAP_HEADER", len(mrow) == len(anon.MAP_HEADER), str(len(mrow)))
    check("map_row[0] = subject_code (стабилен)", mrow[0] == c1)
    check("map_row name присутствует", mrow[anon.MAP_HEADER.index("name")] == "Иван")
    check("map_row phone присутствует", mrow[anon.MAP_HEADER.index("phone")] == "+79001112233")

    import datetime
    erec = dict(mrec, erase_requested_at=datetime.datetime(2026, 6, 1))
    erow = anon.map_row(erec)
    check("отзыв: name обнулён", erow[anon.MAP_HEADER.index("name")] == "")
    check("отзыв: phone обнулён", erow[anon.MAP_HEADER.index("phone")] == "")
    check("отзыв: notes обнулён", erow[anon.MAP_HEADER.index("notes")] == "")
    check("отзыв: tg_user_id обнулён", erow[anon.MAP_HEADER.index("tg_user_id")] == "")
    check("отзыв: флаг проставлен",
          erow[anon.MAP_HEADER.index("erase_status")] == "отзыв — обезличивание в процессе")
    check("отзыв: subject_code сохранён", erow[0] == c1)

    if FAILS:
        print("\n".join("❌ " + f for f in FAILS))
        raise SystemExit(1)
    print("🟢 pii_anon_smoke зелёный")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Запустить смоук — убедиться, что падает**

Run: `cd /Users/konstantin/Downloads/risuy-ecosystem && PYTHONPATH=. ./.venv-smoke/bin/python scripts/pii_anon_smoke.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'shared.anon'`.

- [ ] **Step 3: Реализовать `shared/anon.py`**

```python
"""Псевдонимизация базы лидов для обезличенной выгрузки (Приказ РКН №140) — чистая логика, без БД.

Прямые идентификаторы → стабильный subject_code; для справочника (map) — реверс с обработкой отзыва
согласия. Свободный notes в обезличенный набор НЕ идёт (нет NER) — только boolean has_notes. CSV
formula-guard нейтрализует ведущие =,+,-,@,TAB,CR (анти-инъекция формул в Excel/LibreOffice).
"""
import hashlib

# Заголовки CSV — единый источник правды (порядок столбцов = порядок значений в *_row).
ANON_HEADER = [
    "subject_code", "messenger", "source", "consent", "subscribed", "status",
    "created_at", "updated_at", "guide_sent_at", "follow_up_1_at", "follow_up_2_at",
    "follow_up_3_at", "unsubscribed_at", "erase_requested_at", "ai_persona",
    "bot_paused", "escalated_at", "has_notes",
]
MAP_HEADER = [
    "subject_code", "name", "phone", "tg_user_id", "vk_user_id", "max_user_id",
    "max_chat_id", "web_session_id", "notes", "erase_status",
]

_CSV_FORMULA_LEAD = ("=", "+", "-", "@", "\t", "\r")
_ERASE_FLAG = "отзыв — обезличивание в процессе"


def subject_code(lead_id) -> str:
    """Стабильный псевдоним субъекта из uuid лида: 'СУБЪЕКТ-' + первые 16 hex sha256(str(lead.id)).
    Вход — канонический uuid в нижнем регистре с дефисами (как отдаёт asyncpg/psql). Необратим без map."""
    digest = hashlib.sha256(str(lead_id).encode("utf-8")).hexdigest()
    return "СУБЪЕКТ-" + digest[:16]


def csv_safe(value) -> str:
    """Formula-guard: если строка начинается с опасного символа — префиксуем апострофом. None→''."""
    s = "" if value is None else str(value)
    if s and s[0] in _CSV_FORMULA_LEAD:
        return "'" + s
    return s


def valid_persona(slug, allowed) -> str:
    """ai_persona — слаг; пускаем в выгрузку только если он в allow-list (config.PERSONA_PRESETS).
    Иначе (None/произвольный текст) → пусто (БД хранит свободный text)."""
    return slug if slug in allowed else ""


def _s(value) -> str:
    return "" if value is None else str(value)


def _iso(dt) -> str:
    return dt.isoformat() if dt is not None else ""


def _yn(v) -> str:
    return "да" if v else "нет"


def anon_row(rec, allowed_personas) -> list[str]:
    """Строка обезличенного CSV из записи стрима (whitelist-колонки + has_notes). Прямых идентификаторов
    и raw-notes в rec НЕТ — гарантируется SELECT'ом stream_leads_anon. subject_code из rec['id']."""
    return [
        subject_code(rec["id"]),
        _s(rec["messenger"]), _s(rec["source"]),
        _yn(rec["consent"]), _yn(rec["subscribed"]), _s(rec["status"]),
        _iso(rec["created_at"]), _iso(rec["updated_at"]), _iso(rec["guide_sent_at"]),
        _iso(rec["follow_up_1_at"]), _iso(rec["follow_up_2_at"]), _iso(rec["follow_up_3_at"]),
        _iso(rec["unsubscribed_at"]), _iso(rec["erase_requested_at"]),
        valid_persona(rec["ai_persona"], allowed_personas),
        _yn(rec["bot_paused"]), _iso(rec["escalated_at"]),
        _yn(rec["has_notes"]),
    ]


def map_row(rec) -> list[str]:
    """Строка справочника соответствия subject_code → прямые идентификаторы. Для лида с выставленным
    erase_requested_at ВСЕ ПДн обнуляем (обработка отзыва — не выдаём реверс субъекту, потребовавшему
    прекращения), оставляя только subject_code + флаг."""
    erased = rec["erase_requested_at"] is not None

    def keep(value) -> str:
        return "" if erased else _s(value)

    return [
        subject_code(rec["id"]),
        keep(rec["name"]), keep(rec["phone"]),
        keep(rec["tg_user_id"]), keep(rec["vk_user_id"]), keep(rec["max_user_id"]),
        keep(rec["max_chat_id"]), keep(rec["web_session_id"]),
        keep(rec["notes"]),
        _ERASE_FLAG if erased else "",
    ]
```

- [ ] **Step 4: Запустить смоук — убедиться, что зелёный**

Run: `cd /Users/konstantin/Downloads/risuy-ecosystem && PYTHONPATH=. ./.venv-smoke/bin/python scripts/pii_anon_smoke.py`
Expected: PASS — `🟢 pii_anon_smoke зелёный`.

---

## Task 2: Курсорные RLS-стримы `stream_leads_anon` / `stream_leads_map`

**Files:**
- Modify: `admin-panel/db.py` (вставить после `stream_export_full`, ~L562)
- Test: `scripts/pii_anon_db_smoke.py`

**Interfaces:**
- Consumes: `pool`, `_active_tenant` (модульные, `admin-panel/db.py`).
- Produces (async-генераторы, RLS-скоуп активного тенанта):
  - `stream_leads_anon(*, row_cap: int)` → записи с колонками `id, messenger, source, consent, subscribed, status, created_at, updated_at, guide_sent_at, follow_up_1_at, follow_up_2_at, follow_up_3_at, unsubscribed_at, erase_requested_at, ai_persona, bot_paused, escalated_at, has_notes`.
  - `stream_leads_map(*, row_cap: int)` → записи `id, name, phone, tg_user_id, vk_user_id, max_user_id, max_chat_id, web_session_id, notes, erase_requested_at`.

- [ ] **Step 1: Написать падающий БД-смоук** `scripts/pii_anon_db_smoke.py`

```python
#!/usr/bin/env python3
"""БД-смоук обезличенной выгрузки на risuy_dev: stream_leads_anon (нет прямых идентификаторов,
has_notes), stream_leads_map (соответствие; отзыв → ПДн обнулены), RLS-изоляция (FORCE RLS),
guard при пустом тенанте, round-trip subject_code. Гонится как owner; на время — FORCE RLS на leads.

Запуск:
  ANON_SMOKE_DSN="postgresql://<owner>@<host>:5432/risuy_dev?sslmode=require" \
  DATABASE_URL="$ANON_SMOKE_DSN" SESSION_SECRET="smoke-secret-aaaaaaaaaaaaaaaa" \
  ADMIN_USERNAME=smoke ADMIN_PASSWORD_HASH='$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHQ$aGFzaHN0dWI' \
  PYTHONPATH=admin-panel:. ./.venv-smoke/bin/python scripts/pii_anon_db_smoke.py
"""
import asyncio
import datetime
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "admin-panel"))
os.environ.setdefault("DATABASE_URL", os.environ.get("ANON_SMOKE_DSN", "postgresql://x/y"))
os.environ.setdefault("SESSION_SECRET", "smoke-secret-aaaaaaaaaaaaaaaa")
os.environ.setdefault("ADMIN_USERNAME", "smoke")
os.environ.setdefault("ADMIN_PASSWORD_HASH",
                      "$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHQ$aGFzaHN0dWI")

import asyncpg  # noqa: E402
import db        # noqa: E402  (admin-panel/db.py)
from shared import anon  # noqa: E402

DSN = os.environ.get("ANON_SMOKE_DSN") or os.environ.get("DATABASE_URL")
if not DSN or "/risuy_dev" not in DSN.split("?")[0]:
    raise SystemExit("Задайте ANON_SMOKE_DSN на risuy_dev (FORCE RLS временно + delete тестовых строк).")

FAILS: list[str] = []
SLUG_A, SLUG_B = "smoke-anon-a", "smoke-anon-b"


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


async def _drop(c) -> None:
    await c.execute("delete from leads where tenant_id in (select id from tenants where slug = any($1))",
                    [SLUG_A, SLUG_B])
    await c.execute("delete from tenants where slug = any($1)", [SLUG_A, SLUG_B])


async def main() -> None:
    db.pool = await asyncpg.create_pool(DSN, min_size=1, max_size=4, setup=db._apply_tenant_guc)
    forced = False
    try:
        db.set_active_tenant(None)
        async with db.pool.acquire() as c:
            await _drop(c)
            ta = await c.fetchval("insert into tenants(slug,name,status) values($1,'A','active') returning id", SLUG_A)
            tb = await c.fetchval("insert into tenants(slug,name,status) values($1,'B','active') returning id", SLUG_B)
            # Тенант A: обычный лид (с notes-ФИО + телефон) и лид с отзывом согласия.
            la1 = await c.fetchval(
                "insert into leads(tenant_id,messenger,source,status,name,phone,tg_user_id,notes) "
                "values($1,'tg','vk','new','Иван Петров','+79001112233',990777001,"
                "'живёт на Тверской, муж Пётр') returning id", ta)
            la2 = await c.fetchval(
                "insert into leads(tenant_id,messenger,source,status,name,phone,tg_user_id,erase_requested_at) "
                "values($1,'tg','other','lost','Анна','+79005556677',990777002, now()) returning id", ta)
            # Тенант B: один лид (не должен видеться под ctx A).
            await c.execute(
                "insert into leads(tenant_id,messenger,source,status,name,tg_user_id) "
                "values($1,'tg','vk','new','Чужой',990777003)", tb)
            await c.execute("alter table leads force row level security")
            forced = True

        # ── ctx A: anon-стрим ──
        db.set_active_tenant(str(ta))
        anon_recs = [r async for r in db.stream_leads_anon(row_cap=10000)]
        check("anon: видны только 2 лида тенанта A", len(anon_recs) == 2, str(len(anon_recs)))
        keys = set(anon_recs[0].keys()) if anon_recs else set()
        for forbidden in ("name", "phone", "phone_hash", "tg_user_id", "vk_user_id",
                          "max_user_id", "max_chat_id", "web_session_id", "notes"):
            check(f"anon: нет колонки {forbidden}", forbidden not in keys)
        check("anon: есть has_notes", "has_notes" in keys)
        by_id = {str(r["id"]): r for r in anon_recs}
        check("anon: has_notes=True у лида с заметкой", by_id[str(la1)]["has_notes"] is True)

        # ── ctx A: map-стрим (соответствие + обработка отзыва) ──
        map_recs = {str(r["id"]): r for r in [m async for m in db.stream_leads_map(row_cap=10000)]}
        check("map: 2 лида A", len(map_recs) == 2, str(len(map_recs)))
        row_ok = anon.map_row(dict(map_recs[str(la1)]))
        check("map: обычный лид — имя на месте", row_ok[anon.MAP_HEADER.index("name")] == "Иван Петров")
        check("map: обычный лид — телефон на месте",
              row_ok[anon.MAP_HEADER.index("phone")] == "+79001112233")
        row_er = anon.map_row(dict(map_recs[str(la2)]))
        check("map: отзыв — имя обнулено", row_er[anon.MAP_HEADER.index("name")] == "")
        check("map: отзыв — телефон обнулён", row_er[anon.MAP_HEADER.index("phone")] == "")
        check("map: отзыв — флаг", row_er[anon.MAP_HEADER.index("erase_status")]
              == "отзыв — обезличивание в процессе")

        # ── round-trip subject_code (anon ↔ map один лид → один код) ──
        check("round-trip subject_code la1",
              anon.subject_code(la1) == row_ok[0]
              == anon.anon_row(dict(by_id[str(la1)]), set())[0])

        # ── guard: пустой тенант → стрим падает (не отдаём неопределённый набор) ──
        db.set_active_tenant(None)
        raised = False
        try:
            _ = [r async for r in db.stream_leads_anon(row_cap=10)]
        except RuntimeError:
            raised = True
        check("guard: пустой тенант → RuntimeError", raised)

    finally:
        try:
            db.set_active_tenant(None)
            async with db.pool.acquire() as c:
                if forced:
                    await c.execute("alter table leads no force row level security")
                await _drop(c)
        finally:
            await db.pool.close()

    if FAILS:
        print("\n".join("❌ " + f for f in FAILS))
        raise SystemExit(1)
    print("🟢 pii_anon_db_smoke зелёный")


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: Запустить БД-смоук — убедиться, что падает**

Получить owner-DSN на `risuy_dev` inline из Timeweb API (**VPN выключить**), затем:
Run: `cd /Users/konstantin/Downloads/risuy-ecosystem && ANON_SMOKE_DSN="<owner-dsn risuy_dev>" PYTHONPATH=admin-panel:. ./.venv-smoke/bin/python scripts/pii_anon_db_smoke.py`
Expected: FAIL — `AttributeError: module 'db' has no attribute 'stream_leads_anon'`.

- [ ] **Step 3: Реализовать стримы в `admin-panel/db.py`** (после `stream_export_full`, ~L562)

```python
# --------------------------------------------------------------------------- #
# Обезличенная выгрузка (Приказ №140). Whitelist-колонки + псевдонимизация в app-слое
# (shared.anon). RLS: app.tenant_id ставит pool-хук; здесь ДОПОЛНИТЕЛЬНО ставим явно
# (defence-in-depth) и падаем при пустом активном тенанте — не отдаём неопределённый набор.
# --------------------------------------------------------------------------- #
async def stream_leads_anon(*, row_cap: int):
    """Курсорный стрим обезличиваемой базы лидов активного тенанта (БЕЗ прямых идентификаторов и
    raw-notes; notes → boolean has_notes). Псевдоним subject_code считает app-слой из id."""
    tid = _active_tenant.get()
    if not tid:
        raise RuntimeError("stream_leads_anon: активный тенант не установлен (RLS-скоуп)")
    async with pool.acquire() as c:
        async with c.transaction():
            await c.execute("select set_config('app.tenant_id', $1, true)", tid)
            async for rec in c.cursor(
                "select id, messenger, source, consent, subscribed, status, "
                "created_at, updated_at, guide_sent_at, "
                "follow_up_1_at, follow_up_2_at, follow_up_3_at, "
                "unsubscribed_at, erase_requested_at, ai_persona, "
                "bot_paused, escalated_at, "
                "(notes is not null and notes <> '') as has_notes "
                "from leads order by created_at desc limit $1",
                row_cap,
            ):
                yield rec


async def stream_leads_map(*, row_cap: int):
    """Справочник соответствия subject_code → прямые идентификаторы (gated, реверс псевдонима).
    Лиды с erase_requested_at app-слой обнуляет (обработка отзыва). RLS-скоуп — как в stream_leads_anon."""
    tid = _active_tenant.get()
    if not tid:
        raise RuntimeError("stream_leads_map: активный тенант не установлен (RLS-скоуп)")
    async with pool.acquire() as c:
        async with c.transaction():
            await c.execute("select set_config('app.tenant_id', $1, true)", tid)
            async for rec in c.cursor(
                "select id, name, phone, tg_user_id, vk_user_id, max_user_id, "
                "max_chat_id, web_session_id, notes, erase_requested_at "
                "from leads order by created_at desc limit $1",
                row_cap,
            ):
                yield rec
```

- [ ] **Step 4: Запустить БД-смоук — убедиться, что зелёный**

Run: `cd /Users/konstantin/Downloads/risuy-ecosystem && ANON_SMOKE_DSN="<owner-dsn risuy_dev>" PYTHONPATH=admin-panel:. ./.venv-smoke/bin/python scripts/pii_anon_db_smoke.py`
Expected: PASS — `🟢 pii_anon_db_smoke зелёный`.

---

## Task 3: Слой панели — маршруты, CSV-генераторы, formula-guard, UI

**Files:**
- Modify: `admin-panel/app.py` (import `anon` ~L49; `_csv_line` ~L1662; новые генераторы+маршруты рядом с `export_consents`, ~L1596)
- Create: `admin-panel/templates/data_protection.html`
- Modify: `admin-panel/templates/base.html` (`nav_icon` ~L45; `NAV_TITLES` ~L72; `nav_item` ~L92)

**Interfaces:**
- Consumes: `anon.*` (Task 1), `db.stream_leads_anon`/`db.stream_leads_map`/`db.count_leads`/`db.audit` (Task 2 + существующие), `_enforce_csrf`/`_ip`/`_ua`/`_csv_line`/`_csv_headers`/`templates`/`config.PERSONA_PRESETS`/`config.EXPORT_ROW_CAP`.
- Produces: маршруты `GET /data-protection`, `POST /data-protection/anon.csv`, `POST /data-protection/map.csv`.

- [ ] **Step 1: Импорт `anon`** — в блок импортов shared (рядом с `from shared import leadmagnet, money, nurture, vault`, ~L49)

```python
from shared import anon
```

- [ ] **Step 2: Formula-guard в `_csv_line`** (заменить тело, ~L1662)

```python
def _csv_line(values: list[str], *, bom: bool = False) -> bytes:
    buf = io.StringIO()
    # formula-guard: нейтрализуем ведущие =,+,-,@,TAB,CR (анти-инъекция в Excel/LibreOffice).
    # Покрывает и существующие экспорты (export_masked/full/consents) — defence-in-depth.
    csv.writer(buf, quoting=csv.QUOTE_MINIMAL).writerow([anon.csv_safe(v) for v in values])
    text = buf.getvalue()
    if bom:
        text = "﻿" + text   # BOM для Excel-RU (§3.11)
    return text.encode("utf-8")
```

- [ ] **Step 3: CSV-генераторы + маршруты** (вставить после `_csv_consent_rows`, ~L1607)

```python
# ---- Раздел «Защита данных (152-ФЗ)»: обезличенная выгрузка + справочник (Приказ №140) ---- #
async def _csv_anon_rows(matched: int):
    yield _csv_line(anon.ANON_HEADER, bom=True)
    async for r in db.stream_leads_anon(row_cap=config.EXPORT_ROW_CAP):
        yield _csv_line(anon.anon_row(dict(r), config.PERSONA_PRESETS))
    if matched > config.EXPORT_ROW_CAP:
        yield _csv_line([f"# ВНИМАНИЕ: выгрузка усечена до {config.EXPORT_ROW_CAP} строк из {matched}; "
                         "обратитесь в поддержку для полной выгрузки."])


async def _csv_map_rows(matched: int):
    yield _csv_line(anon.MAP_HEADER, bom=True)
    seen: set[str] = set()
    async for r in db.stream_leads_map(row_cap=config.EXPORT_ROW_CAP):
        row = anon.map_row(dict(r))
        if row[0] in seen:
            raise RuntimeError(f"stream_leads_map: коллизия subject_code {row[0]} — выгрузка прервана")
        seen.add(row[0])
        yield _csv_line(row)
    if matched > config.EXPORT_ROW_CAP:
        yield _csv_line([f"# ВНИМАНИЕ: справочник усечён до {config.EXPORT_ROW_CAP} строк из {matched}."])


@app.get("/data-protection", response_class=HTMLResponse)
async def data_protection_page(
    request: Request,
    session: auth.Session = Depends(require_session),
):
    tid = session.active_tenant_id
    lead_count = await db.count_leads({}) if tid else 0
    return templates.TemplateResponse(request, "data_protection.html", {
        "session": session, "active": "data_protection",
        "csrf_token": session.csrf_token, "has_tenant": bool(tid),
        "lead_count": lead_count, "row_cap": config.EXPORT_ROW_CAP,
    })


@app.post("/data-protection/anon.csv")
async def export_anon(
    request: Request,
    session: auth.Session = Depends(require_session),
    csrf_token: str = Form(""),
):
    await _enforce_csrf(request, session, csrf_token)
    if not session.active_tenant_id:
        raise StarletteHTTPException(status_code=400, detail="Кабинет не привязан к клиенту")
    matched = await db.count_leads({})
    # Аудит ДО стрима (fail-closed). Без ПДн.
    await db.audit(actor=session.actor, action="pii_export_anon", ip=_ip(request),
                   user_agent=_ua(request), detail={"matched": matched, "row_cap": config.EXPORT_ROW_CAP})
    return StreamingResponse(_csv_anon_rows(matched), media_type="text/csv; charset=utf-8",
                             headers=_csv_headers("anon_leads"))


@app.post("/data-protection/map.csv")
async def export_subject_map(
    request: Request,
    session: auth.Session = Depends(require_session),
    csrf_token: str = Form(""),
    confirm: str = Form(""),
    reason: str = Form(""),
):
    await _enforce_csrf(request, session, csrf_token)
    if not session.active_tenant_id:
        raise StarletteHTTPException(status_code=400, detail="Кабинет не привязан к клиенту")
    if confirm != "yes":
        raise StarletteHTTPException(status_code=400, detail="Требуется подтверждение")
    reason = reason.strip()
    if not reason:
        raise StarletteHTTPException(status_code=400, detail="Укажите основание выгрузки справочника")
    matched = await db.count_leads({})
    # Отдельный аудит «передача справочника» с поводом (БЕЗ ПДн).
    await db.audit(actor=session.actor, action="pii_export_map", ip=_ip(request),
                   user_agent=_ua(request),
                   detail={"matched": matched, "row_cap": config.EXPORT_ROW_CAP, "reason": reason[:500]})
    return StreamingResponse(_csv_map_rows(matched), media_type="text/csv; charset=utf-8",
                             headers=_csv_headers("subject_map"))
```

- [ ] **Step 4: Шаблон** `admin-panel/templates/data_protection.html`

```html
{% extends "base.html" %}
{% from "_macros.html" import flash %}

{% block title %}Защита данных (152-ФЗ){% endblock %}
{% block body_class %}page-my-agent{% endblock %}

{% block content %}
<div class="page-head">
  <h1 class="page-head__title">Защита данных (152-ФЗ)</h1>
  <p class="page-head__hint">По требованию Роскомнадзора (Приказ №140) оператор должен уметь <b>обезличить</b> свою базу и передать её в защищённый контур НСУД. Здесь это делается одной кнопкой.</p>
</div>

{% if not has_tenant %}
<section class="card" aria-label="Кабинет не привязан">
  <div class="card__title">Кабинет ещё не привязан</div>
  <p class="card__note">Этот раздел работает с базой вашего кабинета. Сейчас кабинет к учётной записи не привязан — напишите в поддержку.</p>
</section>
{% else %}

<section class="card">
  <h2 class="card__title">Обезличенная база</h2>
  <p class="card__note">CSV вашей базы лидов ({{ lead_count }} {{ 'запись' if lead_count == 1 else 'записей' }}), где прямые идентификаторы (имя, телефон, ID каналов) заменены на стабильный код субъекта вида <code>СУБЪЕКТ-…</code>. Свободные заметки в файл не включаются (только пометка, что заметка есть). Файл предназначен для передачи в <b>защищённый контур НСУД</b>, не для публикации.</p>
  <form method="post" action="/data-protection/anon.csv" autocomplete="off">
    <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
    <div class="form-actions"><button class="btn btn--primary" type="submit">Скачать обезличенную базу</button></div>
  </form>
</section>

<section class="card">
  <h2 class="card__title">Справочник соответствия</h2>
  <p class="card__note">Отдельный файл, связывающий <code>СУБЪЕКТ-…</code> с настоящими ПДн — это <b>реверс обезличивания</b>. ⚠️ Храните его <b>отдельно</b> от обезличенной базы, не объединяйте файлы и передавайте только по отдельному законному запросу. Для отозвавших согласие субъектов ПДн в справочнике скрыты.</p>
  <form method="post" action="/data-protection/map.csv" autocomplete="off">
    <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
    <label class="field">
      <span class="field__label">Основание выгрузки</span>
      <input class="field__input" type="text" name="reason" maxlength="500" required
             placeholder="Например: запрос РКН №… от …">
    </label>
    <label class="field field--check">
      <input type="checkbox" name="confirm" value="yes" required>
      <span class="field__label">Понимаю: это реверс псевдонима, храню файл отдельно</span>
    </label>
    <div class="form-actions"><button class="btn btn--secondary" type="submit">Скачать справочник соответствия</button></div>
  </form>
</section>

{% endif %}
{% endblock %}
```

- [ ] **Step 5: Nav в `base.html`** — три точечные правки

(а) В макрос `nav_icon` добавить ветку перед `{%- endif -%}` (~L45):
```html
{%- elif name == 'data_protection' -%}<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>
```
(б) В словарь `NAV_TITLES` (~L72) добавить ключ:
```html
  'data_protection': 'Защита данных',
```
(в) После строки `nav_item('nurture', …)` (~L92), ДО блока `{%- if session and session.is_platform %}`:
```html
      {{ nav_item('data_protection', '/data-protection', 'Защита данных', active) }}
```

- [ ] **Step 6: Проверка компиляции и шаблонов**

```bash
cd /Users/konstantin/Downloads/risuy-ecosystem
python3 -c "import ast; ast.parse(open('admin-panel/app.py').read()); print('app.py OK')"
python3 -c "import ast; ast.parse(open('shared/anon.py').read()); print('anon.py OK')"
python3 -c "from jinja2 import Environment, FileSystemLoader; \
e=Environment(loader=FileSystemLoader('admin-panel/templates')); \
e.get_template('data_protection.html'); e.get_template('base.html'); print('jinja OK')"
```
Expected: `app.py OK` / `anon.py OK` / `jinja OK`.

- [ ] **Step 7: Перегнать оба смоука (регрессия)**

```bash
cd /Users/konstantin/Downloads/risuy-ecosystem
PYTHONPATH=. ./.venv-smoke/bin/python scripts/pii_anon_smoke.py
ANON_SMOKE_DSN="<owner-dsn risuy_dev>" PYTHONPATH=admin-panel:. ./.venv-smoke/bin/python scripts/pii_anon_db_smoke.py
```
Expected: оба `🟢`.

---

## Task 4: Адверсариальное ревью (3 линзы) + локальный коммит

**Files:** весь дифф (`shared/anon.py`, `admin-panel/db.py`, `admin-panel/app.py`, `admin-panel/templates/{data_protection,base}.html`, `scripts/pii_anon_smoke.py`, `scripts/pii_anon_db_smoke.py`).

- [ ] **Step 1: Финальная проверка** — оба смоука зелёные + py_compile/jinja OK (Task 3, Steps 6–7).

- [ ] **Step 2: Адверсариальное ревью 3 линзы** (Workflow): корректность/RLS-утечки · 152-ФЗ/право · регрессия существующих экспортов (formula-guard в `_csv_line` не сломал `export_masked`/`export_full`/`export_consents`; CSRF/аудит на месте; нет утечки прямых идентификаторов в `anon.csv`). Найденное critical/high — исправить, перегнать смоуки.

- [ ] **Step 3: Локальный коммит** (на ветке, не в main напрямую без нужды; **push НЕ делать** — ждать «да» владельца)

```bash
cd /Users/konstantin/Downloads/risuy-ecosystem
git add shared/anon.py admin-panel/db.py admin-panel/app.py \
        admin-panel/templates/data_protection.html admin-panel/templates/base.html \
        scripts/pii_anon_smoke.py scripts/pii_anon_db_smoke.py \
        docs/superpowers/specs/2026-06-28-pii-export-anon-design.md \
        docs/superpowers/plans/2026-06-28-pii-export-anon.md
git commit -m "feat(panel): раздел «Защита данных (152-ФЗ)» — обезличенная выгрузка лидов + справочник (Приказ №140)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

- [ ] **Step 4: СТОП — запросить у владельца «да» на push/редеплой панели.** После «да»: push → дождаться смены `start_time` панели → live-проверка: `GET /data-protection` → 200; `POST /data-protection/anon.csv` (с CSRF) → CSV без прямых идентификаторов; `POST /data-protection/map.csv` (confirm+повод) → справочник; запись аудита `pii_export_anon`/`pii_export_map`.

---

## Self-Review (выполнено при написании плана)

- **Покрытие спеки:** subject_code 16 hex ✓ (Task1) · notes исключён/has_notes ✓ (Task1/2) · whitelist без прямых id ✓ (Task2 SELECT + Task1 anon_row) · отзыв→обнуление в map ✓ (Task1/2) · formula-guard ✓ (Task1/3) · усиленный гейт map (confirm+повод) ✓ (Task3) · RLS defence-in-depth + guard ✓ (Task2) · аудит pii_export_anon/map ✓ (Task3) · row_cap-маркер неполноты ✓ (Task3) · nav вне is_platform ✓ (Task3) · раздел/шаблон ✓ (Task3) · смоуки pure+db ✓ (Task1/2) · «отношение к существующим экспортам» (UI разведён, поведение не меняем) ✓ (Task3 шаблон + неизменность export_*).
- **Плейсхолдеры:** нет — весь код приведён.
- **Согласованность типов:** `ANON_HEADER`/`MAP_HEADER` — единый источник порядка; `anon_row`/`map_row` принимают `dict`, в app передаём `dict(rec)`; `subject_code(rec["id"])` стабилен между anon/map; `stream_leads_*` читают `_active_tenant` (как `_apply_tenant_guc`), маршруты вызывают после `require_session`.
