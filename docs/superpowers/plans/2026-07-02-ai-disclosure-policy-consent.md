# Раскрытие ИИ-обработки + условная трансгран-декларация — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Убрать ложную хардкод-декларацию 6.3 «трансгран не осуществляется» из Политики и явно раскрыть ИИ-обработку обращений (Task 1.2), сделав трансгран-декларацию управляемой платформенным флагом-истиной.

**Architecture:** Глобальный флаг `app_settings.ai_inference_rf` (дефолт false = трансгранично, fail-safe) читается хелпером в обоих аппах; `build_privacy_policy` получает keyword-параметр `transborder` и рендерит §6.3 условно + §6.5 (раскрытие ИИ) всегда; `build_consent_text` всегда добавляет строку про ИИ. 3 места генерации проводят флаг; платформа ставит его чекбоксом.

**Tech Stack:** Python (aiogram-бот `bot-telegram/`, FastAPI/Jinja2 панель `admin-panel/`, общий `shared/`), asyncpg, Postgres (`app_settings` KV), смоуки `scripts/*_smoke.py` через `.venv-smoke`.

## Global Constraints

- 🇷🇺 Только русский — код-комментарии, тексты, коммиты.
- **Прод-DDL НЕ требуется** — `app_settings` KV уже есть, `panel_rw` пишет; ключ `ai_inference_rf` аддитивен.
- **Fail-safe направление:** ошибка чтения флага / нет ключа → `False` (трансгранично) → «не осуществляется» НЕ печатается.
- **Дефолт параметра** `transborder: bool = True` (безопасный): легаси-вызов без параметра → безопасная ветка.
- Смоуки: unit — `PYTHONPATH=. ./.venv-smoke/bin/python scripts/<name>_smoke.py`; DB — только risuy_dev, гард по `TEAM_DSN`, `PYTHONPATH=bot-telegram:. ./.venv-smoke/bin/python scripts/<name>_db_smoke.py`.
- Коммит явными файлами (НЕ коммитить `CLAUDE.md`/`.claude/`/`.gitignore` — graphify). Push/деплой — по явному «да» владельца.
- Перед деплоем — 3-линзовое адверсариальное ревью (Workflow), как проектный ритм.
- Точные формулировки Политики выверяет юрист (спека §8) — код лишь доставляет согласованный текст.

**Отклонение от спеки (зафиксировано):** параметр `transborder` добавляется ТОЛЬКО в `build_privacy_policy` (в Согласии ИИ-строка безусловна, транс-специфики нет — мёртвый параметр не плодим). Отдельное согласие ст.12 на трансгран — вне scope (Путь A).

---

### Task 1: Тексты Политики/Согласия (`shared/leadmagnet.py`)

**Files:**
- Modify: `shared/leadmagnet.py` (`build_consent_text` L44-72, `build_privacy_policy` L75-150)
- Test: `scripts/consent_text_smoke.py` (расширить)

**Interfaces:**
- Produces: `build_privacy_policy(operator_name, operator_inn, operator_email, operator_ogrn=None, operator_address=None, data_purpose=None, *, phone_step=True, transborder: bool = True) -> str`
- Produces: `build_consent_text(...)` — сигнатура без изменений; результат всегда содержит строку-раскрытие ИИ.

- [ ] **Step 1: Добавить падающие проверки в `scripts/consent_text_smoke.py`**

Дописать в конец `main()` (перед финальным print/return) блок:

```python
    # --- Task 1: раскрытие ИИ + условная трансгран-декларация ---
    pp_tb = build_privacy_policy("ИП Петров П.П.", "770000000000", "hello@petrov.ru", transborder=True)
    assert "за пределы Российской Федерации" in pp_tb, "transborder=True: нет трансгран-раскрытия в 6.3"
    assert "Трансграничная передача персональных данных не осуществляется" not in pp_tb, \
        "transborder=True: ложный абсолют не должен печататься"
    assert "6.5." in pp_tb and "искусственного интеллекта" in pp_tb, "нет раздела 6.5 (раскрытие ИИ)"

    pp_rf = build_privacy_policy("ИП Петров П.П.", "770000000000", "hello@petrov.ru", transborder=False)
    assert "Трансграничная передача персональных данных не осуществляется" in pp_rf, \
        "transborder=False: должна быть декларация «не осуществляется»"
    assert "6.5." in pp_rf and "искусственного интеллекта" in pp_rf, "transborder=False: нет раздела 6.5"

    pp_default = build_privacy_policy("ИП", "7700000000", "a@b.ru")
    assert "за пределы Российской Федерации" in pp_default, "дефолт должен быть безопасной веткой (transborder=True)"

    ct_ai = build_consent_text("ИП", "7700000000", "a@b.ru")
    assert "включая ИИ" in ct_ai, "Согласие: нет строки-раскрытия ИИ"
    print("OK: раскрытие ИИ + условная трансгран-декларация")
```

- [ ] **Step 2: Запустить смоук — убедиться, что падает**

Run: `cd ~/Downloads/risuy-ecosystem && PYTHONPATH=. ./.venv-smoke/bin/python scripts/consent_text_smoke.py`
Expected: FAIL (AssertionError «transborder=True: нет трансгран-раскрытия» либо TypeError про неизвестный kwarg `transborder`).

- [ ] **Step 3: В `build_privacy_policy` добавить параметр и условные пункты**

В сигнатуре после `phone_step: bool = True,` добавить строку:

```python
    transborder: bool = True,
```

Перед `return "\n".join([` вставить вычисление пунктов:

```python
    if transborder:
        clause_63 = (
            "6.3. Для подготовки ответов на обращения оператор использует сторонние "
            "автоматизированные сервисы обработки (в том числе системы искусственного интеллекта). "
            "При этом персональные данные могут передаваться обработчикам, в том числе за пределы "
            "Российской Федерации. Идентифицирующие контакты (номер телефона, адрес электронной почты, "
            "ИНН) обезличиваются до передачи в такие сервисы."
        )
    else:
        clause_63 = "6.3. Трансграничная передача персональных данных не осуществляется."
    clause_65 = (
        "6.5. Для подготовки ответов на обращения оператор использует автоматизированные системы, "
        "в том числе системы искусственного интеллекта. Идентифицирующие контакты (номер телефона, "
        "адрес электронной почты, ИНН) обезличиваются до передачи в такие системы."
    )
```

В возвращаемом списке заменить строку `"6.3. Трансграничная передача ПДн не осуществляется."` на `clause_63`, а после строки `6.4. ...` (перед `""` и `"7. Права ..."`) добавить `clause_65`:

```python
        "6.2. Хранение ПДн осуществляется на серверах, расположенных на территории Российской Федерации "
        "(ч. 5 ст. 18 152-ФЗ).",
        clause_63,
        "6.4. Срок обработки — до достижения цели обработки либо до отзыва согласия субъектом ПДн. "
        "После отзыва согласия данные подлежат обезличиванию/удалению в срок не позднее 30 дней.",
        clause_65,
        "",
        "7. Права субъекта персональных данных",
```

- [ ] **Step 4: В `build_consent_text` добавить строку-раскрытие ИИ**

В списке `lines` после `"• Хранение — на серверах в России",` добавить:

```python
        "• Для подготовки ответов используются автоматизированные системы, включая ИИ; "
        "идентифицирующие контакты обезличиваются",
```

- [ ] **Step 5: Запустить смоук — убедиться, что проходит**

Run: `cd ~/Downloads/risuy-ecosystem && PYTHONPATH=. ./.venv-smoke/bin/python scripts/consent_text_smoke.py`
Expected: PASS (в т.ч. «OK: раскрытие ИИ + условная трансгран-декларация»).

- [ ] **Step 6: Коммит**

```bash
cd ~/Downloads/risuy-ecosystem
git add shared/leadmagnet.py scripts/consent_text_smoke.py
git commit -m "feat(legal): условная трансгран-декларация 6.3 + раскрытие ИИ-обработки в Политике/Согласии"
```

---

### Task 2: Хелпер флага `get_ai_inference_rf()` (оба аппа)

**Files:**
- Modify: `bot-telegram/db.py` (рядом с `get_ai_overrides` L1245)
- Modify: `admin-panel/db.py` (рядом с существующим чтением `app_settings` — `get_ai_settings`)
- Test: `scripts/ai_inference_rf_db_smoke.py` (создать)

**Interfaces:**
- Produces (оба модуля): `async def get_ai_inference_rf() -> bool`

- [ ] **Step 1: Прочитать существующий паттерн чтения `app_settings` в панели**

Run: `cd ~/Downloads/risuy-ecosystem && grep -n "def get_ai_settings\|app_settings" admin-panel/db.py | head`
Читать `admin-panel/db.py::get_ai_settings`, чтобы хелпер использовал ТУ ЖЕ схему acquire/пула панели.

- [ ] **Step 2: Написать падающий DB-смоук `scripts/ai_inference_rf_db_smoke.py`**

```python
"""Смоук флага ai_inference_rf (risuy_dev). Гард: TEAM_DSN обязателен.
Запуск: TEAM_DSN=<owner-DSN risuy_dev> PYTHONPATH=bot-telegram:. ./.venv-smoke/bin/python scripts/ai_inference_rf_db_smoke.py
"""
import asyncio, os, sys

DSN = os.environ.get("TEAM_DSN", "")
if "risuy_dev" not in DSN:
    print("SKIP: нужен TEAM_DSN на risuy_dev (гард)"); sys.exit(0)

async def main():
    import asyncpg
    import importlib
    db = importlib.import_module("db")  # bot-telegram/db.py
    db.pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
    try:
        await db.pool.execute("delete from app_settings where key='ai_inference_rf'")
        assert await db.get_ai_inference_rf() is False, "дефолт (нет ключа) должен быть False"
        await db.pool.execute(
            "insert into app_settings(key,value) values('ai_inference_rf','1') "
            "on conflict(key) do update set value=excluded.value")
        assert await db.get_ai_inference_rf() is True, "'1' → True"
        await db.pool.execute("update app_settings set value='0' where key='ai_inference_rf'")
        assert await db.get_ai_inference_rf() is False, "'0' → False"
    finally:
        await db.pool.execute("delete from app_settings where key='ai_inference_rf'")
        await db.pool.close()
    print("OK: ai_inference_rf helper")

asyncio.run(main())
```

- [ ] **Step 3: Запустить смоук — убедиться, что падает**

Run: `cd ~/Downloads/risuy-ecosystem && TEAM_DSN="<owner-DSN risuy_dev>" PYTHONPATH=bot-telegram:. ./.venv-smoke/bin/python scripts/ai_inference_rf_db_smoke.py`
Expected: FAIL (AttributeError: module 'db' has no attribute 'get_ai_inference_rf'). *(Нужен owner-DSN risuy_dev строкой; без него — SKIP.)*

- [ ] **Step 4: Реализовать хелпер в `bot-telegram/db.py`**

```python
async def get_ai_inference_rf() -> bool:
    """Платформенный флаг-истина: инференс ИИ выполняется в РФ (нетрансгранично).
    Глобальный ключ app_settings 'ai_inference_rf'. Fail-safe: нет ключа / ошибка чтения
    → False (считаем трансграничным — ложную декларацию «трансгран не осуществляется» не публикуем)."""
    try:
        async with pool.acquire() as c:
            v = await c.fetchval("select value from app_settings where key = 'ai_inference_rf'")
    except Exception:  # noqa: BLE001 — сбой чтения не должен ронять генерацию юр-текста
        return False
    return (v or "").strip().lower() in ("1", "true", "yes", "on", "да")
```

- [ ] **Step 5: Реализовать зеркальный хелпер в `admin-panel/db.py`**

Тот же контракт, но по паттерну панели из Step 1 (пул/acquire панели). SQL идентичен: `select value from app_settings where key = 'ai_inference_rf'`; парсинг truthy тот же; fail-safe → `False`.

- [ ] **Step 6: Запустить смоук — убедиться, что проходит**

Run: `cd ~/Downloads/risuy-ecosystem && TEAM_DSN="<owner-DSN risuy_dev>" PYTHONPATH=bot-telegram:. ./.venv-smoke/bin/python scripts/ai_inference_rf_db_smoke.py`
Expected: PASS («OK: ai_inference_rf helper»).

- [ ] **Step 7: Коммит**

```bash
cd ~/Downloads/risuy-ecosystem
git add bot-telegram/db.py admin-panel/db.py scripts/ai_inference_rf_db_smoke.py
git commit -m "feat(legal): хелпер get_ai_inference_rf() (fail-safe False) в боте и панели"
```

---

### Task 3: Проводка флага в 3 места генерации

**Files:**
- Modify: `bot-telegram/bot.py` (`_legal_page` L363-376)
- Modify: `admin-panel/app.py` (превью L5234-5240)
- Modify: `bot-telegram/db.py` (`get_funnel_config` L1598-1599 — только импорт; consent транс-специфики не несёт → правка НЕ нужна, см. ниже)

**Interfaces:**
- Consumes: `db.get_ai_inference_rf() -> bool` (Task 2), `build_privacy_policy(..., transborder=...)` (Task 1)

- [ ] **Step 1: Обновить smoke-ожидание для публичной страницы (регресс)**

В `scripts/consent_text_smoke.py` это чистые функции; проводку страниц проверяем вручную (Step 4). Убедиться, что `consent_text_smoke.py` по-прежнему PASS:
Run: `cd ~/Downloads/risuy-ecosystem && PYTHONPATH=. ./.venv-smoke/bin/python scripts/consent_text_smoke.py`
Expected: PASS.

- [ ] **Step 2: `bot-telegram/bot.py::_legal_page` — прочитать флаг и передать в Политику**

В блоке `if doc_type == "privacy":` перед вызовом добавить чтение флага и параметр:

```python
    rf = await db.get_ai_inference_rf()
    if doc_type == "privacy":
        title = "Политика обработки персональных данных"
        body = build_privacy_policy(
            kv["operator_name"], kv["operator_inn"], kv["operator_email"],
            operator_ogrn=kv.get("operator_ogrn") or None,
            operator_address=kv.get("operator_address") or None,
            data_purpose=kv.get("data_purpose") or None, phone_step=phone,
            transborder=not rf)
```

*(Согласие `build_consent_text` НЕ трогаем — строка про ИИ уже безусловна из Task 1.)*

- [ ] **Step 3: `admin-panel/app.py` превью — прочитать флаг и передать в Политику**

В обработчике превью (около L5240) перед `policy = leadmagnet.build_privacy_policy(...)` добавить:

```python
        rf = await db.get_ai_inference_rf()
```

и в вызове `build_privacy_policy(...)` добавить `transborder=not rf` (в тот же набор именованных аргументов, что уже передаются). `build_consent_text` превью не трогаем.

- [ ] **Step 4: Ручная проверка рендера (без деплоя)**

Мини-скрипт (scratchpad) вызывает `build_privacy_policy(..., transborder=True/False)` и печатает §6.3/§6.5 — визуально сверить формулировки; ИЛИ дождаться Task 5 (полный смоук + ревью). Зафиксировать: страница читает флаг, «не осуществляется» появляется только при `rf=True`.

- [ ] **Step 5: Коммит**

```bash
cd ~/Downloads/risuy-ecosystem
git add bot-telegram/bot.py admin-panel/app.py
git commit -m "feat(legal): проводка ai_inference_rf в публичную страницу и превью (transborder)"
```

---

### Task 4: Чекбокс платформы для флага (`/agents`)

**Files:**
- Modify: `admin-panel/templates/agents.html` (блок `is_platform`)
- Modify: `admin-panel/app.py` (контекст `/agents` + POST-обработчик записи флага)
- Modify: `admin-panel/db.py` (сеттер `set_ai_inference_rf(value: bool)` по паттерну существующей записи `app_settings`)

**Interfaces:**
- Consumes: `db.get_ai_inference_rf()` (для отображения текущего состояния)
- Produces: `db.set_ai_inference_rf(value: bool) -> None`

- [ ] **Step 1: Прочитать существующий write-паттерн `app_settings` в панели**

Run: `cd ~/Downloads/risuy-ecosystem && grep -n "app_settings" admin-panel/db.py | head -30`
Найти, как панель пишет глобальный `app_settings` (напр., в разделе персон/ИИ-настроек), чтобы сеттер использовал тот же upsert + аудит-паттерн.

- [ ] **Step 2: Реализовать `set_ai_inference_rf` в `admin-panel/db.py`**

```python
async def set_ai_inference_rf(value: bool) -> None:
    """Платформенный флаг ai_inference_rf (глобальный app_settings). Пишет '1'/'0'."""
    async with pool.acquire() as c:
        await c.execute(
            "insert into app_settings(key, value) values('ai_inference_rf', $1) "
            "on conflict(key) do update set value = excluded.value",
            "1" if value else "0")
```

- [ ] **Step 3: Добавить чекбокс в `agents.html` (под `is_platform`)**

```html
{% if is_platform %}
<form method="post" action="/agents/ai-inference-rf" class="card">
  {{ csrf_field()|safe }}
  <label>
    <input type="checkbox" name="ai_inference_rf" value="1" {% if ai_inference_rf %}checked{% endif %}>
    Инференс ИИ выполняется в РФ (нетрансгранично)
  </label>
  <p class="hint">Включайте только после письменного подтверждения РФ-размещения инференса и логов
    (см. legal-gate досье). По умолчанию — выкл.: Политика раскрывает возможную трансграничную передачу.</p>
  <button type="submit">Сохранить</button>
</form>
{% endif %}
```

*(Точные имена `csrf_field`/классы/паттерн формы — по образцу соседних форм в `agents.html`.)*

- [ ] **Step 4: Прокинуть `ai_inference_rf` в контекст `/agents` и добавить POST-обработчик**

В GET-хендлере `/agents` в контекст шаблона добавить `ai_inference_rf=await db.get_ai_inference_rf()`.
Добавить POST `/agents/ai-inference-rf` (по образцу POST персон: `_require_admin` + CSRF + PRG-редирект на `/agents`):

```python
@app.post("/agents/ai-inference-rf")
async def set_ai_inference_rf_route(request: Request):
    _require_admin(request)          # платформа-only, как у форм персон
    await _verify_csrf(request)      # тем же способом, что соседние POST
    form = await request.form()
    await db.set_ai_inference_rf(bool(form.get("ai_inference_rf")))
    return RedirectResponse("/agents", status_code=303)
```

*(Имена `_require_admin`/`_verify_csrf`/декоратор — по образцу существующих POST-роутов `/agents/...`.)*

- [ ] **Step 5: Рендер-смоук панели (если есть) + ручная проверка**

Run: `cd ~/Downloads/risuy-ecosystem && ls scripts/ | grep -i "agents\|render\|ui" ` — если есть render-смоук `/agents`, добавить кейс «чекбокс виден платформе, скрыт тенанту» и прогнать. Иначе — `python -c "import ast; ast.parse(open('admin-panel/app.py').read())"` (py_compile) + ручной осмотр.

- [ ] **Step 6: Коммит**

```bash
cd ~/Downloads/risuy-ecosystem
git add admin-panel/templates/agents.html admin-panel/app.py admin-panel/db.py
git commit -m "feat(legal): чекбокс платформы «Инференс ИИ в РФ» → app_settings.ai_inference_rf"
```

---

### Task 5: Верификация, адверсариальное ревью и деплой-гейт

**Files:** нет (гейт).

- [ ] **Step 1: Прогнать все затронутые смоуки**

```bash
cd ~/Downloads/risuy-ecosystem
PYTHONPATH=. ./.venv-smoke/bin/python scripts/consent_text_smoke.py
TEAM_DSN="<owner-DSN risuy_dev>" PYTHONPATH=bot-telegram:. ./.venv-smoke/bin/python scripts/ai_inference_rf_db_smoke.py
PYTHONPATH=. ./.venv-smoke/bin/python scripts/funnel_config_smoke.py   # регресс воронки
```
Expected: все PASS (db-смоук — SKIP без owner-DSN).

- [ ] **Step 2: py_compile затронутых модулей**

```bash
cd ~/Downloads/risuy-ecosystem
./.venv-smoke/bin/python -c "import ast,sys; [ast.parse(open(f).read()) for f in ['shared/leadmagnet.py','bot-telegram/db.py','bot-telegram/bot.py','admin-panel/db.py','admin-panel/app.py']]; print('py OK')"
```
Expected: `py OK`.

- [ ] **Step 3: 3-линзовое адверсариальное ревью (Workflow)**

Линзы: (1) корректность — `transborder=not rf` во ВСЕХ 3 местах, await-покрытие, fail-safe False; (2) комплаенс-формулировки — 6.3/6.5 не переобещают/не недообещают, цитатная гигиена (§5 досье), Согласие честно; (3) изоляция флага — глобальный, запись только `is_platform`+CSRF, дефолт/fail-safe = трансгранично. Внести подтверждённые findings, повторить смоуки.

- [ ] **Step 4: Деплой-гейт (по «да» владельца)**

Push: `git push origin docs/security-audit:main` (FF) → авто-редеплой обоих аппов → поллинг `twc apps get <id> -o json` по `.app.commit_sha` до HEAD + `active`. **Только по явному «да».**

- [ ] **Step 5: Пост-деплой проверка**

Открыть `/legal/{slug}/privacy` живого тенанта → §6.3 отражает флаг (по умолчанию — трансгран-раскрытие), §6.5 присутствует. При включении Gemma-РФ: платформа ставит чекбокс → §6.3 = «не осуществляется».

---

## Self-Review (проведён)

- **Покрытие спеки:** §3.1 флаг → Task 2/4; §3.2 тексты → Task 1; §3.3 проводка → Task 3; §3.4 чекбокс → Task 4; §6 тесты → Task 1/2/5; §5 fail-safe → Task 2. Пробелов нет.
- **Плейсхолдеры:** код показан для leadmagnet + bot-хелпера + смоуков; для панельных write/CSRF/POST дано конкретное тело + указание сверить с существующим паттерном (существующий кодовый образец — легитимная опора в живой кодовой базе).
- **Согласованность типов:** `get_ai_inference_rf() -> bool` и `set_ai_inference_rf(bool)` — единые имена во всех задачах; `transborder` — единое имя параметра.
- **Известное ограничение:** флаг глобальный (платформенный); per-tenant backend-override — вне scope (спека §8).
