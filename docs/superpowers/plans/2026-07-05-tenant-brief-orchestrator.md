# Бриф-онбординг тенанта + Центр решений + Оркестратор — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Клиент проходит брендированный бриф-опрос (лендинг в боте) → ответы падают в `tenant_brief` → оркестратор (LLM, чистая функция) собирает черновик настройки + рекомендации → ваша команда применяет его за HumanGate в панели теми же db-сеттерами, что и ручные формы.

**Architecture:** Сплит. Публичное лицо — новые aiohttp-роуты `/brief/{token}` в боте (паттерн `_club_landing`). Общий слой — таблица `tenant_brief` + модуль `shared/brief_schema.py` (единый источник вопросов, читают бот и оркестратор). Внутренний контур — платформенный раздел «Бриф-центр» в admin-panel: просмотр ответов, запуск оркестратора, диф-предпросмотр и применение. Оркестратор не пишет в прод — только возвращает черновик.

**Tech Stack:** Python 3.11, aiogram/aiohttp (бот), FastAPI + Jinja2 (панель), asyncpg + Postgres (RLS), существующий LLM-бэкенд (cloud-ai/gateway), смоук-скрипты.

## Global Constraints

- **Русский язык везде** — UI-тексты, комментарии, docstrings, коммиты, вопросы брифа. Латиница только техническая (идентификаторы, ключи, SQL).
- **Бот не трогаем в логике диалогов** — только добавляем HTTP-роуты в `_start_health()` и функции чтения/записи в `bot-telegram/db.py`.
- **Оркестратор — чистая функция** `analyze(answers) → proposal`, без побочных эффектов. Запись в прод-настройки — ТОЛЬКО в панели за HumanGate.
- **Применение = вызов существующих сеттеров** (`db.set_funnel_config`, `db.create_product_with_audit`, `db.create_tenant_trigger`, `db.set_channel_agent`, `db.add_dynamic_persona`, `db.set_persona_role`) — никакого параллельного пути записи.
- **152-ФЗ:** обезличивание ответов перед LLM; на форме — просьба не вставлять ПДн третьих лиц; собственные бизнес-данные тенанта не маскируем в хранении.
- **RLS-решение (уточнение спеки §4.1):** таблица `tenant_brief` — БЕЗ tenant-isolation RLS (как таблица `tenants`), потому что это кросс-тенантный платформенный артефакт, тенант его напрямую не читает (managed-модель), а бот обращается к нему по секретному токену. Контроль доступа: `_require_admin` (is_platform) в панели + секретность токена в боте. Применение настроек по-прежнему идёт через `set_active_tenant(tid)` → RLS-сеттеры скоуплены на тенанта.
- **Прод-DDL / деплой / push — только по явному per-действие «да» владельца.** db-смоуки на risuy_dev гоняет КОНТРОЛЛЕР (owner-DSN не отдавать субагентам).
- **Коммитить явными файлами** (НЕ `CLAUDE.md`/`.claude/`/`.gitignore`/`.superpowers/`/`graphify-out/`). Коммиттеры-субагенты — строго последовательно.
- **Метрологические бюджеты:** лендинг — лёгкая самодостаточная страница без тяжёлого JS.

**Кластер/хост для миграции:** `twc-migrate.sh 4171827 81.31.246.136 risuy_dev gen_user db/migrate_tenant_brief.sql` (сначала risuy_dev, прод `risuy` — по «да»).

---

### Task 1: Схема брифа `shared/brief_schema.py`

Единый источник истины: секции, вопросы, типы, ветвления, `maps_to`. Чистый Python, без БД. Читают бот (рендер) и оркестратор (интерпретация).

**Files:**
- Create: `shared/brief_schema.py`
- Test: `scripts/brief_schema_smoke.py`

**Interfaces:**
- Produces:
  - `BRIEF_VERSION: int`
  - `SECTIONS: list[dict]` — каждая: `{"key", "title", optional "branch_on", "questions": [...]}`; вопрос: `{"key", "type", "label", "required": bool, optional "options": list[str], optional "show_if": {"q": str, "in": list[str]}, optional "maps_to": str, optional "max": int}`
  - `def question_index() -> dict[str, dict]` — плоский индекс `question_key -> question` (+`_section` в каждом)
  - `def validate_answers(answers: dict) -> list[str]` — список ошибок (пустой = валидно): пропущенные required (с учётом `show_if`), неизвестные choice-значения, превышение `max`
  - `def visible_questions(answers: dict) -> list[dict]` — вопросы с учётом ветвления `show_if`

- [ ] **Step 1: Написать падающий тест** — `scripts/brief_schema_smoke.py`

```python
#!/usr/bin/env python3
"""Смоук схемы брифа: структура валидна, ветвления ссылаются на реальные вопросы,
validate_answers ловит пропуски required и неизвестные варианты. Без БД."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import importlib
brief_schema = importlib.import_module("shared.brief_schema")

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


def main() -> None:
    idx = brief_schema.question_index()

    print("1. структура схемы:")
    check("BRIEF_VERSION — int >= 1", isinstance(brief_schema.BRIEF_VERSION, int) and brief_schema.BRIEF_VERSION >= 1)
    check("SECTIONS непустой", len(brief_schema.SECTIONS) >= 1)
    allowed_types = {"text", "textarea", "choice", "multichoice", "repeatable"}
    for sec in brief_schema.SECTIONS:
        check(f"секция {sec.get('key')}: есть title", bool(sec.get("title")))
        for q in sec["questions"]:
            check(f"вопрос {q.get('key')}: есть key/type/label",
                  bool(q.get("key") and q.get("type") and q.get("label")))
            check(f"вопрос {q.get('key')}: тип разрешён", q.get("type") in allowed_types)

    print("2. ветвления ссылаются на существующие вопросы:")
    for q in idx.values():
        cond = q.get("show_if")
        if cond:
            check(f"show_if.q '{cond.get('q')}' существует", cond.get("q") in idx)

    print("3. validate_answers ловит пропуск required:")
    errs_empty = brief_schema.validate_answers({})
    check("пустые ответы → есть ошибки required", len(errs_empty) > 0, f"errs={len(errs_empty)}")

    print("4. validate_answers ловит неизвестный choice:")
    # первый choice-вопрос
    choice_q = next((q for q in idx.values() if q["type"] == "choice"), None)
    if choice_q:
        bad = {choice_q["key"]: "___нет_такого_варианта___"}
        errs = brief_schema.validate_answers(bad)
        check("неизвестный вариант choice → ошибка", any(choice_q["key"] in e for e in errs))

    print()
    if FAILS:
        print("❌ ПРОВАЛЫ: " + ", ".join(FAILS))
        sys.exit(1)
    print("✅ brief_schema smoke — OK")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Запустить тест — убедиться, что падает**

Run: `cd ~/Downloads/risuy-ecosystem && PYTHONPATH=. python3 scripts/brief_schema_smoke.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'shared.brief_schema'` (файла ещё нет).

- [ ] **Step 3: Реализовать `shared/brief_schema.py`**

```python
"""Единый источник истины для бриф-опроса тенанта.

Читают: бот (bot-telegram) — рендер формы; оркестратор (admin-panel) — интерпретация
ответов. Меняем вопросы ТОЛЬКО здесь. Номер версии кладётся в ответы при сабмите.
"""
from __future__ import annotations

BRIEF_VERSION = 1

# Тип вопроса: text | textarea | choice | multichoice | repeatable
# show_if: показывать вопрос, только если answers[q] входит в in
# maps_to: подсказка оркестратору, на какую «ручку» настройки влияет ответ
SECTIONS: list[dict] = [
    {
        "key": "business",
        "title": "О бизнесе",
        "questions": [
            {"key": "company_name", "type": "text", "required": True, "max": 200,
             "label": "Название вашего бизнеса", "maps_to": "funnel.company_name"},
            {"key": "b2b_or_b2c", "type": "choice", "required": True,
             "options": ["B2B", "B2C", "Оба"],
             "label": "Вы продаёте бизнесам, людям или и тем и другим?"},
            {"key": "niche", "type": "text", "required": True, "max": 200,
             "label": "Ниша/отрасль в двух словах"},
            {"key": "positioning", "type": "textarea", "required": False, "max": 1000,
             "label": "Чем вы отличаетесь от конкурентов? Одним абзацем."},
        ],
    },
    {
        "key": "products",
        "title": "Продукты и оферы",
        "questions": [
            {"key": "products_list", "type": "textarea", "required": True, "max": 4000,
             "label": "Перечислите продукты/услуги: название — цена — что даёт. По одному в строке.",
             "maps_to": "products"},
            {"key": "best_seller", "type": "textarea", "required": False, "max": 1000,
             "label": "Что покупают чаще всего и что приносит больше всего прибыли — это одно и то же?"},
            {"key": "lead_magnet", "type": "textarea", "required": False, "max": 1000,
             "label": "Есть ли бесплатный материал/пробник для первого касания? Опишите.",
             "maps_to": "funnel.leadmagnet"},
        ],
    },
    {
        "key": "audience",
        "title": "Клиенты",
        "branch_on": "b2b_or_b2c",
        "questions": [
            {"key": "audience_portrait", "type": "textarea", "required": True, "max": 2000,
             "label": "Портрет вашего клиента (сегмент, не список контактов): кто это, какая ситуация."},
            {"key": "trigger_moment", "type": "textarea", "required": True, "max": 1000,
             "label": "В какой момент клиент понимает, что вы ему нужны? Опишите триггер, а не портрет."},
            {"key": "b2b_decision", "type": "textarea", "required": False, "max": 1000,
             "show_if": {"q": "b2b_or_b2c", "in": ["B2B", "Оба"]},
             "label": "Кто принимает решение о покупке и сколько длится цикл сделки?"},
            {"key": "b2c_objections", "type": "textarea", "required": False, "max": 1000,
             "show_if": {"q": "b2b_or_b2c", "in": ["B2C", "Оба"]},
             "label": "Топ-3 возражения, которые вы слышите чаще всего от людей."},
        ],
    },
    {
        "key": "voice",
        "title": "Тон и стиль общения",
        "questions": [
            {"key": "tone", "type": "choice", "required": True,
             "options": ["Дружелюбный на «ты»", "Уважительный на «вы»", "Экспертный/деловой", "Живой/с юмором"],
             "label": "Как ИИ-сотрудник должен общаться с клиентами?",
             "maps_to": "persona.behavior"},
            {"key": "price_objection_example", "type": "textarea", "required": False, "max": 1000,
             "label": "Как ваш лучший продавец отвечает на «дорого»? Дайте пример фразой.",
             "maps_to": "persona.behavior"},
            {"key": "forbidden", "type": "textarea", "required": False, "max": 1000,
             "label": "Чего ИИ-сотрудник НЕ должен делать/говорить? (стоп-темы, обещания)"},
        ],
    },
    {
        "key": "channels",
        "title": "Каналы и анонсы",
        "questions": [
            {"key": "channels_used", "type": "multichoice", "required": True,
             "options": ["Telegram", "VK", "MAX"],
             "label": "В каких каналах работает ИИ-сотрудник?",
             "maps_to": "channels"},
            {"key": "announcements", "type": "textarea", "required": False, "max": 2000,
             "label": "Что важного происходит регулярно, о чём стоит напоминать подписчикам?",
             "maps_to": "triggers"},
            {"key": "escalation_wanted", "type": "choice", "required": False,
             "options": ["Да", "Нет"],
             "label": "Передавать горячие/сложные обращения живому менеджеру?"},
        ],
    },
    {
        "key": "legal",
        "title": "Реквизиты оператора (152-ФЗ)",
        "questions": [
            {"key": "operator_name", "type": "text", "required": True, "max": 300,
             "label": "Юридическое название (ИП/ООО) — оператор персональных данных",
             "maps_to": "funnel.operator_name"},
            {"key": "operator_inn", "type": "text", "required": True, "max": 20,
             "label": "ИНN оператора", "maps_to": "funnel.operator_inn"},
            {"key": "operator_email", "type": "text", "required": True, "max": 200,
             "label": "Контактный email оператора", "maps_to": "funnel.operator_email"},
        ],
    },
]


def question_index() -> dict[str, dict]:
    """Плоский индекс question_key -> вопрос (+ поле _section с ключом секции)."""
    idx: dict[str, dict] = {}
    for sec in SECTIONS:
        for q in sec["questions"]:
            item = dict(q)
            item["_section"] = sec["key"]
            idx[q["key"]] = item
    return idx


def _is_visible(q: dict, answers: dict) -> bool:
    cond = q.get("show_if")
    if not cond:
        return True
    return str(answers.get(cond["q"], "")) in cond["in"]


def visible_questions(answers: dict) -> list[dict]:
    """Вопросы, видимые при текущих ответах (с учётом ветвления show_if)."""
    idx = question_index()
    return [q for q in idx.values() if _is_visible(q, answers)]


def validate_answers(answers: dict) -> list[str]:
    """Проверка ответов по схеме. Возвращает список ошибок (пусто = валидно).

    Ловит: пропущенные required (видимые), неизвестные варианты choice,
    превышение max по длине текста.
    """
    errs: list[str] = []
    idx = question_index()
    for key, q in idx.items():
        visible = _is_visible(q, answers)
        raw = answers.get(key)
        val = "" if raw is None else str(raw).strip()
        if q.get("required") and visible and not val:
            errs.append(f"{key}: обязательный вопрос не заполнен")
        if val and q["type"] == "choice" and q.get("options") and val not in q["options"]:
            errs.append(f"{key}: недопустимый вариант «{val}»")
        if val and q.get("max") and len(val) > int(q["max"]):
            errs.append(f"{key}: превышена длина ({len(val)} > {q['max']})")
    return errs
```

*(Опечатку «ИНN» в label заменить на «ИНН» при реализации — латиница недопустима.)*

- [ ] **Step 4: Запустить тест — убедиться, что проходит**

Run: `cd ~/Downloads/risuy-ecosystem && PYTHONPATH=. python3 scripts/brief_schema_smoke.py`
Expected: PASS — `✅ brief_schema smoke — OK`

- [ ] **Step 5: Коммит**

```bash
cd ~/Downloads/risuy-ecosystem
git add shared/brief_schema.py scripts/brief_schema_smoke.py
git commit -m "feat(brief): схема бриф-опроса тенанта (единый источник) + смоук"
```

---

### Task 2: Слой данных — миграция `tenant_brief` + db-функции (бот и панель) + db-смоук

Таблица со статусной машиной, функции доступа для бота (по токену) и панели (кросс-тенантно), db-смоук на risuy_dev.

**Files:**
- Create: `db/migrate_tenant_brief.sql`
- Modify: `admin-panel/db.py` (добавить функции в конец соответствующего блока)
- Modify: `bot-telegram/db.py` (добавить функции рядом с `get_legal_doc_data`, ~строка 1740)
- Create: `scripts/brief_db_smoke.py`

**Interfaces:**
- Consumes: `db.set_active_tenant`, `db.audit` (существующие, см. Global Constraints)
- Produces (панель, `admin-panel/db.py`):
  - `async def create_tenant_brief(tenant_id, *, actor: str, ip: str | None, user_agent: str | None, ttl_days: int = 30) -> tuple[str, str]` → `(brief_id, token)`
  - `async def list_tenant_briefs() -> list[dict]` — кросс-тенантно: `[{id, tenant_id, tenant_name, tenant_slug, status, created_at, submitted_at, applied_at}]`
  - `async def get_tenant_brief(brief_id: str) -> dict | None` — `{id, tenant_id, tenant_name, tenant_slug, status, answers, proposal, applied, ...timestamps}`
  - `async def set_brief_proposal(brief_id: str, proposal: dict) -> None` — status → `proposed`
  - `async def mark_brief_applied(brief_id: str, applied: dict, *, actor, ip, user_agent) -> None` — status → `applied` + audit
- Produces (бот, `bot-telegram/db.py`):
  - `async def get_brief_by_token(token: str) -> dict | None` — `{id, tenant_id, tenant_name, status, expired: bool}`; None если токен неизвестен
  - `async def submit_brief(token: str, answers: dict) -> str` — возвращает `"ok"` | `"already"` | `"expired"` | `"unknown"`

- [ ] **Step 1: Написать миграцию `db/migrate_tenant_brief.sql`**

```sql
-- tenant_brief: бриф-онбординг тенанта (опрос → черновик оркестратора → применение).
-- Аддитивно, идемпотентно (IF NOT EXISTS). Применение:
--   twc-migrate.sh 4171827 81.31.246.136 risuy_dev gen_user db/migrate_tenant_brief.sql
-- БЕЗ tenant-isolation RLS: кросс-тенантный платформенный артефакт (как tenants),
-- доступ гейтится приложением (is_platform) и секретностью token; бот читает по token.

CREATE TABLE IF NOT EXISTS tenant_brief (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    token        text NOT NULL UNIQUE,
    status       text NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending','submitted','proposed','applied','expired')),
    answers      jsonb,
    proposal     jsonb,
    applied      jsonb,
    created_by   text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    submitted_at timestamptz,
    proposed_at  timestamptz,
    applied_at   timestamptz,
    expires_at   timestamptz
);

CREATE INDEX IF NOT EXISTS tenant_brief_tenant_idx ON tenant_brief (tenant_id);
CREATE INDEX IF NOT EXISTS tenant_brief_status_idx ON tenant_brief (status);

-- Гранты. panel_rw — полный RW (кросс-тенантный платформенный раздел, без RLS).
-- Бот пишет ответы по token: та же роль, что читает tenant_settings в get_legal_doc_data.
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'panel_rw') THEN
        GRANT SELECT, INSERT, UPDATE ON tenant_brief TO panel_rw;
    END IF;
END $$;
```

> **Примечание для реализатора:** имя роли бота проверить: `grep -rn "GRANT" db/*.sql | grep -i tenant_settings` — какой роли выдан SELECT на `tenant_settings` (её же read/update нужен боту на `tenant_brief`). Если роль отдельная (не panel_rw) — добавить второй `DO $$ ... GRANT SELECT, UPDATE ON tenant_brief TO <bot_role> ... $$`. Если бот ходит owner-DSN (gen_user, bypass) — доп. грант не нужен. НЕ применять миграцию без «да» владельца.

- [ ] **Step 2: Написать падающий db-смоук `scripts/brief_db_smoke.py`**

```python
#!/usr/bin/env python3
"""DB-смоук tenant_brief (гонит КОНТРОЛЛЕР на risuy_dev).
Проверяет жизненный цикл: create → get_by_token → submit → set_proposal →
mark_applied, и что чужой токен не резолвится. Использует admin-panel/db.py
для панельных функций и bot-telegram/db.py — для ботовых (обе роли — owner-DSN).

Запуск:
  BRIEF_SMOKE_DSN="postgresql://gen_user:...@81.31.246.136:5432/risuy_dev?sslmode=require" \
  PYTHONPATH=admin-panel:. ./.venv-smoke/bin/python scripts/brief_db_smoke.py
"""
import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "admin-panel"))
os.environ.setdefault("DATABASE_URL", "postgresql://x/y")
os.environ.setdefault("SESSION_SECRET", "smoke-secret")
os.environ.setdefault("ADMIN_USERNAME", "smoke")
os.environ.setdefault("ADMIN_PASSWORD_HASH", "$argon2id$v=19$m=65536,t=3,p=4$c21va2U$c21va2U")

import asyncpg  # noqa: E402
import db  # noqa: E402  (admin-panel/db.py)

DSN = os.environ.get("BRIEF_SMOKE_DSN")
if not DSN or "/risuy_dev" not in DSN.split("?")[0]:
    raise SystemExit("Задайте BRIEF_SMOKE_DSN на risuy_dev")

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


async def _cleanup(c) -> None:
    await c.execute("delete from tenant_brief where tenant_id in "
                    "(select id from tenants where slug like 'smoke-brief-%')")
    await c.execute("delete from tenants where slug like 'smoke-brief-%'")


async def main() -> None:
    db.pool = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    try:
        db.set_active_tenant(None)
        async with db.pool.acquire() as c:
            await _cleanup(c)
            ta = await c.fetchval(
                "insert into tenants(slug,name,status) values('smoke-brief-a','Клиент А','active') returning id")

        print("1. create_tenant_brief:")
        brief_id, token = await db.create_tenant_brief(
            ta, actor="smoke", ip=None, user_agent=None, ttl_days=30)
        check("вернул id и token", bool(brief_id) and len(token) >= 16)

        print("2. get_brief_by_token резолвит:")
        got = await db.get_brief_by_token(token)
        check("токен резолвится в тенанта А", got is not None and str(got["tenant_id"]) == str(ta))
        check("статус pending", got and got["status"] == "pending")

        print("3. get_brief_by_token на мусорный токен → None:")
        check("чужой токен None", await db.get_brief_by_token("нет-такого-токена") is None)

        print("4. submit_brief:")
        res = await db.submit_brief(token, {"version": 1, "business": {"company_name": "А"}})
        check("submit ok", res == "ok")
        again = await db.submit_brief(token, {"version": 1})
        check("повторный submit → already", again == "already")
        got2 = await db.get_tenant_brief(brief_id)
        check("статус submitted", got2 and got2["status"] == "submitted")
        check("answers сохранены", got2 and got2["answers"].get("business", {}).get("company_name") == "А")

        print("5. set_brief_proposal → proposed:")
        await db.set_brief_proposal(brief_id, {"settings": {}, "products": [],
                                               "recommendations": [], "gaps": []})
        got3 = await db.get_tenant_brief(brief_id)
        check("статус proposed", got3 and got3["status"] == "proposed")

        print("6. mark_brief_applied → applied:")
        await db.mark_brief_applied(brief_id, {"sections": ["funnel"]},
                                    actor="smoke", ip=None, user_agent=None)
        got4 = await db.get_tenant_brief(brief_id)
        check("статус applied", got4 and got4["status"] == "applied")

        print("7. list_tenant_briefs содержит наш бриф:")
        lst = await db.list_tenant_briefs()
        check("бриф в списке", any(str(b["id"]) == str(brief_id) for b in lst))

    finally:
        async with db.pool.acquire() as c:
            await _cleanup(c)
        await db.pool.close()

    print()
    if FAILS:
        print("❌ ПРОВАЛЫ: " + ", ".join(FAILS))
        sys.exit(1)
    print("✅ brief_db smoke — OK")


if __name__ == "__main__":
    asyncio.run(main())
```

Примечание: смоук импортирует `admin-panel/db.py`. Ботовые функции `get_brief_by_token`/`submit_brief` продублировать логикой в `admin-panel/db.py` НЕЛЬЗЯ — вместо этого смоук вызывает панельные аналоги. Для теста ботовых функций достаточно, что панельные `get_tenant_brief`/`submit`-путь покрывают ту же таблицу; ботовые `get_brief_by_token`/`submit_brief` тестируются тем же смоуком, если импортировать их из `admin-panel/db.py` — поэтому реализуем `get_brief_by_token`/`submit_brief` в ОБОИХ модулях с идентичным SQL. Смоук зовёт панельные версии (они есть в `db`), а ботовые проверяются `py_compile` в Task 3.

- [ ] **Step 3: Реализовать панельные функции в `admin-panel/db.py`**

Добавить в конец файла (использует существующий `pool`, `audit`/`_insert_audit`, `json`):

```python
# ── Бриф-онбординг тенанта (tenant_brief) ─────────────────────────────────────
import secrets as _secrets
from datetime import datetime as _dt, timedelta as _td, timezone as _tz


async def create_tenant_brief(tenant_id, *, actor: str, ip: str | None,
                              user_agent: str | None, ttl_days: int = 30) -> tuple[str, str]:
    """Создаёт бриф со статусом pending и секретным токеном. Возвращает (id, token)."""
    token = _secrets.token_hex(16)
    expires = _dt.now(_tz.utc) + _td(days=ttl_days)
    async with pool.acquire() as c:
        async with c.transaction():
            row = await c.fetchrow(
                "insert into tenant_brief(tenant_id, token, status, created_by, expires_at) "
                "values($1,$2,'pending',$3,$4) returning id",
                tenant_id, token, actor, expires,
            )
            await _insert_audit(c, actor=actor, action="brief_created", ip=ip,
                                user_agent=user_agent, detail={"tenant_id": str(tenant_id)})
    return str(row["id"]), token


async def list_tenant_briefs() -> list[dict]:
    """Кросс-тенантный список брифов (для платформенного бриф-центра)."""
    async with pool.acquire() as c:
        rows = await c.fetch(
            "select b.id, b.tenant_id, t.name as tenant_name, t.slug as tenant_slug, "
            "b.status, b.created_at, b.submitted_at, b.applied_at "
            "from tenant_brief b join tenants t on t.id = b.tenant_id "
            "order by b.created_at desc")
    return [dict(r) for r in rows]


async def get_tenant_brief(brief_id: str) -> dict | None:
    async with pool.acquire() as c:
        row = await c.fetchrow(
            "select b.*, t.name as tenant_name, t.slug as tenant_slug "
            "from tenant_brief b join tenants t on t.id = b.tenant_id where b.id = $1",
            brief_id)
    if not row:
        return None
    d = dict(row)
    for k in ("answers", "proposal", "applied"):
        if isinstance(d.get(k), str):
            d[k] = json.loads(d[k]) if d[k] else None
    return d


async def set_brief_proposal(brief_id: str, proposal: dict) -> None:
    async with pool.acquire() as c:
        await c.execute(
            "update tenant_brief set proposal = $2::jsonb, status = 'proposed', "
            "proposed_at = now() where id = $1",
            brief_id, json.dumps(proposal, ensure_ascii=False))


async def mark_brief_applied(brief_id: str, applied: dict, *, actor: str,
                             ip: str | None, user_agent: str | None) -> None:
    async with pool.acquire() as c:
        async with c.transaction():
            await c.execute(
                "update tenant_brief set applied = $2::jsonb, status = 'applied', "
                "applied_at = now() where id = $1",
                brief_id, json.dumps(applied, ensure_ascii=False))
            await _insert_audit(c, actor=actor, action="brief_applied", ip=ip,
                                user_agent=user_agent,
                                detail={"brief_id": brief_id, "sections": applied.get("sections", [])})


async def get_brief_by_token(token: str) -> dict | None:
    """По секретному токену → бриф (для рендера/сабмита). None если неизвестен."""
    async with pool.acquire() as c:
        row = await c.fetchrow(
            "select b.id, b.tenant_id, t.name as tenant_name, b.status, b.expires_at "
            "from tenant_brief b join tenants t on t.id = b.tenant_id where b.token = $1",
            token)
    if not row:
        return None
    d = dict(row)
    exp = d.get("expires_at")
    d["expired"] = bool(exp and exp < _dt.now(_tz.utc))
    return d


async def submit_brief(token: str, answers: dict) -> str:
    """Пишет ответы по токену. Возвращает ok|already|expired|unknown."""
    async with pool.acquire() as c:
        async with c.transaction():
            row = await c.fetchrow(
                "select id, status, expires_at from tenant_brief where token = $1 for update",
                token)
            if not row:
                return "unknown"
            if row["expires_at"] and row["expires_at"] < _dt.now(_tz.utc):
                return "expired"
            if row["status"] != "pending":
                return "already"
            await c.execute(
                "update tenant_brief set answers = $2::jsonb, status = 'submitted', "
                "submitted_at = now() where id = $1",
                row["id"], json.dumps(answers, ensure_ascii=False))
    return "ok"
```

Примечание: если в `admin-panel/db.py` уже импортированы `secrets`/`datetime`/`json` вверху файла — не дублировать импорты, убрать локальные.

- [ ] **Step 4: Реализовать ботовые функции в `bot-telegram/db.py`**

Рядом с `get_legal_doc_data` (~строка 1740) добавить `get_brief_by_token` и `submit_brief` с ИДЕНТИЧНЫМ SQL (тот же паттерн `async with pool.acquire() as c`). Скопировать тела `get_brief_by_token`/`submit_brief` из Step 3 дословно (в боте есть свой `pool`, нужен `import json`, `from datetime import datetime, timezone` — проверить, что уже импортированы; если нет — добавить вверху файла).

- [ ] **Step 5: КОНТРОЛЛЕР применяет миграцию на risuy_dev и гонит смоук**

⚠️ Только контроллер, только по «да» владельца. Owner-DSN не в субагенты.

```bash
cd ~/Downloads/risuy-ecosystem
~/.claude/scripts/twc-migrate.sh 4171827 81.31.246.136 risuy_dev gen_user db/migrate_tenant_brief.sql
BRIEF_SMOKE_DSN="<owner-dsn с /risuy_dev>" PYTHONPATH=admin-panel:. ./.venv-smoke/bin/python scripts/brief_db_smoke.py
```
Expected: миграция без ошибок; смоук `✅ brief_db smoke — OK`.

- [ ] **Step 6: py_compile ботовых функций**

Run: `cd ~/Downloads/risuy-ecosystem && ./.venv-smoke/bin/python -m py_compile bot-telegram/db.py && echo OK`
Expected: `OK`

- [ ] **Step 7: Коммит**

```bash
cd ~/Downloads/risuy-ecosystem
git add db/migrate_tenant_brief.sql admin-panel/db.py bot-telegram/db.py scripts/brief_db_smoke.py
git commit -m "feat(brief): слой данных tenant_brief — миграция + db-функции (панель/бот) + db-смоук"
```

---

### Task 3: Бриф-лендинг в боте (публичные роуты `/brief/{token}`)

Рендер брендированного опроса из схемы + приём ответов. Паттерн `_club_landing`.

**Files:**
- Create: `bot-telegram/templates/brief.html` (HTML-оболочка, встроенный CSS + минимальный JS)
- Modify: `bot-telegram/bot.py` (добавить `_brief_landing`, `_brief_submit`, регистрацию роутов в `_start_health()`, регэксп токена)
- Create: `scripts/brief_landing_smoke.py`

**Interfaces:**
- Consumes: `shared.brief_schema` (SECTIONS, validate_answers, visible_questions), `db.get_brief_by_token`, `db.submit_brief`, `config.BOT_PUBLIC_BASE_URL`, `_client_ip`, `_rl_allow_chat`
- Produces: HTTP `GET/POST /brief/{token}` в боте

- [ ] **Step 1: Написать HTML-оболочку `bot-telegram/templates/brief.html`**

Самодостаточная страница: встроенный `<style>`, прогресс, шаги. Схема инжектится как JSON в `<script id="brief-schema">`, ванильный JS строит поля секций и переключает `show_if`. Плейсхолдеры (заменяет Python): `{{TITLE}}`, `{{COMPANY}}`, `{{ACTION}}`, `{{SCHEMA_JSON}}`, `{{ANSWERS_JSON}}` (пусто), `{{NOTICE}}` (152-ФЗ уведомление + просьба не вставлять ПДн третьих лиц). Мобайл-фёрст, акцентный цвет `#E63946` (как кнопка клуба). Форма шлёт `method="post"` на `{{ACTION}}` с полями `q_<key>`.

Ключевой фрагмент (структура — реализатор дополняет CSS/JS):

```html
<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{TITLE}}</title>
<style>
  :root{--accent:#E63946}
  body{font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;margin:0;
       background:#faf8f5;color:#1f2937;line-height:1.5}
  .wrap{max-width:640px;margin:0 auto;padding:24px 16px 64px}
  h1{font-size:22px} .sec{background:#fff;border:1px solid #e5e2da;border-radius:14px;
       padding:16px;margin:16px 0} .sec h2{font-size:16px;margin:0 0 12px}
  label{display:block;margin:12px 0} label>span{display:block;font-weight:600;margin-bottom:6px}
  input,textarea,select{width:100%;box-sizing:border-box;padding:10px;border:1px solid #cfcabc;
       border-radius:10px;font:inherit} textarea{min-height:80px}
  .notice{font-size:13px;color:#6b7280;background:#fff;border:1px dashed #cfcabc;
       border-radius:12px;padding:12px;margin:16px 0}
  .btn{display:block;width:100%;padding:14px;background:var(--accent);color:#fff;border:0;
       border-radius:12px;font-size:16px;font-weight:700;cursor:pointer;margin-top:16px}
  .req{color:var(--accent)}
</style></head><body><div class="wrap">
  <h1>{{TITLE}}</h1>
  <p>Бриф для <b>{{COMPANY}}</b> — чтобы настроить вашего ИИ-сотрудника точно под ваш бизнес.</p>
  <div class="notice">{{NOTICE}}</div>
  <form method="post" action="{{ACTION}}" id="brief-form"><div id="sections"></div>
    <button class="btn" type="submit">Отправить бриф</button></form>
  <script id="brief-schema" type="application/json">{{SCHEMA_JSON}}</script>
  <script>
    const schema = JSON.parse(document.getElementById('brief-schema').textContent);
    const root = document.getElementById('sections');
    function field(q){
      const wrap=document.createElement('label'); wrap.dataset.key=q.key;
      const lab=document.createElement('span');
      lab.innerHTML = q.label + (q.required?' <span class="req">*</span>':'');
      wrap.appendChild(lab); let el;
      if(q.type==='textarea'){el=document.createElement('textarea');}
      else if(q.type==='choice'){el=document.createElement('select');
        const empty=document.createElement('option');empty.value='';empty.textContent='— выберите —';el.appendChild(empty);
        (q.options||[]).forEach(o=>{const op=document.createElement('option');op.value=o;op.textContent=o;el.appendChild(op);});}
      else if(q.type==='multichoice'){el=document.createElement('div');
        (q.options||[]).forEach(o=>{const l=document.createElement('label');l.style.fontWeight='400';
          l.innerHTML='<input type="checkbox" name="q_'+q.key+'" value="'+o+'" style="width:auto"> '+o;el.appendChild(l);});
        wrap.appendChild(el); return wrap;}
      else {el=document.createElement('input');el.type='text';}
      el.name='q_'+q.key; if(q.max)el.maxLength=q.max; wrap.appendChild(el); return wrap;
    }
    schema.sections.forEach(sec=>{const box=document.createElement('div');box.className='sec';
      const h=document.createElement('h2');h.textContent=sec.title;box.appendChild(h);
      sec.questions.forEach(q=>box.appendChild(field(q)));root.appendChild(box);});
    // ветвление show_if
    function applyBranch(){schema.questions.forEach(q=>{if(!q.show_if)return;
      const src=document.querySelector('[name="q_'+q.show_if.q+'"]');
      const cur=src?src.value:''; const box=document.querySelector('label[data-key="'+q.key+'"]');
      if(box)box.style.display = q.show_if.in.includes(cur)?'':'none';});}
    document.getElementById('brief-form').addEventListener('input',applyBranch); applyBranch();
  </script>
</div></body></html>
```

- [ ] **Step 2: Написать падающий смоук `scripts/brief_landing_smoke.py`**

```python
#!/usr/bin/env python3
"""Смоук рендера бриф-лендинга: _brief_html содержит секции схемы и экранирует
брендинг; парсинг ответов из формы. БЕЗ БД и БЕЗ сети (мокаем).
  PYTHONPATH=bot-telegram:. python3 scripts/brief_landing_smoke.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "bot-telegram"))
for k, v in {"BOT_TOKEN": "123:smoke", "DATABASE_URL": "postgresql://x/y",
             "CHANNEL_ID": "-100123", "CHANNEL_URL": "https://t.me/x", "GUIDE_URL": "https://x"}.items():
    os.environ.setdefault(k, v)

import bot as botmod  # noqa: E402  (bot-telegram/bot.py)

FAILS: list[str] = []


def check(name, cond, detail=""):
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


def main() -> None:
    html = botmod._brief_html(title="Бриф", company='Иван «Тест» & Ко', action="/brief/abc")
    print("1. рендер:")
    check("содержит секцию «О бизнесе»", "О бизнесе" in html)
    check("содержит секцию «Реквизиты оператора (152-ФЗ)»", "Реквизиты оператора" in html)
    check("брендинг экранирован (нет сырых кавычек-инъекций)", "«Тест»" in html and "<script>Иван" not in html)
    check("есть форма на action", 'action="/brief/abc"' in html)
    check("есть 152-ФЗ уведомление", "персональные данные" in html.lower() or "не вставляйте" in html.lower())

    print("2. парсинг ответов формы (мультизначные чекбоксы):")
    # эмулируем aiohttp MultiDict как список пар
    pairs = [("q_company_name", "Клиент"), ("q_channels_used", "Telegram"), ("q_channels_used", "VK")]
    answers = botmod._brief_parse(pairs)
    check("company_name разобран", answers.get("company_name") == "Клиент")
    check("channels_used — список", answers.get("channels_used") == ["Telegram", "VK"])

    print()
    if FAILS:
        print("❌ ПРОВАЛЫ: " + ", ".join(FAILS))
        sys.exit(1)
    print("✅ brief_landing smoke — OK")


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Запустить — убедиться, что падает**

Run: `cd ~/Downloads/risuy-ecosystem && PYTHONPATH=bot-telegram:. python3 scripts/brief_landing_smoke.py`
Expected: FAIL — `AttributeError: module 'bot' has no attribute '_brief_html'`.

- [ ] **Step 4: Реализовать в `bot-telegram/bot.py`**

Добавить импорт схемы вверху (рядом с прочими): `from shared import brief_schema` (проверить, что `shared` в PYTHONPATH бота; если нет — план Task 3 включает добавление `sys.path`/пакета; в Dockerfile бота уже копируется `shared/` — проверить `grep -n "shared" bot-telegram/Dockerfile`). Затем:

```python
import html as _html
import json as _json
import re as _re

_BRIEF_TOKEN_RE = _re.compile(r"^[0-9a-f]{32}$")

_BRIEF_NOTICE = (
    "Вы передаёте бизнес-данные оператору сервиса «ИИ-Агент Про». "
    "Пожалуйста, НЕ вставляйте персональные данные ваших клиентов (ФИО, телефоны, "
    "списки контактов) — опишите портрет аудитории, а не конкретных людей."
)


def _brief_schema_payload() -> dict:
    """Схема в форме, удобной для JS фронта."""
    questions = []
    for sec in brief_schema.SECTIONS:
        for q in sec["questions"]:
            questions.append(q)
    return {"sections": brief_schema.SECTIONS, "questions": questions}


def _brief_html(*, title: str, company: str, action: str) -> str:
    """Рендер самодостаточной страницы брифа из шаблона + инъекция схемы."""
    tmpl_path = os.path.join(os.path.dirname(__file__), "templates", "brief.html")
    with open(tmpl_path, encoding="utf-8") as f:
        tmpl = f.read()
    schema_json = _json.dumps(_brief_schema_payload(), ensure_ascii=False)
    return (tmpl
            .replace("{{TITLE}}", _html.escape(title))
            .replace("{{COMPANY}}", _html.escape(company))
            .replace("{{ACTION}}", _html.escape(action))
            .replace("{{NOTICE}}", _html.escape(_BRIEF_NOTICE))
            .replace("{{SCHEMA_JSON}}", schema_json)
            .replace("{{ANSWERS_JSON}}", "{}"))


def _brief_parse(pairs) -> dict:
    """Из пар (name,value) формы → {question_key: value|list}. name = q_<key>."""
    idx = brief_schema.question_index()
    out: dict = {}
    for name, value in pairs:
        if not name.startswith("q_"):
            continue
        key = name[2:]
        q = idx.get(key)
        if not q:
            continue
        if q["type"] == "multichoice":
            out.setdefault(key, []).append(value)
        else:
            out[key] = value
    return out


async def _brief_landing(request: web.Request) -> web.StreamResponse:
    token = request.match_info.get("token", "")
    if not _BRIEF_TOKEN_RE.match(token):
        return web.Response(status=404, text="Ссылка недействительна")
    try:
        data = await db.get_brief_by_token(token)
    except Exception:
        logger.warning("brief landing db error", exc_info=True)
        data = None
    if data is None or data.get("expired"):
        return web.Response(status=404, text="Ссылка недействительна или истекла")
    if data["status"] != "pending":
        return web.Response(text="Бриф уже получен. Спасибо!", content_type="text/html", charset="utf-8")
    html = _brief_html(title="Бриф — ИИ-Агент Про",
                       company=data.get("tenant_name") or "вашей компании",
                       action=f"/brief/{token}")
    resp = web.Response(text=html, content_type="text/html", charset="utf-8")
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


async def _brief_submit(request: web.Request) -> web.StreamResponse:
    token = request.match_info.get("token", "")
    if not _BRIEF_TOKEN_RE.match(token):
        return web.Response(status=404, text="Ссылка недействительна")
    ip = _client_ip(request)
    if not _rl_allow_chat(ip):
        return web.Response(status=429, text="Слишком много попыток, попробуйте позже")
    post = await request.post()
    answers = _brief_parse(list(post.items()))
    errs = brief_schema.validate_answers(answers)
    if errs:
        # простая страница с ошибкой + возврат назад (ответы не теряем на клиенте)
        return web.Response(
            text="<p>Проверьте обязательные поля:</p><ul>"
                 + "".join(f"<li>{_html.escape(e)}</li>" for e in errs)
                 + '</ul><a href="javascript:history.back()">Назад</a>',
            content_type="text/html", charset="utf-8", status=400)
    answers["version"] = brief_schema.BRIEF_VERSION
    try:
        res = await db.submit_brief(token, answers)
    except Exception:
        logger.warning("brief submit db error", exc_info=True)
        return web.Response(status=500, text="Ошибка сохранения, попробуйте ещё раз")
    if res == "ok":
        return web.Response(text="<h1>Спасибо!</h1><p>Бриф получен. Мы настроим вашего "
                                 "ИИ-сотрудника и свяжемся с вами.</p>",
                            content_type="text/html", charset="utf-8")
    return web.Response(text="Бриф уже был отправлен ранее.", content_type="text/html", charset="utf-8")
```

Зарегистрировать роуты в `_start_health()` (bot.py:441-456), рядом с `/club/{slug}`:

```python
app.router.add_get("/brief/{token}", _brief_landing)
app.router.add_post("/brief/{token}", _brief_submit)
```

- [ ] **Step 5: Запустить смоук — убедиться, что проходит**

Run: `cd ~/Downloads/risuy-ecosystem && PYTHONPATH=bot-telegram:. python3 scripts/brief_landing_smoke.py`
Expected: PASS — `✅ brief_landing smoke — OK`

- [ ] **Step 6: py_compile всего бота**

Run: `cd ~/Downloads/risuy-ecosystem && ./.venv-smoke/bin/python -m py_compile bot-telegram/bot.py && echo OK`
Expected: `OK`

- [ ] **Step 7: Коммит**

```bash
cd ~/Downloads/risuy-ecosystem
git add bot-telegram/templates/brief.html bot-telegram/bot.py scripts/brief_landing_smoke.py
git commit -m "feat(brief): публичный бриф-лендинг в боте — рендер схемы + приём ответов"
```

---

### Task 4: Оркестратор `admin-panel/brief_orchestrator.py`

Чистая функция `analyze(answers) → proposal`. LLM-разбор + обезличивание + детерминированный фолбэк. Не пишет в прод.

**Files:**
- Create: `admin-panel/brief_orchestrator.py`
- Test: `scripts/brief_orchestrator_smoke.py`

**Interfaces:**
- Consumes: `shared.brief_schema`, существующий LLM-клиент (реализатор находит его: `grep -rn "def ask_\|gateway\|cloud_ai\|chat/completions" admin-panel/*.py bot-telegram/ai.py` — использовать тот же путь вызова); существующий обезличиватель (`grep -rn "mask\|обезлич\|anonymi" admin-panel/ shared/`).
- Produces:
  - `async def analyze(answers: dict, *, llm=None) -> dict` → `{"settings": {...}, "products": [...], "recommendations": [...], "gaps": [...]}`
  - `def fallback_proposal(answers: dict) -> dict` — детерминированный, без LLM
  - `llm` — необязательная async-функция `llm(prompt: str) -> str` (для тестов; по умолчанию реальный клиент)

- [ ] **Step 1: Написать падающий тест `scripts/brief_orchestrator_smoke.py`**

```python
#!/usr/bin/env python3
"""Смоук оркестратора: форма proposal валидна; фолбэк без LLM работает;
LLM-сбой не крешит; no-fabrication (нет ИНН в ответах → gap, не выдумка).
  PYTHONPATH=admin-panel:. python3 scripts/brief_orchestrator_smoke.py
"""
import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "admin-panel"))
os.environ.setdefault("DATABASE_URL", "postgresql://x/y")
os.environ.setdefault("SESSION_SECRET", "smoke-secret")
os.environ.setdefault("ADMIN_USERNAME", "smoke")
os.environ.setdefault("ADMIN_PASSWORD_HASH", "$argon2id$v=19$m=65536,t=3,p=4$c21va2U$c21va2U")

import brief_orchestrator as orch  # noqa: E402

FAILS: list[str] = []


def check(name, cond, detail=""):
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


def _valid_shape(p: dict) -> bool:
    return (isinstance(p, dict) and isinstance(p.get("settings"), dict)
            and isinstance(p.get("products"), list) and isinstance(p.get("recommendations"), list)
            and isinstance(p.get("gaps"), list))


ANSWERS_NO_INN = {"version": 1, "company_name": "Кофейня «Зерно»", "b2b_or_b2c": "B2C",
                  "niche": "кофейня", "products_list": "Абонемент — 3000 ₽ — 30 чашек",
                  "tone": "Дружелюбный на «ты»", "channels_used": ["Telegram"]}


async def main() -> None:
    print("1. фолбэк без LLM:")
    fb = orch.fallback_proposal(ANSWERS_NO_INN)
    check("форма валидна", _valid_shape(fb))
    check("продукты перенесены", len(fb["products"]) >= 1)
    check("company_name → funnel", fb["settings"].get("funnel", {}).get("company_name") == "Кофейня «Зерно»")

    print("2. no-fabrication: нет ИНН → gap, не выдумка:")
    check("есть gap про ИНН", any("инн" in (g.get("field", "") + g.get("question", "")).lower()
                                  for g in fb["gaps"]))
    check("ИНН НЕ выдуман", not fb["settings"].get("funnel", {}).get("operator_inn"))

    print("3. LLM-сбой → фолбэк, не креш:")
    async def broken_llm(prompt: str) -> str:
        raise RuntimeError("llm down")
    p = await orch.analyze(ANSWERS_NO_INN, llm=broken_llm)
    check("вернул валидный proposal при сбое LLM", _valid_shape(p))

    print("4. LLM-успех (мок валидного JSON):")
    async def ok_llm(prompt: str) -> str:
        return ('{"settings":{"persona":{"name":"Бариста","role":"ИИ-продавец",'
                '"behavior_prompt":"дружелюбно на ты","knowledge":""},"funnel":{},'
                '"triggers":[],"channels":{}},"products":[],"recommendations":'
                '[{"title":"Включить приветствие","why":"первое касание","section":"funnel"}],"gaps":[]}')
    p2 = await orch.analyze(ANSWERS_NO_INN, llm=ok_llm)
    check("распарсил LLM-ответ", _valid_shape(p2) and p2["settings"]["persona"]["name"] == "Бариста")

    print()
    if FAILS:
        print("❌ ПРОВАЛЫ: " + ", ".join(FAILS))
        sys.exit(1)
    print("✅ brief_orchestrator smoke — OK")


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `cd ~/Downloads/risuy-ecosystem && PYTHONPATH=admin-panel:. python3 scripts/brief_orchestrator_smoke.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'brief_orchestrator'`.

- [ ] **Step 3: Реализовать `admin-panel/brief_orchestrator.py`**

```python
"""Оркестратор: из ответов брифа собирает черновик настройки ИИ-сотрудника.

Чистая функция без побочных эффектов — только возвращает proposal. Запись в прод —
в панели за HumanGate. LLM-разбор с обезличиванием; при любом сбое — детерминированный
фолбэк из maps_to. Никогда не выдумывает данные: чего нет — в gaps.
"""
from __future__ import annotations

import json
import logging

from shared import brief_schema

logger = logging.getLogger(__name__)

_EMPTY = {"settings": {"persona": {}, "funnel": {}, "triggers": [], "channels": {}},
          "products": [], "recommendations": [], "gaps": []}


def _get(answers: dict, key: str) -> str:
    """Достаёт ответ по ключу вопроса из плоских или секционных answers."""
    if key in answers:
        v = answers[key]
        return v if isinstance(v, str) else v
    for v in answers.values():
        if isinstance(v, dict) and key in v:
            return v[key]
    return ""


def fallback_proposal(answers: dict) -> dict:
    """Детерминированный черновик из maps_to, без LLM. Не выдумывает — пробелы в gaps."""
    p = json.loads(json.dumps(_EMPTY))  # глубокая копия
    funnel = p["settings"]["funnel"]

    for field, qkey in [("company_name", "company_name"), ("operator_name", "operator_name"),
                        ("operator_inn", "operator_inn"), ("operator_email", "operator_email")]:
        val = str(_get(answers, qkey) or "").strip()
        if val:
            funnel[field] = val

    # продукты — по строкам (название — цена — описание)
    raw = str(_get(answers, "products_list") or "").strip()
    for line in [x for x in raw.splitlines() if x.strip()]:
        parts = [s.strip() for s in line.split("—")]
        prod = {"name": parts[0][:200], "caption": (parts[2] if len(parts) > 2 else "")[:4000],
                "kind": "main", "currency": "RUB", "price": None, "link": None}
        p["products"].append(prod)

    # тон → рекомендация по персоне (не пишем поведение сами — только предлагаем)
    tone = str(_get(answers, "tone") or "").strip()
    if tone:
        p["settings"]["persona"]["behavior_prompt"] = f"Общение с клиентами: {tone}."

    # пробелы: обязательные для 152-ФЗ
    for field, label in [("operator_inn", "ИНН оператора"), ("operator_email", "email оператора")]:
        if not funnel.get(field):
            p["gaps"].append({"field": field, "question": f"Не указан {label} — нужен для 152-ФЗ"})

    p["recommendations"].append(
        {"title": "Проверьте черновик перед применением",
         "why": "Собрано автоматически из ответов; отредактируйте формулировки под бренд",
         "section": "all"})
    return p


def _build_prompt(answers: dict) -> str:
    idx = brief_schema.question_index()
    lines = ["Ты — конфигуратор ИИ-продавца. По ответам брифа собери JSON-черновик настройки.",
             "СТРОГО: не выдумывай данные (ИНН, цены, реквизиты). Чего нет — клади в gaps.",
             "Отвечай ТОЛЬКО валидным JSON вида:",
             '{"settings":{"persona":{"name","role","behavior_prompt","knowledge"},'
             '"funnel":{"company_name","operator_name","operator_inn","operator_email","welcome_text"},'
             '"triggers":[{"kind","value"}],"channels":{}},"products":[{"name","price","currency",'
             '"caption","kind"}],"recommendations":[{"title","why","section"}],"gaps":[{"field","question"}]}',
             "", "Ответы клиента:"]
    for key, q in idx.items():
        val = _get(answers, key)
        if val:
            lines.append(f"- {q['label']}: {val}")
    return "\n".join(lines)


async def _default_llm(prompt: str) -> str:
    """Реальный вызов LLM через существующий бэкенд + обезличивание.

    РЕАЛИЗАТОР: подставить точный путь. Пример каркаса:
        from shared import masker            # обезличиватель проекта
        import ai_client                      # существующий клиент cloud-ai/gateway
        masked = masker.mask(prompt)
        raw = await ai_client.complete(masked, response_format="json_object",
                                       timeout=420, max_tokens=8192)
        return masker.unmask(raw)
    Точные имена — из grep по проекту (см. Interfaces). Если обезличиватель работает
    по «сущностям ПДн» — прогонять только пользовательские ответы, не служебный промпт.
    """
    raise NotImplementedError("подставить реальный LLM-клиент проекта")


def _merge_over_fallback(fb: dict, llm_obj: dict) -> dict:
    """Накладывает валидные поля LLM поверх фолбэка (LLM обогащает, не ломает форму)."""
    out = json.loads(json.dumps(fb))
    s = llm_obj.get("settings") or {}
    for grp in ("persona", "funnel", "channels"):
        if isinstance(s.get(grp), dict):
            out["settings"].setdefault(grp, {}).update({k: v for k, v in s[grp].items() if v})
    if isinstance(s.get("triggers"), list):
        out["settings"]["triggers"] = s["triggers"]
    for k in ("products", "recommendations", "gaps"):
        if isinstance(llm_obj.get(k), list) and llm_obj[k]:
            out[k] = llm_obj[k]
    return out


async def analyze(answers: dict, *, llm=None) -> dict:
    """Главная точка: LLM-разбор поверх детерминированного фолбэка. Никогда не крешит."""
    fb = fallback_proposal(answers)
    call = llm or _default_llm
    try:
        raw = await call(_build_prompt(answers))
        obj = json.loads(raw)
        if not isinstance(obj, dict):
            raise ValueError("LLM вернул не объект")
        return _merge_over_fallback(fb, obj)
    except Exception:
        logger.warning("orchestrator LLM failed, using fallback", exc_info=True)
        return fb
```

- [ ] **Step 4: Запустить тест — убедиться, что проходит**

Run: `cd ~/Downloads/risuy-ecosystem && PYTHONPATH=admin-panel:. python3 scripts/brief_orchestrator_smoke.py`
Expected: PASS — `✅ brief_orchestrator smoke — OK`

- [ ] **Step 5: Реализатор подключает реальный LLM в `_default_llm`**

Заменить `NotImplementedError` фактическим вызовом (см. docstring + Interfaces grep). НЕ ломать сигнатуру. Смоук остаётся зелёным (тест инжектит свой `llm`). Проверить, что no-op импорт клиента не тянет сетевые вызовы на импорте.

- [ ] **Step 6: Коммит**

```bash
cd ~/Downloads/risuy-ecosystem
git add admin-panel/brief_orchestrator.py scripts/brief_orchestrator_smoke.py
git commit -m "feat(brief): оркестратор — черновик из ответов (LLM+обезличивание, детерм. фолбэк)"
```

---

### Task 5: Центр решений + применение (панель, HumanGate)

Платформенный раздел «Бриф-центр»: создать ссылку, смотреть ответы, запускать оркестратор, применять черновик посекционно теми же сеттерами.

**Files:**
- Create: `admin-panel/brief_apply.py` (применение через существующие сеттеры)
- Create: `admin-panel/templates/brief_center.html` (список)
- Create: `admin-panel/templates/brief_detail.html` (ответы + диф + применение)
- Modify: `admin-panel/app.py` (роуты `/brief-center*`)
- Modify: `admin-panel/templates/base.html` (пункт меню под is_platform)
- Test: `scripts/brief_apply_smoke.py` (db-смоук, контроллер на risuy_dev)

**Interfaces:**
- Consumes: `db.list_tenant_briefs`, `db.get_tenant_brief`, `db.create_tenant_brief`, `db.set_brief_proposal`, `db.mark_brief_applied`, `brief_orchestrator.analyze`, сеттеры `db.set_funnel_config`, `db.create_product_with_audit`, `db.create_tenant_trigger`, `db.set_channel_agent`, `db.add_dynamic_persona`, `db.set_persona_role`, `db.set_active_tenant`; плумбинг `require_session`, `_require_admin`, `_enforce_csrf`, `_ip`, `_ua`, `templates`; `config.BOT_PUBLIC_BASE_URL`
- Produces:
  - `admin-panel/brief_apply.py`: `async def apply_proposal(tenant_id, proposal: dict, sections: list[str], *, actor, ip, user_agent) -> dict` → `{"sections": [...], "errors": [...]}`
  - Роуты: `GET /brief-center`, `POST /brief-center/create`, `GET /brief-center/{brief_id}`, `POST /brief-center/{brief_id}/orchestrate`, `POST /brief-center/{brief_id}/apply`

- [ ] **Step 1: Написать падающий db-смоук `scripts/brief_apply_smoke.py`**

```python
#!/usr/bin/env python3
"""DB-смоук применения черновика (контроллер, risuy_dev): apply_proposal пишет в
tenant_settings/products через существующие сеттеры, скоуплено на тенанта.
  BRIEF_APPLY_SMOKE_DSN="...risuy_dev..." PYTHONPATH=admin-panel:. \
    ./.venv-smoke/bin/python scripts/brief_apply_smoke.py
"""
import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "admin-panel"))
os.environ.setdefault("DATABASE_URL", "postgresql://x/y")
os.environ.setdefault("SESSION_SECRET", "smoke-secret")
os.environ.setdefault("ADMIN_USERNAME", "smoke")
os.environ.setdefault("ADMIN_PASSWORD_HASH", "$argon2id$v=19$m=65536,t=3,p=4$c21va2U$c21va2U")

import asyncpg  # noqa: E402
import db  # noqa: E402
import brief_apply  # noqa: E402

DSN = os.environ.get("BRIEF_APPLY_SMOKE_DSN")
if not DSN or "/risuy_dev" not in DSN.split("?")[0]:
    raise SystemExit("Задайте BRIEF_APPLY_SMOKE_DSN на risuy_dev")

FAILS: list[str] = []


def check(name, cond, detail=""):
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


async def _cleanup(c):
    await c.execute("delete from products where tenant_id in "
                    "(select id from tenants where slug like 'smoke-apply-%')")
    await c.execute("delete from tenant_settings where tenant_id in "
                    "(select id from tenants where slug like 'smoke-apply-%')")
    await c.execute("delete from tenants where slug like 'smoke-apply-%'")


async def main() -> None:
    db.pool = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    try:
        db.set_active_tenant(None)
        async with db.pool.acquire() as c:
            await _cleanup(c)
            ta = await c.fetchval(
                "insert into tenants(slug,name,status) values('smoke-apply-a','A','active') returning id")

        proposal = {
            "settings": {"funnel": {"company_name": "A-Компания", "operator_name": "ИП Тест",
                                    "operator_inn": "770000000000", "operator_email": "a@example.com"},
                         "persona": {}, "triggers": [], "channels": {}},
            "products": [{"name": "Абонемент", "price": 3000, "currency": "RUB",
                          "caption": "30 чашек", "kind": "main"}],
            "recommendations": [], "gaps": []}

        print("1. apply секций funnel+products:")
        res = await brief_apply.apply_proposal(
            ta, proposal, ["funnel", "products"], actor="smoke", ip=None, user_agent=None)
        check("нет ошибок применения", not res.get("errors"), str(res.get("errors")))
        check("секции отмечены", set(res["sections"]) >= {"funnel", "products"})

        print("2. funnel записан в tenant_settings:")
        db.set_active_tenant(ta)
        async with db.pool.acquire() as c:
            await c.execute("select set_config('app.tenant_id', $1, true)", str(ta))
            cn = await c.fetchval("select value from tenant_settings where tenant_id=$1 and key='company_name'", ta)
            nprod = await c.fetchval("select count(*) from products where tenant_id=$1", ta)
        check("company_name сохранён", cn == "A-Компания", f"cn={cn}")
        check("продукт создан", nprod == 1, f"n={nprod}")

    finally:
        db.set_active_tenant(None)
        async with db.pool.acquire() as c:
            await _cleanup(c)
        await db.pool.close()

    print()
    if FAILS:
        print("❌ ПРОВАЛЫ: " + ", ".join(FAILS))
        sys.exit(1)
    print("✅ brief_apply smoke — OK")


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: Реализовать `admin-panel/brief_apply.py`**

```python
"""Применение черновика оркестратора к настройкам тенанта — за HumanGate.

Вызывает ТЕ ЖЕ db-сеттеры, что и ручные формы (единая валидация). Каждая секция —
независимо; ошибка секции не рушит остальные. Перед вызовами — set_active_tenant(tid).
"""
from __future__ import annotations

import db


async def apply_proposal(tenant_id, proposal: dict, sections: list[str], *,
                         actor: str, ip: str | None, user_agent: str | None) -> dict:
    """Применяет выбранные секции proposal. Возвращает {sections:[применённые], errors:[...]}. """
    db.set_active_tenant(tenant_id)
    settings = proposal.get("settings") or {}
    done: list[str] = []
    errors: list[str] = []

    if "funnel" in sections:
        try:
            fields = {k: v for k, v in (settings.get("funnel") or {}).items() if v not in (None, "")}
            errs = await db.set_funnel_config(tenant_id, fields, actor=actor, ip=ip, user_agent=user_agent)
            if errs:
                errors.append("funnel: " + "; ".join(errs))
            else:
                done.append("funnel")
        except Exception as e:  # noqa: BLE001
            errors.append(f"funnel: {e}")

    if "products" in sections:
        try:
            n = 0
            for prod in proposal.get("products") or []:
                await db.create_product_with_audit(
                    name=str(prod.get("name") or "")[:200], kind=str(prod.get("kind") or "main"),
                    price=prod.get("price"), currency=str(prod.get("currency") or "RUB"),
                    caption=(prod.get("caption") or None), link=(prod.get("link") or None),
                    file_meta=None, status="active", tenant_id=tenant_id,
                    actor=actor, ip=ip, user_agent=user_agent)
                n += 1
            done.append("products") if n else None
        except Exception as e:  # noqa: BLE001
            errors.append(f"products: {e}")

    if "triggers" in sections:
        try:
            n = 0
            for tr in settings.get("triggers") or []:
                kind = str(tr.get("kind") or "")
                val = str(tr.get("value") or "")
                if kind == "stopword" and val:
                    await db.create_tenant_trigger(
                        tenant_id, type_="stopword", action="notify", stopwords=[val],
                        intent_desc="", msg_count=None, notify_chat_id="", notify_topic_id=None,
                        reply_text="", actor=actor, ip=ip, user_agent=user_agent)
                    n += 1
            done.append("triggers") if n else None
        except Exception as e:  # noqa: BLE001
            errors.append(f"triggers: {e}")

    if "channels" in sections:
        try:
            n = 0
            for source, slug in (settings.get("channels") or {}).items():
                if slug:
                    await db.set_channel_agent(tenant_id, source, str(slug),
                                               actor=actor, ip=ip, user_agent=user_agent)
                    n += 1
            done.append("channels") if n else None
        except Exception as e:  # noqa: BLE001
            errors.append(f"channels: {e}")

    return {"sections": done, "errors": errors}
```

> **Персона в v1:** авто-создание/активацию персоны в apply НЕ включаем (нет однозначного сеттера «сделать активной» без риска выдумки). `proposal.settings.persona` показывается как рекомендация в дифе; вашей команде удобнее донастроить в существующем разделе «ИИ-агенты». Если позже нужно — добавить секцию через `db.add_dynamic_persona` + `db.set_persona_role`.

- [ ] **Step 3: Реализовать роуты в `admin-panel/app.py`**

```python
from brief_orchestrator import analyze as _brief_analyze  # вверху с прочими импортами
import brief_apply


@app.get("/brief-center", response_class=HTMLResponse)
async def brief_center(request: Request, session: auth.Session = Depends(require_session),
                       saved: str | None = None, err: str | None = None):
    _require_admin(session)
    briefs = await db.list_tenant_briefs()
    # список тенантов для формы «создать ссылку»
    tenants = await db.list_tenants_min()  # РЕАЛИЗАТОР: см. Step 3a
    base = config.BOT_PUBLIC_BASE_URL
    return templates.TemplateResponse(request, "brief_center.html", {
        "briefs": briefs, "tenants": tenants, "base_url": base, "saved": saved, "err": err,
        "csrf_token": session.csrf_token, "session": session, "active": "brief"})


@app.post("/brief-center/create")
async def brief_center_create(request: Request, session: auth.Session = Depends(require_session),
                              tenant_id: str = Form(""), csrf_token: str = Form("")):
    _require_admin(session)
    await _enforce_csrf(request, session, csrf_token)
    if not tenant_id:
        return RedirectResponse(url="/brief-center?err=no_tenant", status_code=303)
    _id, token = await db.create_tenant_brief(tenant_id, actor=session.actor,
                                              ip=_ip(request), user_agent=_ua(request))
    return RedirectResponse(url=f"/brief-center/{_id}?saved=created", status_code=303)


@app.get("/brief-center/{brief_id}", response_class=HTMLResponse)
async def brief_detail(request: Request, brief_id: str,
                       session: auth.Session = Depends(require_session),
                       saved: str | None = None, err: str | None = None):
    _require_admin(session)
    brief = await db.get_tenant_brief(brief_id)
    if not brief:
        raise StarletteHTTPException(status_code=404)
    base = config.BOT_PUBLIC_BASE_URL
    return templates.TemplateResponse(request, "brief_detail.html", {
        "brief": brief, "link": f"{base}/brief/{brief.get('token', '')}" if base else "",
        "sections": brief_schema.SECTIONS, "saved": saved, "err": err,
        "csrf_token": session.csrf_token, "session": session, "active": "brief"})


@app.post("/brief-center/{brief_id}/orchestrate")
async def brief_orchestrate(request: Request, brief_id: str,
                            session: auth.Session = Depends(require_session),
                            csrf_token: str = Form("")):
    _require_admin(session)
    await _enforce_csrf(request, session, csrf_token)
    brief = await db.get_tenant_brief(brief_id)
    if not brief or not brief.get("answers"):
        return RedirectResponse(url=f"/brief-center/{brief_id}?err=no_answers", status_code=303)
    proposal = await _brief_analyze(brief["answers"])
    await db.set_brief_proposal(brief_id, proposal)
    return RedirectResponse(url=f"/brief-center/{brief_id}?saved=proposed", status_code=303)


@app.post("/brief-center/{brief_id}/apply")
async def brief_apply_route(request: Request, brief_id: str,
                            session: auth.Session = Depends(require_session),
                            csrf_token: str = Form(""), sections: list[str] = Form(default=[])):
    _require_admin(session)
    await _enforce_csrf(request, session, csrf_token)
    brief = await db.get_tenant_brief(brief_id)
    if not brief or not brief.get("proposal"):
        return RedirectResponse(url=f"/brief-center/{brief_id}?err=no_proposal", status_code=303)
    res = await brief_apply.apply_proposal(brief["tenant_id"], brief["proposal"], sections,
                                           actor=session.actor, ip=_ip(request), user_agent=_ua(request))
    await db.mark_brief_applied(brief_id, {"sections": res["sections"], "errors": res["errors"]},
                                actor=session.actor, ip=_ip(request), user_agent=_ua(request))
    flag = "applied" if not res["errors"] else "applied_partial"
    return RedirectResponse(url=f"/brief-center/{brief_id}?saved={flag}", status_code=303)
```

Также убедиться, что `from shared import brief_schema` импортирован вверху app.py.

- [ ] **Step 3a: Добавить `db.list_tenants_min()` в `admin-panel/db.py`** (если нет)

```python
async def list_tenants_min() -> list[dict]:
    """Минимальный список тенантов (id, name, slug) для выбора в формах."""
    async with pool.acquire() as c:
        rows = await c.fetch("select id, name, slug from tenants order by name")
    return [dict(r) for r in rows]
```
(Реализатор: сперва `grep -n "def list_tenants" admin-panel/db.py` — возможно, аналог уже есть; тогда переиспользовать.)

- [ ] **Step 4: Шаблоны `brief_center.html` и `brief_detail.html`**

`brief_center.html` — наследует base.html, флеши, форма «Создать ссылку» (select тенанта + csrf), таблица брифов (тенант, статус-chip, даты, ссылка на детали).

`brief_detail.html` — наследует base.html. Показывает: личную ссылку (копировать), статус; блок «Ответы» по секциям; кнопку `POST /brief-center/{id}/orchestrate` (если answers есть); при наличии `brief.proposal` — диф-предпросмотр по секциям (recommendations, gaps на видном месте) + форма `POST /brief-center/{id}/apply` с чекбоксами `name="sections"` (funnel/products/triggers/channels) и кнопкой «Применить». Каждая форма — с hidden `csrf_token`.

Форма применения (ключевой фрагмент):
```html
<form method="post" action="/brief-center/{{ brief.id }}/apply">
  <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
  <label><input type="checkbox" name="sections" value="funnel" checked> Воронка/оператор</label>
  <label><input type="checkbox" name="sections" value="products" checked> Продукты</label>
  <label><input type="checkbox" name="sections" value="triggers"> Триггеры/анонсы</label>
  <label><input type="checkbox" name="sections" value="channels"> Каналы</label>
  <button class="btn btn--primary" type="submit">Применить выбранное</button>
</form>
```

- [ ] **Step 5: Пункт меню в `base.html`** (под is_platform, рядом с `/tenants`)

```html
{{ nav_item('brief', '/brief-center', 'Бриф-центр', active) }}
```

- [ ] **Step 6: КОНТРОЛЛЕР гонит db-смоук применения на risuy_dev**

Run: `BRIEF_APPLY_SMOKE_DSN="<owner-dsn risuy_dev>" PYTHONPATH=admin-panel:. ./.venv-smoke/bin/python scripts/brief_apply_smoke.py`
Expected: `✅ brief_apply smoke — OK`

- [ ] **Step 7: py_compile панели**

Run: `cd ~/Downloads/risuy-ecosystem && ./.venv-smoke/bin/python -m py_compile admin-panel/app.py admin-panel/brief_apply.py && echo OK`
Expected: `OK` (фактическая регистрация роутов — на деплое; `.venv-smoke` без fastapi).

- [ ] **Step 8: Коммит**

```bash
cd ~/Downloads/risuy-ecosystem
git add admin-panel/brief_apply.py admin-panel/app.py admin-panel/db.py \
        admin-panel/templates/brief_center.html admin-panel/templates/brief_detail.html \
        admin-panel/templates/base.html scripts/brief_apply_smoke.py
git commit -m "feat(brief): центр решений + применение черновика за HumanGate (панель)"
```

---

## Финальные шаги (после всех задач)

- **Регрессия смоуков:** прогнать существующие ключевые смоуки (онбординг/воронка) — зелёные.
- **Адверсариальное ревью** финального дифа (Workflow, 3 линзы: 152-ФЗ/безопасность · корректность · целостность RLS/скоуп) — как в сессии 8.
- **Деплой (по «да»):** `git push origin docs/security-audit:main` → редеплой панели (205025) и бота (201859); прод-миграция `risuy` по отдельному «да». Проверить commit_sha+active через twc.
- **Глазами владельца:** создать бриф-ссылку в `/brief-center`, открыть `/brief/{token}`, пройти, вернуться, «Собрать черновик», применить секцию, проверить, что настройка тенанта изменилась.

---

## Self-Review (проведён при написании плана)

**1. Покрытие спеки:**
- §3 Архитектура/поток — Tasks 1–5 покрывают все стрелки (создать ссылку→лендинг→submit→центр→оркестратор→apply). ✅
- §4 Модель данных — Task 1 (схема) + Task 2 (tenant_brief + функции). ✅ *(RLS уточнён: без tenant-isolation, обоснование в Global Constraints)*
- §5 Лендинг в боте — Task 3 (роуты, рендер, анти-абьюз, 152-ФЗ уведомление). ✅
- §6 Оркестратор — Task 4 (чистая функция, фолбэк, no-fabrication, обезличивание). ✅
- §7 Центр решений + apply — Task 5 (роуты, диф, посекционное применение через сеттеры, is_platform). ✅
- §8 Ошибки/тесты/метрики — тесты в каждой задаче; аудит-события `brief_created/applied` в db-функциях; ошибки обрабатываются в хендлерах/apply. ✅

**2. Плейсхолдеры:** реальный код во всех шагах. Два намеренных «реализатор подставит»: (а) `_default_llm` — точный LLM-клиент проекта (grep-указание дано, тест инжектит мок); (б) имя роли бота в гранте (grep-указание дано). Оба честно помечены и не блокируют тесты. ✅

**3. Консистентность типов:** сигнатуры сверены с якорями кода. `apply_proposal(...)→{sections,errors}` совпадает между Task 5 Step 1 (тест) и Step 2 (реализация). `analyze(answers,*,llm)→dict` совпадает между Task 4 и Task 5 (`_brief_analyze`). `get_brief_by_token`/`submit_brief` — идентичны в панели (Task 2 Step 3) и боте (Step 4). ✅

**Известное отклонение от спеки:** `tenant_brief` без tenant-isolation RLS (спека §4.1 предполагала RLS). Обоснование — кросс-тенантный платформенный артефакт + token-gated доступ бота; вынесено в Global Constraints и требует явного подтверждения владельца.
