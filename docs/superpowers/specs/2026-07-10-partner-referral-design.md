# Дизайн: Партнёрская реферальная подсистема (Спек 2 / Кусок C)

**Дата:** 2026-07-10
**Статус:** дизайн (спека), реализация после плана
**Автор:** brainstorming-сессия (владелец @profysales)
**Тип:** новая фича, subagent-driven реализация после плана
**Опора:** Спек 1 (A+B) — LIVE (`b7cf58b`); переиспользует надёжный слой доставки `platform_notify`.

---

## 1. Контекст и цель

У владельца есть партнёр, который **уже приводит тенантов**. Нужно дать это как систему:
владелец заводит партнёра одной формой; система выдаёт партнёру готовую публичную
реф-ссылку; клиент по ссылке попадает в бота, называет компанию — и бот сам создаёт
тенанта + бриф-ссылку, **атрибутируя тенанта партнёру**; партнёр и владелец получают
Telegram-уведомления о новом тенанте и о прохождении брифа.

Модель зафиксирована в §10 спеки `2026-07-10-new-tenant-brief-owner-notify-design.md`.
Данный документ дорешает открытые вопросы и фиксирует реализацию.

### Что уже есть (опора)
- **`platform_notify`** — надёжная НЕ-лидовая очередь уведомлений (owner + партнёры): claim
  (SKIP LOCKED, claimed_at), `enqueue_platform_notify(chat_id, text)`, дренаж воркером
  через ОСНОВНОЙ бот, ретрай транзиента (`release_platform_notify`) + reclaim застрявших
  (`reclaim_stuck_platform_notify`), `_PERMANENT` vs транзиент. **Уведомления best-effort,
  никогда не ломают основной поток** (паттерн Critical-фикса Спека 1).
- **Публичные лендинги бота** по образцу `_club_landing` (`GET /club/{slug}`, bot.py):
  HTML-страница без авторизации + deep-link `t.me/{bot}?start=…`. `_BOT_USERNAME` резолвится
  при старте. ⚠️ `GET /r/{token}` УЖЕ занят трекинг-редиректом лид-магнита — реф-лендинг
  использует НОВЫЙ путь `/p/{code}`.
- **Deep-link `?start=…`** парсится в `cmd_start` (handlers.py); входящий source валидируется
  против набора (handlers.py «править ВСЕ три места»). Голый `/start` = холодный лид
  (source='other') — реф-ветку НЕЛЬЗЯ вплетать в голый /start (урок `/whoami` Спека 1).
- **Создание тенанта+брифа:** `db.create_tenant_admin(name)` (status='active', slug) +
  `db.create_tenant_brief(tid)` (token, status='pending') — сейчас ТОЛЬКО панель.
- **`app_settings`** — owner_chat_id (событие владельцу параллельно).

---

## 2. Зафиксированные решения (брейншторм)

| Вопрос | Решение |
|---|---|
| Публичная реф-ссылка | **Брендовая страница на боте `/p/{ref_code}`** (образец `/club/{slug}`). `/r/` занят. |
| Анти-абьюз ref-потока | **Лёгкий гард:** дедуп по TG-юзеру (активный реф-тенант с pending-брифом → отдаём его же ссылку) + per-user rate-limit на создание. Тенант создаётся сразу (без HumanGate). |
| Срок жизни ref-кода | **Бессрочный, отзываемый статусом** партнёра (active/disabled). Без TTL. |
| Отчётность v1 | **Полный отчёт:** страница партнёра со списком его тенантов (компания, дата, статус брифа) + счётчики. |

**Инвариант (наследуется от B):** уведомления партнёру/владельцу НИКОГДА не ломают
создание тенанта / сабмит брифа (enqueue best-effort, вне транзакции, try/except).

---

## 3. Модель данных (аддитивная миграция `db/migrate_partners.sql`)

Платформенные артефакты, **БЕЗ RLS** (как `tenants`/`platform_notify`). Бот ходит owner-DSN
(bypass RLS); панель — роль `panel_rw` (грант select/insert/update).

```sql
create table if not exists partners (
    id         uuid primary key default gen_random_uuid(),
    name       text not null,
    ref_code   text not null unique,             -- авто, secrets.token_hex(4) (8 hex)
    tg_chat_id text,                              -- для уведомлений партнёру (может быть пустым)
    status     text not null default 'active',
    created_at timestamptz not null default now(),
    constraint partners_status_chk check (status in ('active','disabled'))
);
-- Атрибуция тенанта партнёру + кто создал (для дедупа/rate-limit).
alter table tenants add column if not exists partner_id     uuid references partners(id);
alter table tenants add column if not exists ref_tg_user_id bigint;
create index if not exists tenants_partner_idx on tenants (partner_id) where partner_id is not null;
-- грант panel_rw: select/insert/update на partners (id=uuid, sequence нет).
```

`ref_code` уникален; генерация — `secrets.token_hex(4)` с ретраем на редкий конфликт unique.
`ref_tg_user_id` — TG-юзер, инициировавший реф-создание (дедуп/rate-limit; НЕ ПДн третьих лиц).

---

## 4. Архитектура и поток

```
[Панель] /partners «Новый партнёр» (name + tg_chat_id?) ─► create_partner → ref_code
        └─► показывает готовую ссылку {BOT_PUBLIC_BASE}/p/{ref_code}   (копируешь, отдаёшь партнёру)
                     │ партнёр публикует ссылку у себя
                     ▼
[Клиент]  {BOT_PUBLIC_BASE}/p/{ref_code}  ─► HTML-кнопка ─► t.me/{bot}?start=ref_{ref_code}
                     ▼
[Бот]  cmd_start payload ref_{code} → resolve активного партнёра
        → ГАРД (дедуп по (partner_id, tg_user) с pending-брифом → отдать его ссылку; rate-limit)
        → FSM «Название компании?» → create_ref_tenant(partner_id, company, tg_user_id)
             = create_tenant_admin + create_tenant_brief + partner_id + ref_tg_user_id (одна tx)
        → отдаёт клиенту ссылку /brief/{token}
        → уведомления (best-effort, вне tx): владельцу (B) + ПАРТНЁРУ (C) «🎯 Новый тенант: {company}»
                     ▼
[Клиент] проходит /brief/{token} → submit_brief → уведомления: владельцу (B) + ПАРТНЁРУ (C)
                     ▼
[Панель] /partners/{id} — отчёт: тенанты партнёра (компания, дата, статус брифа) + счётчики
```

**Границы кусков** (каждый тестируется независимо): панель (форма+отчёт) · публичный лендинг
(бот) · ref-поток бота (гард+FSM+создание) · уведомления партнёру (submit_brief + создание).

---

## 5. Кусок 1 — Панель: заведение партнёра + отчёт (is_platform)

Раздел `/partners` (образец `/brief-center`, гейт `_require_admin`):
- **Форма «Новый партнёр»:** `name` + `tg_chat_id` (опц.) → `POST /partners/create` (CSRF) →
  `db.create_partner(name, tg_chat_id)` → показать готовую ссылку `{base}/p/{ref_code}`
  (base = `db.get_bot_public_base_url()`; если пусто — предупреждение «бот не публиковал base»).
- **Список партнёров:** name · ref_code · ссылка (copy) · статус (тумблер active/disabled) ·
  tg_chat_id (правка) · счётчики «тенантов N / прошли бриф M».
- **Тумблер статуса:** `POST /partners/{id}/status` (active↔disabled). disabled → лендинг и
  ref-поток бота выдают «ссылка недействительна», тенант не создаётся.
- **Правка chat_id:** `POST /partners/{id}/chat-id` (как owner-chat-id; узнаётся через `/whoami`).
- **Отчёт `/partners/{id}`:** список тенантов партнёра — компания, дата, статус брифа
  (pending/submitted/proposed/applied), ссылка на бриф-центр тенанта.

db-хелперы (панель): `create_partner` · `list_partners` (с counts через LEFT JOIN tenants/tenant_brief) ·
`get_partner` · `list_partner_tenants(partner_id)` · `set_partner_status` · `set_partner_chat_id`.

---

## 6. Кусок 2 — Публичный лендинг `GET /p/{ref_code}` (бот)

Зеркало `_club_landing`/`_club_landing_html` (bot.py):
- resolve `ref_code` → активный партнёр. Не найден / `status='disabled'` / бот не знает свой
  username → нейтральная страница «ссылка недействительна» (404/200 без утечки деталей).
- Иначе HTML-лендинг: бренд + кнопка «Получить бриф» → `https://t.me/{_BOT_USERNAME}?start=ref_{ref_code}`.
- Регистрация роута `app.router.add_get("/p/{code}", _partner_landing)` рядом с `/club/{slug}`.
- Без авторизации; экранирование; без ПДн.

---

## 7. Кусок 3 — Бот `?start=ref_{code}` (гард + FSM + создание)

- `cmd_start`: payload вида `ref_{code}` → **отдельная ветка** (НЕ трогает голый /start и
  лид-воронку; source-набор дополняется как в handlers.py «три места»).
- resolve активного партнёра по `code`; неизвестен/disabled → мягкий ответ, выход (без создания).
- **ГАРД (лёгкий):**
  - **Дедуп:** есть ли у этого `tg_user_id` тенант этого партнёра с брифом в `pending`
    (`tenants.ref_tg_user_id=$1 and partner_id=$2` + join tenant_brief status='pending') →
    отдать ту же ссылку `/brief/{token}` вместо нового тенанта.
  - **Rate-limit:** число реф-тенантов, созданных этим `tg_user_id` за последние 24ч
    (`tenants.ref_tg_user_id=$1 and created_at > now()-24h`) ≥ порог (напр. 3) → мягкий отказ.
- **FSM (1 шаг):** отдельное состояние «жду название компании» (не коллидирует с FSM клуба).
  Ответ пользователя = название компании → `db.create_ref_tenant(partner_id, company, tg_user_id)`
  (бот-сторона, одна tx: зеркало `create_tenant_admin` [slug+status='active'] +
  `create_tenant_brief` [token+pending] + `partner_id` + `ref_tg_user_id`).
- Ответ клиенту: ссылка `{BOT_PUBLIC_BASE}/brief/{token}` «заполните бриф».
- Далее — уведомления (§8), best-effort вне tx.

---

## 8. Кусок 4 — Уведомления партнёру (переиспользуют `platform_notify`)

- **Событие «новый тенант от партнёра»** (после создания в §7, ВНЕ tx, try/except): если у
  партнёра задан `tg_chat_id` → `enqueue_platform_notify(partner_chat_id, "🎯 Новый тенант от
  тебя: {company}")`. Владельцу параллельно — существующее событие B «новый клиент».
- **Событие «партнёрский тенант прошёл бриф»** — в `submit_brief`: пост-коммитный best-effort
  блок (уже есть для владельца) РАСШИРЯЕТСЯ: если у тенанта есть `partner_id` и у партнёра
  `tg_chat_id` → доп. `enqueue_platform_notify(partner_chat_id, "✅ {company} прошёл бриф")`.
  Данные (partner_id/tenant_name) захватываются в tx, enqueue — после коммита (инвариант B).
- Доставка — уже надёжна (retry/reclaim). Партнёр должен был НАЧАТЬ основной бот (как владелец
  через `/whoami`), иначе Telegram не доставит ЛС — та же оговорка, что для владельца.

---

## 9. Обработка ошибок

- ref_code не найден / партнёр disabled → лендинг и /start: нейтральный ответ, тенант не создаётся.
- Гард-дедуп → отдаём существующую бриф-ссылку; rate-limit → мягкий «слишком много попыток, позже».
- Сбой `create_tenant_brief` после создания тенанта → graceful (лог; сообщить «повторите»),
  тенант-сирота валиден (паттерн Спека 1 §6).
- Все уведомления best-effort (не рушат создание/сабмит) — паттерн Critical-фикса.
- Конфликт unique `ref_code` при генерации → ретрай генерации.

---

## 10. Тесты

- **db-смоук (контроллер, risuy_dev):** `create_partner` + уникальность ref_code (+ретрай);
  `create_ref_tenant` создаёт active-тенанта + pending-бриф + `partner_id` + `ref_tg_user_id`;
  `list_partner_tenants`/counts; `set_partner_status('disabled')` → resolve активного не находит;
  дедуп-запрос (pending-бриф того же tg_user/partner) находит; rate-limit-запрос считает за 24ч.
- **Уведомления партнёру:** `submit_brief` тенанта с `partner_id` + партнёр с `tg_chat_id`
  ставит в очередь ДВА уведомления (владелец + партнёр); без `tg_chat_id` — только владельцу;
  сбой enqueue не рушит сабмит (регрессия-инвариант).
- **Панель:** py_compile + jinja-parse шаблонов `/partners`, `/partners/{id}`.
- **Бот:** py_compile; ref-ветка cmd_start не задевает голый /start (source='other' цел).

---

## 11. Выкатка

- Аддитивная миграция `migrate_partners.sql` (partners + tenants.partner_id/ref_tg_user_id) —
  сперва risuy_dev, прод по «да».
- Push через BIZKON → редеплой обоих → сверить commit_sha+active (twc).
- Env — нет новых ключей (base из runtime_status, chat_id из формы). Партнёр узнаёт chat_id
  через `/whoami` (уже есть).
- Порядок «миграция → код» соблюдаем; Critical-паттерн уведомлений делает окно безопасным.

---

## 12. Вне скоупа (YAGNI)

- Кабинет партнёра с логином (партнёр только получает TG-уведомления; управляешь ты в панели).
- Комиссии/выплаты/биллинг рефералов.
- Многоуровневые/вложенные рефералы.
- TTL ref-кода (вместо — отзыв статусом).
- Уведомления партнёру по e-mail (только Telegram).

---

## 13. Открытые импл-заметки (для плана)

- **Бот-зеркала создания тенанта:** `create_ref_tenant` в bot-telegram/db.py — свериться с
  панельными `create_tenant_admin` (генерация slug, status='active', audit) и
  `create_tenant_brief` (token, expires_at?, pending) и повторить их семантику в одной tx.
- **Дедуп/rate-limit пороги** — вынести константами (напр. `REF_RATELIMIT_PER_DAY=3`).
- **source-набор deep-link** (handlers.py «три места») — добавить `ref_` осознанно, не сломав
  валидацию club/intro/голого /start.
