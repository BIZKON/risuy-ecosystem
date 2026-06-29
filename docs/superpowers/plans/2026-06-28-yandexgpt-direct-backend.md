> ⚠️ **ПЕРЕСМОТРЕН 2026-06-29 → см. `docs/superpowers/plans/2026-06-29-private-rf-llm-backend.md`.**
> Решение владельца: разворот с YandexGPT-direct на **self-host Gemma (претендент №1)** — узкое горлышко
> оказалось в КОНТЕКСТЕ (YandexGPT на Timeweb = 32k вход; промпт Лии = 20k символов; Yandex слабо держит
> длинные промпты/сессии). Gemma 4 = 256k контекста + Apache 2.0 + нативный tool-calling + self-host (чище
> по 152-ФЗ). YandexGPT/GigaChat — демотированы в запасной managed-вариант. Этот док — исторический.

# 🔴 ПРИОРИТЕТНЫЙ ПЛАН ИЗМЕНЕНИЙ — #2: YandexGPT-direct backend + тумблер Timeweb↔Yandex (ПЕРЕСМОТРЕН → Gemma self-host)

> **Статус:** ПРИОРИТЕТ №1 в очереди изменений (после live `#1` обезличенной выгрузки, `2f3110e`).
> **Дизайн НЕ залочен** — сначала закрыть блок «Открытые вопросы» (нужен ввод владельца по Yandex Cloud),
> затем brainstorming-подтверждение → этот план финализируется → реализация (TDD, как `#1`).
> Часть плана 152-ФЗ: `#1 выгрузка (✅ live)` → **`#2` РФ-резидентный бэкенд (этот док)** → `#3` «Справка о ПД».

## Зачем (правовая цель)
Timeweb официально подтвердил: AI Gateway и cloud-ai агенты — **посредник к внешним провайдерам**, трафик идёт
через юр.лицо Timeweb в **Казахстане**, ZDR включить нельзя, провайдер может обучаться (см.
`docs/152fz-legal-review-checklist.md` C.1). → декларация Политики «трансгран не осуществляется» для текущего
пути **ложна**. **YandexGPT напрямую в Yandex Cloud** = РФ-оператор ПДн, инференс и хранение в РФ → честно
заявить «трансграничной передачи нет» (после live `#2`). ⚠️ YandexGPT **ЧЕРЕЗ Timeweb Gateway НЕ годится** —
тот же казахстанский хоп; нужен **прямой** вызов `llm.api.cloud.yandex.net`.

## Текущая архитектура (по коду — точки интеграции)
- **Диспетчер:** `bot-telegram/ai.py::_ask_ai_backend(text, parent_message_id, cfg, *, history)` (~L399) ветвит
  по `cfg.get("backend")`: `"gateway"` → `ask_gateway`; иначе → `cloud_ai` (OpenAI-эндпоинт агента) с фолбэком
  на нативный `/call` (`ask_liya`).
- **Конфиг бэкенда:** `cfg` собирает `db.get_ai_overrides` (app_settings) — поля `backend`, `model`,
  `gateway_base_url`, `agent_id`, `system_prompt`, `fallback`. (⚠️ Точное место хранения тумблера — открытый
  вопрос Q5: глобально app_settings vs per-tenant tenant_settings; демо использует tenant_settings.)
- **Эталон для нового бэкенда — `ask_gateway` (~L209):** OpenAI-совместимый `POST {base}/chat/completions`,
  `Authorization: Bearer <token>`, `messages[]` через `_build_chat_messages(system, history, text)`,
  **PII fail-closed** (`pii.redact_messages` → при сбое маскировки сырьё НЕ шлём → фолбэк), на ответе
  `pii.unmask_text`. Возвращает `(answer, meta|None)`, `meta={model,usage,request_id}` для метеринга.
- **Метеринг:** `schedule_gateway_capture(meta)` → `_capture_gateway_usage` → себестоимость по `model_prices`
  → `charge_usage`. (⚠️ Нет цены модели в `model_prices` → ERROR-спам; для Yandex нужны строки цен.)
- **Маркеры:** `ask_ai` (обёртка над `_ask_ai_backend`) вырезает `[[ESCALATE]]`/`[[TRIGGER:N]]` — новый бэкенд
  это наследует автоматически (вырезка после диспетчера).

## Предлагаемый дизайн (черновик — уточнить по открытым вопросам)
1. **Новый бэкенд `yandexgpt`** — ветка в `_ask_ai_backend`: `if cfg.get("backend") == "yandexgpt": answer, meta
   = await ask_yandexgpt(...)` + `schedule_yandex_capture(meta)`. Минимум нового кода — копия структуры
   `ask_gateway`.
2. **`ask_yandexgpt(text, *, model, system_prompt, fallback, history)`** — **рекомендация: OpenAI-совместимый
   эндпоинт Yandex** (`https://llm.api.cloud.yandex.net/v1/chat/completions`, ⚠️ **сверить с актуальной докой
   Yandex Cloud на момент реализации**). Это даёт переиспользование `_build_chat_messages` + `pii.redact_messages`
   + usage-метеринг 1:1 с `ask_gateway`. Модель: `gpt://<folder_id>/<model>/latest`. Auth: `Authorization:
   Api-Key <key>` (+ при необходимости `x-folder-id`). Альтернатива — нативный `foundationModels/v1/completion`
   (другой формат `messages:[{role,text}]`, без usage в OpenAI-форме) — сложнее, НЕ рекомендуется для v1.
3. **Конфиг/секреты:** `config.YANDEX_API_KEY` + `config.YANDEX_FOLDER_ID` (env, секрет; добавить в env панели/бота
   и `config.py` обоих, как `AI_GATEWAY_TOKEN`). ⚠️ Новый env-ключ воркера → добавить в `readBindingsFromEnv`
   (если есть `bindings.ts`-аналог) и в `docker compose --env-file .env.production`.
4. **Тумблер в админке Timeweb↔Yandex** — раздел «ИИ-агенты»/настройки: переключатель `backend` между текущим
   путём и `yandexgpt`, «одной кнопкой». Текущий путь **НЕ трогаем** (остаётся запасным/по выбору). Скоуп тумблера —
   **открытый вопрос Q5**.
5. **Метеринг:** добавить цены YandexGPT в `model_prices` (вход/выход ₽/1k токенов из ЛК Yandex) → `charge_usage`
   работает как у gateway. Заодно закрыть бэклог по отсутствующим ценам (ERROR-спам).
6. **PII-маскировка — ОСТАВИТЬ** (defense-in-depth), даже для РФ-бэкенда. Резидентность (а не маскировка) снимает
   трансгран; маскировка остаётся как доп-слой (handoff).
7. **🔴 ФОЛБЭК НЕ ДОЛЖЕН ПЕРЕСЕКАТЬ ГРАНИЦУ.** При сбое `yandexgpt` фолбэкать **только в мягкий текст** (как
   `ask_gateway` → `_FALLBACK`), **НИКОГДА не падать на `gateway`/cloud-ai** (Казахстан) — иначе при боевом тенанте
   ПДн уйдут за рубеж в обход всей затеи. Это инвариант, проверить тестом.

## 🔴 ОТКРЫТЫЕ ВОПРОСЫ (нужен ввод владельца ДО реализации)
- **Q1. Аккаунт Yandex Cloud:** есть ли облако/каталог (folder), включён ли биллинг? `folder_id`?
- **Q2. Auth:** статический **API-key** (проще, долгоживущий, рекомендуется для v1) vs **IAM-токен**
  (ротация ~12ч, нужен сервис-аккаунт + refresh)? Рекомендую API-key.
- **Q3. Эндпоинт:** OpenAI-совместимый (рекомендуется, переиспользует код) vs нативный `foundationModels`?
- **Q4. Модель:** `yandexgpt-lite` (дешевле/быстрее) vs `yandexgpt` (pro, качество) vs `/rc`? Стартовая?
- **Q5. Скоуп тумблера:** глобально (app_settings, один backend на всю платформу) vs per-tenant
  (tenant_settings, каждый тенант сам)? Для 152-ФЗ логичен глобальный дефолт + возможный per-tenant override.
- **Q6. Логирование/обучение Yandex:** нужен ли договор/настройка отключения логов запросов в Yandex Cloud для
  чистоты «не обучаются на наших данных»? (Yandex — РФ-оператор, но условия по логам уточнить.)
- **Q7. Себестоимость:** актуальные цены YandexGPT (вход/выход) для `model_prices`.

## Задачи (после залочивания дизайна — TDD, как #1)
> Детализируются в финальной версии плана ПОСЛЕ ответов на Q1–Q7. Ориентировочная декомпозиция:
1. `config.py` (бот + панель): `YANDEX_API_KEY`/`YANDEX_FOLDER_ID` (Zod/валидация); env в деплой.
2. `bot-telegram/ai.py`: `ask_yandexgpt` (копия `ask_gateway` под Yandex endpoint/auth/model-URI; mask/unmask;
   usage→meta) + ветка в `_ask_ai_backend` + `schedule_yandex_capture` (или переиспользовать gateway-capture с
   model-префиксом). Тесты: смоук как у gateway (mock HTTP) + инвариант «фолбэк не пересекает границу».
3. `model_prices`: строки цен YandexGPT (миграция данных/сид) — закрыть ERROR-спам.
4. Панель: тумблер backend (Timeweb↔Yandex) в разделе настроек ИИ; запись в app_settings/tenant_settings (по Q5);
   аудит смены backend. Смоук панели.
5. Документация: обновить `docs/152fz-legal-review-checklist.md` — после live `#2` декларация «трансгран нет»
   становится правомерной для Yandex-пути; зафиксировать.
6. Деплой: env на бот+панель, рестарт ТОЛЬКО `docker compose --env-file .env.production` (грабля handoff),
   live-проверка реальным диалогом + что usage метрируется.

## Вне #2
`#3` «Справка о ПД» + публичная страница; GigaChat как альтернативный РФ-бэкенд; durable-outbox метеринга;
полное отключение Timeweb-gateway (остаётся запасным).

## Риски / граблии (из handoff)
- Боевой тенант НЕ включать (`funnel_enabled`), пока `#2` не live (правило handoff).
- `api.telegram.org`/деплой-граблии Timeweb (IPv6, rolling-conflict) — не относятся к Yandex, но деплой воркера
  по процедуре `docker compose --env-file`.
- Не залогировать `YANDEX_API_KEY` (как `AI_GATEWAY_TOKEN` — секрет, не в логи/не в аудит).
