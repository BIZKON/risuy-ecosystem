# Новый тенант: ручная бриф-ссылка + уведомления владельцу — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Владелец из Бриф-Центра создаёт бриф-ссылку под новую компанию одним действием, и получает в Telegram уведомления о (1) новом тенанте и (2) прохождении брифа.

**Architecture:** Кусок A — чистая панель (новый роут `/brief-center/create-new` + блок формы, переиспускает `create_tenant_admin`+`create_tenant_brief`), отгружается ПЕРВЫМ, без DDL. Кусок B — слой уведомлений: настройка `owner_chat_id` (app_settings, поле в `/integrations`), echo chat_id в боте, новая не-лидовая очередь `platform_notify` (т.к. `outbox` lead-scoped), дренаж воркером бота через бот-уведомитель (фолбэк — разговорный бот).

**Tech Stack:** Python 3.11, FastAPI+Jinja2 (панель), aiogram/aiohttp (бот), asyncpg+Postgres, смоук-скрипты.

## Global Constraints

- **Русский язык везде** — UI-тексты, комментарии, docstrings, коммиты. Латиница — идентификаторы/ключи/SQL.
- **Уведомления НИКОГДА не ломают основной поток**: enqueue в try/except; no-op если `owner_chat_id` не задан; нет `NOTIFIER_BOT_TOKEN` → фолбэк на разговорный бот.
- **Бот и панель — РАЗНЫЕ процессы с РАЗНЫМИ `db.py`** (импорт невозможен): `enqueue_platform_notify` + `get_owner_chat_id` — зеркала в обоих модулях.
- **`platform_notify` — БЕЗ tenant-RLS** (платформенный артефакт, как `tenants`); грант `panel_rw` INSERT/SELECT/UPDATE; бот ходит owner-DSN (bypass), доп-грант не нужен.
- **`outbox` НЕ используем** для этих уведомлений — он lead-scoped (`lead_id uuid NOT NULL`).
- **Прод-DDL/деплой/push — по явному «да» владельца.** db-смоуки гонит КОНТРОЛЛЕР на risuy_dev (owner-DSN не в субагенты). Push — одноразово через аккаунт BIZKON.
- Коммит явными файлами (НЕ CLAUDE.md/.claude/.gitignore/.superpowers/graphify-out). Коммиттеры-субагенты последовательно.
- `.venv-smoke` без fastapi → app.py/роуты проверяются `py_compile`; шаблоны — jinja-parse (`jinja2` в .venv-smoke ЕСТЬ).

**Кластер/хост миграции:** `~/.claude/scripts/twc-migrate.sh 4171827 81.31.246.136 risuy_dev gen_user db/migrate_platform_notify.sql`.

---

### Task 1: Кусок A — ручная бриф-ссылка под новую компанию

Панельный роут + блок формы. Без DDL. **Отгружается первым** (разблокирует ждущего клиента).

**Files:**
- Modify: `admin-panel/app.py` (новый роут после `brief_center_create` ~app.py:6350)
- Modify: `admin-panel/templates/brief_center.html` (второй блок формы)
- Test: `scripts/create_tenant_brief_chain_smoke.py` (db, контроллер на risuy_dev)

**Interfaces:**
- Consumes: `db.create_tenant_admin(name, *, actor, ip, user_agent) -> (slug, tenant_id)` (db.py:2561); `db.create_tenant_brief(tenant_id, *, actor, ip, user_agent, ttl_days=30) -> (brief_id, token)` (db.py:5072); `_require_admin(session)`, `_enforce_csrf(request, session, submitted)`, `_ip(request)`, `_ua(request)` (app.py:5178/226/164/168).
- Produces: `POST /brief-center/create-new` роут.

- [ ] **Step 1: Написать db-смоук цепочки `scripts/create_tenant_brief_chain_smoke.py`**

```python
#!/usr/bin/env python3
"""DB-смоук (контроллер, risuy_dev): create_tenant_admin → create_tenant_brief
цепочкой создают active-тенанта + pending-бриф под его id.
  CHAIN_SMOKE_DSN="...risuy_dev..." PYTHONPATH=admin-panel:. \
    ./.venv-smoke/bin/python scripts/create_tenant_brief_chain_smoke.py
"""
import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "admin-panel"))
os.environ.setdefault("DATABASE_URL", "postgresql://x/y")
os.environ.setdefault("SESSION_SECRET", "smoke-secret-padding-0123456789abcdef")
os.environ.setdefault("ADMIN_USERNAME", "smoke")
os.environ.setdefault("ADMIN_PASSWORD_HASH", "$argon2id$v=19$m=65536,t=3,p=4$c21va2U$c21va2U")

import asyncpg  # noqa: E402
import db  # noqa: E402

DSN = os.environ.get("CHAIN_SMOKE_DSN")
if not DSN or "/risuy_dev" not in DSN.split("?")[0]:
    raise SystemExit("Задайте CHAIN_SMOKE_DSN на risuy_dev")

FAILS: list[str] = []


def check(name, cond, detail=""):
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


async def _cleanup(c):
    await c.execute("delete from tenant_brief where tenant_id in "
                    "(select id from tenants where name like 'СМОУК Цепочка%')")
    await c.execute("delete from tenants where name like 'СМОУК Цепочка%'")


async def main():
    db.pool = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    try:
        db.set_active_tenant(None)
        async with db.pool.acquire() as c:
            await _cleanup(c)
        print("1. create_tenant_admin + create_tenant_brief цепочкой:")
        slug, tid = await db.create_tenant_admin("СМОУК Цепочка ООО", actor="smoke", ip=None, user_agent=None)
        check("тенант создан (slug, id)", bool(slug) and bool(tid))
        brief_id, token = await db.create_tenant_brief(tid, actor="smoke", ip=None, user_agent=None)
        check("бриф создан (id, token)", bool(brief_id) and len(token) >= 16)
        async with db.pool.acquire() as c:
            st = await c.fetchval("select status from tenants where id=$1", tid)
            bst = await c.fetchval("select status from tenant_brief where id=$1", brief_id)
            bt = await c.fetchval("select tenant_id from tenant_brief where id=$1", brief_id)
        check("тенант active", st == "active", f"st={st}")
        check("бриф pending", bst == "pending", f"bst={bst}")
        check("бриф привязан к тенанту", str(bt) == str(tid))
    finally:
        async with db.pool.acquire() as c:
            await _cleanup(c)
        await db.pool.close()
    print()
    if FAILS:
        print("❌ ПРОВАЛЫ: " + ", ".join(FAILS)); sys.exit(1)
    print("✅ create_tenant_brief_chain smoke — OK")


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: Реализовать роут в `admin-panel/app.py`** (после `brief_center_create`, ~6350)

```python
@app.post("/brief-center/create-new")
async def brief_center_create_new(request: Request,
                                  session: auth.Session = Depends(require_session),
                                  name: str = Form(""), csrf_token: str = Form("")):
    _require_admin(session)
    await _enforce_csrf(request, session, csrf_token)
    company = name.strip()
    if not company:
        return RedirectResponse(url="/brief-center?err=no_name", status_code=303)
    _slug, tid = await db.create_tenant_admin(
        company, actor=session.actor, ip=_ip(request), user_agent=_ua(request))
    brief_id, _token = await db.create_tenant_brief(
        tid, actor=session.actor, ip=_ip(request), user_agent=_ua(request))
    return RedirectResponse(url=f"/brief-center/{brief_id}?saved=created", status_code=303)
```

- [ ] **Step 3: Второй блок формы в `admin-panel/templates/brief_center.html`** (после существующей секции «Создать ссылку на бриф», ~строка 68)

```html
<section class="section" aria-label="Новая компания">
  <h2 class="section__title section__title--lg">Новая компания</h2>
  <p class="hint hint--block">Клиента ещё нет в списке? Введите название компании — создадим тенанта и бриф-ссылку одним действием.</p>
  <form method="post" action="/brief-center/create-new" class="inline-form">
    <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
    <label class="field">
      <span class="field__label">Название компании</span>
      <input type="text" name="name" required class="field__input" placeholder="ООО «Ромашка»">
    </label>
    <button class="btn btn--dark" type="submit">Создать тенанта и бриф-ссылку</button>
  </form>
</section>
```
И флеш ошибки в шапке `brief_center.html` (рядом с прочими `{% if err ... %}`):
```html
{% if err == 'no_name' %}{{ flash('Введите название компании.', 'error') }}{% endif %}
```
(Проверить, есть ли в brief_center.html блок флешей `saved`/`err`; если нет — добавить `{% if err == 'no_name' %}...` и `{% if err == 'no_tenant' %}...` минимально.)

- [ ] **Step 4: py_compile + jinja-parse**

Run: `cd ~/Downloads/risuy-ecosystem && ./.venv-smoke/bin/python -m py_compile admin-panel/app.py && ./.venv-smoke/bin/python -c "import jinja2,io; jinja2.Environment().parse(io.open('admin-panel/templates/brief_center.html',encoding='utf-8').read()); print('jinja OK')"`
Expected: `jinja OK` (и без ошибок компиляции).

- [ ] **Step 5: КОНТРОЛЛЕР гонит db-смоук цепочки на risuy_dev**

Run: `CHAIN_SMOKE_DSN="<owner-dsn risuy_dev>" PYTHONPATH=admin-panel:. ./.venv-smoke/bin/python scripts/create_tenant_brief_chain_smoke.py`
Expected: `✅ create_tenant_brief_chain smoke — OK`. (Регистрация роута — на деплое.)

- [ ] **Step 6: Коммит**

```bash
git add admin-panel/app.py admin-panel/templates/brief_center.html scripts/create_tenant_brief_chain_smoke.py
git commit -m "feat(brief): создать бриф-ссылку под новую компанию одним действием (Бриф-Центр)"
```

---

### Task 2: Слой данных `platform_notify` (очередь + owner_chat_id)

Не-лидовая очередь уведомлений + функции доступа (панель и бот). DDL gated.

**Files:**
- Create: `db/migrate_platform_notify.sql`
- Modify: `admin-panel/config.py` (константа ключа), `admin-panel/db.py` (функции), `bot-telegram/db.py` (зеркала)
- Create: `scripts/platform_notify_smoke.py` (db, контроллер)

**Interfaces:**
- Produces (панель `admin-panel/db.py`):
  - `async def get_owner_chat_id() -> str | None`
  - `async def set_owner_chat_id_with_audit(chat_id: str | None, *, actor, ip, user_agent) -> None`
  - `async def enqueue_platform_notify(chat_id: int, text: str) -> int` (INSERT, возвращает id)
- Produces (бот `bot-telegram/db.py`):
  - `async def get_owner_chat_id() -> str | None` (зеркало)
  - `async def enqueue_platform_notify(chat_id: int, text: str) -> int` (зеркало)
  - `async def claim_platform_notify(limit: int) -> list[dict]` (atomic SKIP LOCKED, status→sending)
  - `async def mark_platform_notify_sent(id: int) -> None`; `async def mark_platform_notify_failed(id: int, err: str) -> None`
- Config: `admin-panel/config.py`: `OWNER_CHAT_ID_SETTING_KEY = "owner_chat_id"`

- [ ] **Step 1: Миграция `db/migrate_platform_notify.sql`**

```sql
-- platform_notify: очередь НЕ-лидовых уведомлений (владелец платформы; в Спеке 2 — партнёры).
-- outbox lead-scoped (lead_id NOT NULL) → отдельная таблица. БЕЗ RLS (платформенный артефакт).
-- Применение: twc-migrate.sh 4171827 81.31.246.136 risuy_dev gen_user db/migrate_platform_notify.sql
create table if not exists platform_notify (
    id         bigserial primary key,
    chat_id    bigint not null,
    text       text   not null,
    status     text   not null default 'queued',
    attempts   int    not null default 0,
    last_error text,
    created_at timestamptz not null default now(),
    sent_at    timestamptz,
    constraint platform_notify_status_chk check (status in ('queued','sending','sent','failed'))
);
create index if not exists platform_notify_queued_idx on platform_notify (created_at) where status='queued';

do $$ begin
    if exists (select 1 from pg_roles where rolname='panel_rw') then
        grant select, insert, update on platform_notify to panel_rw;
        grant usage, select on sequence platform_notify_id_seq to panel_rw;
    end if;
end $$;
```

- [ ] **Step 2: Написать db-смоук `scripts/platform_notify_smoke.py`**

```python
#!/usr/bin/env python3
"""DB-смоук platform_notify (контроллер, risuy_dev): enqueue при заданном/пустом
owner_chat_id, claim→sending (SKIP LOCKED), mark sent/failed.
  PLATFORM_NOTIFY_SMOKE_DSN="...risuy_dev..." PYTHONPATH=admin-panel:. \
    ./.venv-smoke/bin/python scripts/platform_notify_smoke.py
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

DSN = os.environ.get("PLATFORM_NOTIFY_SMOKE_DSN")
if not DSN or "/risuy_dev" not in DSN.split("?")[0]:
    raise SystemExit("Задайте PLATFORM_NOTIFY_SMOKE_DSN на risuy_dev")

FAILS: list[str] = []
CHAT = 77_000_555


def check(name, cond, detail=""):
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


async def _cleanup(c):
    await c.execute("delete from platform_notify where chat_id=$1", CHAT)
    await c.execute("delete from app_settings where key='owner_chat_id' and value=''")


async def main():
    db.pool = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    try:
        async with db.pool.acquire() as c:
            await _cleanup(c)
        print("1. enqueue_platform_notify:")
        nid = await db.enqueue_platform_notify(CHAT, "тест-уведомление")
        check("enqueue вернул id", nid > 0)
        async with db.pool.acquire() as c:
            st = await c.fetchval("select status from platform_notify where id=$1", nid)
        check("статус queued", st == "queued", f"st={st}")

        print("2. claim (queued→sending, SKIP LOCKED):")
        items = await db.claim_platform_notify(10)
        check("claim вернул нашу строку", any(i["id"] == nid for i in items))
        async with db.pool.acquire() as c:
            st2 = await c.fetchval("select status from platform_notify where id=$1", nid)
        check("статус sending", st2 == "sending", f"st={st2}")
        check("повторный claim пуст (уже sending)", not any(i["id"] == nid for i in await db.claim_platform_notify(10)))

        print("3. mark sent/failed:")
        await db.mark_platform_notify_sent(nid)
        async with db.pool.acquire() as c:
            st3 = await c.fetchval("select status from platform_notify where id=$1", nid)
        check("статус sent", st3 == "sent", f"st={st3}")
        nid2 = await db.enqueue_platform_notify(CHAT, "второе")
        await db.claim_platform_notify(10)
        await db.mark_platform_notify_failed(nid2, "boom")
        async with db.pool.acquire() as c:
            st4, err = await c.fetchrow("select status, last_error from platform_notify where id=$1", nid2)
        check("статус failed + last_error", st4 == "failed" and err == "boom", f"st={st4}")

        print("4. owner_chat_id set/get:")
        await db.set_owner_chat_id_with_audit(str(CHAT), actor="smoke", ip=None, user_agent=None)
        check("get_owner_chat_id вернул заданное", await db.get_owner_chat_id() == str(CHAT))
        await db.set_owner_chat_id_with_audit(None, actor="smoke", ip=None, user_agent=None)
        check("пустой owner_chat_id → '' или None", (await db.get_owner_chat_id() or "") == "")
    finally:
        async with db.pool.acquire() as c:
            await _cleanup(c)
        await db.pool.close()
    print()
    if FAILS:
        print("❌ ПРОВАЛЫ: " + ", ".join(FAILS)); sys.exit(1)
    print("✅ platform_notify smoke — OK")


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 3: Константа в `admin-panel/config.py`** (рядом с `GUIDE_URL_SETTING_KEY`, L706)

```python
OWNER_CHAT_ID_SETTING_KEY = "owner_chat_id"
```

- [ ] **Step 4: Функции в `admin-panel/db.py`** (в конец файла; `pool`, `_insert_audit`, `config` уже есть)

```python
# ── Уведомления платформы (owner_chat_id + очередь platform_notify) ────────────
async def get_owner_chat_id() -> str | None:
    async with pool.acquire() as c:
        return await c.fetchval("select value from app_settings where key=$1",
                                config.OWNER_CHAT_ID_SETTING_KEY)


async def set_owner_chat_id_with_audit(chat_id: str | None, *, actor: str,
                                       ip: str | None, user_agent: str | None) -> None:
    async with pool.acquire() as c:
        async with c.transaction():
            await c.execute(
                "insert into app_settings (key, value) values ($1, $2) "
                "on conflict (key) do update set value = excluded.value",
                config.OWNER_CHAT_ID_SETTING_KEY, chat_id or "")
            await _insert_audit(c, actor=actor, action="owner_chat_id_set", ip=ip,
                                user_agent=user_agent, detail={"set": bool(chat_id)})


async def enqueue_platform_notify(chat_id: int, text: str) -> int:
    async with pool.acquire() as c:
        return await c.fetchval(
            "insert into platform_notify (chat_id, text) values ($1, $2) returning id",
            int(chat_id), text)
```

- [ ] **Step 5: Зеркала + claim/mark в `bot-telegram/db.py`** (рядом с `claim_outbox`, db.py:573)

```python
async def get_owner_chat_id() -> str | None:
    async with pool.acquire() as c:
        return await c.fetchval("select value from app_settings where key=$1", "owner_chat_id")


async def enqueue_platform_notify(chat_id: int, text: str) -> int:
    async with pool.acquire() as c:
        return await c.fetchval(
            "insert into platform_notify (chat_id, text) values ($1, $2) returning id",
            int(chat_id), text)


async def claim_platform_notify(limit: int) -> list[dict]:
    async with pool.acquire() as c:
        rows = await c.fetch(
            "update platform_notify set status='sending', attempts=attempts+1 "
            "where id in (select id from platform_notify where status='queued' "
            "order by id limit $1 for update skip locked) "
            "returning id, chat_id, text, attempts",
            limit)
    return [dict(r) for r in rows]


async def mark_platform_notify_sent(id: int) -> None:
    async with pool.acquire() as c:
        await c.execute("update platform_notify set status='sent', sent_at=now() where id=$1", id)


async def mark_platform_notify_failed(id: int, err: str) -> None:
    async with pool.acquire() as c:
        await c.execute("update platform_notify set status='failed', last_error=$2 where id=$1",
                        id, (err or "")[:500])
```

- [ ] **Step 6: КОНТРОЛЛЕР применяет миграцию + гонит смоук на risuy_dev**

```bash
~/.claude/scripts/twc-migrate.sh 4171827 81.31.246.136 risuy_dev gen_user db/migrate_platform_notify.sql
PLATFORM_NOTIFY_SMOKE_DSN="<owner-dsn risuy_dev>" PYTHONPATH=admin-panel:. ./.venv-smoke/bin/python scripts/platform_notify_smoke.py
```
Expected: миграция OK; `✅ platform_notify smoke — OK`.

- [ ] **Step 7: py_compile ботовых функций**

Run: `cd ~/Downloads/risuy-ecosystem && ./.venv-smoke/bin/python -m py_compile bot-telegram/db.py admin-panel/db.py && echo OK`

- [ ] **Step 8: Коммит**

```bash
git add db/migrate_platform_notify.sql admin-panel/config.py admin-panel/db.py bot-telegram/db.py scripts/platform_notify_smoke.py
git commit -m "feat(notify): очередь platform_notify + owner_chat_id (панель/бот) + db-смоук"
```

---

### Task 3: Настройка owner_chat_id (панель /integrations) + echo chat_id в боте

**Files:**
- Modify: `admin-panel/app.py` (роут `/integrations/owner-chat-id` + контекст `integrations_page`)
- Modify: `admin-panel/templates/integrations.html` (поле)
- Modify: `bot-telegram/handlers.py` (echo в `cmd_start`, ~295)

**Interfaces:**
- Consumes: `db.get_owner_chat_id`, `db.set_owner_chat_id_with_audit` (Task 2); `_require_admin`, `_enforce_csrf`, `_ip`, `_ua`; `integrations_page` (app.py:4794); `cmd_start` (handlers.py:295); `messaging.reply_text` (проверить сигнатуру — как отвечают текстом в handlers).

- [ ] **Step 1: Роут в `admin-panel/app.py`** (рядом с `integrations_set_guide_url`, ~4833)

```python
@app.post("/integrations/owner-chat-id")
async def integrations_set_owner_chat_id(request: Request,
                                         session: auth.Session = Depends(require_session),
                                         owner_chat_id: str = Form(""), csrf_token: str = Form("")):
    _require_admin(session)
    await _enforce_csrf(request, session, csrf_token)
    val = owner_chat_id.strip() or None
    await db.set_owner_chat_id_with_audit(val, actor=session.actor,
                                          ip=_ip(request), user_agent=_ua(request))
    return RedirectResponse(url="/integrations?saved=owner_chat", status_code=303)
```
И в `integrations_page` (app.py:4794) добавить в контекст: `"owner_chat_id": await db.get_owner_chat_id(),`.

- [ ] **Step 2: Поле в `admin-panel/templates/integrations.html`** (отдельная секция-форма; сверить классы с соседними формами интеграций)

```html
<section class="section" aria-label="Уведомления владельца">
  <h2 class="section__title">Уведомления владельца в Telegram</h2>
  <p class="hint hint--block">Напишите боту-уведомителю в личку — он пришлёт ваш chat_id. Вставьте его сюда, чтобы получать уведомления о новых клиентах и прохождении брифов.</p>
  <form method="post" action="/integrations/owner-chat-id" class="inline-form">
    <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
    <label class="field">
      <span class="field__label">Chat ID владельца</span>
      <input type="text" name="owner_chat_id" value="{{ owner_chat_id or '' }}" class="field__input" placeholder="напр. 123456789">
    </label>
    <button class="btn btn--dark" type="submit">Сохранить</button>
  </form>
</section>
```
И флеш (рядом с прочими в integrations.html): `{% if saved == 'owner_chat' %}{{ flash('Chat ID сохранён.', 'ok') }}{% endif %}`.

- [ ] **Step 3: Echo chat_id в `bot-telegram/handlers.py` `cmd_start`** (~295, ПОСЛЕ проверки паузы, ДО парсинга deep-link)

Сначала graphify/grep: как в handlers отвечают текстом (`messaging.reply_text(message, text, ...)` или `message.answer(text)`) — использовать ТОТ ЖЕ способ. Затем добавить: если у `/start` НЕТ deep-link-аргумента (обычный `/start` без `?start=...`), ответить строкой с chat_id — чтобы владелец/партнёр узнали свой id.

```python
# после: if await db.is_bot_paused(...): return
args = (command.args or "").strip()
if not args:
    await message.answer(f"Ваш chat_id: {message.from_user.id}\n"
                         "Вставьте его в панели, чтобы получать уведомления.")
    return
# ...дальше существующий парсинг deep-link (club/intro/…)
```
(Точную форму `command.args`/ответа сверить с текущим кодом `cmd_start` — не сломать существующие ветки club/intro.)

- [ ] **Step 4: py_compile + jinja-parse**

Run: `cd ~/Downloads/risuy-ecosystem && ./.venv-smoke/bin/python -m py_compile admin-panel/app.py bot-telegram/handlers.py && ./.venv-smoke/bin/python -c "import jinja2,io; jinja2.Environment().parse(io.open('admin-panel/templates/integrations.html',encoding='utf-8').read()); print('jinja OK')"`
Expected: `jinja OK`. (Живой echo/поле — на деплое.)

- [ ] **Step 5: Коммит**

```bash
git add admin-panel/app.py admin-panel/templates/integrations.html bot-telegram/handlers.py
git commit -m "feat(notify): поле owner_chat_id в /integrations + echo chat_id в боте на /start"
```

---

### Task 4: Триггеры уведомлений + дренаж воркером

Событие 1 (панель, тенант создан), событие 2 (бот, бриф пройден), дренаж `platform_notify` воркером бота.

**Files:**
- Modify: `admin-panel/app.py` (обёртка `notify_owner_new_tenant` + вызовы в 3 роутах)
- Modify: `bot-telegram/db.py` (`submit_brief` — постановка события 2), `bot-telegram/worker.py` (`_drain_platform_notify` + вызов в цикле)
- Test: расширить `scripts/platform_notify_smoke.py` (Task 2) кейсом дренажа-имитации; py_compile

**Interfaces:**
- Consumes: `db.get_owner_chat_id`, `db.enqueue_platform_notify`, `db.claim_platform_notify`, `db.mark_platform_notify_sent/failed` (Task 2); `notifier.get_notifier_bot()` (notifier.py:23); `messaging.raw_send_text(bot, chat_id, text, ...)` (messaging.py:418); `worker.run` цикл (worker.py:52); `config.WORKER_INTERVAL`, `config.PANEL_BASE_URL`.

- [ ] **Step 1: Обёртка + вызовы события 1 в `admin-panel/app.py`**

Хелпер (рядом с прочими _-хелперами):
```python
async def notify_owner_new_tenant(name: str) -> None:
    """Событие 1: уведомить владельца о новом тенанте. Никогда не бросает."""
    try:
        chat = await db.get_owner_chat_id()
        if chat and chat.strip():
            await db.enqueue_platform_notify(int(chat), f"🆕 Новый клиент: {name}")
    except Exception:  # noqa: BLE001
        logger.warning("notify_owner_new_tenant failed", exc_info=True)
```
Вызовы (после успешного создания тенанта, в try/except уже внутри хелпера):
- в `brief_center_create_new` (Task 1) после `create_tenant_admin`: `await notify_owner_new_tenant(company)`;
- в `/tenants/create` (`tenants_create`, ~app.py:6311) после `create_tenant_admin`: `await notify_owner_new_tenant(name)`;
- в `/signup/register` (`signup_register`, ~app.py:617) после успешного `create_client_account`: `await notify_owner_new_tenant(email)` (имя тенанта = email при self-signup).

- [ ] **Step 2: Событие 2 в `bot-telegram/db.py` `submit_brief`** (db.py:1764)

Расширить внутренний SELECT токена join'ом на tenants (чтобы получить имя), и после `update ... status='submitted'` — поставить уведомление:
```python
# было: select id, status, expires_at from tenant_brief where token=$1 for update
row = await c.fetchrow(
    "select b.id, b.status, b.expires_at, t.name as tenant_name "
    "from tenant_brief b join tenants t on t.id=b.tenant_id where b.token=$1 for update", token)
# ...после update tenant_brief ... status='submitted' ...:
try:
    chat = await c.fetchval("select value from app_settings where key='owner_chat_id'")
    if chat and chat.strip():
        link = f"{config.PANEL_BASE_URL}/brief-center/{row['id']}" if config.PANEL_BASE_URL else ""
        txt = f"✅ {row['tenant_name']} прошёл бриф — пора собирать черновик. {link}".strip()
        await c.execute("insert into platform_notify (chat_id, text) values ($1,$2)", int(chat), txt)
except Exception:
    logger.warning("brief submit notify failed", exc_info=True)
```
(Постановка в той же транзакции, что и update — безопасно; сбой в try/except не рушит submit.)

- [ ] **Step 3: Дренаж в `bot-telegram/worker.py`**

Новая функция (рядом с `_drain_outbox`, worker.py:141):
```python
async def _drain_platform_notify(bot) -> None:
    """Дренаж очереди уведомлений владельцу/партнёрам. Бот-уведомитель или фолбэк — разговорный бот."""
    import notifier
    items = await db.claim_platform_notify(config.OUTBOX_BATCH)
    if not items:
        return
    nbot = notifier.get_notifier_bot() or bot
    for it in items:
        try:
            await messaging.raw_send_text(nbot, it["chat_id"], it["text"])
            await db.mark_platform_notify_sent(it["id"])
        except Exception as e:  # noqa: BLE001
            await db.mark_platform_notify_failed(it["id"], str(e))
```
И вызов в главном цикле `worker.run` (worker.py:52, рядом с `await _drain_outbox(bot)`):
```python
        await _drain_platform_notify(bot)
```

- [ ] **Step 4: py_compile + jinja (панель/бот)**

Run: `cd ~/Downloads/risuy-ecosystem && ./.venv-smoke/bin/python -m py_compile admin-panel/app.py bot-telegram/db.py bot-telegram/worker.py && echo OK`
Expected: `OK`. (Живой дренаж/отправка — на деплое; db-часть покрыта смоуком Task 2.)

- [ ] **Step 5: КОНТРОЛЛЕР перегоняет `platform_notify_smoke` на risuy_dev** (регресс — claim/mark не сломаны)

Run: `PLATFORM_NOTIFY_SMOKE_DSN="<owner-dsn risuy_dev>" PYTHONPATH=admin-panel:. ./.venv-smoke/bin/python scripts/platform_notify_smoke.py`
Expected: `✅ platform_notify smoke — OK`.

- [ ] **Step 6: Коммит**

```bash
git add admin-panel/app.py bot-telegram/db.py bot-telegram/worker.py
git commit -m "feat(notify): триггеры (тенант создан/бриф пройден) + дренаж platform_notify воркером"
```

---

## Финальные шаги (после всех задач)
- Регрессия ключевых смоуков зелёная; py_compile всех тронутых модулей.
- Адверсариальное финал-ревью ветки (Workflow, лензы: корректность / уведомления-не-ломают-поток / grants-RLS).
- Деплой (по «да»): прод-миграция `platform_notify` на risuy → `git push origin docs/security-audit:main` (одноразово через BIZKON) → редеплой обоих; commit_sha+active по twc.
- Глазами владельца: `/brief-center` «Новая компания» → создать; `/integrations` вставить chat_id; написать боту `/start` → получить chat_id; пройти бриф тест-клиентом → получить оба уведомления.

---

## Self-Review (проведён при написании плана)

**1. Покрытие спеки:** §4 A — Task 1; §5.1 настройка chat_id — Task 3; §5.2 platform_notify — Task 2; §5.3 enqueue панель — Task 2/4; §5.4 событие 2 бот — Task 4; §5.5 дренаж — Task 4; §6 ошибки (try/except, no-op) — Task 4 хелпер + submit try/except; §7 тесты — смоуки в Task 1/2. ✅
**2. Плейсхолдеры:** реальный код во всех шагах. Точки «сверить сигнатуру `messaging.reply_text`/`command.args`» (Task 3 Step 3) — явные grep-указания, не заглушки. ✅
**3. Консистентность типов:** `enqueue_platform_notify(chat_id:int, text)→int`, `claim_platform_notify(limit)→list[dict]`, `get_owner_chat_id()→str|None`, `notify_owner_new_tenant(name)` — совпадают между Task 2 (определение) и Task 4 (использование). Зеркала бот/панель идентичны. ✅
**Известные зависимости:** прод `NOTIFIER_BOT_TOKEN` — если не задан, фолбэк на разговорный бот (рабочий); `PANEL_BASE_URL` — если пусто, ссылка в уведомлении опускается.
