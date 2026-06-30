# СП-2b — UI «База знаний» обоим контурам + отдел-тегирование (role_tag=slug) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** раздел `/knowledge` доступен ОБОИМ контурам (платформа-под-клиента в стиле A1 + тенант self-serve), а документ можно привязать к ОТДЕЛУ через `role_tag = slug` team-агента активного тенанта (для School-тенанта — прежние `PERSONA_PRESETS`, без регресса).

**Architecture:** Чистые правки кода и шаблонов — **нового прод-DDL НЕ требуется** (`kb_documents.tenant_id`/`role_tag`, `kb_chunks.tenant_id`/`metadata->>'role_tag'`, RLS `tenant_isolation`, гранты `panel_rw` уже живут со СП-2a/Wave-3). Доступ раздваивается снятием `_require_admin` с `/knowledge` GET/upload/delete (как `/my-team` в A1); скоуп записи/чтения/удаления даёт RLS через pool-хук `_apply_tenant_guc` + `require_session→set_active_tenant`. Источник дропдаунов «отдел» — `db.list_team_agents(active_tenant_id)` (slug→name), а для School-тенанта — `config.PERSONA_PRESETS`.

**Tech Stack:** Python/FastAPI/asyncpg (роль `panel_rw`, RLS), Jinja2-шаблоны, self-host TEI e5 (768) — без новых зависимостей. Смоуки — standalone на `.venv-smoke`.

**Спека:** `docs/superpowers/specs/2026-06-29-sp2-knowledge-memory-design.md` (§4.D UI обоим контурам, §4.A интерпретация `role_tag` в пределах tenant-scope: School → `PERSONA_PRESETS`, тенант → slug team-агентов). Опирается на СП-2a (`1e2df9b`) и A1 (`af7d85a`).

## Global Constraints
- **Только русский** в UI-текстах/коммитах/комментариях/docstrings. Латиница — только техника.
- **РФ-резидентность:** эмбеддер TEI (НЕ OpenAI), хранение — РФ-кластер. Без новых зависимостей.
- **Нового прод-DDL нет.** Если в ходе работы окажется, что DDL всё же нужен — СТОП, согласовать с владельцем (прод-DDL ПЕРЕД кодом, по явному «да»).
- **Изоляция тенантов — через RLS** (`panel_rw` не имеет bypassrls). Любой новый путь чтения/записи/удаления KB обязан оставаться tenant-scoped; cross-tenant — проверяется смоуком (Task 4).
- Смоук-ранер: `cd ~/Downloads/risuy-ecosystem && PYTHONPATH=admin-panel:bot-telegram:. ./.venv-smoke/bin/python scripts/<name>.py`. **DB-смоуки ТОЛЬКО на `risuy_dev`** (assert в скрипте), owner-DSN даёт владелец.
- Коммиты локально; **push, деплой — ТОЛЬКО по явному «да» владельца.**
- Перед финальным коммитом — **3-линзовое адверсариальное ревью** (особо security/изоляция tenant_id, отсутствие регресса School-пути), 0 critical/high.

**Staging (Scope Check):** Этот план = **СП-2b** (UI `/knowledge` обоим контурам + отдел-тегирование). **СП-2-память** (движок `agent_memory`) — отдельным планом. A2 (ретайр `/my-agent`), #2 (Gemma) — вне этого плана.

## File Structure
- `admin-panel/knowledge_roles.py` — **NEW**, чистый хелпер `normalize_role` (нормализация/валидация `role_tag` отдела). Одна ответственность, без БД (Task 1).
- `admin-panel/config.py` — `DEFAULT_TENANT_SLUG` (слаг School-тенанта; тот же env, что у бота) (Task 2).
- `admin-panel/auth.py` — `Session.active_tenant_slug` + `t.slug` в загрузчике сессии (Task 2).
- `admin-panel/app.py` — `_kb_roles_for` helper + правки `knowledge_page`/`knowledge_upload`/`knowledge_delete` (раздвоение контура, дропдаун, валидация) (Task 3).
- `admin-panel/templates/knowledge.html` — `has_tenant`-гейт + бейдж клиента + дропдаун отдела + гейт глобального тумблера (Task 3).
- `admin-panel/templates/base.html` — пункт nav `/knowledge` в ветке тенанта (Task 3).
- `scripts/kb_roles_smoke.py` — **NEW**, чистый смоук хелпера (Task 1).
- `scripts/kb_ui_smoke.py` — **NEW**, render-смоук `/knowledge` (контуры/дропдаун/бейдж/nav) (Task 3).
- `scripts/kb_panel_isolation_smoke.py` — **NEW**, DB-смоук RLS-изоляции панели A≠B на `risuy_dev` (Task 4).

---

### Task 1: Чистый хелпер нормализации `role_tag` отдела + смоук

**Files:**
- Create: `admin-panel/knowledge_roles.py`
- Test: `scripts/kb_roles_smoke.py`

**Interfaces:**
- Produces: `normalize_role(role: str, allowed: set[str]) -> str` — `''` (общая справка) либо валидный slug отдела из `allowed`; всё иное (опечатка/чужой/мусор) → `''` (не тегируем — иначе чанк станет «тихо невидим» для ретрива).

- [ ] **Step 1: Написать падающий смоук** `scripts/kb_roles_smoke.py`

```python
#!/usr/bin/env python3
"""Чистый смоук СП-2b: normalize_role нормализует role_tag отдела (без БД).
  PYTHONPATH=admin-panel:. ./.venv-smoke/bin/python scripts/kb_roles_smoke.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "admin-panel"))

from knowledge_roles import normalize_role  # noqa: E402

FAILS: list[str] = []


def check(name, cond):
    print(f"  {'OK ' if cond else 'FAIL'} {name}")
    if not cond:
        FAILS.append(name)


ALLOWED = {"sales", "support"}
check("пусто → общая справка ''", normalize_role("", ALLOWED) == "")
check("пробелы → ''", normalize_role("   ", ALLOWED) == "")
check("валидный slug отдела сохраняется", normalize_role("sales", ALLOWED) == "sales")
check("slug с пробелами обрезается и сохраняется", normalize_role("  support  ", ALLOWED) == "support")
check("чужой/несуществующий slug → '' (не тегируем)", normalize_role("liya", ALLOWED) == "")
check("мусор → ''", normalize_role("'; drop", ALLOWED) == "")
check("пустой allowed → любой slug сбрасывается в ''", normalize_role("sales", set()) == "")

print("\n" + ("ВСЕ ОК" if not FAILS else "ПРОВАЛЫ: " + ", ".join(FAILS)))
sys.exit(1 if FAILS else 0)
```

- [ ] **Step 2: Запустить — упадёт (модуля нет)**

Run: `cd ~/Downloads/risuy-ecosystem && PYTHONPATH=admin-panel:. ./.venv-smoke/bin/python scripts/kb_roles_smoke.py`
Expected: `ModuleNotFoundError: No module named 'knowledge_roles'`.

- [ ] **Step 3: Реализовать** `admin-panel/knowledge_roles.py`

```python
"""СП-2b: нормализация role_tag (отдела) для базы знаний. Чистая логика, без БД.

role_tag в KB = '' (общая справка тенанта, видна всем его агентам) ИЛИ slug отдела
(team-агент тенанта; для School-тенанта — slug персоны PERSONA_PRESETS). Ретрив бота
(kb_search) матчит role_tag ТОЧНЫМ равенством slug, поэтому опечатка/чужой slug сделает
чанк невидимым для всех агентов — такие значения сбрасываем в '' (общая справка)."""


def normalize_role(role: str, allowed: set[str]) -> str:
    """'' (общая) либо slug из allowed; иначе → '' (не тегируем мусором)."""
    role = (role or "").strip()
    return role if (role and role in allowed) else ""
```

- [ ] **Step 4: Запустить — зелено**

Run: `cd ~/Downloads/risuy-ecosystem && PYTHONPATH=admin-panel:. ./.venv-smoke/bin/python scripts/kb_roles_smoke.py`
Expected: `ВСЕ ОК` (7 проверок).

- [ ] **Step 5: Commit**
```bash
cd ~/Downloads/risuy-ecosystem
git add admin-panel/knowledge_roles.py scripts/kb_roles_smoke.py
git commit -m 'feat(panel): СП-2b — чистый хелпер normalize_role для role_tag отдела (+ смоук)' \
  -m 'Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>'
```

---

### Task 2: Панель знает слаг School-тенанта и слаг активного клиента

**Files:**
- Modify: `admin-panel/config.py` (рядом с прочими env-константами)
- Modify: `admin-panel/auth.py` (dataclass `Session` ~242-245; SQL загрузчика ~321; конструктор ~350-351)

**Interfaces:**
- Produces: `config.DEFAULT_TENANT_SLUG: str` (по умолчанию `"lesov-school"`, тот же env-ключ, что у бота); `Session.active_tenant_slug: str | None`.
- Consumes (далее, Task 3): сравнение `session.active_tenant_slug == config.DEFAULT_TENANT_SLUG` → «активен School-тенант».

- [ ] **Step 1: Добавить `DEFAULT_TENANT_SLUG` в `admin-panel/config.py`**

Дописать (рядом с прочими `os.environ.get(...)`-константами; `import os` в файле уже есть):
```python
# СП-2b: слаг тенанта Школы (легаси одиночный путь). Тот же env-ключ, что у бота
# (bot-telegram/config.py). Под этим тенантом role_tag KB интерпретируется по
# PERSONA_PRESETS (School-путь ретрива ходит по lead_persona), под остальными — по
# slug'ам team-агентов тенанта.
DEFAULT_TENANT_SLUG = os.environ.get("DEFAULT_TENANT_SLUG", "lesov-school")
```

- [ ] **Step 2: Добавить поле в dataclass `Session`** (`admin-panel/auth.py`, рядом с `active_tenant_status`)

После строки `active_tenant_status: str | None = None` добавить:
```python
    active_tenant_slug: str | None = None
```

- [ ] **Step 3: Прокинуть `t.slug` в загрузчике сессии** (`admin-panel/auth.py`)

В SELECT заменить строку
```python
                       t.name as active_tenant_name, t.status as active_tenant_status
```
на
```python
                       t.name as active_tenant_name, t.status as active_tenant_status,
                       t.slug as active_tenant_slug
```
И в конструкторе `Session(...)` после `active_tenant_status=row["active_tenant_status"],` добавить:
```python
                active_tenant_slug=row["active_tenant_slug"],
```

- [ ] **Step 4: Парс-проверка + наличие колонок**

Run:
```bash
cd ~/Downloads/risuy-ecosystem && ./.venv-smoke/bin/python -c "
import ast
for f in ('admin-panel/config.py','admin-panel/auth.py'):
    ast.parse(open(f).read())
src_a = open('admin-panel/auth.py').read()
assert 'active_tenant_slug' in src_a, 'нет поля active_tenant_slug'
assert 't.slug as active_tenant_slug' in src_a, 'нет t.slug в SQL'
assert 'DEFAULT_TENANT_SLUG' in open('admin-panel/config.py').read(), 'нет DEFAULT_TENANT_SLUG'
print('parse OK + поля на месте')
"
```
Expected: `parse OK + поля на месте`.

- [ ] **Step 5: Commit**
```bash
cd ~/Downloads/risuy-ecosystem
git add admin-panel/config.py admin-panel/auth.py
git commit -m 'feat(panel): СП-2b — Session.active_tenant_slug + config.DEFAULT_TENANT_SLUG (детект School-тенанта)' \
  -m 'Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>'
```

---

### Task 3: `/knowledge` обоим контурам + дропдаун «отдел» + валидация записи

**Files:**
- Modify: `admin-panel/app.py` (`knowledge_page` :3764-3792, `knowledge_upload` :3813-3859, `knowledge_delete` :3862-3883; новый helper `_kb_roles_for` рядом с `_knowledge_err_text` :3752)
- Modify: `admin-panel/templates/knowledge.html` (полный рестрак)
- Modify: `admin-panel/templates/base.html` (nav-пункт `/knowledge` в else-ветке :105-108)
- Test: `scripts/kb_ui_smoke.py` (NEW)

**Interfaces:**
- Consumes: `normalize_role` (Task 1); `config.DEFAULT_TENANT_SLUG`, `Session.active_tenant_slug` (Task 2); `db.list_team_agents(tenant_id)` (slug/name/role_preset), `db.kb_list_documents()` (RLS-scoped), `db.kb_insert_document(... role_tag, tenant_id ...)`, `db.kb_delete_document(...)`, `_safe_support_url`, `config.SUPPORT_URL`, `config.PERSONA_PRESETS` (существуют).
- Produces: дропдаун отдела = `kb_roles` (dict `{slug: {"name","role"}}`), контекст шаблона `has_tenant`/`show_global_toggle`/`support_url`.

> **Контур доступа (важно):** `knowledge_toggle` (POST /knowledge/toggle) — ОСТАВЛЯЕМ `_require_admin` (глобальный `app_settings['kb_enabled']` — только School/платформа). Снимаем `_require_admin` ТОЛЬКО с GET `/knowledge`, `/knowledge/upload`, `/knowledge/delete`. Скоуп — RLS (active_tenant_id).

- [ ] **Step 1: Написать падающий render-смоук** `scripts/kb_ui_smoke.py`

```python
#!/usr/bin/env python3
"""Render-смоук СП-2b: /knowledge обоим контурам (платформа-под-клиента + тенант self-serve).
Чистый Jinja-рендер knowledge.html + base.html (без БД/HTTP): has_tenant-гейт, бейдж клиента,
дропдаун отдела из kb_roles, nav-пункт в обеих ветках, гейт глобального тумблера.
  PYTHONPATH=. ./.venv-smoke/bin/python scripts/kb_ui_smoke.py
"""
import os
import sys

from jinja2 import ChainableUndefined, Environment, FileSystemLoader, select_autoescape

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TPL_DIR = os.path.join(ROOT, "admin-panel", "templates")

env = Environment(
    loader=FileSystemLoader(TPL_DIR),
    autoescape=select_autoescape(["html", "xml"]),
    undefined=ChainableUndefined,
)
env.globals["asset_version"] = ""
env.globals["service_site_url"] = ""

FAILS: list[str] = []


def check(name, cond, detail=""):
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


BASE_CTX = dict(
    csrf_token="csrf", err="", kb_saved=0, active="knowledge",
    has_tenant=True, kb_docs=[], kb_enabled=False, show_global_toggle=False,
    embedder_enabled=True, kb_roles={}, kb_max_mb=10,
    support_url="https://t.me/support",
)


def render(**over):
    ctx = {**BASE_CTX, **over}
    return env.get_template("knowledge.html").render(**ctx)


KNOW_NAV = '<span class="snav__label">Базы знаний</span>'

# 1. платформа без клиента → CTA «Клиенты», без «поддержки»
html = render(session={"is_platform": True}, has_tenant=False)
check("платформа без клиента — «Клиент не выбран» + /tenants, без «поддержки»",
      ("Клиент не выбран" in html) and ("/tenants" in html) and ("напишите в поддержку" not in html))

# 2. тенант без привязки → «напишите в поддержку»
html = render(session={"is_platform": False}, has_tenant=False)
check("тенант без кабинета — «напишите в поддержку»", "напишите в поддержку" in html)

# 3. тенант с кабинетом → форма загрузки + дропдаун с НАЗВАНИЯМИ отделов из kb_roles
html = render(
    session={"is_platform": False},
    has_tenant=True,
    kb_roles={"sales": {"name": "Отдел продаж", "role": ""}, "support": {"name": "Поддержка", "role": ""}},
)
check("тенант — форма загрузки и дропдаун отделов (названия)",
      ('action="/knowledge/upload"' in html) and ("Отдел продаж" in html) and ("Поддержка" in html))
check("тенант — есть опция общей справки", "Для всех ролей" in html or "общая справка" in html.lower())

# 4. платформа с клиентом → бейдж «Клиент: …»
html = render(session={"is_platform": True, "active_tenant_name": "ООО Ромашка"}, has_tenant=True)
check("платформа — бейдж «Клиент: ООО Ромашка»", "Клиент: ООО Ромашка" in html)

# 5. тенант → бейджа «Клиент:» нет
html = render(session={"is_platform": False, "active_tenant_name": "X"}, has_tenant=True)
check("тенант — без бейджа «Клиент:»", "Клиент:" not in html)

# 6. глобальный тумблер показывается только при show_global_toggle (School/платформа)
html_off = render(session={"is_platform": False}, has_tenant=True, show_global_toggle=False)
html_on = render(session={"is_platform": True}, has_tenant=True, show_global_toggle=True)
check("тумблер скрыт у тенанта", 'action="/knowledge/toggle"' not in html_off)
check("тумблер виден при show_global_toggle", 'action="/knowledge/toggle"' in html_on)

# 7. nav: пункт «Базы знаний» рендерится В ОБЕИХ ветках (платформа и тенант)
check("nav: платформа видит «Базы знаний»", KNOW_NAV in render(session={"is_platform": True}, has_tenant=True))
check("nav: тенант видит «Базы знаний»", KNOW_NAV in render(session={"is_platform": False}, has_tenant=True))


def _summary():
    print(f"\n{'ВСЕ ОК' if not FAILS else 'ПРОВАЛЫ: ' + ', '.join(FAILS)}")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    _summary()
```

- [ ] **Step 2: Запустить — упадёт** (шаблон ещё старый: нет `has_tenant`-гейта/бейджа/гейта тумблера; nav тенанту скрыт)

Run: `cd ~/Downloads/risuy-ecosystem && PYTHONPATH=. ./.venv-smoke/bin/python scripts/kb_ui_smoke.py`
Expected: несколько FAIL (no-tenant, бейдж, тумблер, nav тенанта).

- [ ] **Step 3: `base.html` — пункт `/knowledge` в ветке тенанта** (`admin-panel/templates/base.html`)

В else-ветке nav (строки 105-108) после `triggers` добавить пункт `knowledge`:
```jinja
      {%- else %}
      {{ nav_item('my_team',      '/my-team',    'ИИ-команда',       active) }}
      {{ nav_item('knowledge',    '/knowledge',  'Базы знаний',      active) }}
      {{ nav_item('triggers',     '/triggers',   'Триггеры',         active) }}
      {%- endif %}
```
(Платформенная ветка `/knowledge` на строке 102 остаётся без изменений.)

- [ ] **Step 4: `app.py` — helper `_kb_roles_for`** (добавить сразу после `_knowledge_err_text`, :3761)

```python
async def _kb_roles_for(session: auth.Session) -> tuple[dict, bool]:
    """Карта отделов для дропдауна KB активного клиента + признак School-тенанта.
    School (DEFAULT_TENANT_SLUG) → PERSONA_PRESETS (ретрив School ходит по lead_persona);
    остальные тенанты → их team-агенты (slug→название). Вид {slug: {'name','role'}} —
    совместим с knowledge.html (kb_roles.items() и kb_roles.get(role_tag).name)."""
    is_school = bool(session.active_tenant_slug) and session.active_tenant_slug == config.DEFAULT_TENANT_SLUG
    if is_school:
        return config.PERSONA_PRESETS, True
    tid = session.active_tenant_id
    rows = await db.list_team_agents(tid) if tid else []
    roles = {r["slug"]: {"name": r["name"], "role": (r["role_preset"] or "")} for r in rows}
    return roles, False
```

- [ ] **Step 5: `app.py` — `knowledge_page` (раздвоить контур, отделы, тумблер-гейт)**

Заменить тело `knowledge_page` (строки 3771-3791) на:
```python
    # СП-2b: раздел доступен обоим контурам (как /my-team в A1). Скоуп — активный клиент
    # (RLS через require_session→set_active_tenant). _require_admin снят.
    tid = session.active_tenant_id
    kb_roles, is_school = await _kb_roles_for(session)
    kb_docs = await db.kb_list_documents() if tid else []
    # Глобальный тумблер поиска (app_settings) релевантен только School-пути → показываем
    # его лишь для School-тенанта; у обычных тенантов поиск управляется per-agent в /my-team.
    show_global_toggle = is_school
    kb_enabled = await db.get_kb_enabled() if is_school else False
    return templates.TemplateResponse(
        request,
        "knowledge.html",
        {
            "csrf_token": session.csrf_token,
            "session": session,
            "active": "knowledge",
            "err": _knowledge_err_text(err),
            "has_tenant": bool(tid),
            "kb_docs": kb_docs,
            "kb_enabled": kb_enabled,
            "show_global_toggle": show_global_toggle,
            "embedder_enabled": config.EMBEDDER_ENABLED,
            "kb_roles": kb_roles,
            "kb_saved": kb_saved,
            "kb_max_mb": config.MAX_KB_FILE_BYTES // 1024 // 1024,
            "support_url": _safe_support_url(config.SUPPORT_URL),
        },
    )
```
(Строку `_require_admin(session)` :3771 удалить.)

- [ ] **Step 6: `app.py` — `knowledge_upload` (снять гейт, валидировать отдел)**

Удалить `_require_admin(session)` (:3824). Заменить блок валидации роли (строки 3849-3851)
```python
    role = role.strip()
    if role and role not in config.PERSONA_PRESETS:
        role = ""
```
на:
```python
    kb_roles, _ = await _kb_roles_for(session)
    role = knowledge_roles.normalize_role(role, set(kb_roles))
```
Убедиться, что вверху `app.py` есть `import knowledge_roles` (если нет — добавить к остальным локальным импортам admin-panel). `tenant_id=session.active_tenant_id` в `kb_insert_document` (:3856) остаётся как есть.

- [ ] **Step 7: `app.py` — `knowledge_delete` (снять гейт; изоляцию держит RLS)**

Удалить `_require_admin(session)` (:3870). Остальное без изменений: `db.kb_delete_document` под `panel_rw` + RLS удалит ТОЛЬКО документ активного тенанта (cross-tenant физически невозможен — проверяется Task 4).

- [ ] **Step 8: `knowledge.html` — рестрак (контур + бейдж + дропдаун + гейт тумблера)**

Заменить файл целиком на:
```jinja
{% extends "base.html" %}
{% from "_macros.html" import flash %}

{% block title %}База знаний{% endblock %}
{% block body_class %}page-knowledge{% endblock %}

{% block content %}
<div class="page-head">
  <h1 class="page-head__title">База знаний — документы для бота</h1>
  <p class="page-head__hint">Загрузите сюда прайс, FAQ, условия, методички — бот находит нужное по смыслу и подмешивает в ответ. Данные в РФ, без сторонних сервисов. <b>Роль и инструкции</b> сотрудника задаются в разделе «ИИ-команда».</p>
  {% if session and session.is_platform and has_tenant and session.active_tenant_name %}
  <p class="page-head__hint"><span class="pill pill--warn">Клиент: {{ session.active_tenant_name }}</span></p>
  {% endif %}
</div>

{% if err %}{{ flash(err, 'error') }}{% endif %}
{% if kb_saved %}{{ flash('Файл загружен: ' ~ kb_saved ~ ' фрагм. добавлено в базу. Бот учтёт со следующего ответа.', 'ok') }}{% endif %}

{% if not has_tenant %}
<section class="card">
  {% if session and session.is_platform %}
  <div class="card__title">Клиент не выбран</div>
  <p class="card__note">Выберите клиента в разделе «Клиенты», чтобы вести его базу знаний.</p>
  <div class="acct-actions"><a class="btn btn--primary" href="/tenants">Перейти к «Клиентам»</a></div>
  {% else %}
  <div class="card__title">Кабинет ещё не привязан</div>
  <p class="card__note">База знаний ведётся в кабинете клиента. Сейчас к учётной записи кабинет не привязан — напишите в поддержку.</p>
  {% if support_url %}<div class="acct-actions"><a class="btn btn--primary" href="{{ support_url }}" target="_blank" rel="noopener noreferrer nofollow">Написать в поддержку</a></div>{% endif %}
  {% endif %}
</section>
{% else %}

{% if show_global_toggle %}
{# глобальный тумблер поиска по базе (School) #}
<form method="post" action="/knowledge/toggle" class="card" autocomplete="off">
  <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
  <input type="hidden" name="kb_enabled" value="{{ '0' if kb_enabled else '1' }}">
  <p class="card__note">Поиск по базе знаний: <b>{{ 'включён' if kb_enabled else 'выключен' }}</b> — когда выключен, бот отвечает только по инструкции сотрудника.</p>
  <div class="form-actions"><button class="btn {{ 'btn--muted' if kb_enabled else 'btn--dark' }}" type="submit">{{ 'Выключить поиск' if kb_enabled else 'Включить поиск' }}</button></div>
</form>
{% endif %}

{% if not embedder_enabled %}
<p class="hint hint--block">⚠️ Загрузка файлов недоступна: не задан <code>EMBEDDER_URL</code> в окружении панели. Добавьте его скриптом деплоя (как у бота) — и появится форма загрузки.</p>
{% else %}
{# форма загрузки #}
<form method="post" action="/knowledge/upload" enctype="multipart/form-data" autocomplete="off" class="card">
  <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
  <label class="field">
    <span class="field__label">Файл <span class="field__hint">txt, md, csv, pdf · до {{ kb_max_mb }} МБ</span></span>
    <input class="field__input" type="file" name="file" accept=".txt,.md,.csv,.pdf" required>
  </label>
  <label class="field">
    <span class="field__label">Название <span class="field__hint">необязательно</span></span>
    <input class="field__input" type="text" name="title" maxlength="200" placeholder="Напр.: Прайс и условия записи">
  </label>
  <label class="field">
    <span class="field__label">Отдел <span class="field__hint">какой агент видит документ</span></span>
    <select class="field__input" name="role">
      <option value="">Для всех ролей (общая справка)</option>
      {% for slug, p in kb_roles.items() %}<option value="{{ slug }}">{{ p.name|e }}{% if p.role %} — {{ p.role|e }}{% endif %}</option>{% endfor %}
    </select>
  </label>
  <p class="hint hint--block">Таблицы лучше грузить как CSV. Из PDF берётся только текст (картинки и схемы не попадут). Один файл = один документ.</p>
  <div class="form-actions"><button class="btn btn--dark" type="submit">Загрузить в базу</button></div>
</form>

{# список документов #}
{% if kb_docs %}
<h3 class="section__title">Загруженные документы ({{ kb_docs|length }})</h3>
{% for d in kb_docs %}
<div class="card">
  <strong>{{ d.title|e }}</strong>
  {% if d.role_tag %}<span class="pill">{{ (kb_roles.get(d.role_tag) or {}).get('name', d.role_tag)|e }}</span>{% else %}<span class="pill pill--muted">все роли</span>{% endif %}
  <span class="pill pill--ok">{{ d.chunks }} фрагм.</span>
  <p class="card__note muted">{{ d.source|e }} · {{ d.created }}</p>
  <form method="post" action="/knowledge/delete">
    <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
    <input type="hidden" name="doc_id" value="{{ d.id }}">
    <button class="btn btn--muted" type="submit">Удалить</button>
  </form>
</div>
{% endfor %}
{% else %}
<p class="empty__text">База пуста. Загрузите первый файл выше.</p>
{% endif %}
{% endif %}
{% endif %}
{% endblock %}
```

- [ ] **Step 9: Запустить render-смоук — зелено**

Run: `cd ~/Downloads/risuy-ecosystem && PYTHONPATH=. ./.venv-smoke/bin/python scripts/kb_ui_smoke.py`
Expected: `ВСЕ ОК` (11 проверок).

- [ ] **Step 10: Парс-проверка app.py + регрессия A1-смоука**

Run:
```bash
cd ~/Downloads/risuy-ecosystem && ./.venv-smoke/bin/python -c "import ast; ast.parse(open('admin-panel/app.py').read()); print('app.py parse OK')" \
  && PYTHONPATH=. ./.venv-smoke/bin/python scripts/platform_team_access_smoke.py
```
Expected: `app.py parse OK` + `ВСЕ ОК` (A1-смоук /my-team без регресса).

- [ ] **Step 11: Commit**
```bash
cd ~/Downloads/risuy-ecosystem
git add admin-panel/app.py admin-panel/templates/knowledge.html admin-panel/templates/base.html scripts/kb_ui_smoke.py
git commit -m 'feat(panel): СП-2b — /knowledge обоим контурам + дропдаун отдела (role_tag=slug) + валидация' \
  -m 'Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>'
```

---

### Task 4: DB-смоук RLS-изоляции панели (A≠B) на `risuy_dev` + 3-линзовое ревью

**Files:**
- Create: `scripts/kb_panel_isolation_smoke.py`

**Interfaces:**
- Consumes: RLS `tenant_isolation` на `kb_documents`/`kb_chunks`; роль `panel_rw` (без bypassrls).

> **Зачем:** СП-2b открывает тенанту self-serve доступ к `/knowledge`. `kb_list_documents`/`kb_delete_document` НЕ принимают tenant_id — изоляцию держит ИСКЛЮЧИТЕЛЬНО RLS (`app.tenant_id` от pool-хука). Смоук доказывает: под `panel_rw` клиент B не видит и не может удалить документ клиента A. Owner-DSN (`gen_user`) даёт владелец; внутри сессии `set role panel_rw` — иначе owner обходит RLS и тест бессмыслен.

- [ ] **Step 1: Написать смоук** `scripts/kb_panel_isolation_smoke.py`

```python
#!/usr/bin/env python3
"""DB-смоук СП-2b: RLS-изоляция базы знаний В ПАНЕЛИ (роль panel_rw). Клиент B не видит и не
удаляет документ клиента A. Прямой SQL под `set role panel_rw` (owner обошёл бы RLS).
  TEAM_DSN="postgresql://gen_user:<pw>@<host>:5432/risuy_dev?sslmode=require" \
  ./.venv-smoke/bin/python scripts/kb_panel_isolation_smoke.py
"""
import asyncio
import os
import sys

import asyncpg

DSN = os.environ.get("TEAM_DSN", "")
assert DSN and "/risuy_dev" in DSN.split("?")[0], "только risuy_dev (owner-DSN от владельца)"

FAILS: list[str] = []


def check(name, cond):
    print(f"  {'OK ' if cond else 'FAIL'} {name}")
    if not cond:
        FAILS.append(name)


VEC = "[" + ",".join("0.01" for _ in range(768)) + "]"


async def main():
    conn = await asyncpg.connect(DSN)
    try:
        # setup как owner
        ta = await conn.fetchval("insert into tenants(slug,name,status) values('kbui-a','A','active') returning id")
        tb = await conn.fetchval("insert into tenants(slug,name,status) values('kbui-b','B','active') returning id")
        try:
            await conn.execute("set role panel_rw")
            # A пишет свой документ (WITH CHECK совпадает с app.tenant_id)
            await conn.execute("select set_config('app.tenant_id', $1, false)", str(ta))
            da = await conn.fetchval(
                "insert into kb_documents(tenant_id,title,content) values($1,'A-doc','a') returning id", ta)
            await conn.execute(
                "insert into kb_chunks(tenant_id,document_id,chunk_index,content,embedding,metadata) "
                "values($1,$2,0,'ФАКТ-A',$3::vector,'{\"role_tag\":\"\"}'::jsonb)", ta, da, VEC)
            a_titles = [r["title"] for r in await conn.fetch("select title from kb_documents")]
            check("A видит свой документ", "A-doc" in a_titles)

            # B переключается — НЕ видит документ A
            await conn.execute("select set_config('app.tenant_id', $1, false)", str(tb))
            b_titles = [r["title"] for r in await conn.fetch("select title from kb_documents")]
            check("B НЕ видит документ A (RLS list)", "A-doc" not in b_titles)
            b_chunks = [r["content"] for r in await conn.fetch("select content from kb_chunks")]
            check("B НЕ видит чанк A (RLS chunks)", "ФАКТ-A" not in b_chunks)

            # B пытается удалить документ A по id — 0 строк (RLS)
            res = await conn.execute("delete from kb_documents where id=$1", da)
            check("B НЕ удаляет документ A (RLS delete = 0 rows)", res.endswith(" 0"))

            await conn.execute("reset role")
            # документ A всё ещё на месте (owner-проверка)
            still = await conn.fetchval("select count(*) from kb_documents where id=$1", da)
            check("документ A пережил попытку удаления B", still == 1)
        finally:
            await conn.execute("reset role")
    finally:
        await conn.execute("delete from tenants where slug in ('kbui-a','kbui-b')")  # cascade чистит kb_*
        await conn.close()
    print("\n" + ("ВСЕ ОК" if not FAILS else "ПРОВАЛЫ: " + ", ".join(FAILS)))
    sys.exit(1 if FAILS else 0)


asyncio.run(main())
```

- [ ] **Step 2: Запустить на `risuy_dev`** (owner-DSN от владельца)

Run: `cd ~/Downloads/risuy-ecosystem && TEAM_DSN="<owner-dsn risuy_dev>" ./.venv-smoke/bin/python scripts/kb_panel_isolation_smoke.py`
Expected: `ВСЕ ОК` (5 проверок). Если FAIL «B НЕ видит документ A» — RLS на kb-таблицах не применяется к `panel_rw`: СТОП, эскалировать (это блокер безопасности СП-2b, возможно нужен прод-DDL для RLS — согласовать с владельцем).

- [ ] **Step 3: 3-линзовое адверсариальное ревью** диффа `git diff <baseline>..HEAD`:
  - **security/изоляция:** тенант self-serve не видит/не удаляет/не перетегирует чужой KB (RLS list/delete/upload WITH CHECK); снятие `_require_admin` не открыло платформенных действий тенанту; `normalize_role` не пускает чужой slug; `knowledge_toggle` остался платформенным.
  - **correctness:** `_kb_roles_for` корректно детектит School (slug==DEFAULT_TENANT_SLUG) и иначе берёт team-агентов; дропдаун `kb_roles.items()`/pill `kb_roles.get(role_tag).name` совместимы с обоими видами карты; `has_tenant`-гейт не ломает рендер; A1-смоук зелёный.
  - **152-ФЗ/RU:** эмбеддер РФ; контент не уходит в иностранные сервисы; все тексты русские.
  Внести critical/high, повторить смоуки (`kb_roles_smoke`, `kb_ui_smoke`, `platform_team_access_smoke`, `kb_panel_isolation_smoke`). Добиться 0 critical/high.

- [ ] **Step 4: Commit**
```bash
cd ~/Downloads/risuy-ecosystem
git add scripts/kb_panel_isolation_smoke.py
git commit -m 'test(panel): СП-2b — DB-смоук RLS-изоляции базы знаний в панели (A≠B, panel_rw)' \
  -m 'Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>'
```

- [ ] **Step 5: Отчёт + гейт владельца**

СП-2b готов локально: `/knowledge` доступен обоим контурам, отдел-тегирование `role_tag=slug` живёт, изоляция доказана смоуком. **Прод-DDL не требуется.** **Push и деплой (push → авто-редеплой App Platform) — ТОЛЬКО по явному «да» владельца.** После деплоя — live-проверка route-уровня (тенант видит `/knowledge`, платформа под выбранным клиентом грузит/тегирует/удаляет; боевой ИИ-ответ остаётся на `risuy_dev` — гейт #2).

---

## Self-Review

**1. Spec coverage (СП-2b, спека §4.D + §4.A):**
- §4.D `/knowledge` видим И платформе (под active_tenant, A1), И тенанту (self-serve) → Task 3 (снят `_require_admin`, `has_tenant`-гейт, nav обеим веткам, бейдж) ✅
- §4.D загрузка/список/удаление per-tenant под RLS → существующие функции (СП-2a) + Task 3 (скоуп active_tenant) + Task 4 (доказательство изоляции) ✅
- §4.D привязка документа к отделу (агенту) через `role_tag` → Task 3 дропдаун из team-агентов + Task 1 `normalize_role` ✅
- §4.A `role_tag` в пределах tenant-scope: School → `PERSONA_PRESETS`, тенант → slug team-агентов → Task 2 (детект School) + Task 3 (`_kb_roles_for`) ✅
- §4.D тумблеры `kb_enabled` в /my-team → уже в СП-2a (вне СП-2b) ✅; глобальный School-тумблер скрыт у тенанта → Task 3 `show_global_toggle` ✅
- Память (§4.C), sectioned-prompt (§4.E) → вне СП-2b (СП-2-память) ✅ (staging задекларирован)

**2. Placeholder scan:** `<owner-dsn risuy_dev>`/`<pw>`/`<host>`/`<baseline>` — секреты/ref, подставляет владелец/исполнитель (не логика). `import knowledge_roles` в Task 3 Step 6 — проверить факт наличия и дописать (конкретное действие). Остальное — конкретный код/диффы.

**3. Type consistency:** `normalize_role(role: str, allowed: set[str]) -> str` — Task 1 (def), Task 3 Step 6 (вызов с `set(kb_roles)`). `_kb_roles_for(session) -> (dict, bool)` — Task 3 Step 4 (def), Step 5/6 (вызовы). `kb_roles` вид `{slug: {"name","role"}}` — совместим с `PERSONA_PRESETS` (есть `name`/`role`) и с team-картой → шаблон `kb_roles.items()`/`kb_roles.get(role_tag).name` работает для обоих. `Session.active_tenant_slug` — Task 2 (поле/SQL), Task 3 (`_kb_roles_for`). Согласовано.
