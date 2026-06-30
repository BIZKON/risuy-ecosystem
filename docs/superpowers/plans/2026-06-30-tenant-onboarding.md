# Онбординг тенанта — обучение по каждому разделу + getting-started — Spec & Plan

> Источник: фоновый Workflow `tenant-onboarding-audit` (research best-practices + аудит 16 tenant-разделов + синтез). Этот документ фиксирует решение и план реализации.

**Goal:** нетехнический РФ-тенант понимает каждый раздел панели и доходит до «первой ценности» (агент ответил клиенту его данными) за ~15 минут — через server-rendered онбординг без навязчивых JS-туров.

**Architecture:** 5 механизмов на УЖЕ существующих паттернах (`_macros.html`, `styles.css`, `provision-banner`, KV `tenant_settings`), **без нового DDL** (флаги — новые ключи в `tenant_settings`). Никакого JS-движка туров (CSP запрещает inline-JS; стек — Jinja2 + 3 мелких vanilla-файла).

**Research-вывод (NN/g, Appcues, Userpilot, Pendo, Intercom, RU-кейс Habr активация 9%→23%):** для server-rendered + нетехнической аудитории выигрывают **pull-помощь по требованию**, персистентный **чеклист 3-5 шагов** с прогресс-баром, **инструктивные empty-states**, **progressive disclosure** (`<details>`). Push-туры / coach-marks / micro-surveys — fit=low (требуют JS), отвергнуты, и нарушают принцип владельца «всё видно, ничего скрытого».

## Механизмы (все fit=high под Jinja/min-JS)
1. **Getting-started чеклист (5 шагов)** — постоянный блок вверху Дашборда (стиль `provision-banner`); шаги вычисляются из РЕАЛЬНЫХ данных (не ручные галочки); прогресс-бар = inline-width CSS; dismissible.
2. **Welcome-карточка** при первом входе (на Дашборде, не full-screen модал) + 1 вопрос-ниша (radio) для подбора шаблона.
3. **Инструктивные empty-states** в каждом разделе с одним явным CTA (расширяем `.empty/.empty__title/.empty__text`).
4. **`help_card(...)`** — переиспользуемый dismissible-макрос «как это работает» (1-2 фразы + пример + «Подробнее»); закрытие = POST-флаг в `tenant_settings`.
5. **Inline-hint** (`.field__hint`, уже есть) + нативные `<details>` для продвинутого.

## Onboarding flow (первая ценность)
Шаг 0 welcome (+ниша) → **1** Подключить бота (Ключи/Каналы) → **2** Собрать ИИ-команду → **3** Загрузить базу знаний → **4** Включить воронку/лид-магнит → **5 (aha)** Проверить агента вживую (Диалоги). Done-сигналы (реальные): `list_tenant_secrets`/привязка канала; `list_team_agents` непуст; `kb_list_documents` непуст; `funnel_enabled`; `dashboard_counts.total>0`. После 5/5 — свернуть в «Настройка завершена ✓», точка возврата остаётся.

## Per-section (приоритет)
- **Core (онбординг-путь):** dashboard (носитель чеклиста+welcome), channels (шаг 1 + «где взять токен у @BotFather»), my_team (шаг 2 + предзасев шаблона), knowledge (шаг 3 + скрыть жаргон embeddings/chunks, демо-шаблон), lead_magnet (шаг 4, цель-карточка над `<details>`), dialogs (шаг 5 aha + режимы авто/ручное).
- **Не-core (help_card + усиленный empty-state + inline-hint):** nurture, triggers (вне core, «по желанию»), broadcasts, products, payments, subscription, usage, keys, data_protection (кнопка поддержки в тупик «кабинет не привязан»), account.
- Полные `copy_direction_ru` и `placement` по каждому разделу — в выводе Workflow (синтез `per_section`); переносятся в шаблоны при импле.

## Implementation plan (9 задач, из синтеза)
1. **[S]** `admin-panel/db.py`: `get_onboarding_flags(tid)` + `set_onboarding_flag(tid,key,value,...)` на паттерне `set_funnel_config` (on conflict do update, RLS, аудит). Ключи: `welcome_seen, onboarding_niche, onboarding_dismissed, help_dismissed__<section>`. **Без DDL.**
2. **[M]** `compute_onboarding_state(tid)` (db.py или новый `admin-panel/onboarding.py`): 5 шагов из реальных сигналов → `[{key,label,href,done,cta}], done_count, total, pct`.
3. **[M]** `_macros.html` + `styles.css`: макросы `help_card(...)`, `onboarding_checklist(state, csrf)`, `welcome_card(niches, csrf)`. Ноль JS (CSS-ширина, POST-формы, нативный `<details>`/`title`).
4. **[S]** `app.py` POST-эндпоинты с CSRF+PRG: `/onboarding/welcome`, `/onboarding/dismiss`, `/onboarding/dismiss-help`.
5. **[M]** `dashboard()` + `dashboard.html`: welcome_card (если `!welcome_seen`) + checklist (если `!onboarding_dismissed` и `!is_platform`) над «Воронкой» под provision-banner; расшифровка этапов воронки/конверсии.
6. **[L, ГЕЙТ]** Сидинг шаблонов по нише (агент + демо-документ KB с пометкой «Пример») — `shared/onboarding_templates.py` + `upsert_team_agent`/`kb_insert_document`, идемпотентно. ⚠️ **Гейт владельца** (см. Open Q2 — RU-кейс советует не автоматизировать рано).
7. **[L]** Empty-states + help_card по разделам (core сначала: dialogs/channels/my_team/knowledge/lead_magnet/nurture; затем остальные).
8. **[M]** Переформулировать ошибки/системные сообщения в задачные RU; ссылки на поддержку (`config.SERVICE_CONTACT_URL`) в тупиковые empty-states; приглушить инфра-ошибки (embedder/vault) для роли тенанта.
9. **[S]** CSS онбординга в `styles.css` (`.onb-checklist/.onb-progress/.help-card`), переиспользуя токены provision-banner; проверить мобильную раскладку.

**Тесты:** render-смоуки макросов (как `platform_team_access_smoke`); DB-смоук флагов онбординга на `risuy_dev`; 3-линзовое ревью перед коммитом. **Push/деплой — по «да» владельца.**

## Open Questions (за владельцем)
1. Порядок/обязательность 5 шагов: бот→команда→база→воронка→тест — ок? Что «по желанию» (триггеры — точно вне core)?
2. 🔴 **Сидинг демо-шаблонов по нише — сейчас или ПОЗЖЕ** (RU-кейс: не автоматизировать до 10-20 ручных онбордингов)? Если сейчас — ниши первого релиза (салон/магазин/услуги/общепит/другое)?
3. Welcome+ниша — карточка на дашборде (реком.) или отдельная страница? Можно ли пропустить без ниши (тогда без сидинга)?
4. Aha-сигнал шага 5 — `dashboard_counts.total>0` (просто) или точный «первый ответ агента» (доп. запрос)?
5. Аналитика онбординга (completion/drop-off) — серверные счётчики/аудит на v1 (PostHog в risuy нет)?
6. Инфра-ошибки (EMBEDDER_URL/VAULT) тенанту — скрывать/заменять на «идёт подготовка» или показывать (принцип «всё видно»)?
7. Точка возврата к справке после закрытия — иконка ?/пункт сайдбара «Справка»/Профиль?
8. Onboarding только для роли тенанта (не is_platform) — подтверждаем?

## Scope-рекомендация для реализации
**Core-first:** задачи 1-5 + 9 (движок чеклиста/welcome/макросы/CSS + дашборд) → задача 7 по core-разделам → затем не-core и задача 8. Задача 6 (сидинг) — отдельно, по гейту Q2. Каждая пачка: смоук + 3-линзовое ревью + коммит.
