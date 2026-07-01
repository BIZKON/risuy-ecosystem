# Восстановление пароля тенанта по email — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Дать тенант-админу self-service сброс забытого пароля через письмо с одноразовой ссылкой.

**Architecture:** Классический flow `/forgot-password → письмо со ссылкой → /reset-password`. Одноразовый токен (в БД — sha256, в письме — сырой), TTL 30 мин, анти-enumeration, rate-limit, отзыв всех сессий после сброса. Максимум переиспользования существующего auth-кода; нового — минимум.

**Tech Stack:** FastAPI, Jinja2, asyncpg, argon2-cffi (есть), aiosmtplib (новый), Postgres.

## Global Constraints

- 🇷🇺 Только русский: код-комментарии, docstrings, UI-тексты, коммиты, письмо.
- Целевая аудитория flow — учётки с email в `account_identities(provider='email')`; OAuth-учётки без email обрабатываются анти-enumeration (одинаковый ответ).
- env-админ (`config.ADMIN_USERNAME`, вне БД) через flow **недоступен** намеренно.
- Политика пароля: `config.ACCOUNT_PASSWORD_MIN=10` … `ACCOUNT_PASSWORD_MAX=200` (config.py:786-787).
- Токен в письме: `secrets.token_urlsafe(32)`; в БД только `hashlib.sha256(raw.encode()).hexdigest()`.
- TTL токена: `config.RESET_TOKEN_TTL_MIN` (дефолт 30).
- CSRF на всех POST-формах через pre-session `auth.set_login_csrf_cookie` / `auth.verify_login_csrf` (auth.py:430-461).
- Прод-DDL применяется owner-DSN, СНАЧАЛА `risuy_dev`, затем прод **по явному «да»** владельца. Идемпотентно (`if not exists`).
- Смоуки — только на `risuy_dev` (гард в скрипте: `if "risuy_dev" not in DSN: raise SystemExit`).
- Каждый код-деплой = 3-линзовое адверсариальное ревью + смоук ПЕРЕД коммитом; push/деплой — по «да».
- graphify-файлы (`CLAUDE.md`/`.claude/`/`.gitignore`) из коммитов исключать (стейджить файлы явно).

---

## Карта файлов

| Файл | Действие | Ответственность |
|---|---|---|
| `db/migrate_password_reset_tokens.sql` | Create | DDL таблицы токенов + грант panel_rw |
| `admin-panel/db.py` | Modify | 5 хелперов: `get_active_username_by_email`, `recent_reset_counts`, `create_reset_token`, `peek_reset_token`, `consume_reset_token` |
| `admin-panel/mailer.py` | Create | Отправка письма сброса через aiosmtplib (+dry-run без креды) |
| `admin-panel/config.py` | Modify | env-переменные `SMTP_*`, `RESET_*` |
| `admin-panel/requirements.txt` | Modify | `aiosmtplib==3.0.1` |
| `admin-panel/app.py` | Modify | 4 маршрута + хелперы `_reset_token_hash`/`_reset_error_text` + баннер `?reset=1` на /login + ссылка «Забыли пароль?» |
| `admin-panel/templates/forgot_password.html` | Create | Форма ввода email + экран «письмо отправлено» |
| `admin-panel/templates/reset_password.html` | Create | Форма нового пароля / «ссылка недействительна» |
| `admin-panel/templates/login.html` | Modify | Ссылка «Забыли пароль?» + баннер успеха сброса |
| `scripts/password_reset_db_smoke.py` | Create | DB-смоук жизненного цикла токена (risuy_dev) |
| `scripts/password_reset_ui_smoke.py` | Create | Render-смоук шаблонов |

---

### Task 1: Миграция `password_reset_tokens` + применение на risuy_dev

**Files:**
- Create: `db/migrate_password_reset_tokens.sql`

**Interfaces:**
- Produces: таблица `password_reset_tokens(token_hash text PK, username text FK→admin_users on delete cascade, created_at, expires_at, used_at, request_ip text)`; гранты `select, insert, update` для `panel_rw`.

- [ ] **Step 1: Написать файл миграции**

Create `db/migrate_password_reset_tokens.sql`:

```sql
-- ── Токены сброса пароля оператора (self-service восстановление по email) ─────
-- Одноразовые токены. В БД хранится sha256(hex) токена; сам токен только в письме
-- (утечка БД не даёт рабочих ссылок). Глобальный реестр по username (как admin_users),
-- НЕ tenant-scoped → RLS не нужен. panel_rw: select/insert/update (update — used_at).
--
-- ⚠️ DDL: применять owner-DSN, СНАЧАЛА risuy_dev, ПЕРЕД деплоем кода. Идемпотентно.

create table if not exists password_reset_tokens (
    token_hash  text        primary key,
    username    text        not null references admin_users(username) on delete cascade,
    created_at  timestamptz not null default now(),
    expires_at  timestamptz not null,
    used_at     timestamptz,
    request_ip  text
);

create index if not exists password_reset_tokens_username_idx
    on password_reset_tokens (username, created_at desc);
create index if not exists password_reset_tokens_expires_idx
    on password_reset_tokens (expires_at);

grant select, insert, update on password_reset_tokens to panel_rw;
-- sequence грант НЕ нужен: PK = token_hash (text), не serial.
```

- [ ] **Step 2: Применить на risuy_dev** (owner-DSN даёт владелец строкой)

Run:
```bash
psql "$OWNER_RISUY_DEV_DSN" -f db/migrate_password_reset_tokens.sql
```
Expected: `CREATE TABLE`, `CREATE INDEX` ×2, `GRANT`.

- [ ] **Step 3: Проверить структуру**

Run:
```bash
psql "$OWNER_RISUY_DEV_DSN" -c "\d password_reset_tokens"
```
Expected: колонки `token_hash/username/created_at/expires_at/used_at/request_ip`, PK по `token_hash`, FK на `admin_users`.

- [ ] **Step 4: Commit**

```bash
git add db/migrate_password_reset_tokens.sql
git commit -m "feat(panel): DDL password_reset_tokens — токены сброса пароля (миграция)"
```

> ⚠️ Прод применяется ПОЗЖЕ (Task 6), по явному «да» владельца.

---

### Task 2: DB-хелперы токенов в `admin-panel/db.py`

**Files:**
- Modify: `admin-panel/db.py` (добавить 5 функций рядом с `resolve_username_by_email` db.py:2385 и `set_admin_user_password_with_audit` db.py:2526)
- Create: `scripts/password_reset_db_smoke.py`

**Interfaces:**
- Consumes: `pool` (db.py:32), паттерн `async with pool.acquire() as c`.
- Produces:
  - `get_active_username_by_email(email: str) -> str | None`
  - `recent_reset_counts(username: str, request_ip: str | None, *, window_min: int) -> tuple[int, int]`
  - `create_reset_token(username: str, token_hash: str, *, ttl_min: int, request_ip: str | None) -> None`
  - `peek_reset_token(token_hash: str) -> bool`
  - `consume_reset_token(token_hash: str) -> str | None`

- [ ] **Step 1: Написать DB-смоук (падающий — функций ещё нет)**

Create `scripts/password_reset_db_smoke.py` (env-стабы скопировать из существующего панельного DB-смоука `scripts/tenant_create_db_smoke.py`; гард risuy_dev обязателен):

```python
#!/usr/bin/env python3
"""Smoke: жизненный цикл токенов сброса пароля (password_reset_tokens) на risuy_dev.
Throwaway admin_user + account_identity(email); чистка каскадом. Реальные письма НЕ шлём.

Запуск:
  RESET_SMOKE_DSN="postgresql://gen_user:<pw>@HOST:5432/risuy_dev?sslmode=require" \
  PYTHONPATH=admin-panel DATABASE_URL="$RESET_SMOKE_DSN" \
  SESSION_SECRET="smoke-session-secret-min-32-chars-long-xx" ADMIN_USERNAME=smokeadmin \
  ADMIN_PASSWORD_HASH='$argon2id$v=19$m=65536,t=3,p=4$c21va2VzYWx0c2FsdA$c21va2VoYXNoc21va2VoYXNoc21va2VoYXNoMDA' \
  ./.venv-smoke/bin/python scripts/password_reset_db_smoke.py
"""
import asyncio
import hashlib
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "admin-panel"))

DSN = os.environ.get("RESET_SMOKE_DSN") or os.environ.get("DATABASE_URL")
if not DSN or "risuy_dev" not in DSN:
    raise SystemExit("Задайте DSN на risuy_dev (защита от прода).")

import db  # noqa: E402
import auth  # noqa: E402

U = "smoke-reset-user"
EMAIL = "smoke-reset@example.com"


def h(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


async def main() -> None:
    await db.init()
    fails: list[str] = []
    async with db.pool.acquire() as c:
        async def drop() -> None:
            await c.execute("delete from password_reset_tokens where username=$1", U)
            await c.execute("delete from admin_sessions where actor=$1", U)
            await c.execute("delete from account_identities where username=$1", U)
            await c.execute("delete from admin_users where username=$1", U)

        await drop()
        await c.execute(
            "insert into admin_users (username,password_hash,role,active) "
            "values ($1,$2,'operator',true)", U, auth.hash_password("irrelevant-old-pw"))
        await c.execute(
            "insert into account_identities (provider,external_id,username,verified) "
            "values ('email',$1,$2,true)", EMAIL, U)

        try:
            # 1. email → активный username; неизвестный → None
            got = await db.get_active_username_by_email(EMAIL)
            if got != U:
                fails.append(f"get_active_username_by_email: {got!r} != {U!r}")
            if await db.get_active_username_by_email("nope@example.com") is not None:
                fails.append("неизвестный email должен давать None")

            # 2. неактивная учётка → None (анти-enumeration покрывает inactive)
            await c.execute("update admin_users set active=false where username=$1", U)
            if await db.get_active_username_by_email(EMAIL) is not None:
                fails.append("неактивная учётка должна давать None")
            await c.execute("update admin_users set active=true where username=$1", U)

            # 3. создание токена: строка есть, не использована, expires в будущем
            raw1 = "tok-one"
            await db.create_reset_token(U, h(raw1), ttl_min=30, request_ip="1.2.3.4")
            row = await c.fetchrow(
                "select used_at, expires_at>now() as future from password_reset_tokens where token_hash=$1", h(raw1))
            if row is None or row["used_at"] is not None or not row["future"]:
                fails.append(f"создание токена некорректно: {row}")

            # 4. повторный create гасит прежние неиспользованные
            raw2 = "tok-two"
            await db.create_reset_token(U, h(raw2), ttl_min=30, request_ip="1.2.3.4")
            used1 = await c.fetchval("select used_at is not null from password_reset_tokens where token_hash=$1", h(raw1))
            if used1 is not True:
                fails.append("прежний неиспользованный токен должен быть погашен новым запросом")

            # 5. peek валиден → True; consume возвращает username и гасит; повтор → None (one-use)
            if await db.peek_reset_token(h(raw2)) is not True:
                fails.append("peek валидного токена должен быть True")
            if await db.consume_reset_token(h(raw2)) != U:
                fails.append("consume валидного токена должен вернуть username")
            if await db.peek_reset_token(h(raw2)) is not False:
                fails.append("после consume peek должен быть False")
            if await db.consume_reset_token(h(raw2)) is not None:
                fails.append("повторный consume должен дать None (one-use)")

            # 6. истёкший токен: consume → None, peek → False
            raw3 = "tok-expired"
            await c.execute(
                "insert into password_reset_tokens (token_hash,username,expires_at) "
                "values ($1,$2, now()-interval '1 minute')", h(raw3), U)
            if await db.peek_reset_token(h(raw3)) is not False:
                fails.append("истёкший токен: peek должен быть False")
            if await db.consume_reset_token(h(raw3)) is not None:
                fails.append("истёкший токен: consume должен дать None")

            # 7. rate-limit счётчики растут
            by_user, by_ip = await db.recent_reset_counts(U, "1.2.3.4", window_min=15)
            if by_user < 1 or by_ip < 1:
                fails.append(f"recent_reset_counts не считает: user={by_user} ip={by_ip}")

            # 8. отзыв сессий (переиспользуем существующую revoke_all_sessions_with_audit)
            sid = await auth.create_session(U, "operator")
            n = await db.revoke_all_sessions_with_audit(U, keep_sid=None, ip="1.2.3.4", user_agent="smoke")
            revoked = await c.fetchval("select revoked from admin_sessions where sid=$1", sid)
            if n < 1 or revoked is not True:
                fails.append(f"отзыв сессий не сработал: n={n} revoked={revoked}")
        finally:
            await drop()

    if fails:
        print("\n".join("❌ " + f for f in fails))
        raise SystemExit(1)
    print("🟢 password_reset_db_smoke зелёный")


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: Запустить смоук — убедиться, что падает**

Run (owner-DSN даёт владелец):
```bash
RESET_SMOKE_DSN="postgresql://gen_user:<pw>@HOST:5432/risuy_dev?sslmode=require" \
PYTHONPATH=admin-panel DATABASE_URL="$RESET_SMOKE_DSN" \
SESSION_SECRET="smoke-session-secret-min-32-chars-long-xx" ADMIN_USERNAME=smokeadmin \
ADMIN_PASSWORD_HASH='$argon2id$v=19$m=65536,t=3,p=4$c21va2VzYWx0c2FsdA$c21va2VoYXNoc21va2VoYXNoc21va2VoYXNoMDA' \
./.venv-smoke/bin/python scripts/password_reset_db_smoke.py
```
Expected: FAIL — `AttributeError: module 'db' has no attribute 'get_active_username_by_email'`.

- [ ] **Step 3: Реализовать 5 хелперов в `admin-panel/db.py`**

Добавить рядом с `resolve_username_by_email` (db.py:2385):

```python
async def get_active_username_by_email(email: str) -> str | None:
    """email → username АКТИВНОЙ учётки (для сброса пароля). None — нет такой/неактивна.
    Анти-enumeration: вызывающий отвечает одинаково независимо от результата."""
    e = (email or "").strip().lower()
    if not e:
        return None
    async with pool.acquire() as c:
        return await c.fetchval(
            "select ai.username from account_identities ai "
            "join admin_users au on au.username = ai.username "
            "where ai.provider = 'email' and ai.external_id = $1 and au.active = true",
            e,
        )


async def recent_reset_counts(
    username: str, request_ip: str | None, *, window_min: int,
) -> tuple[int, int]:
    """(по username, по ip) число токенов сброса за окно window_min минут — для rate-limit."""
    async with pool.acquire() as c:
        by_user = await c.fetchval(
            "select count(*) from password_reset_tokens "
            "where username = $1 and created_at > now() - make_interval(mins => $2::int)",
            username, window_min,
        )
        by_ip = 0
        if request_ip:
            by_ip = await c.fetchval(
                "select count(*) from password_reset_tokens "
                "where request_ip = $1 and created_at > now() - make_interval(mins => $2::int)",
                request_ip, window_min,
            )
    return int(by_user or 0), int(by_ip or 0)


async def create_reset_token(
    username: str, token_hash: str, *, ttl_min: int, request_ip: str | None,
) -> None:
    """Создать токен сброса. Хранится хеш; сам токен уходит в письмо. Прежние
    неиспользованные токены того же юзера гасятся (единственный активный токен)."""
    async with pool.acquire() as c:
        async with c.transaction():
            await c.execute(
                "update password_reset_tokens set used_at = now() "
                "where username = $1 and used_at is null",
                username,
            )
            await c.execute(
                "insert into password_reset_tokens (token_hash, username, expires_at, request_ip) "
                "values ($1, $2, now() + make_interval(mins => $3::int), $4)",
                token_hash, username, ttl_min, request_ip,
            )


async def peek_reset_token(token_hash: str) -> bool:
    """Валиден ли токен (не использован, не истёк) БЕЗ погашения — для показа формы сброса."""
    async with pool.acquire() as c:
        row = await c.fetchval(
            "select 1 from password_reset_tokens "
            "where token_hash = $1 and used_at is null and expires_at > now()",
            token_hash,
        )
    return row is not None


async def consume_reset_token(token_hash: str) -> str | None:
    """Проверить и ПОГАСИТЬ токен атомарно (one-use). Возврат username при успехе, иначе None.
    Успех = существует, не использован, не истёк. used_at ставится тем же UPDATE →
    конкурентный второй consume получит 0 строк → None."""
    async with pool.acquire() as c:
        return await c.fetchval(
            "update password_reset_tokens set used_at = now() "
            "where token_hash = $1 and used_at is null and expires_at > now() "
            "returning username",
            token_hash,
        )
```

- [ ] **Step 4: Запустить смоук — убедиться, что зелёный**

Run: та же команда из Step 2.
Expected: `🟢 password_reset_db_smoke зелёный`.

- [ ] **Step 5: Commit**

```bash
git add admin-panel/db.py scripts/password_reset_db_smoke.py
git commit -m "feat(panel): db-хелперы токенов сброса пароля + смоук жизненного цикла (risuy_dev)"
```

---

### Task 3: Mailer + config + зависимость

**Files:**
- Create: `admin-panel/mailer.py`
- Modify: `admin-panel/config.py` (после блока `PANEL_PUBLIC_BASE_URL`, config.py:362)
- Modify: `admin-panel/requirements.txt`

**Interfaces:**
- Consumes: `config.SMTP_*`, `config.RESET_*`.
- Produces: `mailer.send_password_reset(to_email: str, reset_url: str, *, ttl_min: int) -> None` (dry-run логирует ссылку, если SMTP не настроен).

- [ ] **Step 1: Добавить env-переменные в `admin-panel/config.py`**

```python
# --- SMTP + сброс пароля (self-service восстановление по email) ---
# SMTP не настроен (пустой SMTP_HOST) → mailer работает в dry-run (логирует ссылку, не шлёт).
SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
SMTP_FROM = os.environ.get("SMTP_FROM", "")
SMTP_STARTTLS = os.environ.get("SMTP_STARTTLS", "1") not in ("0", "false", "False", "")
RESET_TOKEN_TTL_MIN = int(os.environ.get("RESET_TOKEN_TTL_MIN", "30"))
RESET_WINDOW_MIN = int(os.environ.get("RESET_WINDOW_MIN", "15"))
RESET_MAX_PER_WINDOW = int(os.environ.get("RESET_MAX_PER_WINDOW", "3"))
RESET_MAX_PER_IP = int(os.environ.get("RESET_MAX_PER_IP", "10"))
```

- [ ] **Step 2: Добавить зависимость в `admin-panel/requirements.txt`**

Добавить строкой (перед завершающей пустой строкой, после `cryptography==48.0.1`):
```
aiosmtplib==3.0.1
```

- [ ] **Step 3: Установить зависимость в smoke-venv** (для render/import смоуков)

Run:
```bash
./.venv-smoke/bin/pip install aiosmtplib==3.0.1
```
Expected: `Successfully installed aiosmtplib-3.0.1`.

- [ ] **Step 4: Написать `admin-panel/mailer.py`**

```python
"""Отправка транзакционного письма сброса пароля через aiosmtplib.

Провайдер-агностично (SMTP через env). Если SMTP не настроен (пустой SMTP_HOST) —
dry-run: логируем ссылку вместо отправки (локаль/смоуки). На проде креды обязательны.
Письмо минимальное, на русском, без маркетинга (спам-safe / 152-ФЗ)."""
import logging
from email.message import EmailMessage

import aiosmtplib

import config

log = logging.getLogger("mailer")


def is_configured() -> bool:
    """SMTP настроен для реальной отправки?"""
    return bool(config.SMTP_HOST and config.SMTP_FROM)


async def send_password_reset(to_email: str, reset_url: str, *, ttl_min: int) -> None:
    """Отправить письмо со ссылкой сброса. Dry-run (лог) при ненастроенном SMTP."""
    text = (
        "Вы запросили сброс пароля к панели.\n\n"
        f"Чтобы задать новый пароль, откройте ссылку (действительна {ttl_min} минут):\n"
        f"{reset_url}\n\n"
        "Если вы этого не запрашивали — просто проигнорируйте письмо. Пароль не изменится."
    )
    if not is_configured():
        log.warning("SMTP не настроен (dry-run). Ссылка сброса для %s: %s", to_email, reset_url)
        return

    msg = EmailMessage()
    msg["From"] = config.SMTP_FROM
    msg["To"] = to_email
    msg["Subject"] = "Восстановление доступа"
    msg.set_content(text)

    await aiosmtplib.send(
        msg,
        hostname=config.SMTP_HOST,
        port=config.SMTP_PORT,
        username=config.SMTP_USER or None,
        password=config.SMTP_PASS or None,
        start_tls=config.SMTP_STARTTLS,
    )
```

- [ ] **Step 5: Проверить dry-run (падать нечему — проверяем поведение)**

Run:
```bash
PYTHONPATH=admin-panel DATABASE_URL=x \
SESSION_SECRET="smoke-session-secret-min-32-chars-long-xx" ADMIN_USERNAME=smokeadmin \
ADMIN_PASSWORD_HASH='$argon2id$v=19$m=65536,t=3,p=4$c21va2VzYWx0c2FsdA$c21va2VoYXNoc21va2VoYXNoc21va2VoYXNoMDA' \
./.venv-smoke/bin/python -c "import asyncio, mailer; assert mailer.is_configured() is False; asyncio.run(mailer.send_password_reset('a@b.ru','https://x/reset-password?token=t', ttl_min=30)); print('🟢 mailer dry-run ok')"
```
Expected: строка `mailer:...Ссылка сброса для a@b.ru` в логе + `🟢 mailer dry-run ok`.

- [ ] **Step 6: Commit**

```bash
git add admin-panel/mailer.py admin-panel/config.py admin-panel/requirements.txt
git commit -m "feat(panel): mailer сброса пароля (aiosmtplib, dry-run без креды) + env SMTP/RESET"
```

---

### Task 4: Маршрут `/forgot-password` + шаблон

**Files:**
- Modify: `admin-panel/app.py` (рядом с `login_form` app.py:298; хелперы рядом с `_login_error_text`)
- Create: `admin-panel/templates/forgot_password.html`
- Create: `scripts/password_reset_ui_smoke.py`

**Interfaces:**
- Consumes: `templates` (app.py:124), `auth.set_login_csrf_cookie`/`verify_login_csrf`/`LOGIN_CSRF_COOKIE`, `auth.apply_tarpit`, `_ip`/`_ua`/`_valid_email`/`secrets`, `db.get_active_username_by_email`/`recent_reset_counts`/`create_reset_token`, `mailer.send_password_reset`, `config.*`.
- Produces: `GET /forgot-password`, `POST /forgot-password`, `_reset_token_hash(raw) -> str`.

- [ ] **Step 1: Написать render-смоук (падающий — шаблона ещё нет)**

Create `scripts/password_reset_ui_smoke.py` (env-стабы — как в `scripts/onboarding_ui_smoke.py`):

```python
#!/usr/bin/env python3
"""Render-смоук шаблонов сброса пароля (без БД). Проверяет: CSRF-поле, action форм,
ветки valid/invalid у reset_password. Env-стабы панельного config (как onboarding_ui_smoke)."""
import os
import sys

os.environ.setdefault("DATABASE_URL", "postgresql://x/x")
os.environ.setdefault("SESSION_SECRET", "smoke-session-secret-min-32-chars-long-xx")
os.environ.setdefault("ADMIN_USERNAME", "smokeadmin")
os.environ.setdefault(
    "ADMIN_PASSWORD_HASH",
    "$argon2id$v=19$m=65536,t=3,p=4$c21va2VzYWx0c2FsdA$c21va2VoYXNoc21va2VoYXNoc21va2VoYXNoMDA",
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "admin-panel"))

from app import templates  # noqa: E402

fails: list[str] = []


def render(name: str, ctx: dict) -> str:
    return templates.env.get_template(name).render(ctx)


# forgot: форма
html = render("forgot_password.html", {"csrf_token": "TESTCSRF", "sent": False})
if 'name="csrf_token"' not in html or "TESTCSRF" not in html:
    fails.append("forgot: нет CSRF-поля")
if 'action="/forgot-password"' not in html or 'name="email"' not in html:
    fails.append("forgot: нет формы email→/forgot-password")

# forgot: экран «письмо отправлено»
html_sent = render("forgot_password.html", {"csrf_token": "TESTCSRF", "sent": True})
if "отправ" not in html_sent.lower():
    fails.append("forgot: нет подтверждения отправки при sent=True")

# reset: валидный токен → форма пароля
html_ok = render("reset_password.html", {
    "csrf_token": "TESTCSRF", "token": "TOK", "valid": True, "err": "", "password_min": 10})
if 'name="new_password"' not in html_ok or 'action="/reset-password"' not in html_ok:
    fails.append("reset(valid): нет формы нового пароля")
if 'value="TOK"' not in html_ok:
    fails.append("reset(valid): токен не проброшен в скрытое поле")

# reset: невалидный токен → нет формы пароля, есть ссылка на /forgot-password
html_bad = render("reset_password.html", {
    "csrf_token": "TESTCSRF", "token": "", "valid": False, "err": "", "password_min": 10})
if 'name="new_password"' in html_bad:
    fails.append("reset(invalid): форма пароля не должна показываться")
if "/forgot-password" not in html_bad:
    fails.append("reset(invalid): нет ссылки на повторный запрос")

if fails:
    print("\n".join("❌ " + f for f in fails))
    raise SystemExit(1)
print("🟢 password_reset_ui_smoke зелёный")
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run:
```bash
./.venv-smoke/bin/python scripts/password_reset_ui_smoke.py
```
Expected: FAIL — `jinja2.exceptions.TemplateNotFound: forgot_password.html`.

- [ ] **Step 3: Создать `admin-panel/templates/forgot_password.html`**

> Самодостаточный минимальный шаблон (не зависит от блоков base); визуальное выравнивание с `login.html` — в Task 6 (не влияет на функциональность).

```html
<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Восстановление доступа</title>
</head>
<body>
  <main class="auth">
    {% if sent %}
      <h1>Проверьте почту</h1>
      <p>Если такой email зарегистрирован, мы отправили на него ссылку для сброса пароля.
         Ссылка действительна ограниченное время. Не пришло — проверьте «Спам».</p>
      <p><a href="/login">Вернуться ко входу</a></p>
    {% else %}
      <h1>Забыли пароль?</h1>
      <p>Введите email, которым вы регистрировались. Мы пришлём ссылку для сброса.</p>
      <form class="auth__form" method="post" action="/forgot-password" autocomplete="off" novalidate>
        <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
        <label>Email
          <input class="field__input" type="email" name="email" required autofocus>
        </label>
        <button type="submit">Прислать ссылку</button>
      </form>
      <p><a href="/login">Вспомнил пароль — ко входу</a></p>
    {% endif %}
  </main>
</body>
</html>
```

- [ ] **Step 4: Добавить хелперы + маршруты в `admin-panel/app.py`**

Убедиться, что вверху есть `import hashlib` (если нет — добавить к прочим stdlib-импортам). Затем хелпер рядом с `_login_error_text`:

```python
def _reset_token_hash(raw: str) -> str:
    """sha256(hex) сырого токена — то, что хранится в password_reset_tokens."""
    return hashlib.sha256((raw or "").encode()).hexdigest()
```

Маршруты (рядом с `login_form`, app.py:298):

```python
@app.get("/forgot-password")
async def forgot_password_form(request: Request, sent: int = 0):
    # Уже вошёл — на дашборд.
    sid = auth.unsign_sid(request.cookies.get(config.COOKIE_NAME))
    if sid and await auth.load_session(sid):
        return RedirectResponse(url="/", status_code=303)
    token = secrets.token_urlsafe(32)
    resp = templates.TemplateResponse(
        request, "forgot_password.html", {"csrf_token": token, "sent": bool(sent)},
    )
    auth.set_login_csrf_cookie(resp, token)
    return resp


@app.post("/forgot-password")
async def forgot_password_submit(
    request: Request,
    email: str = Form(""),
    csrf_token: str = Form(""),
):
    # Невалидный CSRF → тот же обобщённый ответ (без утечки существования email).
    if not auth.verify_login_csrf(request.cookies.get(auth.LOGIN_CSRF_COOKIE), csrf_token):
        return RedirectResponse(url="/forgot-password?sent=1", status_code=303)
    ip = _ip(request)
    email_norm = (email or "").strip().lower()
    # Timing-floor против enumeration/брута (глобальный тарпит логина, read-only).
    await auth.apply_tarpit(email_norm or "__reset__", bypass=False)
    username = (await db.get_active_username_by_email(email_norm)) if _valid_email(email_norm) else None
    if username:
        by_user, by_ip = await db.recent_reset_counts(username, ip, window_min=config.RESET_WINDOW_MIN)
        if by_user < config.RESET_MAX_PER_WINDOW and by_ip < config.RESET_MAX_PER_IP:
            raw = secrets.token_urlsafe(32)
            await db.create_reset_token(
                username, _reset_token_hash(raw),
                ttl_min=config.RESET_TOKEN_TTL_MIN, request_ip=ip,
            )
            base = config.PANEL_PUBLIC_BASE_URL or str(request.base_url).rstrip("/")
            reset_url = f"{base}/reset-password?token={raw}"
            await mailer.send_password_reset(email_norm, reset_url, ttl_min=config.RESET_TOKEN_TTL_MIN)
            await db.audit(actor=username, action="password_reset_request",
                           ip=ip, user_agent=_ua(request), detail={})
    # ВСЕГДА одинаковый ответ (anti-enumeration).
    return RedirectResponse(url="/forgot-password?sent=1", status_code=303)
```

Добавить `import mailer` к импортам панельных модулей (рядом с `import auth`, `import db`).

- [ ] **Step 5: Запустить render-смоук по forgot — зелёная его часть**

Run:
```bash
./.venv-smoke/bin/python scripts/password_reset_ui_smoke.py
```
Expected: FAIL на reset-части (`TemplateNotFound: reset_password.html`), forgot-проверки пройдены. (reset — Task 5.)

- [ ] **Step 6: py_compile app.py**

Run: `./.venv-smoke/bin/python -m py_compile admin-panel/app.py`
Expected: без ошибок.

- [ ] **Step 7: Commit**

```bash
git add admin-panel/app.py admin-panel/templates/forgot_password.html scripts/password_reset_ui_smoke.py
git commit -m "feat(panel): маршрут /forgot-password — запрос сброса, письмо, anti-enumeration, rate-limit"
```

---

### Task 5: Маршрут `/reset-password` + шаблон + ссылка на /login

**Files:**
- Modify: `admin-panel/app.py`
- Create: `admin-panel/templates/reset_password.html`
- Modify: `admin-panel/templates/login.html` (ссылка «Забыли пароль?» + баннер `reset_ok`)

**Interfaces:**
- Consumes: `db.peek_reset_token`/`consume_reset_token`/`set_admin_user_password_with_audit`/`revoke_all_sessions_with_audit`/`audit`, `auth.hash_password`, `_reset_token_hash`, `config.ACCOUNT_PASSWORD_MIN/MAX`.
- Produces: `GET /reset-password`, `POST /reset-password`, `_reset_error_text(code) -> str`, `login_form` принимает `reset: int = 0`.

- [ ] **Step 1: Создать `admin-panel/templates/reset_password.html`**

```html
<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Новый пароль</title>
</head>
<body>
  <main class="auth">
    {% if valid %}
      <h1>Задайте новый пароль</h1>
      {% if err %}<p class="auth__error">{{ err }}</p>{% endif %}
      <form class="auth__form" method="post" action="/reset-password" autocomplete="off" novalidate>
        <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
        <input type="hidden" name="token" value="{{ token }}">
        <label>Новый пароль
          <input class="field__input" type="password" name="new_password"
                 autocomplete="new-password" minlength="{{ password_min }}" required autofocus>
        </label>
        <label>Повторите пароль
          <input class="field__input" type="password" name="confirm_password"
                 autocomplete="new-password" minlength="{{ password_min }}" required>
        </label>
        <button type="submit">Сохранить пароль</button>
      </form>
    {% else %}
      <h1>Ссылка недействительна</h1>
      <p>Ссылка сброса истекла или уже использована.</p>
      <p><a href="/forgot-password">Запросить новую ссылку</a></p>
    {% endif %}
  </main>
</body>
</html>
```

- [ ] **Step 2: Добавить `_reset_error_text` + маршруты в `admin-panel/app.py`**

Хелпер рядом с `_login_error_text`:

```python
def _reset_error_text(code: str | None) -> str:
    """Текст ошибки формы сброса пароля."""
    return {
        "csrf": "Сессия формы устарела. Откройте ссылку из письма заново.",
        "bad_password": "Пароль должен быть от 10 до 200 символов.",
        "mismatch": "Пароли не совпадают.",
        "expired": "Ссылка истекла или уже использована. Запросите новую.",
    }.get(code or "", "")
```

Маршруты:

```python
@app.get("/reset-password")
async def reset_password_form(request: Request, token: str = "", err: str = ""):
    valid = bool(token) and await db.peek_reset_token(_reset_token_hash(token))
    csrf = secrets.token_urlsafe(32)
    resp = templates.TemplateResponse(
        request, "reset_password.html",
        {"csrf_token": csrf, "token": token, "valid": bool(valid),
         "err": _reset_error_text(err), "password_min": config.ACCOUNT_PASSWORD_MIN},
    )
    auth.set_login_csrf_cookie(resp, csrf)
    return resp


@app.post("/reset-password")
async def reset_password_submit(
    request: Request,
    token: str = Form(""),
    new_password: str = Form(""),
    confirm_password: str = Form(""),
    csrf_token: str = Form(""),
):
    if not auth.verify_login_csrf(request.cookies.get(auth.LOGIN_CSRF_COOKIE), csrf_token):
        return RedirectResponse(url=f"/reset-password?token={token}&err=csrf", status_code=303)
    if not (config.ACCOUNT_PASSWORD_MIN <= len(new_password) <= config.ACCOUNT_PASSWORD_MAX):
        return RedirectResponse(url=f"/reset-password?token={token}&err=bad_password", status_code=303)
    if new_password != confirm_password:
        return RedirectResponse(url=f"/reset-password?token={token}&err=mismatch", status_code=303)
    ip = _ip(request)
    ua = _ua(request)
    # Атомарно проверяем+гасим токен (one-use). Только теперь узнаём username.
    username = await db.consume_reset_token(_reset_token_hash(token))
    if not username:
        return RedirectResponse(url="/reset-password?err=expired", status_code=303)
    await db.set_admin_user_password_with_audit(
        username, auth.hash_password(new_password), actor=username, ip=ip, user_agent=ua,
    )
    # Выкидываем возможного угонщика: отзыв всех сессий учётки.
    await db.revoke_all_sessions_with_audit(username, keep_sid=None, ip=ip, user_agent=ua)
    await db.audit(actor=username, action="password_reset_done", ip=ip, user_agent=ua, detail={})
    return RedirectResponse(url="/login?reset=1", status_code=303)
```

- [ ] **Step 3: Ссылка «Забыли пароль?» + баннер успеха в `login.html`**

В `templates/login.html` после блока формы логина (после строки с `name="next"`, login.html:46) добавить:
```html
      <p class="auth__hint"><a href="/forgot-password">Забыли пароль?</a></p>
```
И в начале блока входа (рядом с выводом `error`) добавить баннер успеха:
```html
      {% if reset_ok %}<p class="auth__ok">Пароль обновлён. Войдите с новым паролем.</p>{% endif %}
```

- [ ] **Step 4: Прокинуть `reset_ok` в `login_form`**

В сигнатуру `login_form` (app.py:298) добавить параметр `reset: int = 0`, и в контекст `templates.TemplateResponse(... "login.html", {...})` добавить ключ:
```python
            "reset_ok": bool(reset),
```

- [ ] **Step 5: Запустить render-смоук — полностью зелёный**

Run:
```bash
./.venv-smoke/bin/python scripts/password_reset_ui_smoke.py
```
Expected: `🟢 password_reset_ui_smoke зелёный`.

- [ ] **Step 6: py_compile**

Run: `./.venv-smoke/bin/python -m py_compile admin-panel/app.py`
Expected: без ошибок.

- [ ] **Step 7: Commit**

```bash
git add admin-panel/app.py admin-panel/templates/reset_password.html admin-panel/templates/login.html
git commit -m "feat(panel): маршрут /reset-password — новый пароль, one-use токен, отзыв сессий; ссылка на /login"
```

---

### Task 6: Адверсариальное ревью, полный прогон, раскатка

**Files:** — (процессная задача; правки — точечные по итогам ревью)

- [ ] **Step 1: Прогнать оба смоука на risuy_dev + весь регресс панельных смоуков**

Run (owner-DSN):
```bash
# новые
RESET_SMOKE_DSN=... ./.venv-smoke/bin/python scripts/password_reset_db_smoke.py
./.venv-smoke/bin/python scripts/password_reset_ui_smoke.py
# регресс: смежные панельные смоуки не сломаны
```
Expected: все зелёные.

- [ ] **Step 2: 3-линзовое адверсариальное ревью (Workflow)**

Линзы: **correctness** (атомарность consume/one-use, гонки, PRG-редиректы, обработка None), **security** (анти-enumeration идентичность ответа и тайминг; токен только по хешу; CSRF на обеих формах; rate-limit не обходится; отзыв сессий; env-админ недоступен; отсутствие утечки токена в логи/Referer), **изоляция** (учётка резолвится строго по своему email; сброс не затрагивает чужие сессии; неактивная учётка). Находки HIGH/CRITICAL — исправить до коммита.

- [ ] **Step 3: Внести фиксы ревью, перепрогнать смоуки, коммит фиксов**

```bash
git add -A -- admin-panel scripts
git commit -m "fix(panel): правки 3-линзового ревью сброса пароля"
```

- [ ] **Step 4: 🔴 Прод-DDL — по явному «да» владельца**

После «да»:
```bash
psql "$OWNER_PROD_DSN" -f db/migrate_password_reset_tokens.sql
```
Expected: `CREATE TABLE` + индексы + `GRANT` (идемпотентно).

- [ ] **Step 5: 🔴 Push + деплой — по явному «да»**

```bash
git push origin main
# авто-редеплой App Platform; поллинг по app.commit_sha до совпадения с git rev-parse HEAD + status=active
```

- [ ] **Step 6: Проставить env панели + проверить доставку**

Владелец задаёт в env панели: `SMTP_HOST/SMTP_PORT/SMTP_USER/SMTP_PASS/SMTP_FROM`, `PANEL_PUBLIC_BASE_URL` (если ещё не задан), при желании `RESET_TOKEN_TTL_MIN`. DNS: **SPF + DKIM** на отправляющем домене. Затем боевая проверка: `/forgot-password` реальным email тест-учётки → письмо пришло → ссылка → новый пароль → вход.

> До проставления SMTP-креды flow работает в dry-run (ссылка только в логах) — письма НЕ уходят. Это осознанное безопасное состояние на время выкатки.

---

## Self-Review (проверка плана против спеки)

**Покрытие спеки:**
- §4 поток (forgot→email→reset) → Tasks 4, 5. ✅
- §5 модель данных (таблица + переиспользование) → Task 1 (DDL), Task 2 (хелперы); `revoke_all_sessions_with_audit`/`set_admin_user_password_with_audit` — существующие, вызовы в Task 5. ✅
- §6 безопасность: токен-хеш (Task 2 `_reset_token_hash`/consume), TTL+one-use (Task 2), rate-limit (Task 2 `recent_reset_counts` + Task 4 проверки), анти-enumeration (Task 4 одинаковый ответ + тарпит), отзыв сессий (Task 5), CSRF (Tasks 4/5), политика пароля (Task 5), неактивная учётка (Task 2 `get_active_username_by_email` + смоук). ✅
- §7 email-транспорт (SMTP env, dry-run, письмо RU) → Task 3. ✅
- §9 проверка (смоуки risuy_dev + 3-линзовое ревью) → Tasks 2/4/6. ✅
- §10 раскатка (DDL→код→деплой→env, гейты «да») → Task 6. ✅

**Плейсхолдеры:** нет — каждый шаг содержит реальный код/команду/ожидаемый вывод.

**Согласованность типов:** `_reset_token_hash(raw)->str` используется в Tasks 4/5 идентично; `consume_reset_token->str|None`, `peek_reset_token->bool`, `create_reset_token->None`, `get_active_username_by_email->str|None`, `recent_reset_counts->tuple[int,int]` — сигнатуры совпадают между определением (Task 2) и вызовами (Tasks 4/5). Параметры `set_admin_user_password_with_audit(username, password_hash, *, actor, ip, user_agent)` и `revoke_all_sessions_with_audit(username, *, keep_sid, ip, user_agent)` вызываются по фактическим сигнатурам из кода. ✅

**Открытый пункт:** визуальное выравнивание новых шаблонов с `login.html` — косметика, вынесена в Task 6 (функционально смоук-покрыто).
