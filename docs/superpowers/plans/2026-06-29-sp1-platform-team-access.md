# A1 — «ИИ-команда клиента» в панели владельца-платформы — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Дать владельцу платформы (`is_platform`) настраивать команду ИИ-агентов любого клиента из своей панели, переключившись на клиента.

**Architecture:** Переиспользуем существующее: reseller-switch (`POST /tenants/switch` → `admin_sessions.active_tenant_id`) и CRUD СП-1 (`/my-team` уже без admin-гейта, работает на `active_tenant_id`). Меняем только презентацию: пункт меню для платформы + ролевое состояние «клиент не выбран» + бейдж активного клиента. Без БД, без правок резолвера/бота.

**Tech Stack:** Python/FastAPI, Jinja2 (`fastapi.templating.Jinja2Templates`), смоук — standalone-скрипт на `.venv-smoke`.

**Спека:** `docs/superpowers/specs/2026-06-29-sp1-platform-team-access-design.md`

## Global Constraints

- **Только русский** во всех UI-текстах, коммитах, комментариях (латиница — только идентификаторы/код).
- **Без новых зависимостей** (jinja2 уже в проекте), **без БД/миграций**, **без правок резолвера/бота**.
- Метки verbatim: пункт меню платформы — **«ИИ-команда клиента»**; CTA «клиент не выбран» ведёт на **`/tenants`** («Клиенты»); бейдж — **«Клиент: {active_tenant_name}»**.
- Смоук-ранер: `PYTHONPATH=. ./.venv-smoke/bin/python scripts/<name>.py` (cwd = корень репо).
- Коммиты — **локально**. Push в risuy и любой прод-DDL/деплой — **ТОЛЬКО по явному «да» владельца**.
- Перед финальным коммитом — **3-линзовое адверсариальное ревью** (correctness / security-RLS / UX), 0 critical/high.

## File Structure

- `admin-panel/templates/base.html` — навигация: добавить пункт «ИИ-команда клиента» в `is_platform`-блок (Task 1).
- `admin-panel/templates/my_team.html` — ролевое «клиент не выбран» (CTA) + бейдж активного клиента (Task 2).
- `scripts/platform_team_access_smoke.py` — **создать**: standalone Jinja render-смоук (Task 1 создаёт + nav-проверки; Task 2 расширяет).
- `admin-panel/app.py` — **без правок кода**; только верификация RLS-скоупа (Task 3).

---

### Task 1: Пункт меню «ИИ-команда клиента» для платформы + render-смоук

**Files:**
- Create: `scripts/platform_team_access_smoke.py`
- Modify: `admin-panel/templates/base.html` (платформенный nav-блок, строки ~99-107)

**Interfaces:**
- Consumes: шаблоны `admin-panel/templates/{base,my_team,_macros}.html`; env-globals `asset_version`, `service_site_url`.
- Produces: смоук-функция `render(**over)` → HTML строка `my_team.html` (использует Task 2).

- [ ] **Step 1: Создать смоук с nav-проверками (упадёт)**

Создать `scripts/platform_team_access_smoke.py`:

```python
#!/usr/bin/env python3
"""Render-смоук A1: «ИИ-команда клиента» в панели владельца-платформы.
Чистый Jinja-рендер шаблонов admin-panel (без БД/HTTP): nav-пункт платформы,
ролевое «клиент не выбран» (CTA «Клиенты» vs «поддержка»), бейдж активного клиента.
Запуск:
  PYTHONPATH=. ./.venv-smoke/bin/python scripts/platform_team_access_smoke.py
"""
import os
import sys

from jinja2 import ChainableUndefined, Environment, FileSystemLoader, select_autoescape

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TPL_DIR = os.path.join(ROOT, "admin-panel", "templates")

env = Environment(
    loader=FileSystemLoader(TPL_DIR),
    autoescape=select_autoescape(["html", "xml"]),
    undefined=ChainableUndefined,  # base.html может ссылаться на необязательные globals — не падаем
)
env.globals["asset_version"] = ""
env.globals["service_site_url"] = ""

FAILS: list[str] = []


def check(name, cond, detail=""):
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


BASE_CTX = dict(
    csrf_token="csrf", saved="", err="", active="my_team",
    agents=[], channel_map={}, messengers=[], presets=[],
    prompt_max=4000, support_url="https://t.me/support",
)


def render(**over):
    ctx = {**BASE_CTX, **over}
    return env.get_template("my_team.html").render(**ctx)


# 1. nav: платформа видит «ИИ-команда клиента» → /my-team
html_p = render(session={"is_platform": True}, has_tenant=False)
check("nav: платформа — пункт «ИИ-команда клиента» на /my-team",
      "ИИ-команда клиента" in html_p and "/my-team" in html_p)

# 2. nav: тенант видит «ИИ-команда», но НЕ «ИИ-команда клиента»
html_t = render(session={"is_platform": False}, has_tenant=True)
check("nav: тенант — «ИИ-команда» без платформенной метки",
      "ИИ-команда клиента" not in html_t)


def _summary():
    print(f"\n{'ВСЕ ОК' if not FAILS else 'ПРОВАЛЫ: ' + ', '.join(FAILS)}")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    _summary()
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `cd ~/Downloads/risuy-ecosystem && PYTHONPATH=. ./.venv-smoke/bin/python scripts/platform_team_access_smoke.py`
Expected: `FAIL nav: платформа …` (метки «ИИ-команда клиента» ещё нет) → exit 1.
⚠️ Если падает с Jinja-исключением (не-AssertionError) на рендере base.html — добавить недостающий ключ в `BASE_CTX` и повторить (ChainableUndefined должен это покрыть).

- [ ] **Step 3: Добавить пункт меню в `base.html`**

В `admin-panel/templates/base.html`, в `is_platform`-блоке навигации, добавить строку сразу после `agents`:

```html
      {%- if session and session.is_platform %}
      {{ nav_item('agents',       '/agents',     'ИИ-агенты',   active) }}
      {{ nav_item('my_team',      '/my-team',    'ИИ-команда клиента', active) }}
      {{ nav_item('knowledge',    '/knowledge',  'Базы знаний',  active) }}
      {{ nav_item('integrations', '/integrations', 'Интеграции',   active) }}
      {{ nav_item('demo_monitor', '/demo-monitor', 'Демо-монитор',  active) }}
      {%- else %}
      {{ nav_item('my_team',      '/my-team',    'ИИ-команда',       active) }}
      {{ nav_item('triggers',     '/triggers',   'Триггеры',         active) }}
      {%- endif %}
```

(Изменение — единственная новая строка `my_team` в `if`-ветке; `active='my_team'` совпадает с тем, что handler `/my-team` уже передаёт, поэтому подсветка активного пункта работает в обеих ветках.)

- [ ] **Step 4: Запустить — убедиться, что зелено**

Run: `cd ~/Downloads/risuy-ecosystem && PYTHONPATH=. ./.venv-smoke/bin/python scripts/platform_team_access_smoke.py`
Expected: `OK nav: платформа …`, `OK nav: тенант …` → `ВСЕ ОК`, exit 0.

- [ ] **Step 5: Коммит**

```bash
cd ~/Downloads/risuy-ecosystem
git add scripts/platform_team_access_smoke.py admin-panel/templates/base.html
git commit -m 'feat(panel): пункт «ИИ-команда клиента» в меню владельца-платформы (A1) + render-смоук' \
  -m 'Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>'
```

---

### Task 2: Ролевое «клиент не выбран» (CTA) + бейдж активного клиента

**Files:**
- Modify: `admin-panel/templates/my_team.html` (блок `{% if not has_tenant %}` строки ~16-22; page-head строки ~8-11)
- Modify: `scripts/platform_team_access_smoke.py` (добавить проверки 3-6)

**Interfaces:**
- Consumes: `render(**over)` и `check(...)` из Task 1; `session.is_platform`, `session.active_tenant_name`, `has_tenant` в шаблоне.
- Produces: финальный набор render-проверок A1.

- [ ] **Step 1: Добавить проверки CTA и бейджа (упадут)**

В `scripts/platform_team_access_smoke.py`, ПЕРЕД блоком `def _summary():`, вставить:

```python
# 3. no-tenant + платформа → CTA на «Клиенты», без текста «поддержка»
html = render(session={"is_platform": True}, has_tenant=False)
check("no-tenant платформа — CTA «Клиенты» (/tenants), без «поддержки»",
      ("Клиент не выбран" in html) and ("/tenants" in html) and ("напишите в поддержку" not in html))

# 4. no-tenant + тенант → «напишите в поддержку» (без регрессии)
html = render(session={"is_platform": False}, has_tenant=False)
check("no-tenant тенант — «напишите в поддержку»", "напишите в поддержку" in html)

# 5. бейдж активного клиента: платформа + активный тенант
html = render(session={"is_platform": True, "active_tenant_name": "ООО Ромашка"}, has_tenant=True)
check("бейдж — «Клиент: ООО Ромашка»", "Клиент: ООО Ромашка" in html)

# 6. у тенанта бейджа «Клиент:» нет
html = render(session={"is_platform": False, "active_tenant_name": "X"}, has_tenant=True)
check("тенант — без бейджа «Клиент:»", "Клиент:" not in html)
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `cd ~/Downloads/risuy-ecosystem && PYTHONPATH=. ./.venv-smoke/bin/python scripts/platform_team_access_smoke.py`
Expected: `FAIL no-tenant платформа …` и `FAIL бейдж …` (CTA/бейдж ещё не реализованы); проверки 2/4/6 — `OK`.

- [ ] **Step 3: Реализовать ролевой CTA в `my_team.html`**

Заменить блок `{% if not has_tenant %}` … (строки ~16-22):

```html
{% if not has_tenant %}
<section class="card">
  <div class="card__title">Кабинет ещё не привязан</div>
  <p class="card__note">Команда настраивается в кабинете клиента. Сейчас к учётной записи кабинет не привязан — напишите в поддержку.</p>
  {% if support_url %}<div class="acct-actions"><a class="btn btn--primary" href="{{ support_url }}" target="_blank" rel="noopener noreferrer nofollow">Написать в поддержку</a></div>{% endif %}
</section>
{% else %}
```

на:

```html
{% if not has_tenant %}
<section class="card">
  {% if session and session.is_platform %}
  <div class="card__title">Клиент не выбран</div>
  <p class="card__note">Выберите клиента в разделе «Клиенты», чтобы настроить его команду ИИ-агентов.</p>
  <div class="acct-actions"><a class="btn btn--primary" href="/tenants">Перейти к «Клиентам»</a></div>
  {% else %}
  <div class="card__title">Кабинет ещё не привязан</div>
  <p class="card__note">Команда настраивается в кабинете клиента. Сейчас к учётной записи кабинет не привязан — напишите в поддержку.</p>
  {% if support_url %}<div class="acct-actions"><a class="btn btn--primary" href="{{ support_url }}" target="_blank" rel="noopener noreferrer nofollow">Написать в поддержку</a></div>{% endif %}
  {% endif %}
</section>
{% else %}
```

- [ ] **Step 4: Реализовать бейдж клиента в `my_team.html`**

Сразу ПОСЛЕ закрывающего `</div>` блока `page-head` (строка ~11), вставить:

```html
{% if session and session.is_platform and has_tenant and session.active_tenant_name %}
<p class="page-head__hint"><span class="pill">Клиент: {{ session.active_tenant_name }}</span></p>
{% endif %}
```

- [ ] **Step 5: Запустить — убедиться, что зелено**

Run: `cd ~/Downloads/risuy-ecosystem && PYTHONPATH=. ./.venv-smoke/bin/python scripts/platform_team_access_smoke.py`
Expected: все 6 проверок `OK` → `ВСЕ ОК`, exit 0.

- [ ] **Step 6: Коммит**

```bash
cd ~/Downloads/risuy-ecosystem
git add admin-panel/templates/my_team.html scripts/platform_team_access_smoke.py
git commit -m 'feat(panel): ролевое «клиент не выбран» (CTA «Клиенты») + бейдж активного клиента на /my-team (A1)' \
  -m 'Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>'
```

---

### Task 3: Верификация RLS-скоупа + адверсариальное ревью + финализация

**Files:**
- Read-only: `admin-panel/app.py` (около строки 202 и 1034 — `db.set_active_tenant`)
- (правок кода НЕ ожидается; если ревью найдёт дефект — фикс + повтор смоука)

**Interfaces:**
- Consumes: вся реализация Task 1-2.
- Produces: подтверждение, что платформенная запись через `/my-team` скоупится на активного клиента (RLS).

- [ ] **Step 1: Подтвердить RLS-скоуп для платформенных запросов**

Прочитать контекст `admin-panel/app.py` вокруг строк 195-210 и 1030-1040:
Run: `cd ~/Downloads/risuy-ecosystem && sed -n '195,210p;1030,1040p' admin-panel/app.py`
Ожидаемо: `db.set_active_tenant(session.active_tenant_id)` вызывается в per-request хуке/зависимости (а не только в роутах тенанта) → POST `/my-team/*` от платформы пишет под RLS активного клиента.
⚠️ Если `set_active_tenant` НЕ применяется к платформенным запросам — это блокер: завести фикс-таск (вызвать перед CRUD `/my-team` для платформы) и НЕ финализировать.

- [ ] **Step 2: 3-линзовое адверсариальное ревью диффа**

Запустить ревью диффа (`git diff da5f54e..HEAD -- admin-panel scripts/platform_team_access_smoke.py`) по 3 линзам, каждая — отдельный скептик:
- **correctness:** nav рендерится в обеих ветках; `active='my_team'` не ломает подсветку; CTA/бейдж только в нужных комбинациях `is_platform`×`has_tenant`.
- **security (RLS):** платформа через `/my-team` POST не выходит за `active_tenant_id`; нет утечки чужого тенанта; бейдж берёт имя из `session` (не из непроверенного ввода).
- **UX:** метка «ИИ-команда клиента» не путается с «ИИ-агенты»; бейдж виден; CTA ведёт на `/tenants`; тексты по-русски.
Внести подтверждённые critical/high; LOW — зафиксировать в хвостах. Цель: 0 critical/high.

- [ ] **Step 3: Финальный прогон смоука**

Run: `cd ~/Downloads/risuy-ecosystem && PYTHONPATH=. ./.venv-smoke/bin/python scripts/platform_team_access_smoke.py`
Expected: `ВСЕ ОК`, exit 0.

- [ ] **Step 4: Коммит фиксов ревью (если были)**

```bash
cd ~/Downloads/risuy-ecosystem
git add -A
git commit -m 'fix(panel): правки 3-линзового ревью A1 (ИИ-команда клиента)' \
  -m 'Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>'
```

(Если фиксов нет — шаг пропустить.)

- [ ] **Step 5: Отчёт владельцу + гейт на push/деплой**

Сообщить: A1 готов локально (коммиты, смоук зелёный, ревью 0 critical/high). **Push в risuy** (ahead — спека + A1) и **деплой** (`twc`/App Platform) — по явному «да» владельца. После деплоя — live-проверка: войти платформой → «Клиенты» → переключиться → «ИИ-команда клиента» → бейдж + CRUD.

---

## Self-Review

**1. Spec coverage:**
- §3.1 nav-пункт «ИИ-команда клиента» → Task 1 ✅
- §3.2 ролевое «клиент не выбран» (CTA) → Task 2 Step 3 ✅
- §3.3 бейдж активного клиента → Task 2 Step 4 ✅
- §3.4 RLS-верификация → Task 3 Step 1 ✅
- §5 тест (render-смоук + ревью) → смоук Task 1-2, ревью Task 3 ✅
- §4 YAGNI (не трогаем `/my-agent`, БД, резолвер) → ни одна задача их не касается ✅

**2. Placeholder scan:** нет TBD/«добавить обработку»; всё код-содержащее показано целиком. ✅

**3. Type consistency:** `render(**over)` и `check(...)` определены в Task 1, используются в Task 2; метки «ИИ-команда клиента» / «Клиент не выбран» / «Клиент:» / `/tenants` едины в шаблонах и проверках смоука. ✅
