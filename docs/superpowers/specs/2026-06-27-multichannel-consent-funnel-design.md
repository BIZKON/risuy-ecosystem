# Дизайн: мультиканальный сбор согласия 152-ФЗ + порт воронки лид-магнита на VK / MAX / Web

**Дата:** 2026-06-27 · **Статус:** на ревью владельца · **Автор:** ассистент (сессия risuy)
**Связано с:** Находка 2 adversarial-верификации пер-тенант согласий (сбор согласия был реализован только для Telegram).

---

## 1. Проблема и цель

Сбор согласия 152-ФЗ и единственный вызов `db.set_consent` живут **только на Telegram-пути**
(`multiplex.t_consent`, callback `consent_yes`). Каналы тенанта VK / MAX и веб-чат на сайте
персистят ПДн лида (имя, переписка, телефон) **без шага согласия** и **без записи в `consent_events`**:

- `_vk_respond` / `_max_respond` (`bot-telegram/multiplex.py`) идут `upsert_start → unsub → продажи → Лия`,
  шага согласия нет; `vk_driver` / `max_driver` не упоминают согласие.
- `bot._demo_chat` (веб-виджет demo-sandbox) персистит веб-лида (`upsert_start` + `log_message`),
  согласие не собирает.

**Итог:** тенант, собирающий лиды на VK / MAX (или платформа через веб-виджет), получает **пустую базу
согласий** не из-за бездействия, а потому что путь сбора физически отсутствует. Для 152-ФЗ — пробел в
точке сбора.

**Цель:** портировать пер-тенантную воронку лид-магнита (приветствие → согласие → телефон → гейт →
выдача) на VK / MAX и добавить явное согласие в веб-виджет, с записью каждого согласия в `consent_events`
(`channel = vk | max | web`). Сделать это канал-агностично, не трогая Telegram-путь и Школу.

## 2. Решения владельца (зафиксированы 2026-06-27)

| # | Развилка | Выбор |
|---|---|---|
| Объём | гейт-согласия / полный порт / уведомление | **Полный порт воронки** на VK/MAX |
| Когда требовать | реквизиты / тумблер / всегда | **При заполненных реквизитах оператора** |
| Веб-виджет | галочка / не трогать | **Галочка согласия в виджете** (demo-sandbox) |
| (а) Файл на VK/MAX | хранить байты / link-фолбэк | **Хранить байты → реальная файл-выдача** |
| (б) Гейт на VK/MAX | TG-only / per-channel | **Per-channel сразу** (VK `groups.isMember`; MAX-канал) |

## 3. Архитектура

**Подход: канал-адаптер + обобщение db-сеттеров.** Чистые хелперы `funnel.py`
(`start_text`, `next_after_consent`, `next_after_phone`, `deliver_plan`) уже канал-агностичны. Выносим
**I/O воронки** (отправка, клавиатуры, захват согласия/телефона, проверка подписки, выдача) за тонкий
per-channel адаптер — ровно как уже сделано в проекте для `triggers.TriggerCtx(messenger, …)` и
`escalation.escalate(messenger=…)`. Одна машина состояний на все каналы; per-channel — только адаптер.

Машина состояний — **DB-state-driven (без FSM)**, как сейчас: шаг лида выводится из флагов в `leads`
(`consent`, `phone`, `subscribed`, `status`), а не из FSM-хранилища. Это уже устойчиво к редеплою и не
требует storage на тенант-бот.

**Принцип изоляции:** адаптер имеет одну ответственность — канальный I/O воронки; машина состояний и
тексты от него не зависят и тестируются отдельно (чистые функции в `.venv-smoke`).

## 4. Компоненты

### 4.1. DB: обобщение состояния воронки на гибрид-идентичность (C0)

Сейчас `set_consent` (L110), `request_erase` (L139), `is_erase_requested` (L161), `set_name` (L170),
`set_phone` (L178), `set_subscribed` (L187), `get_lead_status` (L195), `mark_guide_sent` (L205) жёстко
ключатся на `where tg_user_id = $1`.

**Правка:** добавить параметр `messenger: str = "tg"` и ключевать через `db._user_col(messenger)`
(колонка по каналу: tg→tg_user_id, vk→vk_user_id, max→max_user_id, web→web_session_id) — точный паттерн
C0, уже применённый к `upsert_start` / `get_lead_id` / `log_message` и др. **TG-вызовы байт-в-байт не
меняются** (дефолт `messenger="tg"`). `set_consent` дополнительно уже принимает `channel` для строки
`consent_events` — теперь и lead-lookup станет канальным.

Инвариант (зеркало существующего C0-правила): новый канальный код ОБЯЗАН передавать `messenger` и явно
скоупить tenant (бот = owner, RLS обходит).

### 4.2. Адаптер воронки (`funnel.py`)

Тонкий протокол канального I/O. Async-шаги `start` / `after_consent` / `after_phone` / `go_to_gate` /
`deliver` принимают **адаптер** вместо `bot` / `message`. Методы адаптера:

- `send_text(text)`
- `send_consent(text, privacy_url)` — кнопка согласия + опц. кнопка «Политика»
- `ask_phone(text)` — TG: reply-кнопка `request_contact`; VK/MAX: просьба ввести номер текстом
- `ask_gate(text, channel_url)` — кнопка «подписаться» + «я подписался» (per-channel)
- `check_subscription(gate_cfg, external_id) -> bool` — TG: `get_chat_member`; VK: `groups.isMember`;
  MAX: членство в MAX-канале (см. 4.4)
- `deliver_text(text)` / `deliver_url(caption, url)` / `deliver_file(caption, product)` /
  `deliver_video_note(file_id)` — выдача (file/video — per-channel, см. 4.5)

Реализации: `TgFunnelChannel` (обёртка над текущим `messaging.*` + aiogram-клавиатурами — поведение
сохраняется), `VkFunnelChannel` (над `vk_driver.VKBot`), `MaxFunnelChannel` (над `max_driver.MAXBot`).
Чистые хелперы остаются модульными функциями (тестируемы без aiogram/aiohttp).

### 4.3. Захват шагов и гейтинг на VK / MAX

В `_vk_respond` / `_max_respond` (после `upsert_start` + unsub-проверки, ДО продаж/Лии) — **диспетчер
воронки**:

```
funnel_active = cfg.funnel_enabled AND requisites_filled(cfg)   # «при заполненных реквизитах»
if funnel_active AND lead.status != 'guide_sent':
    → шаг по DB-state лида (consent? phone? gate?) через адаптер канала
    → return (Лию/продажи на этот ход не зовём)
```

Состояние → шаг (общая логика, канал-агностично):
- нет `consent` → (повторно) показать приветствие+согласие; если входящее — нажатие кнопки согласия →
  `set_consent(channel) → after_consent`.
- `consent` есть, phone-step включён, `phone` пуст → входящий текст трактуем как телефон
  (валидация: ≥10 цифр) → `set_phone(channel) → after_phone`; иначе повтор просьбы.
- `consent` (+ phone готов / нет шага) + gate включён, `subscribed` пуст → проверка подписки (per-channel) →
  выдача / повтор просьбы.
- `status == 'guide_sent'` → диспетчер не вмешивается → дальше продажи/Лия как сейчас.

**Захват кнопки согласия per-channel:**
- VK: inline-кнопка с `payload={"cmd":"consent_yes"}` (как кнопка покупки) → приходит в `_vk_respond`
  параметром `payload`.
- MAX: inline callback-кнопка → приходит в `_max_callback` (есть `on_callback`) → ветка `consent_yes`.
- TG: без изменений (`t_consent`, callback `consent_yes`).

### 4.4. Per-channel гейт подписки

Настроенный сейчас гейт (`gate_channel_id` = TG-канал `-100…`, `gate_channel_url`) остаётся TG-гейтом.
Для VK/MAX вводим **отдельные поля гейта** в конструкторе (см. 4.7):

- **VK:** `groups.isMember(group_id, user_id)` через `vk_driver` (новый метод `is_member`). Цель —
  VK-сообщество (`vk_gate_group_id`; по умолчанию можно предложить сам `vk_group_id` бота). Fail-closed:
  ошибка/нет права → `False` (гейт держит), как в TG.
- **MAX:** членство в MAX-канале (`max_gate_chat_id`). ⚠️ Точный endpoint проверки подписки MAX
  **не верифицирован вживую** (dev.max.ru). Реализуем по доке с **fail-closed** дефолтом и логом сырого
  ответа (паттерн репо «spec-built → live-verify», как `recipient.chat_id` MAX). Если endpoint
  недоступен — гейт держит (лид не теряется, повтор после live-правки).

### 4.5. Файл-выдача через сохранённые байты (решение «а»)

Сейчас воркёр заливки `set_product_file_id` ставит `file_tg_id` и **обнуляет** `products.file` (bytea)
(`update products set file_tg_id=$2, file=null`). Значит файл-лид-магнит на VK/MAX переотправить нечем.

**Правка:** для продуктов `kind='lead_magnet'` **не обнулять** `products.file` (оставить байты). Объём
ограничен — один небольшой файл на тенанта. Тогда:
- TG-выдача — по `file_tg_id` (как сейчас).
- VK-выдача — `vk_driver.send_document(peer_id, bytes, filename, caption)` (паттерн C3-рассылок уже есть).
- MAX-выдача — `max_driver.send_media(chat_id, media_type, bytes, caption)` (паттерн C3 уже есть).

`get_funnel_product` дополнить выдачей байтов (или отдельный `get_funnel_product_bytes(product_id)` для
не-TG-каналов, чтобы не тянуть bytea в TG-путь без нужды). `link`-лид-магниты работают на всех каналах
без изменений. Видео-кружок (`video_note`) — TG-only, на VK/MAX шаг пропускается.

### 4.6. Веб-виджет: галочка согласия (решение Q3)

- `service-site/index.html` (+`styles.css`): перед первым сообщением в `#demo-chat` — чекбокс/уведомление
  согласия (текст из реквизитов demo-sandbox или платформенный дефолт) со ссылкой на Политику;
  «Отправить» неактивна, пока не отмечено. После согласия — флаг в `localStorage` (как `x10_demo_sid`),
  запросы несут `consent: true`.
- `bot._demo_chat`: при первом запросе сессии с `consent=true` → `db.set_consent(sid, True, channel="web")`
  (раз на сессию, идемпотентно). Без согласия — `ask_gateway` не зовём, мягкий ответ «отметьте согласие».
  Бэкенд-гард дублирует клиентский (нельзя обойти, дёрнув API напрямую).

### 4.7. Панель: поля per-channel гейта в конструкторе

В `shared/leadmagnet.FUNNEL_FIELDS` + форму `/lead-magnet` + `validate_funnel_fields` добавить (опц.,
условно-обязательные при включённом гейте соответствующего канала):
- `vk_gate_group_id` (VK-сообщество гейта; число)
- `max_gate_chat_id` (MAX-канал гейта; число)

TG-поля гейта (`gate_channel_id`, `gate_channel_url`) — без изменений. Валидация зеркалит существующую
(числовой id, http-url). Тексты согласия/Политики (`build_consent_text`/`build_privacy_policy`) — без
изменений (реквизиты те же).

## 5. Маппинг шагов по каналам

| Шаг | TG | VK | MAX | Web |
|---|---|---|---|---|
| Согласие | callback `consent_yes` | inline payload `consent_yes` | inline callback `consent_yes` | галочка в виджете |
| Телефон | `request_contact` | текст (≥10 цифр) | текст (≥10 цифр) | — (не собираем) |
| Гейт | `get_chat_member` | `groups.isMember` | MAX-канал (fail-closed, live-verify) | — |
| Выдача link | текст+url | текст+url | текст+url | — |
| Выдача file | `file_tg_id` | `send_document` (байты) | `send_media` (байты) | — |
| Видео-кружок | ✅ | пропуск | пропуск | — |
| Запись согласия | `consent_events(channel='tg')` | `…='vk'` | `…='max'` | `…='web'` |

## 6. 152-ФЗ корректность

- Согласие фиксируется атомарно (`set_consent`: `leads.consent` + строка `consent_events` одной
  транзакцией), `text_hash` = sha256 текста, на который дано согласие, `channel` = канал сбора.
- Отзыв (152-ФЗ ст.9 ч.2 — «в любой момент»): `request_erase` + `is_erase_requested` обобщаются на каналы
  (4.1). VK/MAX: детект слова-команды «отозвать согласие» / `/revoke` в `_vk_respond`/`_max_respond` →
  `request_erase(channel)`; после отзыва диспетчер/Лия молчат (гейт `is_erase_requested(channel)`). Web:
  отзыв вне v1 — веб-лид анонимен по `session_id`, отзыв через email-оператора из Политики.
- До согласия Лия/диалог не обрабатывает ПДн на VK/MAX (диспетчер перехватывает ход).

## 7. Тестирование

Смоуки в `.venv-smoke` (чистые функции, без сети; зеркало `funnel_flow`/`consent_*`):
- `multichannel_funnel_dispatch_smoke` — диспетчер шага по DB-state канал-агностично (fake-адаптер).
- `consent_capture_smoke` — парсинг payload-согласия VK/MAX (`{"cmd":"consent_yes"}`), web-флаг.
- `db_channel_setters_smoke` — обобщённые сеттеры: TG-регрессия (байт-в-байт) + vk/max через `_user_col`
  (на `risuy_dev`, owner-DSN).
- `phone_text_validation_smoke` — телефон текстом (≥10 цифр, парс/хэш = `phone_hash`).
- Веб-галочка — curl `_demo_chat` (`consent` true/false) + ручной тест владельца на сайте.

TG-регрессия обязательна: существующие `funnel_flow` / `consent_text` / `consent_events` должны остаться
зелёными.

## 8. Границы v1 / риски

- ⚠️ **MAX-гейт** (членство в MAX-канале) — API не верифицирован вживую; реализуем по доке с fail-closed
  + лог сырого ответа, точечная правка по живому тесту (как `recipient.chat_id` MAX).
- ⚠️ **End-to-end VK/MAX-доставка** не тестировалась вживую (нет боевых токенов) — db-слой и pure-хелперы
  смоучим, фактическая отправка подтверждается живым тестом владельца.
- Видео-кружок — TG-only by design.
- Веб-отзыв согласия (`/revoke` для web) — вне v1 (веб-лид анонимен; отзыв через email-оператора).
- Хранение байтов лид-магнита — +1 файл/тенант в БД; ретеншн-политика байтов лид-магнита (когда чистить)
  — отдельный мелкий вопрос (можно по архивации продукта).

## 9. Что НЕ трогаем

- **Telegram-путь** (`t_start`/`t_consent`/`t_contact`/`t_check_sub`, `messaging.*`) — байт-в-байт;
  обобщённые db-функции с дефолтом `messenger="tg"` дают прежний SQL.
- **Школа** (`handlers.py`, env-бот) — вне мультиплекса, воронка тенанта её не касается.
- Тексты согласия/Политики (`shared/leadmagnet`) — генератор не меняем.

## 10. Порядок реализации (для плана)

1. DB-обобщение сеттеров (`messenger` через `_user_col`) + TG-регресс-смоук.
2. Адаптер воронки + `TgFunnelChannel` (рефактор TG на адаптер без смены поведения) + смоук.
3. `VkFunnelChannel` + диспетчер в `_vk_respond` + захват payload-согласия + телефон-текст + детект отзыва.
4. `MaxFunnelChannel` + диспетчер в `_max_respond`/`_max_callback` + детект отзыва.
5. Файл-выдача: не обнулять байты lead_magnet + VK/MAX `send_document`/`send_media`.
6. Per-channel гейт: VK `groups.isMember`; MAX-канал (fail-closed).
7. Панель: поля `vk_gate_group_id`/`max_gate_chat_id` в конструкторе + валидация.
8. Веб-виджет: галочка + `_demo_chat` `set_consent(channel='web')`.
9. Смоуки зелёные (TG-регрессия + новые). Деплой/DDL — за владельцем под явное «да».
