# Спека: восстановление пароля тенанта по email (self-service)

**Дата:** 2026-07-01
**Статус:** дизайн утверждён владельцем (канал = email, «ок»); ожидает ревью спеки → план (writing-plans)
**Проект:** risuy-ecosystem (admin-panel FastAPI/Jinja2 + Postgres/asyncpg)
**Ветка возврата:** `6149e48` (main, 0 ahead)

---

## 1. Проблема

Тенант-админ, забывший пароль, **не может восстановить доступ**. Сегодня единственный путь —
владелец вручную ставит новый пароль через `/team` (`db.set_admin_user_password_with_audit`,
`db.py:2526`) и сообщает его вне системы, что означает **утечку пароля в открытом виде в переписке**.

Механизма self-service сброса нет: нет таблицы токенов, нет маршрутов `/forgot-password` /
`/reset-password`, нет email-инфраструктуры.

## 2. Решение (одна фраза)

Классический self-service: тенант вводит email → получает письмо с одноразовой ссылкой
(короткий TTL) → сам задаёт новый пароль. Владелец не вовлечён, пароль знает только тенант.

## 3. Кого покрывает (границы, честно)

- ✅ **Email-регистрации** — реальный пароль + email в `account_identities(provider='email')`
  (`db/schema_account_identities.sql:14-28`). Основная и единственная целевая аудитория MVP.
- ⏭️ **Telegram/VK OAuth-тенанты** — пароля нет (случайный неюзабельный), вход по виджету
  (`verify_telegram_login`, `auth.py:493`); email может отсутствовать. «Забыл пароль» им
  неприменим — входят повторно через OAuth. Флоу обрабатывает их через анти-enumeration
  (не находит email → показывает тот же обобщённый ответ).
- **Платформа (env-админ)** — вне БД (`ADMIN_USERNAME`/`ADMIN_PASSWORD_HASH`), сброс через
  этот флоу неприменим и **намеренно недоступен** (у env-админа нет строки в `admin_users`).

## 4. Поток

1. **GET `/forgot-password`** — форма с полем `email`, без сессии, pre-session CSRF
   (`_login_csrf_signer`, как на `/login`).
2. **POST `/forgot-password`** — троттлинг (см. §6) → поиск
   `account_identities(provider='email', external_id=lower(email))` → `username` → активная
   учётка `admin_users(active=true)`. Если найдена: гасим прежние неиспользованные токены
   пользователя, создаём новый токен, отправляем письмо. **Всегда** (найдена/нет) —
   одинаковый ответ (PRG → страница «Проверьте почту»).
3. **GET `/reset-password?token=…`** — валидация токена (существует, не истёк, не использован).
   Невалиден → обобщённая страница «Ссылка недействительна или истекла» + линк на
   `/forgot-password`. Валиден → форма нового пароля (+ подтверждение), скрытый `token`,
   pre-session CSRF.
4. **POST `/reset-password`** — повторная валидация токена → проверка политики пароля →
   `auth.hash_password` (argon2id) → `db.set_admin_user_password_with_audit` → пометить
   токен `used_at=now()` → **отозвать все активные сессии** учётки → аудит → редирект на
   `/login` с флеш-сообщением «Пароль обновлён, войдите заново».

## 5. Модель данных

### Новая таблица (прод-DDL перед кодом)

`db/migrate_password_reset_tokens.sql` (идемпотентно, по паттерну `db/migrate_consent_events.sql`):

```sql
create table if not exists password_reset_tokens (
    token_hash  text        primary key,          -- sha256(hex) от сырого токена; сам токен в БД НЕ хранится
    username    text        not null references admin_users(username) on delete cascade,
    created_at  timestamptz not null default now(),
    expires_at  timestamptz not null,             -- created_at + TTL (30 мин)
    used_at     timestamptz,                       -- null = не использован
    request_ip  text                               -- для аудита/троттлинга
);
create index if not exists password_reset_tokens_username_idx on password_reset_tokens (username);
create index if not exists password_reset_tokens_expires_idx  on password_reset_tokens (expires_at);

grant select, insert, update on password_reset_tokens to panel_rw;
-- sequence грант НЕ нужен: PK = token_hash (text), не serial.
```

Замечание по RLS: таблица привязана к `username` (глобальный реестр операторов, не tenant-scoped —
как и сам `admin_users`). RLS не требуется; доступ только служебный через `panel_rw`.

### Переиспользуем (без изменений)

| Что | Где |
|---|---|
| argon2id-хеш пароля | `auth.hash_password` (`auth.py:74`) |
| Установка пароля + аудит | `db.set_admin_user_password_with_audit` (`db.py:2526`) |
| Серверные сессии / отзыв | `admin_sessions.revoked` (`db/schema_admin.sql:35-45`) |
| Pre-session CSRF | `_login_csrf_signer`, `auth.check_csrf`, `_enforce_csrf` (`app.py:176`) |
| Троттлинг-паттерн | `admin_login_throttle` (`db/schema_admin.sql:77-81`), `auth.apply_tarpit` (`auth.py:188`) |
| Аудит | `db.audit(...)` |
| Идентичности (email lookup) | `account_identities` (`db/schema_account_identities.sql:14-28`) |
| Механизм миграций + грант | `db/migrate_consent_events.sql` |

### Новый код

- `admin-panel/mailer.py` — отправка через `aiosmtplib`, провайдер-агностично, креды из env.
- Новый helper `db.revoke_all_sessions(username)` — `update admin_sessions set revoked=true where actor=$1`
  (проверить, нет ли уже такого; если есть — переиспользовать).
- Хелперы токенов в `db.py`: `create_reset_token`, `consume_reset_token`, `invalidate_user_reset_tokens`.
- 4 маршрута в `admin-panel/app.py` + 3 шаблона (`forgot_password.html`, `reset_sent.html`,
  `reset_password.html`) + линк «Забыли пароль?» на `/login`.

## 6. Безопасность

- **Токен:** `secrets.token_urlsafe(32)`; в письмо уходит сырой токен, в БД — только `sha256(hex)`.
  Валидация: хешируем входящий токен и ищем по `token_hash` (утечка БД не даёт рабочих ссылок).
- **TTL:** 30 минут (`expires_at`). **Одноразовость:** `used_at`. При новом запросе гасим прежние
  неиспользованные токены пользователя (`invalidate_user_reset_tokens`).
- **Rate-limit `/forgot-password`:** по email + по IP + глобальный счётчик (паттерн тарпита) —
  против почтового флуда и перебора. Порог/задержки — как у логина.
- **Анти-enumeration:** одинаковый ответ и поведение независимо от существования email;
  избегаем тайминг-утечки (постоянное время до ответа).
- **После сброса:** отзыв всех сессий учётки (выкидывает возможного угонщика с активной сессией).
- **Политика пароля:** та же, что на регистрации (мин. длина и пр.) — переиспользовать валидатор.
- **CSRF:** на обеих POST-формах через pre-session signer (формы без сессии, как `/login`).
- **Неактивная учётка** (`active=false`): трактуется как несуществующая (обобщённый ответ).
- **Constant-time** сравнения токена/CSRF (`secrets.compare_digest`).

## 7. Email-транспорт

- **Транспорт:** SMTP через `aiosmtplib`, все параметры в env:
  `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`, `SMTP_FROM`, `PANEL_BASE_URL`,
  `RESET_TOKEN_TTL_MIN` (дефолт 30). Работает с любым РФ-провайдером (Timeweb-почта /
  Yandex 360 / Mailopost) — без привязки к конкретному API.
- **Письмо:** минимальное, транзакционное, на русском, без маркетинга (спам-safe / 152-ФЗ).
  Subject: «Восстановление доступа». Тело (plain + минимальный HTML): кто запросил, ссылка,
  «действительна 30 минут», «если это не вы — проигнорируйте письмо».
- **Dry-run режим:** если SMTP-креды не заданы (`SMTP_HOST` пуст) — mailer логирует ссылку
  вместо отправки (для локали/смоуков; на проде креды обязательны).

### 🔑 Deploy-требования (готовит владелец)

- Отправляющий домен + SMTP-ящик.
- DNS-записи **SPF** и **DKIM** на домене (иначе письма уходят в спам).
- Значения `SMTP_*` + `PANEL_BASE_URL` в env панели (Timeweb App Platform).

## 8. Вне scope (YAGNI)

- Owner-fallback «сгенерить ссылку сброса из /team» (страховка для OAuth/сменивших email) —
  дёшево добавить позже поверх этой же таблицы токенов.
- SMS-канал.
- Привязка Telegram к учётке оператора.
- Смена/верификация email из настроек (отдельная фича).

## 9. План проверки

- **Смоуки на `risuy_dev`** (гард risuy_dev в скрипте; throwaway-учётки; чистка токенов до учётки —
  FK cascade закрывает): жизненный цикл токена (создать/валидировать/истечь/one-use);
  анти-enumeration (существующий vs несуществующий email → идентичный ответ); отзыв сессий
  после сброса; троттлинг; неактивная учётка. Mailer — через dry-run/захват (реальные письма
  в смоуке не шлём).
- **3-линзовое адверсариальное ревью** (correctness / security / изоляция) через Workflow +
  смоук перед коммитом.

## 10. Раскатка (по «да» на каждый прод-шаг)

1. Прод-DDL `db/migrate_password_reset_tokens.sql` — сначала `risuy_dev` (смоук), затем прод
   (owner-DSN) **по явному «да»**.
2. Код (маршруты + mailer + шаблоны + хелперы) — коммит после смоука+ревью.
3. Деплой (push → авто-редеплой App Platform, поллинг по `app.commit_sha`) **по «да»**.
4. Проставить `SMTP_*` + `PANEL_BASE_URL` в env панели; проверить доставку письма.

## 11. Открытые вопросы

- Точные значения политики пароля — взять из существующего валидатора регистрации (проверить в коде).
- Провайдер SMTP и домен — на усмотрение владельца к деплою (код не зависит).
