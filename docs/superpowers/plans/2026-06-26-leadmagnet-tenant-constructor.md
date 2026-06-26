# Конструктор выдачи лид-магнита (пер-тенант) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: используйте superpowers:subagent-driven-development (рекомендуется) или superpowers:executing-plans для выполнения задача-за-задачей. Шаги размечены чекбоксами (`- [ ]`).

**Goal:** Любой платящий тенант через панель настраивает СВОЮ воронку выдачи лид-магнита (приветствие, структурное согласие 152-ФЗ, гейт-канал, сам лид-магнит, видео), и его тенант-бот в `multiplex.py` отрабатывает её автоматически — без правки кода и без затрагивания работающей Школы Лесова.

**Architecture:** Source-of-truth — таблица `tenant_settings` (KV, RLS-изоляция уже есть). Воронка для тенант-ботов живёт в `multiplex.py` (`tenant_router`) и ведётся **по состоянию лида в БД** (`leads.consent/subscribed/status`), а НЕ через aiogram-FSM (callback-и в тенант-роутере уже работают, см. `buy:`). Текст согласия 152-ФЗ генерируется из структурных полей через канонический шаблон в `shared/`. Школа (env-бот `handlers.py`) на первом этапе не трогается: тенант-ключей в её `tenant_settings` нет → её старый путь работает как раньше; параметризация Школы — отдельная поздняя задача.

**Tech Stack:** Python 3.12, aiogram 3 (бот), FastAPI + Jinja2 (панель), asyncpg + Postgres (Neon/Timeweb), RLS по `app.tenant_id`. Тесты — smoke-скрипты `scripts/*_smoke.py` через `.venv-smoke` (asyncpg/jinja2; БЕЗ aiogram/aiohttp), owner-DSN на `risuy_dev`.

## Global Constraints

- **Только русский** во всех текстах, комментариях, коммитах, UI. Латиница — только идентификаторы/ключи/SQL.
- **Школа Лесова не должна сломаться.** Все новые чтения — с фолбэком на текущий env/хардкод; у Школы новых ключей `tenant_settings` нет → старый путь.
- **DDL нет.** `tenant_settings (tenant_id, key, value)` уже есть (RLS, `db/schema_tenancy.sql:49-66`). Только новые KV-ключи. Если позже понадобится колонка — отдельная schema-first миграция через `twc-migrate.sh` (СНАЧАЛА `risuy_dev`), под явное «да» владельца.
- **152-ФЗ:** текст согласия НЕ свободный — строится из структурных полей (`operator_name`, `operator_inn`, `operator_email`, `data_purpose`, `privacy_url`) каноническим шаблоном. Один источник истины шаблона.
- **RLS:** бот = owner (обходит RLS), но ОБЯЗАН фильтровать `tenant_id` явно / писать под contextvar тенанта. Панель = `panel_rw`, скоупит через `set_active_tenant`. Новый раздел демо/тенанта — fail-closed.
- **Прод-write tenant_settings и активация подписки** трогают живой платный путь → правки + smoke на `risuy_dev`; деплой (push) и любые прод-`tenant_settings`-записи — только под явное «да» владельца.
- **Деплой = push в main** (Timeweb, 4 app; «код live» по смене `start_time`). Хук graphify пересоберёт граф локально.

## Список новых ключей `tenant_settings` (контракт «конструктора»)

| Ключ | Тип (в value, текст) | Назначение | Дефолт при отсутствии |
|---|---|---|---|
| `funnel_enabled` | `"1"`/`""` | мастер-переключатель воронки тенант-бота | выкл (только Лия — текущее v1) |
| `welcome_text` | текст | приветствие на /start | `_TENANT_GREETING` (хардкод) |
| `operator_name` | текст | оператор ПДн (юр.лицо/ИП) | — (обяз. для согласия) |
| `operator_inn` | текст (цифры) | ИНН оператора | — (обяз.) |
| `operator_email` | email | контакт отзыва согласия | — (обяз.) |
| `data_purpose` | текст | цель обработки ПДн | дефолт-формулировка |
| `privacy_url` | http(s) URL | ссылка на политику | пусто (кнопка не показывается) |
| `company_name` | текст | название в платёжных описаниях | `operator_name` |
| `gate_enabled` | `"1"`/`""` | требовать подписку на канал | выкл |
| `gate_channel_id` | int-строка | id гейт-канала | — (обяз. если gate_enabled) |
| `gate_channel_url` | URL | ссылка на канал | — (обяз. если gate_enabled) |
| `phone_step_enabled` | `"1"`/`""` | спрашивать телефон | выкл |
| `leadmagnet_kind` | `link`/`file` | тип лид-магнита | — (обяз. если funnel_enabled) |
| `leadmagnet_url` | http(s) URL | ссылка-лид-магнит (kind=link) | — |
| `leadmagnet_file_id` | tg file_id | файл-лид-магнит (kind=file) | — |
| `leadmagnet_caption` | текст | подпись к выдаче | дефолт |
| `video_note_file_id` | tg file_id | видео-кружок до выдачи | пусто (пропуск) |

---

## File Structure

**Создаётся:**
- `shared/leadmagnet.py` — канонический генератор текста согласия 152-ФЗ + схема полей конструктора + валидаторы. Импортируется И ботом, И панелью (единый источник истины формулировки и состава полей).
- `bot-telegram/funnel.py` — пер-тенантная воронка тенант-бота (DB-state-driven): построение приветствия+согласия, обработка согласия/телефона/гейта/выдачи, читает конфиг из `tenant_settings`.
- `admin-panel/templates/lead_magnet.html` — раздел-конструктор: форма всех полей + живой preview сгенерированного согласия и приветствия.
- `scripts/funnel_config_smoke.py`, `scripts/consent_text_smoke.py`, `scripts/leadmagnet_seed_smoke.py` — smoke-тесты.

**Модифицируется:**
- `bot-telegram/db.py` — добавить `get_funnel_config(tid) -> FunnelConfig` (читает все ключи) и `set_lead_magnet_delivered(...)` (если нужно отдельно от `mark_guide_sent`).
- `bot-telegram/multiplex.py` — `t_start` → пер-тенант приветствие+согласие при `funnel_enabled`; новые `@tenant_router.callback_query` (`consent_yes`, `check_sub`); подключить `funnel.py`.
- `admin-panel/db.py` — `get_funnel_config_panel(tid)` (чтение для формы), `set_funnel_config(tid, fields)` (валидирующая запись KV), `seed_default_funnel(tid)` (шаблон при покупке) + врезка в `activate_subscription_from_payment`.
- `admin-panel/app.py` — роут `/lead-magnet` (GET форма + preview, POST сохранение), нав-пункт.
- `admin-panel/templates/base.html` — нав-пункт «Лид-магнит».
- `bot-telegram/handlers.py` (ПОЗДНЯЯ задача, опц.) — параметризация текстов Школы через тот же `get_funnel_config` с фолбэком.

---

## Phase 0 — Фундамент: согласие 152-ФЗ + чтение конфига (без изменения поведения)

### Task 1: Канонический генератор согласия 152-ФЗ (`shared/leadmagnet.py`)

**Files:**
- Create: `shared/leadmagnet.py`
- Test: `scripts/consent_text_smoke.py`

**Interfaces:**
- Produces:
  - `FUNNEL_FIELDS: list[FieldSpec]` — описание полей конструктора (key, label, required, kind: text|email|url|inn|bool|tg_file|channel) для рендера формы и валидации.
  - `build_consent_text(operator_name: str, operator_inn: str, operator_email: str, data_purpose: str | None = None, privacy_url: str | None = None) -> str` — формулировка согласия из шаблона.
  - `validate_funnel_fields(d: dict) -> list[str]` — список человекочитаемых ошибок (пусто = ок).

- [ ] **Step 1: Написать падающий smoke** `scripts/consent_text_smoke.py`:

```python
#!/usr/bin/env python3
"""Smoke генератора согласия 152-ФЗ: шаблон подставляет реквизиты, обязательные поля валидируются."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared"))
from leadmagnet import build_consent_text, validate_funnel_fields, FUNNEL_FIELDS

def main():
    t = build_consent_text("ИП Петров П.П.", "770000000000", "hello@petrov.ru",
                            data_purpose=None, privacy_url="https://petrov.ru/privacy")
    assert "ИП Петров П.П." in t and "770000000000" in t and "hello@petrov.ru" in t, t
    assert "отзыв" in t.lower() or "отозвать" in t.lower(), "нет упоминания отзыва согласия"
    # обязательные поля
    errs = validate_funnel_fields({"funnel_enabled": "1", "leadmagnet_kind": "link"})
    assert any("оператор" in e.lower() or "operator" in e.lower() for e in errs), errs
    assert any("лид-магнит" in e.lower() or "leadmagnet" in e.lower() for e in errs), errs
    # корректный набор → без ошибок
    ok = validate_funnel_fields({
        "funnel_enabled": "1", "operator_name": "ИП Петров", "operator_inn": "770000000000",
        "operator_email": "a@b.ru", "leadmagnet_kind": "link", "leadmagnet_url": "https://x.ru/g.pdf",
    })
    assert ok == [], ok
    print("🟢 consent_text_smoke зелёный")

if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Запустить — убедиться, что падает** (`ModuleNotFoundError: leadmagnet`).

Run: `./.venv-smoke/bin/python scripts/consent_text_smoke.py`
Expected: FAIL (модуля нет).

- [ ] **Step 3: Реализовать `shared/leadmagnet.py`** — шаблон согласия (формулировка, согласованная с действующим текстом Школы `bot-telegram/texts.py:4-19`, но с подстановкой реквизитов), `FUNNEL_FIELDS`, `validate_funnel_fields`. Шаблон обязан содержать: оператор, ИНН, цель обработки (дефолт «для связи и предоставления материалов»), срок/основание, способ отзыва (email), опц. ссылку на политику. Без внешних зависимостей (чистый Python — импортится и из `.venv-smoke`).

- [ ] **Step 4: Запустить — зелёный.**

Run: `./.venv-smoke/bin/python scripts/consent_text_smoke.py`
Expected: `🟢 consent_text_smoke зелёный`

- [ ] **Step 5: Commit** — `feat(leadmagnet): канонический генератор согласия 152-ФЗ + схема полей конструктора`

### Task 2: Чтение пер-тенант конфига воронки в боте (`get_funnel_config`)

**Files:**
- Modify: `bot-telegram/db.py` (рядом с `get_tenant_ai_overrides`/`get_tenant_setting` ~L1348)
- Test: `scripts/funnel_config_smoke.py`

**Interfaces:**
- Consumes: `db.get_tenant_setting(tid, key)` (уже есть, ~L1348), таблица `tenant_settings`.
- Produces: `get_funnel_config(tid) -> dict` — все ключи воронки c фолбэками: `{"enabled": bool, "welcome_text": str, "consent_text": str (через build_consent_text при наличии operator_*), "privacy_url": str|None, "gate": {...}, "phone_step": bool, "leadmagnet": {"kind","url","file_id","caption"}, "video_note_file_id": str|None, "company_name": str}`. `enabled=False` при отсутствии `funnel_enabled` (Школа/непровиженный тенант).

- [ ] **Step 1: Написать падающий smoke** `scripts/funnel_config_smoke.py` (owner-DSN `risuy_dev`): сидит во временный тест-тенант ключи `funnel_enabled/operator_*/leadmagnet_*`, вызывает `db.get_funnel_config(tid)`, проверяет: `enabled is True`, `consent_text` содержит реквизиты, `leadmagnet["kind"]=="link"`; для тенанта БЕЗ ключей — `enabled is False`. Чистит тест-данные.

- [ ] **Step 2: Запустить — падает** (нет `get_funnel_config`).

Run: `WEB_SMOKE_DSN=... PYTHONPATH=bot-telegram ./.venv-smoke/bin/python scripts/funnel_config_smoke.py`

- [ ] **Step 3: Реализовать `get_funnel_config(tid)`** в `bot-telegram/db.py`: один `fetch` всех ключей воронки из `tenant_settings`, сборка dict, `consent_text` через `shared/leadmagnet.build_consent_text` при наличии `operator_*` (иначе пусто). Импорт `shared` через расширение sys.path или относительный — согласовать с тем, как бот уже видит `shared/` (проверить наличие в PYTHONPATH бота; если нет — продублировать импорт-путь как в существующем коде).

- [ ] **Step 4: Запустить — зелёный.**

- [ ] **Step 5: Commit** — `feat(bot): get_funnel_config — пер-тенант чтение настроек воронки из tenant_settings`

---

## Phase 1 — Панель: раздел-конструктор «Лид-магнит» → `tenant_settings`

### Task 3: Валидирующий writer конфига воронки (`set_funnel_config`)

**Files:**
- Modify: `admin-panel/db.py` (по образцу `set_tenant_escalation_config`, ~L3242-3275)
- Test: `scripts/funnel_config_smoke.py` (расширить: write→read round-trip)

**Interfaces:**
- Consumes: `shared/leadmagnet.validate_funnel_fields`, `set_active_tenant`, RLS на `tenant_settings`.
- Produces: `set_funnel_config(tid, fields: dict) -> list[str]` — валидирует (возвращает ошибки или пусто), upsert каждого ключа в `tenant_settings` под скоупом тенанта. `get_funnel_config_panel(tid) -> dict` — сырые значения для пред-заполнения формы.

- [ ] **Step 1–4:** smoke round-trip (set → get → значения совпали; невалидный набор → ошибки, ничего не записано), реализация upsert KV, прогон.
- [ ] **Step 5: Commit** — `feat(panel): set_funnel_config — валидирующая запись конструктора в tenant_settings`

### Task 4: Роут и форма `/lead-magnet` + живой preview

**Files:**
- Create: `admin-panel/templates/lead_magnet.html`
- Modify: `admin-panel/app.py` (роут GET/POST `/lead-magnet`, нав), `admin-panel/templates/base.html` (нав-пункт)

**Interfaces:**
- Consumes: `db.get_funnel_config_panel`, `db.set_funnel_config`, `shared/leadmagnet.build_consent_text` (для server-side preview), сессия/CSRF/`_require_admin`-или-operator-скоуп как у `/my-agent`.
- Produces: раздел панели «Лид-магнит» (форма всех полей из `FUNNEL_FIELDS`, структурные поля согласия + кнопка-preview сгенерированного текста; поля гейта показываются по чекбоксу `gate_enabled`; лид-магнит link/file по `leadmagnet_kind`). PRG + флеш «Сохранено».

- [ ] **Step 1:** GET `/lead-magnet` рендерит форму из `FUNNEL_FIELDS` с текущими значениями + блок preview согласия (server-side `build_consent_text`).
- [ ] **Step 2:** POST валидирует через `set_funnel_config`; ошибки → форма с подсветкой; успех → PRG `?saved=1`.
- [ ] **Step 3:** нав-пункт «Лид-магнит» (для operator-клиента; платформе — за активного тенанта, как `/channels`).
- [ ] **Step 4: Проверка:** jinja-парс шаблона + `py_compile app.py`; вручную — GET `/lead-magnet`→200, POST невалид→ошибки, POST валид→saved.
- [ ] **Step 5: Commit** — `feat(panel): раздел «Лид-магнит» — конструктор воронки с preview согласия`

### Task 5: Загрузка файла-лид-магнита

**Files:** Modify `admin-panel/app.py` (переиспользовать существующий upload из `/products`), `lead_magnet.html`.

**Interfaces:** Consumes — существующий механизм загрузки файла продукта (валидация ext/MIME/magic, антивирус-эвристика). Produces — при `leadmagnet_kind=file` файл уезжает в хранилище/бота, в `tenant_settings.leadmagnet_file_id` пишется tg file_id (по образцу как Школа хранит `active_lead_magnet_product_id` → продукт с `file_tg_id`).

- [ ] **Step 1–4:** форма-загрузка → получение `file_id` (бот заливает в OPS_CHAT, как ответы оператора) → запись ключа; smoke на запись ключа. ⚠️ Если получение tg file_id требует живого бота — в первой итерации поддержать только `kind=link`, файл — отдельной под-задачей с реальным ботом (отметить в коммите).
- [ ] **Step 5: Commit** — `feat(panel): загрузка файла-лид-магнита в конструкторе`

---

## Phase 2 — Бот: воронка тенанта в `multiplex.py` (DB-state-driven)

### Task 6: Приветствие + согласие на /start тенант-бота

**Files:**
- Create: `bot-telegram/funnel.py`
- Modify: `bot-telegram/multiplex.py` (`t_start`)

**Interfaces:**
- Consumes: `db.get_funnel_config`, `db.upsert_start`, `messaging.send_text`, `db.is_bot_paused`.
- Produces: `funnel.start(message, cfg)` — если `cfg["enabled"]`: шлёт `welcome_text` + `consent_text` с inline-кнопкой «Даю согласие» (callback `consent_yes`) + опц. кнопка «Политика» (`privacy_url`). Если `not enabled` → текущее поведение (`_TENANT_GREETING`, только Лия).

- [ ] **Step 1:** smoke (pure) `funnel.build_start_keyboard(cfg)` → правильный набор кнопок по флагам (consent + опц. policy). Без сети.
- [ ] **Step 2–3:** реализовать `funnel.py` (построение сообщений/клавиатур из cfg) + врезать в `t_start`: `cfg = await db.get_funnel_config(db.tenant_id()); if cfg["enabled"]: await funnel.start(...); else: <старый путь>`.
- [ ] **Step 4:** smoke зелёный; ручная сверка — у тенанта с `funnel_enabled` /start даёт согласие, у Школы/непровиженного — без изменений.
- [ ] **Step 5: Commit** — `feat(bot): тенант-воронка — приветствие+согласие на /start при funnel_enabled`

### Task 7: Callback согласия → (телефон) → выдача лид-магнита

**Files:** Modify `bot-telegram/multiplex.py` (новые `@tenant_router.callback_query`), `bot-telegram/funnel.py`.

**Interfaces:**
- Consumes: `db.set_consent`, `db.set_phone`, `db.mark_guide_sent`, `db.get_funnel_config`, `messaging`.
- Produces: `@tenant_router.callback_query(F.data == "consent_yes")` → `db.set_consent(True)` → если `phone_step` запрошен и нет телефона → просьба телефона (reply-кнопка contact) → иначе `funnel.deliver(...)`. `funnel.deliver(message, cfg)` → опц. видео-кружок → выдача `leadmagnet` (link-кнопка ИЛИ файл по `file_id`) + `leadmagnet_caption` → `db.mark_guide_sent`. Состояние ведётся по `leads.consent/subscribed` (без aiogram-FSM).

- [ ] **Step 1:** smoke (pure) `funnel.deliver_payload(cfg)` → выбор link vs file + наличие caption.
- [ ] **Step 2–3:** реализация callback + `funnel.deliver`; контакт-телефон обрабатывается уже существующим `@tenant_router.message(F.contact)`-аналогом (добавить, если нет) → `db.set_phone` → `funnel.deliver`.
- [ ] **Step 4:** smoke зелёный.
- [ ] **Step 5: Commit** — `feat(bot): тенант-воронка — согласие→(телефон)→выдача лид-магнита`

### Task 8: Опц. гейт подписки на канал тенанта (за флагом)

**Files:** Modify `bot-telegram/multiplex.py`, `bot-telegram/funnel.py`.

**Interfaces:**
- Consumes: `bot.get_chat_member(gate_channel_id, user_id)` (fail-closed), `db.set_subscribed`.
- Produces: при `gate_enabled` между согласием/телефоном и выдачей — гейт на `gate_channel_id`/`gate_channel_url` (кнопки «Перейти в канал» + «Я подписался» → callback `check_sub`). При `not gate_enabled` — шаг пропускается (fail-open: канала Школы НЕ касаемся).

- [ ] **Step 1–4:** smoke на ветвление (gate on/off); реализация `check_sub` callback с fail-closed проверкой членства; прогон.
- [ ] **Step 5: Commit** — `feat(bot): тенант-воронка — опц. гейт подписки на канал тенанта`

---

## Phase 3 — Провижининг при покупке + параметризация Школы

### Task 9: Сид дефолт-шаблона воронки при активации подписки

**Files:**
- Modify: `admin-panel/db.py` — `seed_default_funnel(tid)` + врезка в `activate_subscription_from_payment` (~L3734-3793)
- Test: `scripts/leadmagnet_seed_smoke.py`

**Interfaces:**
- Consumes: `set_funnel_config` (или прямой KV-upsert), RLS.
- Produces: `seed_default_funnel(tid)` — идемпотентно засевает ПЛЕЙСХОЛДЕР-шаблон (`funnel_enabled=""` ВЫКЛ по умолчанию — чтобы недонастроенная воронка не запустилась без реквизитов; `welcome_text`/`data_purpose`/`leadmagnet_caption` = дефолты; `operator_*`/`leadmagnet_url` пустые → тенант заполняет). Вызывается в `activate_subscription_from_payment` ОДИН раз на нового тенанта.

- [ ] **Step 1:** smoke: вызвать `seed_default_funnel(tid)` дважды → ключи есть, идемпотентно (без дублей), `funnel_enabled` пуст (воронка не активна, пока тенант не заполнит реквизиты и не включит).
- [ ] **Step 2–3:** реализация + врезка (под существующей транзакцией активации, best-effort, не ломает активацию при сбое сида).
- [ ] **Step 4:** smoke зелёный; проверка, что активация Школы/существующих не дублирует.
- [ ] **Step 5: Commit** — `feat(panel): сид дефолт-воронки в tenant_settings при активации подписки`

### Task 10 (опц., поздняя): Параметризация Школы через тот же конфиг

**Files:** Modify `bot-telegram/handlers.py` (greeting/consent/deliver), `bot-telegram/texts.py`.

**Interfaces:** Consumes `get_funnel_config(tid)`. Produces: env-бот Школы читает `tenant_settings` Школы ПОВЕРХ хардкода/env (фолбэк = текущее поведение). Делать ТОЛЬКО после стабилизации тенант-пути; цель — единый код-путь и возможность редактировать Школу из той же формы.

- [ ] Перевести `texts.greeting`/согласие/`_deliver` на `get_funnel_config` с фолбэком на текущие env/хардкод; TG-регрессия Школы (smoke `c0_identity`/ручной /start) обязательна.
- [ ] **Commit** — `refactor(bot): Школа читает воронку из tenant_settings с фолбэком на env`

---

## Verification (общая, перед хендоффом владельцу)

- [ ] Все smoke зелёные на `risuy_dev`: `consent_text_smoke`, `funnel_config_smoke`, `leadmagnet_seed_smoke` + регрессия Школы (`c0_identity_smoke`, `rls_leads_messages_smoke`).
- [ ] `py_compile` всех тронутых `.py`; jinja-парс `lead_magnet.html`.
- [ ] Ручной чек панели: `/lead-magnet` GET→200, preview согласия корректен, POST валид/невалид.
- [ ] Ручной чек бота (на тест-тенант-боте с `funnel_enabled=1`): /start → согласие → выдача лид-магнита; у Школы /start без изменений.
- [ ] Деплой — push под «да» владельца; «код live» по `start_time`. Прод-`tenant_settings`-write (вкл. сид) — под «да».

---

## Self-Review (чеклист автора)

**1. Покрытие спеки:** приветствие ✓(T6), согласие 152-ФЗ структурное ✓(T1,T4), политика ✓(T1,T4), гейт-канал ✓(T8), лид-магнит link/file ✓(T5,T7), видео ✓(T7), сид при покупке ✓(T9), Школа не ломается ✓(фолбэк, T2/T6/T10), пер-тенант source-of-truth ✓(tenant_settings, T2/T3). Эскалация/прогрев/Лия/каналы — уже пер-тенант (вне scope, но `nurture`-якорь после выдачи учесть в T7: `mark_guide_sent` → `guide_sent_at`).

**2. Плейсхолдеры:** заменить «реализовать шаблон» в T1 на конкретную формулировку при выполнении (взять за основу `texts.py:4-19`). В T5 явно помечена развилка link-only-first.

**3. Согласованность типов:** `get_funnel_config` (бот) и `get_funnel_config_panel` (панель) — РАЗНЫЕ (первый отдаёт собранный cfg с готовым `consent_text`; второй — сырые поля для формы). `build_consent_text` — единая сигнатура в `shared/leadmagnet.py`, зовётся из обоих. Ключи `tenant_settings` — из одной таблицы контракта выше (не плодить синонимы).

**Открытый технический риск (решить в T5/T7):** получение tg `file_id` для файла-лид-магнита требует живого бота — поэтому первая итерация по умолчанию `kind=link`; файл — следующей под-волной. Гейт-канал тенанта требует, чтобы бот тенанта был админом канала (как у Школы) — задокументировать в подсказке формы T4/T8.
