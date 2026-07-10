# Партнёрская реферальная подсистема — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Владелец заводит партнёра одной формой → партнёр получает публичную реф-ссылку → клиент по ней в боте называет компанию → бот создаёт тенанта+бриф с атрибуцией партнёру → партнёр и владелец получают TG-уведомления; отчёт по партнёру в панели.

**Architecture:** Новая таблица `partners` + атрибуция `tenants.partner_id`/`ref_tg_user_id`. Панель (`/partners`) — заведение/отчёт. Бот — публичный лендинг `/p/{code}` + ветка `?start=ref_{code}` (гард+FSM+создание) + уведомления через уже-надёжный `platform_notify`. Бот и панель — разные процессы/разные `db.py`, функции зеркалятся.

**Tech Stack:** Python 3.11, asyncpg, aiohttp (публичный сервер бота), aiogram (FSM), FastAPI+Jinja2 (панель), Postgres (Neon/Timeweb). Тесты — smoke-скрипты (`.venv-smoke`, без fastapi/aiohttp) + py_compile + jinja-parse.

## Global Constraints

- 🇷🇺 Только русский: код-комментарии, docstrings, UI-тексты, коммиты — на русском. Латиница — только идентификаторы/ключи/SQL.
- **db-смоуки гонит КОНТРОЛЛЕР** на `risuy_dev` (owner-DSN НЕ в субагенты). Субагент пишет код+смоук; контроллер применяет миграцию и гоняет db-смоук между задачами.
- **Платформенные таблицы БЕЗ RLS** (`partners`, как `tenants`/`platform_notify`); грант `panel_rw` select/insert/update. Бот ходит owner-DSN (bypass RLS).
- **Инвариант уведомлений:** enqueue best-effort, ВНЕ транзакции создания/сабмита, в try/except — НИКОГДА не рушит основной поток (паттерн Critical-фикса Спека 1).
- **Лид-воронка неприкосновенна:** ветка `ref_` в `cmd_start` — ДО нормализации source, как `club`/`intro_`; голый `/start` (source='other') не трогаем.
- Только позиционные параметры `$1..$n` в SQL (никакой f-string интерполяции ввода).
- Коммиттеры ПОСЛЕДОВАТЕЛЬНО (гонка git-индекса). Стейджить файлы явно.
- Миграция аддитивна, идемпотентна (`if not exists` / `add column if not exists`), сперва risuy_dev, прод по «да».
- Выкатка: push через аккаунт **BIZKON** (`BIZKON_TOKEN="$(gh auth token --user BIZKON)" git -c credential.helper= -c credential.helper='!f(){ echo username=BIZKON; echo "password=${BIZKON_TOKEN}"; }; f' push origin docs/security-audit:main`).

---

## File Structure

- `db/migrate_partners.sql` — CREATE (partners) + ALTER (tenants.partner_id/ref_tg_user_id) — новый.
- `admin-panel/db.py` — панель-хелперы partners (create/list/get/list_tenants/status/chat_id) — модифиц.
- `admin-panel/app.py` — роуты `/partners*` — модифиц.
- `admin-panel/templates/partners.html`, `partner_detail.html` — новые.
- `admin-panel/templates/base.html` (или nav-партиал) — ссылка на «Партнёры» — модифиц.
- `bot-telegram/db.py` — бот-хелперы (get_partner_by_ref_code, create_ref_tenant, find_pending_ref_brief, count_recent_ref_tenants, get_partner_chat_id) + расширение submit_brief — модифиц.
- `bot-telegram/bot.py` — `_partner_landing`/`_partner_landing_html` + роут `/p/{code}` — модифиц.
- `bot-telegram/handlers.py` — `PartnerRef` FSM + ветка `ref_` в cmd_start + `_ref_start` + `on_ref_company` — модифиц.
- `bot-telegram/config.py` — `REF_RATELIMIT_HOURS`/`REF_RATELIMIT_MAX` — модифиц.
- `scripts/partners_smoke.py`, `scripts/partner_ref_bot_smoke.py` — новые.

---

## Task 1: Миграция + панель-слой данных partners

**Files:**
- Create: `db/migrate_partners.sql`
- Modify: `admin-panel/db.py` (добавить хелперы partners после блока owner_chat_id/platform_notify)
- Test: `scripts/partners_smoke.py`

**Interfaces:**
- Produces (panel db):
  - `create_partner(name: str, tg_chat_id: str | None, *, actor: str, ip: str | None, user_agent: str | None) -> tuple[str, str]` → `(partner_id, ref_code)`
  - `list_partners() -> list[dict]` (id, name, ref_code, tg_chat_id, status, created_at, tenant_count, brief_done)
  - `get_partner(partner_id: str) -> dict | None`
  - `list_partner_tenants(partner_id: str) -> list[dict]` (id, name, slug, created_at, brief_id, brief_status, token)
  - `set_partner_status(partner_id: str, status: str, *, actor, ip, user_agent) -> bool`
  - `set_partner_chat_id(partner_id: str, tg_chat_id: str | None, *, actor, ip, user_agent) -> bool`

- [ ] **Step 1: Написать миграцию `db/migrate_partners.sql`**

```sql
-- partners: реестр партнёров реферальной программы (платформенный артефакт, БЕЗ RLS, как tenants).
-- Применение: apply_migration.py (APPLY_EXPECT_DB=risuy_dev|risuy). Аддитивно, идемпотентно.
create table if not exists partners (
    id         uuid primary key default gen_random_uuid(),
    name       text not null,
    ref_code   text not null unique,             -- авто secrets.token_hex(4)
    tg_chat_id text,                             -- для уведомлений партнёру (может быть пустым)
    status     text not null default 'active',
    created_at timestamptz not null default now(),
    constraint partners_status_chk check (status in ('active','disabled'))
);
-- Атрибуция тенанта партнёру + кто создал (дедуп/rate-limit реф-потока).
alter table tenants add column if not exists partner_id     uuid references partners(id);
alter table tenants add column if not exists ref_tg_user_id bigint;
create index if not exists tenants_partner_idx on tenants (partner_id) where partner_id is not null;

do $$ begin
    if exists (select 1 from pg_roles where rolname='panel_rw') then
        grant select, insert, update on partners to panel_rw;
    end if;
end $$;
```

- [ ] **Step 2: Написать смоук `scripts/partners_smoke.py` (панель-сторона)**

```python
#!/usr/bin/env python3
"""DB-смоук partners — ПАНЕЛЬ-сторона (контроллер, risuy_dev):
  PARTNERS_SMOKE_DSN="...risuy_dev..." PYTHONPATH=admin-panel:. \
    ./.venv-smoke/bin/python scripts/partners_smoke.py
"""
import asyncio, os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, "admin-panel"))
os.environ.setdefault("DATABASE_URL", "postgresql://x/y")
os.environ.setdefault("SESSION_SECRET", "smoke-secret-padding-0123456789abcdef")
os.environ.setdefault("ADMIN_USERNAME", "smoke")
os.environ.setdefault("ADMIN_PASSWORD_HASH", "$argon2id$v=19$m=65536,t=3,p=4$c21va2U$c21va2U")
import asyncpg  # noqa: E402
import db  # noqa: E402
DSN = os.environ.get("PARTNERS_SMOKE_DSN")
if not DSN or "/risuy_dev" not in DSN.split("?")[0]:
    raise SystemExit("Задайте PARTNERS_SMOKE_DSN на risuy_dev")
FAILS = []
PNAME = "СМОУК Партнёр"
TNAME = "СМОУК РефТенант ООО"


def check(name, cond, detail=""):
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


async def _cleanup(c):
    await c.execute("delete from tenant_brief where tenant_id in (select id from tenants where name=$1)", TNAME)
    await c.execute("delete from tenants where name=$1", TNAME)
    await c.execute("delete from partners where name=$1", PNAME)


async def main():
    db.pool = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    try:
        async with db.pool.acquire() as c:
            await _cleanup(c)
        print("1. create_partner:")
        pid, ref = await db.create_partner(PNAME, "555111", actor="smoke", ip=None, user_agent=None)
        check("вернул (id, ref_code)", bool(pid) and len(ref) >= 8, f"ref={ref}")
        print("2. list_partners видит партнёра со счётчиками:")
        rows = await db.list_partners()
        mine = [r for r in rows if str(r["id"]) == pid]
        check("партнёр в списке", len(mine) == 1)
        check("tenant_count=0 пока нет тенантов", mine and mine[0]["tenant_count"] == 0)
        print("3. атрибуция: тенант с partner_id учитывается:")
        async with db.pool.acquire() as c:
            tid = await c.fetchval("insert into tenants(slug,name,status,partner_id) "
                                   "values($1,$2,'active',$3) returning id",
                                   "smoke-reft", TNAME, pid)
            await c.execute("insert into tenant_brief(tenant_id,token,status) values($1,'smoke-reft-tok','submitted')", tid)
        pt = await db.list_partner_tenants(pid)
        check("list_partner_tenants вернул тенанта", any(str(r["id"]) == str(tid) for r in pt))
        check("brief_status виден", any(r["brief_status"] == "submitted" for r in pt))
        rows2 = await db.list_partners()
        m2 = [r for r in rows2 if str(r["id"]) == pid][0]
        check("tenant_count=1", m2["tenant_count"] == 1, f"c={m2['tenant_count']}")
        check("brief_done=1", m2["brief_done"] == 1, f"d={m2['brief_done']}")
        print("4. set_partner_status disabled → get_partner видит:")
        await db.set_partner_status(pid, "disabled", actor="smoke", ip=None, user_agent=None)
        check("status disabled", (await db.get_partner(pid))["status"] == "disabled")
        print("5. set_partner_chat_id:")
        await db.set_partner_chat_id(pid, "999000", actor="smoke", ip=None, user_agent=None)
        check("chat_id обновлён", (await db.get_partner(pid))["tg_chat_id"] == "999000")
    finally:
        async with db.pool.acquire() as c:
            await _cleanup(c)
        await db.pool.close()
    print()
    if FAILS:
        print("❌ ПРОВАЛЫ: " + ", ".join(FAILS)); sys.exit(1)
    print("✅ partners smoke (panel-side) — OK")


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 3: КОНТРОЛЛЕР применяет миграцию + запускает смоук — verify FAIL**

Контроллер (owner-DSN):
```bash
APPLY_DSN="$DEV_DSN" APPLY_EXPECT_DB=risuy_dev .venv-smoke/bin/python scratchpad/apply_migration.py db/migrate_partners.sql
PARTNERS_SMOKE_DSN="$DEV_DSN" PYTHONPATH=admin-panel:. .venv-smoke/bin/python scripts/partners_smoke.py
```
Expected: FAIL — `AttributeError: module 'db' has no attribute 'create_partner'`.

- [ ] **Step 4: Реализовать хелперы в `admin-panel/db.py`** (после блока platform_notify)

```python
# ── Партнёрская реферал-программа (partners + атрибуция tenants) ───────────────
async def create_partner(name: str, tg_chat_id: str | None, *, actor: str,
                         ip: str | None, user_agent: str | None) -> tuple[str, str]:
    """Создать партнёра с авто-ref_code (secrets.token_hex(4), ретрай на unique). Возврат (id, ref_code)."""
    safe_name = (name or "").strip()[:120] or "Партнёр"
    chat = (tg_chat_id or "").strip() or None
    async with pool.acquire() as c:
        for _ in range(5):
            ref = secrets.token_hex(4)
            try:
                async with c.transaction():
                    pid = await c.fetchval(
                        "insert into partners(name, ref_code, tg_chat_id) values($1,$2,$3) returning id",
                        safe_name, ref, chat)
                    await _insert_audit(c, actor=actor, action="partner_create", ip=ip,
                                        user_agent=user_agent, detail={"partner_id": str(pid), "ref_code": ref})
                return str(pid), ref
            except asyncpg.UniqueViolationError:
                continue
        raise RuntimeError("не удалось сгенерировать уникальный ref_code")


async def list_partners() -> list[dict]:
    async with pool.acquire() as c:
        rows = await c.fetch(
            "select p.id, p.name, p.ref_code, p.tg_chat_id, p.status, p.created_at, "
            "count(distinct t.id) as tenant_count, "
            "count(distinct t.id) filter (where b.status in ('submitted','proposed','applied')) as brief_done "
            "from partners p "
            "left join tenants t on t.partner_id = p.id "
            "left join tenant_brief b on b.tenant_id = t.id "
            "group by p.id order by p.created_at desc")
    return [dict(r) for r in rows]


async def get_partner(partner_id: str) -> dict | None:
    async with pool.acquire() as c:
        row = await c.fetchrow(
            "select id, name, ref_code, tg_chat_id, status, created_at from partners where id = $1",
            partner_id)
    return dict(row) if row else None


async def list_partner_tenants(partner_id: str) -> list[dict]:
    async with pool.acquire() as c:
        rows = await c.fetch(
            "select t.id, t.name, t.slug, t.created_at, "
            "b.id as brief_id, b.status as brief_status, b.token "
            "from tenants t left join tenant_brief b on b.tenant_id = t.id "
            "where t.partner_id = $1 order by t.created_at desc", partner_id)
    return [dict(r) for r in rows]


async def set_partner_status(partner_id: str, status: str, *, actor: str,
                            ip: str | None, user_agent: str | None) -> bool:
    if status not in ("active", "disabled"):
        return False
    async with pool.acquire() as c:
        async with c.transaction():
            res = await c.execute("update partners set status = $2 where id = $1", partner_id, status)
            if res.endswith(" 0"):
                return False
            await _insert_audit(c, actor=actor, action="partner_status", ip=ip,
                                user_agent=user_agent, detail={"partner_id": partner_id, "status": status})
    return True


async def set_partner_chat_id(partner_id: str, tg_chat_id: str | None, *, actor: str,
                             ip: str | None, user_agent: str | None) -> bool:
    val = (tg_chat_id or "").strip() or None
    async with pool.acquire() as c:
        async with c.transaction():
            res = await c.execute("update partners set tg_chat_id = $2 where id = $1", partner_id, val)
            if res.endswith(" 0"):
                return False
            await _insert_audit(c, actor=actor, action="partner_chat_id", ip=ip,
                                user_agent=user_agent, detail={"partner_id": partner_id, "set": bool(val)})
    return True
```

- [ ] **Step 5: КОНТРОЛЛЕР запускает смоук — verify PASS**

Run: `PARTNERS_SMOKE_DSN="$DEV_DSN" PYTHONPATH=admin-panel:. .venv-smoke/bin/python scripts/partners_smoke.py`
Expected: `✅ partners smoke (panel-side) — OK`

- [ ] **Step 6: py_compile + коммит**

```bash
.venv-smoke/bin/python -m py_compile admin-panel/db.py scripts/partners_smoke.py
git add db/migrate_partners.sql admin-panel/db.py scripts/partners_smoke.py
git commit -m "feat(partners): миграция partners + панель-слой данных (create/list/report/status/chat_id)"
```

---

## Task 2: Панель — /partners (форма + список + статус + chat_id) + отчёт /partners/{id}

**Files:**
- Modify: `admin-panel/app.py` (роуты `/partners*` рядом с `/brief-center`)
- Create: `admin-panel/templates/partners.html`, `admin-panel/templates/partner_detail.html`
- Modify: nav (ссылка «Партнёры») — там же, где ссылка «Бриф-центр» (найти в base.html/partial грепом `brief-center`)

**Interfaces:**
- Consumes (Task 1): `create_partner`, `list_partners`, `get_partner`, `list_partner_tenants`, `set_partner_status`, `set_partner_chat_id`, `db.get_bot_public_base_url()`.

- [ ] **Step 1: Роуты в `admin-panel/app.py`** (образец `brief_center*`, гейт `_require_admin`, CSRF)

```python
@app.get("/partners", response_class=HTMLResponse)
async def partners_page(request: Request, session: auth.Session = Depends(require_session),
                        saved: str | None = None, err: str | None = None):
    _require_admin(session)
    partners = await db.list_partners()
    base = await db.get_bot_public_base_url()
    return templates.TemplateResponse(request, "partners.html", {
        "partners": partners, "base_url": base, "saved": saved, "err": err,
        "csrf_token": session.csrf_token, "session": session, "active": "partners"})


@app.post("/partners/create")
async def partners_create(request: Request, session: auth.Session = Depends(require_session),
                          name: str = Form(""), tg_chat_id: str = Form(""), csrf_token: str = Form("")):
    _require_admin(session)
    await _enforce_csrf(request, session, csrf_token)
    if not name.strip():
        return RedirectResponse(url="/partners?err=no_name", status_code=303)
    await db.create_partner(name, tg_chat_id, actor=session.actor, ip=_ip(request), user_agent=_ua(request))
    return RedirectResponse(url="/partners?saved=created", status_code=303)


@app.post("/partners/{partner_id}/status")
async def partners_set_status(request: Request, partner_id: str,
                              session: auth.Session = Depends(require_session),
                              status: str = Form(""), csrf_token: str = Form("")):
    _require_admin(session)
    await _enforce_csrf(request, session, csrf_token)
    await db.set_partner_status(partner_id, status.strip(), actor=session.actor,
                                ip=_ip(request), user_agent=_ua(request))
    return RedirectResponse(url="/partners?saved=status", status_code=303)


@app.post("/partners/{partner_id}/chat-id")
async def partners_set_chat_id(request: Request, partner_id: str,
                               session: auth.Session = Depends(require_session),
                               tg_chat_id: str = Form(""), csrf_token: str = Form("")):
    _require_admin(session)
    await _enforce_csrf(request, session, csrf_token)
    await db.set_partner_chat_id(partner_id, tg_chat_id, actor=session.actor,
                                 ip=_ip(request), user_agent=_ua(request))
    return RedirectResponse(url="/partners?saved=chat", status_code=303)


@app.get("/partners/{partner_id}", response_class=HTMLResponse)
async def partner_detail(request: Request, partner_id: str,
                         session: auth.Session = Depends(require_session)):
    _require_admin(session)
    partner = await db.get_partner(partner_id)
    if not partner:
        raise StarletteHTTPException(status_code=404)
    tenants = await db.list_partner_tenants(partner_id)
    return templates.TemplateResponse(request, "partner_detail.html", {
        "partner": partner, "tenants": tenants,
        "csrf_token": session.csrf_token, "session": session, "active": "partners"})
```

- [ ] **Step 2: Шаблон `admin-panel/templates/partners.html`** (образец brief_center.html)

```html
{% extends "base.html" %}
{% from "_macros.html" import flash %}
{% block title %}Партнёры{% endblock %}
{% block content %}
<div class="page-head"><h1 class="page-head__title">Партнёры</h1>
  <p class="page-head__hint">Заведите партнёра — получите готовую реф-ссылку. Клиент по ней в боте называет компанию, бот создаёт бриф и атрибутирует тенанта партнёру.</p></div>
{% if saved == 'created' %}{{ flash('Партнёр создан. Скопируйте реф-ссылку из списка.', 'ok') }}{% endif %}
{% if saved == 'status' %}{{ flash('Статус партнёра обновлён.', 'ok') }}{% endif %}
{% if saved == 'chat' %}{{ flash('Chat ID партнёра сохранён.', 'ok') }}{% endif %}
{% if err == 'no_name' %}{{ flash('Введите имя партнёра.', 'error') }}{% endif %}

<form method="post" action="/partners/create" class="card">
  <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
  <label class="field"><span class="field__label">Имя партнёра</span>
    <input class="field__input" type="text" name="name" maxlength="120" required></label>
  <label class="field"><span class="field__label">Chat ID для уведомлений (опц.)</span>
    <input class="field__input mono" type="text" name="tg_chat_id"></label>
  <button class="btn btn--primary" type="submit">Создать партнёра</button>
</form>

{% if partners %}
<div class="table-wrap"><table class="table table--zebra">
  <thead><tr><th>Партнёр</th><th>Реф-ссылка</th><th>Тенантов</th><th>Прошли бриф</th><th>Chat ID</th><th>Статус</th></tr></thead>
  <tbody>
  {% for p in partners %}
  <tr>
    <td><a href="/partners/{{ p.id }}">{{ p.name|e }}</a></td>
    <td>{% if base_url %}<code class="mono">{{ base_url }}/p/{{ p.ref_code }}</code>{% else %}<span class="muted">бот не публиковал base</span>{% endif %}</td>
    <td>{{ p.tenant_count }}</td>
    <td>{{ p.brief_done }}</td>
    <td>
      <form method="post" action="/partners/{{ p.id }}/chat-id" class="inline-form">
        <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
        <input class="field__input mono" type="text" name="tg_chat_id" value="{{ p.tg_chat_id|e if p.tg_chat_id else '' }}">
        <button class="btn btn--sm" type="submit">Сохранить</button>
      </form>
    </td>
    <td>
      <form method="post" action="/partners/{{ p.id }}/status" class="inline-form">
        <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
        <input type="hidden" name="status" value="{{ 'disabled' if p.status == 'active' else 'active' }}">
        <button class="btn btn--sm" type="submit">{{ 'Отключить' if p.status == 'active' else 'Включить' }}</button>
      </form>
      <span class="pill {{ 'pill--ok' if p.status == 'active' else 'pill--muted' }}">{{ p.status }}</span>
    </td>
  </tr>
  {% endfor %}
  </tbody></table></div>
{% else %}<p class="empty-state">Партнёров пока нет.</p>{% endif %}
{% endblock %}
```

- [ ] **Step 3: Шаблон `admin-panel/templates/partner_detail.html`**

```html
{% extends "base.html" %}
{% block title %}Партнёр — {{ partner.name }}{% endblock %}
{% block content %}
<div class="page-head"><h1 class="page-head__title">{{ partner.name|e }}</h1>
  <p class="page-head__hint">Реф-код <code class="mono">{{ partner.ref_code }}</code> · статус {{ partner.status }}</p></div>
<p><a href="/partners">← Все партнёры</a></p>
{% if tenants %}
<div class="table-wrap"><table class="table table--zebra">
  <thead><tr><th>Компания</th><th>Создан</th><th>Статус брифа</th><th></th></tr></thead>
  <tbody>
  {% for t in tenants %}
  <tr>
    <td>{{ t.name|e }} <span class="mono muted">{{ t.slug|e }}</span></td>
    <td>{{ t.created_at.strftime('%d.%m.%Y') if t.created_at else '' }}</td>
    <td>{{ t.brief_status or '—' }}</td>
    <td>{% if t.brief_id %}<a href="/brief-center/{{ t.brief_id }}">Бриф</a>{% endif %}</td>
  </tr>
  {% endfor %}
  </tbody></table></div>
{% else %}<p class="empty-state">От этого партнёра пока нет тенантов.</p>{% endif %}
{% endblock %}
```

- [ ] **Step 4: Ссылка в навигации**

Найти nav-блок (грепнуть по `href="/brief-center"` в `admin-panel/templates/`), рядом добавить:
```html
<a href="/partners" class="{{ 'active' if active == 'partners' }}">Партнёры</a>
```
(скопировать точный класс/разметку соседнего пункта — стиль навигации панели).

- [ ] **Step 5: py_compile + jinja-parse — verify**

```bash
.venv-smoke/bin/python -m py_compile admin-panel/app.py
.venv-smoke/bin/python -c "import jinja2,sys; [jinja2.Environment().parse(open('admin-panel/templates/'+f,encoding='utf-8').read()) for f in ('partners.html','partner_detail.html')]; print('jinja OK')"
```
Expected: без ошибок; `jinja OK`.

- [ ] **Step 6: Коммит**

```bash
git add admin-panel/app.py admin-panel/templates/partners.html admin-panel/templates/partner_detail.html admin-panel/templates/base.html
git commit -m "feat(partners): панель /partners (форма+список+статус+chat_id) + отчёт /partners/{id}"
```

---

## Task 3: Бот-слой данных реф-потока

**Files:**
- Modify: `bot-telegram/db.py` (хелперы partners после блока platform_notify)
- Modify: `bot-telegram/config.py` (пороги rate-limit)
- Test: `scripts/partner_ref_bot_smoke.py`

**Interfaces:**
- Produces (bot db):
  - `get_partner_by_ref_code(ref_code: str) -> dict | None` (только active; id, name, tg_chat_id)
  - `create_ref_tenant(partner_id: str, company: str, tg_user_id: int) -> tuple[str, str]` → `(tenant_id, brief_token)`
  - `find_pending_ref_brief(tg_user_id: int, partner_id: str) -> str | None` (token pending-брифа для дедупа)
  - `count_recent_ref_tenants(tg_user_id: int, hours: int) -> int`
  - `get_partner_chat_id(partner_id: str) -> str | None`
- Produces (config): `REF_RATELIMIT_HOURS: int`, `REF_RATELIMIT_MAX: int`

- [ ] **Step 1: Пороги в `bot-telegram/config.py`** (рядом с прочими константами)

```python
# Партнёрский реф-поток: анти-абьюз (лёгкий гард).
REF_RATELIMIT_HOURS = int(os.environ.get("REF_RATELIMIT_HOURS", "24"))
REF_RATELIMIT_MAX = int(os.environ.get("REF_RATELIMIT_MAX", "3"))
```

- [ ] **Step 2: Смоук `scripts/partner_ref_bot_smoke.py` (бот-сторона)**

```python
#!/usr/bin/env python3
"""DB-смоук реф-потока — БОТ-сторона (контроллер, risuy_dev):
  PARTNERS_SMOKE_DSN="...risuy_dev..." PYTHONPATH=bot-telegram:. \
    ./.venv-smoke/bin/python scripts/partner_ref_bot_smoke.py
"""
import asyncio, os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, "bot-telegram"))
os.environ.setdefault("BOT_TOKEN", "smoke-token")
os.environ.setdefault("DATABASE_URL", "postgresql://x/y")
os.environ.setdefault("CHANNEL_ID", "-1001234567890")
os.environ.setdefault("CHANNEL_URL", "https://t.me/smoke")
os.environ.setdefault("GUIDE_URL", "https://example.com/guide")
import asyncpg  # noqa: E402
import db  # noqa: E402
DSN = os.environ.get("PARTNERS_SMOKE_DSN")
if not DSN or "/risuy_dev" not in DSN.split("?")[0]:
    raise SystemExit("Задайте PARTNERS_SMOKE_DSN на risuy_dev")
FAILS = []
PNAME = "СМОУК РефБот Партнёр"
UID = 91_000_222


def check(name, cond, detail=""):
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


async def _cleanup(c, pid):
    if pid:
        await c.execute("delete from tenant_brief where tenant_id in (select id from tenants where partner_id=$1)", pid)
        await c.execute("delete from tenants where partner_id=$1", pid)
    await c.execute("delete from partners where name=$1", PNAME)


async def main():
    db.pool = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    pid = None
    try:
        async with db.pool.acquire() as c:
            await _cleanup(c, None)
            ref = "smokeref01"
            pid = await c.fetchval("insert into partners(name,ref_code,tg_chat_id,status) "
                                   "values($1,$2,'700700','active') returning id", PNAME, ref)
        print("1. get_partner_by_ref_code (active):")
        p = await db.get_partner_by_ref_code(ref)
        check("резолв активного партнёра", p is not None and str(p["id"]) == str(pid))
        print("2. create_ref_tenant → тенант+бриф+атрибуция:")
        tid, tok = await db.create_ref_tenant(str(pid), "СМОУК РефКомпания", UID)
        check("вернул (tid, token)", bool(tid) and len(tok) >= 16)
        async with db.pool.acquire() as c:
            r = await c.fetchrow("select status, partner_id, ref_tg_user_id from tenants where id=$1", tid)
            bst = await c.fetchval("select status from tenant_brief where tenant_id=$1", tid)
        check("тенант active", r["status"] == "active")
        check("partner_id проставлен", str(r["partner_id"]) == str(pid))
        check("ref_tg_user_id проставлен", r["ref_tg_user_id"] == UID)
        check("бриф pending", bst == "pending", f"bst={bst}")
        print("3. дедуп: find_pending_ref_brief находит незавершённый:")
        dtok = await db.find_pending_ref_brief(UID, str(pid))
        check("вернул тот же token", dtok == tok, f"dtok={dtok}")
        print("4. rate-limit: count_recent_ref_tenants:")
        n = await db.count_recent_ref_tenants(UID, 24)
        check("посчитал >=1 за 24ч", n >= 1, f"n={n}")
        print("5. get_partner_chat_id:")
        check("chat_id партнёра", await db.get_partner_chat_id(str(pid)) == "700700")
        print("6. disabled партнёр не резолвится:")
        async with db.pool.acquire() as c:
            await c.execute("update partners set status='disabled' where id=$1", pid)
        check("get_partner_by_ref_code(disabled) → None", await db.get_partner_by_ref_code(ref) is None)
    finally:
        async with db.pool.acquire() as c:
            await _cleanup(c, pid)
        await db.pool.close()
    print()
    if FAILS:
        print("❌ ПРОВАЛЫ: " + ", ".join(FAILS)); sys.exit(1)
    print("✅ partner ref bot smoke — OK")


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 3: КОНТРОЛЛЕР запускает смоук — verify FAIL**

Run: `PARTNERS_SMOKE_DSN="$DEV_DSN" PYTHONPATH=bot-telegram:. .venv-smoke/bin/python scripts/partner_ref_bot_smoke.py`
Expected: FAIL — `AttributeError: module 'db' has no attribute 'get_partner_by_ref_code'`.

- [ ] **Step 4: Реализовать в `bot-telegram/db.py`** (после блока platform_notify)

```python
# ── Партнёрский реф-поток (partners resolve + создание реф-тенанта) ────────────
async def get_partner_by_ref_code(ref_code: str) -> dict | None:
    """Активный партнёр по ref_code (для лендинга и ?start=ref_). None — нет/disabled."""
    async with pool.acquire() as c:
        row = await c.fetchrow(
            "select id, name, tg_chat_id from partners where ref_code = $1 and status = 'active'",
            ref_code)
    return dict(row) if row else None


async def get_partner_chat_id(partner_id: str) -> str | None:
    async with pool.acquire() as c:
        return await c.fetchval("select tg_chat_id from partners where id = $1", partner_id)


async def create_ref_tenant(partner_id: str, company: str, tg_user_id: int) -> tuple[str, str]:
    """Зеркало create_tenant_admin + create_tenant_brief в одной tx, с атрибуцией партнёру.
    Возврат (tenant_id, brief_token). Бот=owner-DSN (bypass RLS); audit не пишем (нет admin-actor)."""
    import secrets
    from datetime import datetime, timezone, timedelta
    safe_name = (company or "").strip()[:120] or "Новый клиент"
    slug = f"client-{secrets.token_hex(10)}"
    token = secrets.token_hex(16)
    expires = datetime.now(timezone.utc) + timedelta(days=30)
    async with pool.acquire() as c:
        async with c.transaction():
            tid = await c.fetchval(
                "insert into tenants (slug, name, status, partner_id, ref_tg_user_id) "
                "values ($1, $2, 'active', $3, $4) returning id",
                slug, safe_name, partner_id, tg_user_id)
            await c.execute(
                "insert into tenant_brief (tenant_id, token, status, created_by, expires_at) "
                "values ($1, $2, 'pending', 'bot:ref', $3)",
                tid, token, expires)
    return str(tid), token


async def find_pending_ref_brief(tg_user_id: int, partner_id: str) -> str | None:
    """Дедуп: token незавершённого (pending) брифа, созданного этим tg_user для этого партнёра."""
    async with pool.acquire() as c:
        return await c.fetchval(
            "select b.token from tenant_brief b join tenants t on t.id = b.tenant_id "
            "where t.ref_tg_user_id = $1 and t.partner_id = $2 and b.status = 'pending' "
            "order by t.created_at desc limit 1",
            tg_user_id, partner_id)


async def count_recent_ref_tenants(tg_user_id: int, hours: int) -> int:
    """Rate-limit: сколько реф-тенантов создал этот tg_user за последние `hours` часов."""
    async with pool.acquire() as c:
        return await c.fetchval(
            "select count(*) from tenants where ref_tg_user_id = $1 "
            "and created_at > now() - make_interval(hours => $2)",
            tg_user_id, hours)
```

- [ ] **Step 5: КОНТРОЛЛЕР запускает смоук — verify PASS**

Run: `PARTNERS_SMOKE_DSN="$DEV_DSN" PYTHONPATH=bot-telegram:. .venv-smoke/bin/python scripts/partner_ref_bot_smoke.py`
Expected: `✅ partner ref bot smoke — OK`

- [ ] **Step 6: py_compile + коммит**

```bash
.venv-smoke/bin/python -m py_compile bot-telegram/db.py bot-telegram/config.py scripts/partner_ref_bot_smoke.py
git add bot-telegram/db.py bot-telegram/config.py scripts/partner_ref_bot_smoke.py
git commit -m "feat(partners): бот-слой реф-потока (resolve/create_ref_tenant/дедуп/rate-limit/chat_id)"
```

---

## Task 4: Публичный лендинг /p/{code} + ветка ?start=ref_ (FSM + создание + уведомления)

**Files:**
- Modify: `bot-telegram/bot.py` (`_partner_landing_html`, `_partner_landing`, роут)
- Modify: `bot-telegram/handlers.py` (`PartnerRef` FSM, ветка `ref_`, `_ref_start`, `on_ref_company`)

**Interfaces:**
- Consumes (Task 3): `db.get_partner_by_ref_code`, `db.create_ref_tenant`, `db.find_pending_ref_brief`, `db.count_recent_ref_tenants`, `db.get_partner_chat_id`, `db.get_owner_chat_id`, `db.enqueue_platform_notify`, `config.REF_RATELIMIT_HOURS/MAX`, `config.BOT_PUBLIC_BASE_URL`, `_BOT_USERNAME`.

- [ ] **Step 1: Лендинг в `bot-telegram/bot.py`** (рядом с `_club_landing`)

```python
def _partner_landing_html(partner_name: str, deeplink: str) -> str:
    """Самодостаточный HTML-лендинг реф-партнёра (без внешних ресурсов). Публичный."""
    import html as _html
    name = _html.escape(partner_name or "")
    return (
        '<!doctype html><html lang="ru"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>Персональный бриф — {name}</title>'
        '<style>body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:640px;'
        'margin:0 auto;padding:32px 20px;color:#1F2937;line-height:1.55}'
        '.btn{display:inline-block;background:#E63946;color:#fff;padding:14px 28px;'
        'border-radius:12px;text-decoration:none;font-weight:600;margin:20px 0}'
        '.muted{color:#6b7280;font-size:14px}h1{font-size:24px}</style></head><body>'
        f'<h1>Персональный бриф от партнёра {name}</h1>'
        '<p>Нажмите кнопку — бот задаст пару вопросов о вашей компании и подготовит '
        'персональный бриф для настройки ИИ-сотрудника.</p>'
        f'<a class="btn" href="{_html.escape(deeplink)}">Получить бриф</a>'
        '<p class="muted">Продолжая, вы соглашаетесь на обработку данных вашего бизнеса.</p>'
        '</body></html>'
    )


async def _partner_landing(request: web.Request) -> web.StreamResponse:
    """Публичная страница реф-партнёра: GET /p/{code}. Неизвестный/disabled/нет username → 404."""
    code = request.match_info.get("code", "")
    try:
        partner = await db.get_partner_by_ref_code(code)
    except Exception:  # noqa: BLE001
        logger.warning("partner-landing: резолв упал code=%s", code, exc_info=True)
        partner = None
    if partner is None or not _BOT_USERNAME:
        return web.Response(status=404, text="Ссылка недействительна")
    deeplink = f"https://t.me/{_BOT_USERNAME}?start=ref_{code}"
    resp = web.Response(text=_partner_landing_html(partner["name"], deeplink),
                        content_type="text/html", charset="utf-8")
    resp.headers["Cache-Control"] = "public, max-age=600"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp
```

- [ ] **Step 2: Регистрация роута** в `_start_health` (рядом с `/club/{slug}`)

```python
    app.router.add_get("/p/{code}", _partner_landing)  # публичный лендинг реф-партнёра
```

- [ ] **Step 3: FSM + ветка в `bot-telegram/handlers.py`**

FSM-класс (рядом с `ClubSignup`):
```python
class PartnerRef(StatesGroup):
    """Реф-поток: клиент по партнёрской ссылке называет компанию → бот создаёт тенанта+бриф."""
    company = State()
```

Ветка в `cmd_start` (ПОСЛЕ ветки `intro_`, ДО нормализации source):
```python
    if args.lower().startswith("ref_"):
        ref_code = args[len("ref_"):]
        await _ref_start(message, ref_code, state)
        return
```
⚠️ `ref_` — action-payload (как `club`/`intro_`), НЕ рекламный source. `VALID_SOURCES` и «три места» НЕ трогаем: ветка перехватывает раньше нормализации source и делает early-return, поэтому голый `/start` (source='other') и валидация площадок не затрагиваются.

Функции (рядом с `_club_start`):
```python
async def _ref_start(message: Message, ref_code: str, state: FSMContext) -> None:
    """Вход реф-потока по ?start=ref_<code>. Резолв партнёра + лёгкий гард (дедуп + rate-limit)."""
    partner = await db.get_partner_by_ref_code(ref_code)
    if partner is None:
        await messaging.reply_text(message, "Ссылка недействительна или отозвана.", source="system")
        return
    uid = message.from_user.id
    dup = await db.find_pending_ref_brief(uid, str(partner["id"]))
    if dup:
        await messaging.reply_text(
            message, f"Вы уже начали. Заполните бриф: {config.BOT_PUBLIC_BASE_URL}/brief/{dup}",
            source="system")
        return
    if await db.count_recent_ref_tenants(uid, config.REF_RATELIMIT_HOURS) >= config.REF_RATELIMIT_MAX:
        await messaging.reply_text(message, "Слишком много обращений. Попробуйте позже.", source="system")
        return
    await state.update_data(ref_partner_id=str(partner["id"]))
    await state.set_state(PartnerRef.company)
    await messaging.reply_text(message, "Как называется ваша компания?", source="system")


@router.message(PartnerRef.company)
async def on_ref_company(message: Message, state: FSMContext) -> None:
    """Приём названия компании → создание реф-тенанта+брифа + уведомления партнёру/владельцу."""
    company = (message.text or "").strip()[:120]
    if not company:
        await messaging.reply_text(message, "Напишите название компании текстом.", source="system")
        return
    data = await state.get_data()
    partner_id = data.get("ref_partner_id")
    await state.clear()
    if not partner_id:
        return
    _tid, token = await db.create_ref_tenant(partner_id, company, message.from_user.id)
    # Уведомления best-effort (не рушат создание): партнёру + владельцу.
    try:
        pchat = await db.get_partner_chat_id(partner_id)
        if pchat and pchat.strip():
            await db.enqueue_platform_notify(int(pchat), f"🎯 Новый тенант от тебя: {company}")
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).warning("partner ref-create notify failed", exc_info=True)
    try:
        ochat = await db.get_owner_chat_id()
        if ochat and ochat.strip():
            await db.enqueue_platform_notify(int(ochat), f"🆕 Новый клиент: {company} (от партнёра)")
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).warning("owner ref-create notify failed", exc_info=True)
    await messaging.reply_text(
        message, f"Готово! Заполните бриф по ссылке: {config.BOT_PUBLIC_BASE_URL}/brief/{token}",
        source="system")
```

Проверить импорты в handlers.py: `logging`, `config`, `messaging`, `db`, `Message`, `FSMContext`, `State/StatesGroup`, `router` — все уже есть (см. верх файла; `logging` — если нет, добавить `import logging`).

- [ ] **Step 4: py_compile — verify**

```bash
.venv-smoke/bin/python -m py_compile bot-telegram/bot.py bot-telegram/handlers.py
```
Expected: без ошибок.

- [ ] **Step 5: Коммит**

```bash
git add bot-telegram/bot.py bot-telegram/handlers.py
git commit -m "feat(partners): лендинг /p/{code} + ветка ?start=ref_ (FSM+создание+уведомления партнёру/владельцу)"
```

---

## Task 5: Уведомление партнёру при прохождении брифа (расширение submit_brief)

**Files:**
- Modify: `bot-telegram/db.py` (`submit_brief` — пост-коммитный блок)
- Test: `scripts/submit_brief_notify_isolation_smoke.py` (расширить: партнёрский тенант шлёт ДВА уведомления)

**Interfaces:**
- Consumes (Task 3): `db.get_partner_chat_id`. Уже есть: `get_owner_chat_id`, `enqueue_platform_notify`.

- [ ] **Step 1: Расширить `submit_brief` в `bot-telegram/db.py`**

В SELECT добавить `t.partner_id`; расширить кортеж `submitted` до `(brief_id, tenant_name, partner_id)`; в пост-коммитном блоке — доп. best-effort уведомление партнёру.

Заменить SELECT-строку:
```python
            row = await c.fetchrow(
                "select b.id, b.status, b.expires_at, t.name as tenant_name, t.partner_id "
                "from tenant_brief b join tenants t on t.id = b.tenant_id "
                "where b.token = $1 for update",
                token)
```
Заменить захват:
```python
            submitted = (str(row["id"]), row["tenant_name"], row["partner_id"])
```
Обновить инициализацию типа:
```python
    submitted: tuple[str, str, object] | None = None  # (brief_id, tenant_name, partner_id) для событий после коммита
```
В пост-коммитном блоке ПОСЛЕ существующего уведомления владельцу добавить:
```python
        try:
            partner_id = submitted[2]
            if partner_id is not None:
                pchat = await get_partner_chat_id(str(partner_id))
                if pchat and pchat.strip():
                    await enqueue_platform_notify(int(pchat), f"✅ {submitted[1]} прошёл бриф")
        except Exception:  # noqa: BLE001 — уведомление не должно рушить submit
            logging.getLogger(__name__).warning("brief submit partner notify failed", exc_info=True)
```

- [ ] **Step 2: Расширить регрессию `scripts/submit_brief_notify_isolation_smoke.py`**

Добавить секцию: создать партнёра + реф-тенанта (partner_id, partner tg_chat_id) с pending-брифом, задать owner_chat_id, вызвать `submit_brief`, проверить, что в `platform_notify` появились ДВА queued-уведомления (владельцу + партнёру), и что бриф `submitted` (инвариант цел). Пример проверки:
```python
        # ... после создания партнёрского тенанта с брифом (token TOKEN2) и owner_chat_id ...
        await db.submit_brief(TOKEN2, {"q1": "ответ"})
        async with db.pool.acquire() as c:
            n = await c.fetchval(
                "select count(*) from platform_notify where status='queued' and "
                "(chat_id=$1 or chat_id=$2) and created_at > now()-interval '1 minute'",
                12345, 700700)  # owner_chat_id, partner tg_chat_id
        check("оба уведомления (владелец+партнёр) поставлены", n >= 2, f"n={n}")
```
(Реализатор дополняет фикстуру/cleanup партнёром аналогично существующему стилю смоука; чистит partners/tenants/platform_notify по своим маркерам.)

- [ ] **Step 3: КОНТРОЛЛЕР гоняет регрессию — verify PASS**

Run: `PLATFORM_NOTIFY_SMOKE_DSN="$DEV_DSN" PYTHONPATH=bot-telegram:. .venv-smoke/bin/python scripts/submit_brief_notify_isolation_smoke.py`
Expected: `✅ submit_brief notify-isolation regression — OK` + новая проверка «оба уведомления» OK.

- [ ] **Step 4: py_compile + коммит**

```bash
.venv-smoke/bin/python -m py_compile bot-telegram/db.py scripts/submit_brief_notify_isolation_smoke.py
git add bot-telegram/db.py scripts/submit_brief_notify_isolation_smoke.py
git commit -m "feat(partners): уведомление партнёру при прохождении брифа (расширение submit_brief)"
```

---

## Финал (после всех задач — контроллер)

- [ ] Финал-ревью ветки (Workflow: 3 линзы — поток/152-ФЗ · корректность · интеграция/grants; диф от базовой точки) → фикс подтверждённых находок.
- [ ] Полный прогон смоуков на risuy_dev: `partners_smoke`, `partner_ref_bot_smoke`, `submit_brief_notify_isolation`, регрессии `platform_notify_smoke`/`create_tenant_brief_chain`.
- [ ] Деплой по «да»: прод-миграция `migrate_partners.sql` на `risuy` → push через BIZKON → редеплой обоих → сверить commit_sha+active.
- [ ] После деплоя (владелец, глазами): завести партнёра в `/partners` → открыть `/p/{code}` → пройти как клиент (компания) → проверить создание тенанта+атрибуцию (отчёт `/partners/{id}`) + уведомления партнёру/владельцу (событие создания и событие брифа).
