# Дизайн: Слой C — VK и MAX как разговорные каналы

Статус: **ЧЕРНОВИК для ревью владельца.** Кода нет (как со Слоем B — сначала дизайн, потом
решения, потом реализация). Основано на исследовании 4 агентов (карта архитектуры + VK Bot API
+ MAX Bot API + модель данных) — см. ниже, всё привязано к файлам:строкам.

Цель: лиды могут общаться с ИИ-сотрудником тенанта не только в Telegram, но и в **VK** (сообщения
сообщества) и **MAX** (мессенджер VK). Триггеры/эскалация (Слой B) и метеринг переиспользуются.

---

## 0. Главный вывод исследования

**Архитектура уже наполовину канал-агностична.** Полностью нейтральны (переиспользуются БЕЗ
изменений): `ai.ask_ai` («мозг»), метеринг (`shared/metering.py`, `metering_worker`), биллинг/
кошелёк, vault (`shared/vault.py`), тенант-реестр + contextvar, AI-конфиг тенанта
(`get_tenant_ai_overrides`), ЛОГИКА триггеров/эскалации (`parse_escalation`, `match_stopwords`,
`build_intent_addendum`, `parse_trigger_markers`), единый нотификатор-бот (карточки менеджерам
всегда в TG-группу — это нормально, менеджеры сидят в Telegram).

Telegram-привязка сконцентрирована в ЧЕТЫРЁХ местах:
1. **Транспорт** — `multiplex.py` (lifecycle aiogram) + `messaging.py` (де-факто «канальный драйвер»).
2. **Идентичность лида** — сквозной `tg_user_id` (~50 мест `where tg_user_id = $1`).
3. **Ссылки на клиента** — `tg://user?id=` в карточках эскалации/триггеров.
4. **Панель «Диалоги»** — композер ручного ответа оператора + `outbox` (Telegram).

**Оба новых канала доступны из РФ-ЦОД НАПРЯМУЮ, без прокси** (в отличие от `api.telegram.org`,
которому нужен SOCKS5/IPv6-watchdog) — `api.vk.com` и `platform-api.max.ru` российские. Это
существенно упрощает Слой C относительно Telegram-плеча.

**Схема уже частично готова:** `leads.messenger` = свободный `text` дефолт `'tg'` (коммент
`tg | max`), `leads.max_user_id bigint` уже есть (задел), `phone_hash` для склейки каналов есть,
`TENANT_SECRET_KEYS` уже содержит `vk_token`.

---

## 1. Скрытый блокер №1 (читать первым)

**`messages.tg_user_id NOT NULL` (`db/schema_panel_ext.sql:57`) — Telegram-специфичен.** Пока он
обязателен, VK/MAX-сообщение НЕ запишется в `messages`. Это не «нет новой фичи», а **молчаливая
деградация ЧЕТЫРЁХ вещей** для VK/MAX-лида:
- переписка (история диалога),
- `per_message`-метеринг (считает `messages source='liya' direction='out'`),
- триггер `message_count` (`count_inbound_messages` по `tg_user_id`),
- AI-история (`get_ai_history` по `tg_user_id`).

→ **Обобщение идентичности в `messages` и `leads` — фундамент Слоя C, без него каналы «работают»,
но тихо ломают метеринг и историю.** Это определяет Фазу C0 (см. §6).

---

## 2. Архитектура: интерфейс `ChannelDriver` + обобщённая идентичность

### 2.1 ChannelDriver (транспорт-абстракция)
Ввести интерфейс канала (по одному драйверу на мессенджер). Методы:
- `run(tenant_id, token, on_update)` — поднять приёмный цикл (TG: aiogram polling; VK: Bots Long
  Poll; MAX: `GET /updates` long-poll) как asyncio-таску;
- `normalize(raw_update) -> IncomingMessage` — привести к единому виду `{channel, external_user_id,
  reply_to, text, attachments, external_message_id}`;
- `send_text(reply_to, text, *, rich)` — отправка (TG: `bot.send_message`; VK: `messages.send`
  +random_id; MAX: `POST /messages?chat_id=`);
- `send_typing(reply_to)`, `send_media(...)` (Фаза позже);
- `secret_key_name` (`telegram_bot_token` / `vk_token` / `max_bot_token`);
- `client_link(external_user_id) -> (url, label)` (TG `tg://user?id=`; VK `https://vk.com/id<id>`;
  MAX — по формату).

Тогда `multiplex.run/_reconcile` становится мультиканальным (перебирает `(тенант × доступные
каналы тенанта)`), а пайплайн сводится к: `normalize → triggers.handle → ai.ask_ai → driver.send_text
→ escalate`. Меняется только драйвер; `ai.ask_ai`, метеринг, vault, контекст, логика триггеров —
нетронуты.

`messaging.py` сегодня — это де-факто Telegram-драйвер. Рефактор: выделить из него транспорт-часть
(aiogram `Bot.send_*`, `file_id`, TG-HTML rich, token-bucket ~30/с, `TelegramRetryAfter`) в
`TelegramDriver`, а канал-нейтральное (лог в `messages`, выбор rich) — в общий слой.

### 2.2 Обобщённая идентичность лида — развилка (открытый вопрос §10)
- **Вариант А (колоночный, минимальный):** добавить `leads.vk_user_id bigint` (как уже есть
  `tg_user_id`/`max_user_id`). Коллизий между каналами НЕТ by design (каждый id в своей колонке,
  NULL не конфликтуют). Минус: ~50 мест `where tg_user_id=$1` нужно ветвить по каналу.
- **Вариант Б (нормализованный):** один `external_id text` + уникальность `(tenant_id, messenger,
  external_id)`. Чище для 2+ каналов; **обязателен дискриминатор `messenger` в unique-ключе** —
  иначе VK `from_id` и Telegram id (оба bigint, разные пространства) схлопнут двух людей в один лид.
- **Рекомендация — гибрид:** ввести helper `resolve_lead(messenger, external_id)` и параметризовать
  читающие функции по `(messenger, external_id)` ИЛИ по `lead_id`, а ПОД капотом пока оставить
  колонки (А). Это разводит «где хранится id» и «как его спрашивают»: новый код зовёт helper, а не
  сырой `where tg_user_id`. Миграцию на единый `external_id` (Б) можно сделать позже под капотом
  без переписывания вызовов.

---

## 3. MAX Bot API — спек (подтверждён докой dev.max.ru + навыком + SDK)

- **Хост:** `https://platform-api.max.ru`. **Доступен из РФ-ЦОД без прокси** (VK-инфра). ✅
- **Авторизация:** заголовок `Authorization: <token>` (без `Bearer`; query-токен официально убран).
- **Токен:** у системного `@MasterBot` внутри MAX (аналог BotFather). До 5 ботов на верифицированную
  организацию (business.max.ru, ИП/юрлицо РФ).
- **Приём (рекомендую long-poll):** `GET /updates?marker=&limit=&timeout=` — курсор `marker` (как
  aiogram offset): из ответа берёшь новый `marker`, передаёшь в следующий запрос. Webhook
  (`POST /subscriptions`, HTTPS-only с 25.05.2026) — взаимоисключающ с polling.
- **Формат update:** дискриминатор `update_type`. Текст: `message_created` →
  `message.body.text` / `message.sender.user_id` / `message.recipient.chat_id`. Старт: `bot_started`
  (`payload` из `?start=`). Кнопка: `message_callback`.
- **Отправка:** `POST /messages?chat_id=<id>` тело `{text, format:"html"}`. ⚠️ Адресат — в query
  (`chat_id`/`user_id`), не в теле. Rate ~30 rps; `403` = заблокировал бота.
- **⚠️ Важное отличие:** в личке `recipient.chat_id ≠ sender.user_id` (в TG совпадают). Отвечать
  ВСЕГДА на `recipient.chat_id`, идентичность лида = `sender.user_id`.
- **Python:** `maxapi` (aiogram-подобный: Bot/Dispatcher/декораторы) или сырой `aiohttp`-поллер.
- **Схема:** `leads.max_user_id bigint` уже есть. ⚠️ Составной `(tenant_id, max_user_id)` НЕ создан
  (только для tg) — `leads_max_user_id_key` глобален → один MAX-человек не сможет быть лидом у двух
  тенантов (нарушение изоляции). Перед активацией повторить expand-contract как для tg.
- **Проверить на месте:** механика `marker`, РФ-доступность (`curl platform-api.max.ru/me` с прода),
  лимит длины текста (держать ≤4096 до проверки).

## 4. VK Bot API — спек (подтверждён зеркалами доки + исходник vk_api)

- **Хост:** `https://api.vk.com/method/*`. **Доступен из РФ-ЦОД без прокси** (российский сервис). ✅
- **Токен сообщества:** Сообщество → Управление → Работа с API → Ключи доступа (право `messages`).
  Бессрочный. + включить: Сообщения, Возможности ботов, Long Poll API + событие `message_new`,
  зафиксировать МАКСИМАЛЬНУЮ версию Long Poll (иначе формат `object` плоский/старый).
- **Приём (рекомендую Bots Long Poll):** `groups.getLongPollServer(group_id, access_token, v)` →
  `{server, key, ts}`; цикл `GET {server}?act=a_check&key=&ts=&wait=25`; ответ → новый `ts` (ВСЕГДА
  продвигать) + `updates`. Обработка `failed`: `1`→новый ts; `2`→пере-getLongPollServer (ts оставить);
  `3`→пере-getLongPollServer полностью.
- **Событие `message_new`:** `object.message.{peer_id, from_id, text}`. Идентичность = `from_id`
  (>0 = юзер); отвечать на `peer_id` (в личке `peer_id == from_id`).
- **Отправка:** `messages.send(peer_id, message, random_id, access_token, v)`. ⚠️ **`random_id`
  обязателен и уникален на каждое сообщение** (`random.getrandbits(31)`) — иначе VK молча
  дедуплицирует и НЕ отправит. ⚠️ `v` (версия API) обязателен (актуальная `5.x`).
- **Ограничение:** пользователь должен «Разрешить сообщения сообществу» — для реактивного бота не
  проблема (юзер сам пишет → право ответить даётся). Проактивные рассылки — окно ~24ч + согласие
  (отдельная волна, см. §6 C3).
- **Python:** `vkbottle` (async, держит LP-цикл и `failed` сам) или тонкий `aiohttp`-слой.
- **Схема:** нужна `leads.vk_user_id bigint` + уникальность `(tenant_id, vk_user_id)`.
  `TENANT_SECRET_KEYS` уже содержит `vk_token`.
- **Проверить на месте:** актуальная `v`, РФ-доступность (`curl api.vk.com/method/utils.getServerTime`).

---

## 5. Что переиспользуется БЕЗ изменений (общий слой VK/MAX)

- **`ai.ask_ai`** и весь AI-слой (`_ask_ai_backend`, `ask_gateway`, `ask_agent_openai`, вырезка
  маркеров `[[ESCALATE]]`/`[[TRIGGER:N]]`) — принимает text+cfg+history, ноль ссылок на Telegram.
- **Метеринг:** `cloud_ai`-снапшоты (`tenant_agents`, used_tokens) — по tenant_id; `per_message`
  (`source='liya' direction='out'`) — канал-нейтрален, заработает для VK/MAX автоматически, КАК
  ТОЛЬКО их ответы пишутся в `messages` (зависит от §1).
- **Vault:** `schema_vault.sql`, `shared/vault.py`, `db.get_tenant_secret` — **изменений схемы НЕТ**,
  только новые `key_name` (`max_bot_token`; `vk_token` уже есть). Панель «Ключи» рендерит из
  `TENANT_SECRET_KEYS` автоматически.
- **AI-конфиг тенанта** (`get_tenant_ai_overrides`), тенант-реестр, contextvar, RLS-модель,
  reconcile-каркас мультиплекса (структурно).
- **Логика триггеров/эскалации** (парсинг, matching, аддендум) и **транспорт уведомления
  менеджеру** (единый нотификатор в TG-группу) — независимы от канала лида.

---

## 6. Фазировка (Слой C — большой, режем на волны)

- **C0 — Фундамент (обобщение идентичности + драйвер-абстракция).** Снять `messages.tg_user_id
  NOT NULL` → обобщить адрес сообщения (`messenger` + `external_user_id`/`external_message_id`),
  ввести `resolve_lead(messenger, external_id)` helper, выделить `ChannelDriver` из `messaging.py`.
  БЕЗ нового канала — рефактор + миграция (expand-contract, risuy_dev → прод). Доказать смоуком,
  что Telegram-путь не сломан.
- **C1 — MAX** (проще: РФ-native, простой HTTP, `max_user_id` есть). MAX-драйвер (long-poll поллер +
  send) в мультиплексе; MAX-лид общается с Лией; триггеры/эскалация работают; карточки линкуют на
  MAX. + составной ключ `(tenant_id, max_user_id)`. + `max_bot_token` в vault/панель «Ключи».
- **C2 — VK** (VK-драйвер: Bots Long Poll + `messages.send`+random_id; `failed`-обработка). +
  `leads.vk_user_id` + уникальность. `vk_token` уже в панели.
- **C3 — Панель «Диалоги» + рассылки** (опционально, позже): композер ручного ответа оператора
  VK/MAX-лиду (сейчас гейт `lead.tg_user_id is not none`), канальный `outbox`/`worker`-дренаж,
  VK/MAX-рассылки (`BROADCAST_MESSENGERS`, `_AUDIENCE_WHERE`). Медиа (вложения) — тоже сюда.

### РЕШЕНИЕ владельца: self-serve через РОЛЕВОЙ раздел «Каналы»
Клиент подключает ВСЕ каналы сам — расширяем СУЩЕСТВУЮЩИЙ раздел «Каналы» (не «Ключи»), сделав
его ролевым:
- **Платформа (Школа, is_platform):** текущий вид «Каналы» (атрибуция по `source` + deep-link'и +
  персона-на-канал → глобальный app_settings) — БЕЗ изменений, остаётся под платформой.
- **Клиент (operator):** НОВЫЙ вид — карточки подключения Telegram/VK/MAX: поле токена → tenant-vault
  (`db.upsert_tenant_secret`, как «Ключи», но по-канально), статус «бот подключён/активен», подсказка
  «как получить токен». ТОЛЬКО tenant-scoped, никаких глобальных app_settings (анти-кросс-тенант).
- Один пункт меню «Каналы»; для клиента — un-gate (сейчас платформенный, коммит 97c74a2); route
  рендерит client-view vs platform-view по роли, глобально-пишущие POST остаются под is_platform.
- Зависимость: карточка хранит токен сразу (vault готов), но канал «оживает» после драйверов
  (C0-код → VK/MAX). Порядок: C0-код → VK/MAX-драйверы → ролевой раздел «Каналы».
- Внешняя настройка платформы (VK-сообщество+LongPoll; MAX org-верификация) — на стороне клиента,
  панель только принимает токен; в карточке — гайд «как получить».

**Рекомендация:** начать с **C0 → C1 (MAX)** как первый рабочий не-Telegram канал (минимум риска:
RF-native, схема-задел готова), затем C2 (VK). C3 — после, по запросу.

---

## 7. Изменения схемы (сводка)

| Таблица | Изменение | Фаза |
|---|---|---|
| `messages` | `tg_user_id` → nullable + `messenger`/`external_user_id`/`external_message_id` (или составной адрес). **Блокер №1.** | C0 |
| `leads` | helper-резолв по `(messenger, external_id)`; составной `(tenant_id, max_user_id)` (expand-contract, как сделали для tg); `vk_user_id bigint` + `(tenant_id, vk_user_id)` | C0/C1/C2 |
| `tenant_secrets` | изменений НЕТ; только `TENANT_SECRET_KEYS += max_bot_token` | C1 |
| `outbox`, `broadcasts`/`broadcast_recipients` | обобщить адрес (для ручного ответа/рассылок) | C3 |

Все DDL — `twc-migrate.sh` owner-DSN, СНАЧАЛА risuy_dev, expand-contract (NOT NULL снимаем до кода,
новые NOT NULL — после). RLS наследуется бесплатно (политики по `app.tenant_id`, не по мессенджеру).

---

## 8. Telegram-специфичное → требует абстракции (чек-лист реализации)

1. **Отправка** — весь `messaging.py` (aiogram `Bot.send_*`, `file_id`, TG-HTML rich, token-bucket,
   `TelegramRetryAfter/BadRequest`) → `TelegramDriver`; добавить `VKDriver`/`MAXDriver`.
2. **Lifecycle** — `multiplex._start_tenant/_run_tenant` (aiogram Dispatcher/polling) → per-channel
   приёмный цикл; `_reconcile` перебирает `(тенант × канал)`, читает токен по `secret_key_name`.
3. **Идентичность** — `escalate`/`claim_lead_escalation`/`get_lead_id`/`count_inbound_messages`/
   `pause_lead`/`log_message`/`upsert_start`/`get_ai_history` — переключить с `tg_user_id` на
   `(messenger, external_id)`/`lead_id`.
4. **Ссылка на клиента в карточках** — `tg://user?id=` в `escalation.format_card`/
   `triggers.format_trigger_card` → `driver.client_link()` (VK `vk.com/id<id>`, MAX-формат).
5. **Панель «Диалоги»** (C3) — гейт `_chat.html:24`, плейсхолдеры «…в Telegram», reply→outbox(tg).

---

## 9. Риски

- 🔴 **`messages.tg_user_id NOT NULL` — молчаливая деградация** (§1). Главный риск: каналы «вроде
  работают», но метеринг/история/счётчик-триггер тихо отваливаются. Закрывается в C0.
- 🔴 **Уникальность MAX/VK не дотянута до tenant-scope** — `leads_max_user_id_key` глобален → один
  человек не сможет быть лидом у двух тенантов (нарушение изоляции воронок, для tg уже починено).
  Повторить expand-contract `migrate_lead_tenant_key.sql` для каждого канала.
- 🟡 **Коллизии id между каналами** — при переходе на единый `external_id` обязателен дискриминатор
  `messenger` в unique-ключе (VK `from_id` и TG id оба bigint). В колоночном варианте — нет риска.
- 🟡 **Процедурный RLS-риск нового кода** — бот ходит под owner (RLS обходит, ENABLE не FORCE),
  полагаясь на ЯВНЫЙ `where tenant_id = tenant_id()`. Новый VK/MAX-код ОБЯЗАН так же явно скоупить
  тенанта — пропуск = утечка между тенантами, которую база не поймает.
- 🟡 **VK `random_id`** — забыть/повторить → VK молча не отправит. **MAX `recipient.chat_id ≠
  user_id`** в личке — отвечать не туда. Оба покрываются драйвером + смоуком.
- 🟢 **Не путать понятия:** `leads.messenger` (транспорт, расширяем) ≠ раздел «Каналы»/`leads.source`
  (площадка привлечения, там vk/max уже как метка) ≠ `account_identities.provider='vk'` (вход
  ВЛАДЕЛЬЦА в панель через VK ID, другая таблица). Vault-токены живут в разделе «Ключи», не «Каналы».

---

## 10. Открытые вопросы владельцу

1. **Какой канал первым** — MAX (рекомендую: RF-native, схема-задел готова) или VK, или оба сразу?
2. **Модель идентичности** — колоночная (А, минимум) / обобщённая `(messenger, external_id)` (Б,
   чище) / гибрид с helper-резолвом (рекомендую)?
3. **Приём** — long-poll для обоих (рекомендую: RF-friendly, как aiogram) или webhook?
4. **Скоуп per channel сейчас** — только ИИ-разговор + триггеры + эскалация (C1/C2), а «Диалоги»-
   композер + рассылки + медиа (C3) — позже? (рекомендую да)
5. **Провижининг токенов** — владельцем (vault «Ключи», как Telegram-боты тенантов) или self-serve?
6. **MAX-бот/VK-сообщество** — у кого аккаунты (нужна верификация организации в business.max.ru для
   MAX; сообщество ВК с включённым Long Poll для VK)? Это операционная предпосылка, не код.

---

## 11. Оценка объёма
- **C0 (фундамент):** средне-крупно — миграция `messages`/`leads` (expand-contract) + helper-резолв
  + выделение `ChannelDriver`/`TelegramDriver` из `messaging.py` + смоук «Telegram не сломан».
- **C1 (MAX):** средне — MAX-драйвер (поллер+send, ~150–250 строк aiohttp) + проводка в мультиплекс
  + составной ключ + панель «Ключи» (1 строка) + смоук.
- **C2 (VK):** средне — VK-драйвер (Long Poll + `messages.send`+random_id+failed) + `vk_user_id` + смоук.
- **C3 (панель/рассылки/медиа):** крупно, отдельная волна.

Деплой каждой фазы — push в main (+ DDL перед кодом), с явного «да» владельца.
