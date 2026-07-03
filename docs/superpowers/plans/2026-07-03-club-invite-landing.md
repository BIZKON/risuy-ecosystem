# Публичное приглашение в клуб — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Публичный минимальный лендинг клуба `GET /club/{slug}` (бот) с кнопкой «Вступить» → deep-link `?start=club`, плюс карточка-ссылка для оператора в панельном `/club`.

**Architecture:** Лендинг живёт на публичном aiohttp-сервере бота рядом с `_legal_page`, переиспользует `get_legal_doc_data` (гейт реквизитов + operator_name). Bot username бот кэширует при старте из `get_me()`. Панель показывает URL лендинга + deep-link, беря базу и username из `get_runtime_status()`.

**Tech Stack:** Python, aiohttp (бот), FastAPI/Jinja2 (панель), asyncpg, Postgres. Тесты — smoke-скрипты проекта (`scripts/*_smoke.py`, `.venv-smoke`), НЕ pytest.

## Global Constraints

- 🇷🇺 Только русский: код-комментарии, UI, коммиты.
- Юр-инвариант: лендинг — публичная inbound-страница; вступление = существующий флоу согласия; холодного контакта/парсинга нет. Лендинг обязан давать ссылку на Политику (когда база задана).
- Tenant-scope: `get_legal_doc_data` уже фильтрует по слагу; новых запросов к чужим тенантам не вводим.
- **Без прод-DDL, без новых настроек** (`tenant_settings` не расширяем).
- Коммит только явными файлами (НЕ `CLAUDE.md`/`.claude/`/`.gitignore`/`.superpowers`/`graphify-out`).
- Смоуки на risuy_dev гонит контроллер inline (owner-DSN, `TEAM_DSN`), не субагенты. `.venv-smoke/bin/python`.
- Гейт готовности лендинга = реквизиты оператора (`operator_name`+`operator_inn`+`operator_email`) заполнены И есть bot_username; иначе 404 (лендинг) / подсказка (панель).

---

### Task 1: HTML-билдер лендинга `_club_landing_html` + unit-смоук

**Files:**
- Modify: `bot-telegram/bot.py` (добавить функцию рядом с `_legal_html`/`_legal_page`, ~L348)
- Create: `scripts/club_landing_smoke.py`

**Interfaces:**
- Produces: `_club_landing_html(operator_name: str, deeplink: str, policy_url: str) -> str` — самодостаточный HTML (без внешних ресурсов), с кнопкой-ссылкой на `deeplink`, экранированным `operator_name`, ссылкой на `policy_url` (если непустой).

- [ ] **Step 1: Написать unit-смоук (падающий)** — `scripts/club_landing_smoke.py`:

```python
#!/usr/bin/env python3
"""Unit-смоук лендинга клуба: _club_landing_html (bot.py) — без сети/БД.
Запуск: PYTHONPATH=bot-telegram:. ./.venv-smoke/bin/python scripts/club_landing_smoke.py"""
import os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, "bot-telegram"))
os.environ.setdefault("BOT_TOKEN", "0:smoke")
os.environ.setdefault("DATABASE_URL", "postgresql://x/y")
os.environ.setdefault("CHANNEL_ID", "-1001234567890")
os.environ.setdefault("CHANNEL_URL", "https://t.me/smoke")
os.environ.setdefault("GUIDE_URL", "https://example.org/guide")
import bot  # noqa: E402  (bot-telegram/bot.py)

FAILS = []
def check(name, cond, detail=""):
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not cond: FAILS.append(name)

def main():
    html = bot._club_landing_html("ООО <Ромашка>", "https://t.me/mybot?start=club",
                                  "https://x.ru/legal/romashka/privacy")
    check("есть кнопка «Вступить»", "Вступить в клуб" in html)
    check("есть deep-link", "https://t.me/mybot?start=club" in html)
    check("есть ссылка на Политику", "/legal/romashka/privacy" in html)
    check("operator_name экранирован (нет сырого <)", "<Ромашка>" not in html and "&lt;Ромашка&gt;" in html)
    check("название клуба в заголовке", "Клуб предпринимателей" in html)
    html2 = bot._club_landing_html("ООО Роза", "https://t.me/mybot?start=club", "")
    check("без policy_url — Политику не рендерим, но лендинг цел", "Политика" not in html2 and "Вступить в клуб" in html2)
    print()
    if FAILS: print(f"❌ ПРОВАЛЫ ({len(FAILS)}): " + ", ".join(FAILS)); sys.exit(1)
    print("✅ club_landing_smoke — все проверки зелёные")

if __name__ == "__main__": main()
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `cd ~/Downloads/risuy-ecosystem && PYTHONPATH=bot-telegram:. ./.venv-smoke/bin/python scripts/club_landing_smoke.py`
Expected: FAIL — `AttributeError: module 'bot' has no attribute '_club_landing_html'`.

- [ ] **Step 3: Реализовать `_club_landing_html`** в `bot-telegram/bot.py` (рядом с `_legal_html`):

```python
def _club_landing_html(operator_name: str, deeplink: str, policy_url: str) -> str:
    """Минимальный самодостаточный HTML-лендинг клуба (без внешних ресурсов). Публичный,
    inbound: посетитель приходит сам и жмёт «Вступить» → бот-воронка клуба (согласие 152-ФЗ)."""
    import html as _html
    name = _html.escape(operator_name or "")
    if policy_url:
        policy = (f'<p class="muted">Вступая, вы даёте согласие на обработку данных вашего '
                  f'бизнеса. <a href="{_html.escape(policy_url)}">Политика конфиденциальности</a>.</p>')
    else:
        policy = '<p class="muted">Вступая, вы даёте согласие на обработку данных вашего бизнеса.</p>'
    return (
        '<!doctype html><html lang="ru"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>Клуб предпринимателей — {name}</title>'
        '<style>body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:640px;'
        'margin:0 auto;padding:32px 20px;color:#1F2937;line-height:1.55}'
        '.btn{display:inline-block;background:#E63946;color:#fff;padding:14px 28px;'
        'border-radius:12px;text-decoration:none;font-weight:600;margin:20px 0}'
        '.muted{color:#6b7280;font-size:14px}h1{font-size:24px}</style></head><body>'
        f'<h1>Клуб предпринимателей — {name}</h1>'
        '<p>Сообщество предпринимателей для поиска комплементарных партнёров. Система сама '
        'подбирает, кто может быть вам полезен, а знакомство происходит только по взаимному '
        'согласию обеих сторон.</p>'
        '<p>Вступление бесплатное. Ваши контакты не раскрываются, пока вы сами не согласитесь '
        'на знакомство.</p>'
        f'<a class="btn" href="{_html.escape(deeplink)}">Вступить в клуб</a>'
        f'{policy}</body></html>'
    )
```

- [ ] **Step 4: Запустить — зелёный**

Run: `cd ~/Downloads/risuy-ecosystem && PYTHONPATH=bot-telegram:. ./.venv-smoke/bin/python scripts/club_landing_smoke.py`
Expected: PASS — `✅ club_landing_smoke — все проверки зелёные`.

- [ ] **Step 5: Коммит**

```bash
cd ~/Downloads/risuy-ecosystem && git add bot-telegram/bot.py scripts/club_landing_smoke.py && \
git commit -m "feat(club): HTML-билдер публичного лендинга клуба + unit-смоук"
```

---

### Task 2: Роут лендинга `_club_landing` + `/club/{slug}` + кэш bot_username

**Files:**
- Modify: `bot-telegram/bot.py` — модульная переменная `_BOT_USERNAME`; заполнение при старте (там, где вызывается `publish_runtime_status`, ~L437, где уже есть `me` из `get_me()`); хендлер `_club_landing`; регистрация роута рядом с `/legal/{slug}/{doc_type}` (~L390).

**Interfaces:**
- Consumes: `_club_landing_html` (Task 1); `db.get_legal_doc_data(slug) -> dict|None` (существует, bot db.py:1714 — None если тенант не найден/реквизиты не заполнены; иначе kv с `operator_name`); `config.BOT_PUBLIC_BASE_URL` (bot config.py:298).
- Produces: публичный `GET /club/{slug}` → 200 HTML (готовый клуб) / 404 (не найден/не настроен/нет bot_username).

- [ ] **Step 1: Добавить модульную переменную** в `bot-telegram/bot.py` (рядом с другими module-level, вверху после импортов):

```python
_BOT_USERNAME = ""  # username бота (из get_me при старте) — для deep-link лендинга клуба
```

- [ ] **Step 2: Заполнить `_BOT_USERNAME` при старте.** Найти в `bot-telegram/bot.py` место (~L430-440), где перед `db.publish_runtime_status(...)` уже получен `me = await bot.get_me()` (username публикуется в runtime_status). Сразу после получения `me` добавить:

```python
            global _BOT_USERNAME
            _BOT_USERNAME = (me.username or "").strip()
```

(Если `me` там не именован — взять username из того же значения, что идёт в `publish_runtime_status(bot_username=...)`.)

- [ ] **Step 3: Реализовать хендлер `_club_landing`** в `bot-telegram/bot.py` (рядом с `_legal_page`):

```python
async def _club_landing(request: web.Request) -> web.StreamResponse:
    """Публичная страница-приглашение в клуб тенанта: GET /club/{slug}, без авторизации.
    404, если тенанта нет, реквизиты оператора не заполнены (клуб не может принять вступление),
    или бот ещё не знает свой username (deep-link не построить)."""
    slug = request.match_info.get("slug", "")
    try:
        kv = await db.get_legal_doc_data(slug)
    except Exception:  # noqa: BLE001
        logger.warning("club-landing: чтение реквизитов упало slug=%s", slug, exc_info=True)
        kv = None
    if kv is None or not _BOT_USERNAME:
        return web.Response(status=404, text="Клуб не найден или не настроен")
    deeplink = f"https://t.me/{_BOT_USERNAME}?start=club"
    base = config.BOT_PUBLIC_BASE_URL
    policy_url = f"{base}/legal/{slug}/privacy" if base else ""
    resp = web.Response(
        text=_club_landing_html(kv["operator_name"], deeplink, policy_url),
        content_type="text/html", charset="utf-8",
    )
    resp.headers["Cache-Control"] = "public, max-age=600"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp
```

- [ ] **Step 4: Зарегистрировать роут.** Рядом с `app.router.add_get("/legal/{slug}/{doc_type}", _legal_page)` (~L390) добавить:

```python
    app.router.add_get("/club/{slug}", _club_landing)
```

- [ ] **Step 5: py_compile + db-смоук гейта.** Прогнать (контроллер, TEAM_DSN risuy_dev):

Расширить `scripts/club_landing_smoke.py` секцией db (или отдельный db-прогон): проверить `db.get_legal_doc_data` (bot) отдаёт None для несуществующего слага и для тенанта без реквизитов; kv с `operator_name` для настроенного. Пример добавления в smoke (после unit-части, только если задан `TEAM_DSN` на risuy_dev):

```python
    dsn = os.environ.get("TEAM_DSN", "")
    if "/risuy_dev" in dsn.split("?")[0]:
        import asyncio, asyncpg
        async def _db():
            db.pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
            try:
                none1 = await db.get_legal_doc_data("no-such-slug-zzz")
                check("get_legal_doc_data(неизвестный слаг) → None", none1 is None)
            finally:
                await db.pool.close()
        asyncio.run(_db())
```

Run (unit): `cd ~/Downloads/risuy-ecosystem && PYTHONPATH=bot-telegram:. ./.venv-smoke/bin/python scripts/club_landing_smoke.py`
Run (db, контроллер): та же команда с `TEAM_DSN="<risuy_dev DSN>"` в окружении.
Expected: PASS. py_compile: `python3 -m py_compile bot-telegram/bot.py` → без ошибок.

- [ ] **Step 6: Коммит**

```bash
cd ~/Downloads/risuy-ecosystem && git add bot-telegram/bot.py scripts/club_landing_smoke.py && \
git commit -m "feat(club): публичный роут /club/{slug} + кэш bot_username для deep-link"
```

- [ ] **Step 7: LIVE-заметка (Task 8-стиль, после деплоя):** открыть `{BOT_PUBLIC_BASE_URL}/club/<slug настроенного тенанта>` → 200 с кнопкой; `/club/no-such` → 404; клик «Вступить» → бот показывает согласие club_join. (Роут — live-only, полноценно верифицируется на проде.)

---

### Task 3: Панельная карточка-приглашение в `/club`

**Files:**
- Create (helper): `admin-panel/db.py` — `get_tenant_slug(tenant_id) -> str | None`
- Modify: `admin-panel/app.py` — в обработчике `GET /club` (~L4227) собрать `invite`-словарь и передать в шаблон
- Modify: `admin-panel/templates/club.html` — блок карточки «Ссылка-приглашение в клуб»

**Interfaces:**
- Consumes: `db.get_runtime_status() -> dict` (admin-panel/db.py:2284 — ключи `bot_username`, `bot_public_base_url`); реквизиты активного тенанта (для гейта готовности — как уже проверяется в `/club`/лид-магните).
- Produces: `db.get_tenant_slug(tenant_id) -> str|None`; в шаблон приходит `invite = {"ready": bool, "landing_url": str, "deeplink": str}`.

- [ ] **Step 1: db-смоук хелпера (падающий).** Добавить в существующий `scripts/club_db_smoke.py` (панель-side) проверку `get_tenant_slug`:

```python
    # get_tenant_slug возвращает слаг созданного тенанта и None для мусора
    slug = await db.get_tenant_slug(ta)   # ta — id временного тенанта смоука
    check("get_tenant_slug вернул слаг тенанта", isinstance(slug, str) and len(slug) > 0, repr(slug))
    check("get_tenant_slug(случайный uuid) → None",
          await db.get_tenant_slug("00000000-0000-0000-0000-000000000000") is None)
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run (контроллер, TEAM_DSN risuy_dev): `PYTHONPATH=admin-panel:. ./.venv-smoke/bin/python scripts/club_db_smoke.py`
Expected: FAIL — `AttributeError: module 'db' has no attribute 'get_tenant_slug'`.

- [ ] **Step 3: Реализовать `get_tenant_slug`** в `admin-panel/db.py` (рядом с club-хелперами, ~L4731):

```python
async def get_tenant_slug(tenant_id) -> str | None:
    """Слаг тенанта по id (для URL публичного лендинга клуба). None — тенанта нет."""
    async with pool.acquire() as c:
        v = await c.fetchval("select slug from tenants where id = $1", tenant_id)
    return (v or "").strip() or None
```

- [ ] **Step 4: Запустить — зелёный**

Run: `PYTHONPATH=admin-panel:. ./.venv-smoke/bin/python scripts/club_db_smoke.py`
Expected: PASS.

- [ ] **Step 5: Собрать `invite` в обработчике `GET /club`** (`admin-panel/app.py`, ~L4227, там где уже есть `tid`/active tenant и рендер `club.html`). Перед `TemplateResponse` добавить:

```python
    runtime = await db.get_runtime_status()
    _bot_u = (runtime.get("bot_username") or "").strip()
    _bot_base = (runtime.get("bot_public_base_url") or "").strip()
    _slug = await db.get_tenant_slug(tid)
    # Готовность = клуб может принять вступление: реквизиты оператора заполнены (тот же гейт,
    # что get_legal_doc_data / _club_start). Переиспользуем get_tenant_legal_urls — он непустой
    # только при заполненных реквизитах.
    _legal = await db.get_tenant_legal_urls(tid)
    _ready = bool(_bot_u and _bot_base and _slug and _legal)
    invite = {
        "ready": _ready,
        "landing_url": f"{_bot_base}/club/{_slug}" if (_bot_base and _slug) else "",
        "deeplink": f"https://t.me/{_bot_u}?start=club" if _bot_u else "",
    }
```

Передать `invite=invite` в контекст `TemplateResponse` (в том же вызове, что рендерит `club.html`).

- [ ] **Step 6: Блок карточки** в `admin-panel/templates/club.html` (вверху, до списка членов):

```html
<div class="card">
  <h3>Ссылка-приглашение в клуб</h3>
  {% if invite and invite.ready %}
    <p class="muted">Публичная страница клуба — делитесь ссылкой в своих каналах, подписи, сообществе. Бизнесы вступают сами.</p>
    <label>Страница-лендинг</label>
    <div class="copy-row"><input id="clubLanding" readonly value="{{ invite.landing_url }}">
      <button type="button" onclick="navigator.clipboard.writeText(document.getElementById('clubLanding').value)">Копировать</button></div>
    <label>Ссылка в бот (deep-link)</label>
    <div class="copy-row"><input id="clubDeep" readonly value="{{ invite.deeplink }}">
      <button type="button" onclick="navigator.clipboard.writeText(document.getElementById('clubDeep').value)">Копировать</button></div>
  {% else %}
    <p class="muted">Чтобы открыть публичное приглашение в клуб, заполните реквизиты оператора (наименование, ИНН, e-mail) в разделе «Лид-магнит».</p>
  {% endif %}
</div>
```

(Классы `card`/`muted`/`copy-row` — использовать уже существующие в панели; если `copy-row` нет — обойтись `<div>` с инлайн-стилем `display:flex;gap:8px`.)

- [ ] **Step 7: Render-проверка панели.** Если есть render-смоук `/club` (`club_catalog_ui_smoke.py`) — расширить проверкой: при `invite.ready=True` в HTML есть `landing_url` и `deeplink`; при `ready=False` — подсказка про реквизиты. Иначе — ручная проверка страницы после деплоя. Прогнать существующий:

Run: `PYTHONPATH=admin-panel:. ./.venv-smoke/bin/python scripts/club_catalog_ui_smoke.py`
Expected: PASS (регресс не сломан).

- [ ] **Step 8: Коммит**

```bash
cd ~/Downloads/risuy-ecosystem && git add admin-panel/db.py admin-panel/app.py admin-panel/templates/club.html scripts/club_db_smoke.py && \
git commit -m "feat(club): карточка-ссылка приглашения в /club панели"
```

---

## Деплой (после всех задач, по явному «да» владельца)

Прод-DDL нет. Порядок: коммиты → push `docs/security-audit:main` (FF) → авто-редеплой обоих аппов → поллинг `commit_sha`/active → LIVE: `{BOT_PUBLIC_BASE_URL}/club/<slug>` 200 + кнопка; панель `/club` показывает карточку. Push/деплой — только по «да».

## Self-Review (выполнено автором плана)
- **Покрытие спеки:** §3.1 лендинг → Task 1+2; §3.2 данные (reuse get_legal_doc_data) → Task 2; §3.3 карточка → Task 3; §5 краевые (404, пустой username) → Task 2 Step 3; §6 тесты → unit (T1) + db-смоук гейта (T2) + helper-смоук (T3). Гейт готовности — согласован (get_legal_doc_data / get_tenant_legal_urls, оба непусты только при заполненных реквизитах).
- **Плейсхолдеры:** нет — весь код приведён.
- **Консистентность типов:** `_club_landing_html(operator_name, deeplink, policy_url)` одинаково в T1 и T2; `get_tenant_slug(tenant_id)->str|None`, `invite={ready,landing_url,deeplink}` — согласованы T3.
