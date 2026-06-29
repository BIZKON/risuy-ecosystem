"""Аутентификация: argon2id, серверные сессии, троттл логина, CSRF, cookie.

Серверная сессия — ЕДИНСТВЕННЫЙ источник правды по ревокации (§3.2). Cookie несёт
лишь opaque sid (uuid), подписанный itsdangerous для tamper-evidence; вся
авторитетная проверка — SELECT строки admin_sessions на каждом защищённом запросе.

require_session — deny-by-default Depends: вешается на КАЖДЫЙ маршрут с ПДн.
Возвращает объект Session (actor, sid, csrf_token); при любом сбое — 303 на /login
(для HTML) через RedirectException, ловится в app.py.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import asyncpg
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError, InvalidHashError
from itsdangerous import BadSignature, URLSafeSerializer, URLSafeTimedSerializer

import config
import db

# --------------------------------------------------------------------------- #
# Argon2id (§3.1). Параметры ≥ OWASP. Один общий PasswordHasher.
# --------------------------------------------------------------------------- #
_ph = PasswordHasher(
    time_cost=3, memory_cost=65536, parallelism=4, hash_len=32, salt_len=16
)

# Dummy-хеш для constant-time на неизвестном юзере (§3.4): верифицируем ВСЕГДА,
# чтобы тайминг не отличал «нет такого логина» от «неверный пароль».
_DUMMY_HASH = _ph.hash("x10-admin-dummy-constant-time-password")

# Подпись sid в cookie (только tamper-evidence; секрета сессии в cookie нет).
_signer = URLSafeSerializer(config.SESSION_SECRET, salt="admin-session-sid")


def verify_password(raw_password: str) -> bool:
    """Constant-time проверка пароля оператора против ADMIN_PASSWORD_HASH.

    Всегда выполняет полный argon2-verify (на реальном или dummy-хеше), поэтому
    стоимость/тайминг не зависят от того, совпал ли логин. Без user-enumeration:
    вызывающий объединяет провал логина и провал пароля в один ответ.
    """
    target = config.ADMIN_PASSWORD_HASH
    try:
        _ph.verify(target, raw_password)
        ok = True
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        ok = False
    # Дополнительный verify на dummy выравнивает тайминг даже при раннем исключении
    # парсинга реального хеша; результат отбрасываем.
    try:
        _ph.verify(_DUMMY_HASH, raw_password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        pass
    return ok


def verify_username(raw_username: str) -> bool:
    """Constant-time сравнение логина (не секрет, но не сливаем тайминг)."""
    return secrets.compare_digest(
        (raw_username or "").encode("utf-8"),
        config.ADMIN_USERNAME.encode("utf-8"),
    )


def hash_password(raw_password: str) -> str:
    """argon2id PHC-хеш для нового/сброшенного пароля оператора (раздел «Команда»).
    Plain-пароль в БД НЕ хранится; те же параметры _ph, что и у проверки."""
    return _ph.hash(raw_password)


async def authenticate(username: str, password: str) -> tuple[str, str] | None:
    """Единая точка входа логина → (actor, role) при успехе, иначе None.

    АДДИТИВНО, lockout НЕВОЗМОЖЕН:
      1) env-админ (ADMIN_USERNAME/ADMIN_PASSWORD_HASH) — bootstrap-суперюзер, работает
         ВСЕГДА мимо БД, роль 'admin'; проверяется ПЕРВЫМ. Имя env-админа зарезервировано:
         при совпадении логина, но неверном пароле — в БД НЕ уходим.
      2) БД-юзеры (admin_users): active + argon2 → (username, role).
    Без user-enumeration: обе ветки всегда выполняют полный argon2 (реальный/ _DUMMY_HASH)."""
    env_user_ok = verify_username(username)
    env_pass_ok = verify_password(password)  # всегда полный argon2 (реальный + dummy)
    if env_user_ok:
        return (config.ADMIN_USERNAME, "admin") if env_pass_ok else None
    return await _authenticate_db_user(username, password)


async def _authenticate_db_user(username: str, password: str) -> tuple[str, str] | None:
    """Проверка БД-пользователя (admin_users). ВСЕГДА один полный argon2-verify — на
    реальном хеше (юзер есть и активен) или на _DUMMY_HASH (нет/неактивен) — чтобы тайминг
    не отличал «нет логина» от «неверный пароль»."""
    uname = (username or "").strip().lower()
    row = await db.get_admin_user(uname) if uname else None
    usable = bool(row and row["active"])
    target = row["password_hash"] if usable else _DUMMY_HASH
    try:
        _ph.verify(target, password)
        ok = True
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        ok = False
    if ok and usable:
        return (row["username"], row["role"])
    return None


# --------------------------------------------------------------------------- #
# Троттл логина в БД (§3.4). НЕ in-memory: переживает редеплой контейнера.
# Стратегия — экспоненциальный TARPIT, не жёсткий account-lock: реальный оператор
# всегда сможет войти после окна, флудер лишь замедляется (анти-self-lockout —
# причина выбора tarpit над hard-lock в плане §3.4).
#
# ДВА счётчика (находка medium про распределённый брут + XFF-bypass):
#   • per-account (ключ = lowercased submitted username) — мягкий; атакующий может
#     дробить его, варьируя username, поэтому сам по себе он защиту не несёт;
#   • ГЛОБАЛЬНЫЙ (фиксированный ключ _GLOBAL_KEY) — атакующий не может его бесплатно
#     ротировать: ЛЮБОЙ неуспешный логин (с любым username/IP) инкрементит его.
# Тарпит берётся как max(per-account, global) → распределённый брут по разным
# username всё равно копит глобальную задержку. Панель — single-operator, поэтому
# глобальный счётчик не «блокирует чужих»: легитимный вход один.
#
# allowlist-CIDR (advisory, IP спуфится за LB) снимает ТОЛЬКО мягкую per-account
# составляющую; ГЛОБАЛЬНЫЙ тарпит не обходится bypass'ом — иначе подделка
# X-Forwarded-For: <ip из allowlist> полностью снимала бы единственный rate-limit.
# --------------------------------------------------------------------------- #
_TARPIT_FREE_FAILS = 5          # первые N промахов — без задержки (per-account)
_TARPIT_MAX_SECONDS = 30        # потолок сна
_GLOBAL_KEY = "__global__"      # фиксированный ключ глобального счётчика (не username)
_GLOBAL_FREE_FAILS = 20         # глобальный порог выше: не мешать нормальным опечаткам оператора


def _delay_for(fails: int, free: int) -> float:
    fails = int(fails or 0)
    if fails <= free:
        return 0.0
    return float(min(2 ** (fails - free), _TARPIT_MAX_SECONDS))


async def login_tarpit_delay(account: str) -> float:
    """Per-account тарпит-задержка (мягкая составляющая, может быть снята bypass'ом)."""
    async with db.pool.acquire() as c:
        fails = await c.fetchval(
            "select fail_count from admin_login_throttle where account = $1", account
        )
    return _delay_for(fails, _TARPIT_FREE_FAILS)


async def _global_tarpit_delay() -> float:
    """Глобальная тарпит-задержка (жёсткая: НЕ снимается allowlist-bypass'ом)."""
    async with db.pool.acquire() as c:
        fails = await c.fetchval(
            "select fail_count from admin_login_throttle where account = $1", _GLOBAL_KEY
        )
    return _delay_for(fails, _GLOBAL_FREE_FAILS)


async def register_login_failure(account: str) -> None:
    """Инкремент per-account И глобального счётчиков (оба переживают редеплой)."""
    async with db.pool.acquire() as c:
        await c.executemany(
            """
            insert into admin_login_throttle (account, fail_count, locked_until)
            values ($1, 1, null)
            on conflict (account) do update
              set fail_count = admin_login_throttle.fail_count + 1
            """,
            [(account,), (_GLOBAL_KEY,)],
        )


async def reset_login_throttle(account: str) -> None:
    """Успешный вход: сбрасываем per-account И глобальный счётчики (брут прерван)."""
    async with db.pool.acquire() as c:
        await c.execute(
            "update admin_login_throttle set fail_count = 0, locked_until = null "
            "where account = any($1::text[])",
            [account, _GLOBAL_KEY],
        )


async def apply_tarpit(account: str, *, bypass: bool) -> None:
    """Спим max(per-account, global) тарпит-задержку.

    bypass (advisory allowlist-CIDR) снимает только per-account составляющую;
    глобальный тарпит применяется ВСЕГДА — спуф X-Forwarded-For из allowlist не
    обходит единственный rate-limit против распределённого брута.
    """
    global_delay = await _global_tarpit_delay()
    account_delay = 0.0 if bypass else await login_tarpit_delay(account)
    delay = max(global_delay, account_delay)
    if delay > 0:
        await asyncio.sleep(delay)


async def signup_tarpit_and_count(account: str) -> None:
    """Тарпит ПУБЛИЧНОЙ регистрации (анти-абьюз self-serve): спим по НАКОПЛЕННОМУ счётчику
    ЭТОГО ключа (per-IP), затем инкрементим его — каждая следующая регистрация с того же IP
    медленнее (экспонента до _TARPIT_MAX_SECONDS). БЕЗ глобального ключа: массовая регистрация
    НЕ должна деградировать вход всем операторам (в отличие от login-тарпита). Сбросов нет —
    счётчик копится (admin_login_throttle, отдельное пространство ключей 'signup:<ip>').
    NB: это лишь brake; жёсткий лимит/капча на число тенантов — Фаза 2 (ревью)."""
    async with db.pool.acquire() as c:
        fails = await c.fetchval(
            "select fail_count from admin_login_throttle where account = $1", account
        )
    delay = _delay_for(fails, _TARPIT_FREE_FAILS)
    if delay > 0:
        await asyncio.sleep(delay)
    async with db.pool.acquire() as c:
        await c.execute(
            "insert into admin_login_throttle (account, fail_count, locked_until) values ($1, 1, null) "
            "on conflict (account) do update set fail_count = admin_login_throttle.fail_count + 1",
            account,
        )


# --------------------------------------------------------------------------- #
# Серверные сессии (§3.2). Один активный sid на оператора (single-session):
# при логине ревокаем прежние строки актора и пишем новую (ротация → анти-fixation).
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Session:
    sid: str
    actor: str
    csrf_token: str
    # Роль актора: 'admin' | 'operator'. Гейтит раздел «Команда» (_require_admin).
    # env-админ ВСЕГДА 'admin' (вычисляется в load_session поверх хранимого значения).
    role: str = "operator"
    # Серверное состояние поиска по телефону (§3.10): обратимый хеш живёт здесь,
    # а НЕ в query-string/истории/логах. None = активного поиска по телефону нет.
    search_phone_hash: str | None = None
    # Reseller Wave 1: активный тенант сессии (admin_sessions.active_tenant_id).
    # None = тенант не выбран (легаси-сессии до миграции) — маршруты резолвят дефолт.
    active_tenant_id: str | None = None
    active_tenant_name: str | None = None
    # Статус активного тенанта (tenants.status). 'provisioning' → кабинет показывает
    # баннер «ИИ-сотрудник готовится» (self-serve клиент до привязки бота владельцем).
    active_tenant_status: str | None = None
    # СП-2b: слаг активного тенанта (tenants.slug) — для детекта School-тенанта
    # (config.DEFAULT_TENANT_SLUG) при выборе источника отделов базы знаний.
    active_tenant_slug: str | None = None

    @property
    def is_platform(self) -> bool:
        """Платформенный супер-админ = ТОЛЬКО env-админ (config.ADMIN_USERNAME), живущий ВНЕ
        admin_users. Гейтит ПЛАТФОРМЕННЫЕ поверхности: /team (все учётки), /tenants-switch на
        любой тенант, сводку/экономику по всем клиентам. ⚠️ НЕ отождествлять с role=='admin':
        клиент-владелец своего кабинета (self-serve) платформенным супером НЕ является."""
        return self.actor == config.ADMIN_USERNAME


def _csrf_for_sid(sid: str) -> str:
    """CSRF-токен, детерминированно выведенный из sid (§3.5, Synchronizer Token).

    HMAC(SESSION_SECRET, "csrf:"+sid): per-session, неугадываем без секрета,
    ротируется вместе с sid при логине, «умирает» с сессией (load_session→None →
    сравнивать не с чем). Без отдельной колонки в схеме — не зависим от того,
    добавит ли её миграция; привязка к серверной сессии сохраняется.
    """
    mac = hmac.new(config.SESSION_SECRET.encode(), ("csrf:" + sid).encode(), hashlib.sha256)
    return mac.hexdigest()


async def create_session(actor: str, role: str = "operator") -> str:
    """Ротация: ревокация всех прежних сессий актора + новая строка. Возврат sid.
    role фиксируется в момент логина (для env-админа load_session всё равно поднимет до 'admin')."""
    sid = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    expires = now + timedelta(hours=config.SESSION_MAX_HOURS)
    async with db.pool.acquire() as c:
        async with c.transaction():
            await c.execute(
                "update admin_sessions set revoked = true where actor = $1 and revoked = false",
                actor,
            )
            # Активный тенант по умолчанию: ТОЛЬКО env-админ (платформенный супер) видит
            # «первый живой» тенант; ВСЕ БД-юзеры — операторы И клиенты-владельцы (role='admin'
            # своего кабинета) — строго по memberships. Иначе клиент-admin получил бы «первый
            # глобальный» = ЧУЖОЙ тенант (утечка между клиентами при self-serve регистрации).
            if actor == config.ADMIN_USERNAME:
                default_tenant = await c.fetchval(
                    "select id from tenants where status in ('provisioning','active') "
                    "order by created_at limit 1"
                )
            else:
                default_tenant = await c.fetchval(
                    "select t.id from tenants t join memberships m on m.tenant_id = t.id "
                    "where m.username = $1 and t.status in ('provisioning','active') "
                    "order by t.created_at limit 1",
                    actor,
                )
            await c.execute(
                """
                insert into admin_sessions
                    (sid, actor, issued_at, last_seen, expires_at, revoked, role, active_tenant_id)
                values ($1, $2, $3, $3, $4, false, $5, $6)
                """,
                sid, actor, now, expires, role, default_tenant,
            )
    return sid


async def load_session(sid: str) -> Session | None:
    """Авторитетная проверка сессии + скользящий idle. Любой сбой → None.

    Отказ если: нет строки / revoked / expires_at<now (жёсткий 8ч потолок) /
    last_seen < now - idle. Иначе бампаем last_seen (скользящее окно) и возвращаем.
    """
    now = datetime.now(timezone.utc)
    idle_cut = now - timedelta(minutes=config.SESSION_IDLE_MIN)
    async with db.pool.acquire() as c:
        async with c.transaction():
            row = await c.fetchrow(
                """
                select s.sid, s.actor, s.last_seen, s.expires_at, s.revoked, s.role,
                       s.search_phone_hash, s.active_tenant_id,
                       t.name as active_tenant_name, t.status as active_tenant_status,
                       t.slug as active_tenant_slug
                from admin_sessions s
                left join tenants t on t.id = s.active_tenant_id
                where s.sid = $1
                for update of s
                """,
                sid,
            )
            if (
                row is None
                or row["revoked"]
                or row["expires_at"] <= now
                or row["last_seen"] < idle_cut
            ):
                return None
            await c.execute(
                "update admin_sessions set last_seen = $2 where sid = $1", sid, now
            )
            sid_str = str(row["sid"])
            # env-админ — суперюзер вне БД: его роль ВСЕГДА 'admin', независимо от хранимого
            # (защита от мис-скоупа старых строк/бэкфилла). БД-юзеры — по хранимой роли.
            role = "admin" if row["actor"] == config.ADMIN_USERNAME else (row["role"] or "operator")
            return Session(
                sid=sid_str,
                actor=row["actor"],
                csrf_token=_csrf_for_sid(sid_str),
                role=role,
                search_phone_hash=row["search_phone_hash"],
                active_tenant_id=str(row["active_tenant_id"]) if row["active_tenant_id"] else None,
                active_tenant_name=row["active_tenant_name"],
                active_tenant_status=row["active_tenant_status"],
                active_tenant_slug=row["active_tenant_slug"],
            )


async def revoke_session(sid: str) -> None:
    async with db.pool.acquire() as c:
        await c.execute(
            "update admin_sessions set revoked = true where sid = $1", sid
        )


async def set_search_phone_hash(sid: str, phone_hash: str | None) -> None:
    """Сохранить/сбросить серверное состояние поиска по телефону (§3.10).

    Обратимый unsalted sha256(phone) держим в строке сессии, а не в URL/истории/
    логах. В query-string едет лишь opaque-маркер qid=phone. None очищает поиск.
    """
    async with db.pool.acquire() as c:
        await c.execute(
            "update admin_sessions set search_phone_hash = $2 where sid = $1",
            sid, phone_hash,
        )


# --------------------------------------------------------------------------- #
# Cookie: подпись/чтение sid. itsdangerous даёт tamper-evidence (подделанный sid
# не пройдёт verify раньше, чем дойдёт до БД).
# --------------------------------------------------------------------------- #
def sign_sid(sid: str) -> str:
    return _signer.dumps(sid)


def unsign_sid(cookie_value: str | None) -> str | None:
    if not cookie_value:
        return None
    try:
        val = _signer.loads(cookie_value)
        return val if isinstance(val, str) else None
    except BadSignature:
        return None


def set_session_cookie(response, sid: str) -> None:
    """__Host-session: HttpOnly, Secure (безусловно по конфигу), SameSite=Strict,
    Path=/, без Domain. __Host- блокирует cookie-tossing с соседних *.twc1.net.
    """
    response.set_cookie(
        key=config.COOKIE_NAME,
        value=sign_sid(sid),
        max_age=config.SESSION_MAX_HOURS * 3600,
        httponly=True,
        secure=config.COOKIE_SECURE,
        samesite="strict",
        path="/",
    )


def clear_session_cookie(response) -> None:
    response.delete_cookie(key=config.COOKIE_NAME, path="/")


# --------------------------------------------------------------------------- #
# CSRF — Synchronizer Token Pattern (§3.5). Токен привязан к серверной сессии
# (admin_sessions.csrf_token), кладётся в скрытый input каждой формы, на каждом
# POST сверяется constant-time. Несовпадение → 403 (бросаем в app.py).
# Для /login (сессии ещё нет) — отдельный pre-session токен в подписанной cookie.
# --------------------------------------------------------------------------- #
def check_csrf(session_token: str, submitted: str | None) -> bool:
    if not submitted:
        return False
    return secrets.compare_digest(session_token.encode(), submitted.encode())


# --- pre-session CSRF для формы логина ---
_login_csrf_signer = URLSafeSerializer(config.SESSION_SECRET, salt="admin-login-csrf")
LOGIN_CSRF_COOKIE = ("__Host-login_csrf" if config.COOKIE_SECURE else "login_csrf")


def set_login_csrf_cookie(response, token: str) -> None:
    """Привязать pre-session CSRF-токен формы логина к подписанной cookie."""
    response.set_cookie(
        key=LOGIN_CSRF_COOKIE,
        value=_login_csrf_signer.dumps(token),
        max_age=1800,
        httponly=True,
        secure=config.COOKIE_SECURE,
        samesite="strict",
        path="/",
    )


def verify_login_csrf(cookie_value: str | None, submitted: str | None) -> bool:
    if not cookie_value or not submitted:
        return False
    try:
        expected = _login_csrf_signer.loads(cookie_value)
    except BadSignature:
        return False
    if not isinstance(expected, str):
        return False
    return secrets.compare_digest(expected.encode(), submitted.encode())


def clear_login_csrf(response) -> None:
    response.delete_cookie(key=LOGIN_CSRF_COOKIE, path="/")


# --- Telegram Login Widget: проверка подписи payload (вход через Telegram) ---
# Виджет (telegram.org/js/telegram-widget.js) после авторизации отдаёт поля
# id/first_name/last_name/username/photo_url/auth_date/hash. Подлинность: HMAC-SHA256
# по data_check_string (все поля КРОМЕ hash, "k=v" через \n, отсортированы по ключу) с
# ключом = sha256(bot_token). Сверка constant-time + свежесть auth_date (анти-replay
# перехваченного callback-URL). Алгоритм — официальный (core.telegram.org/widgets/login).
_TG_AUTH_MAX_AGE = 300  # сек: интерактивный вход доезжает за секунды; окно сужает replay

# Anti-CSRF / anti-replay state для Telegram-входа (паритет с ВК): на /login GET ставим
# подписанную cookie со state, в data-auth-url виджета кладём ?st=<state>; callback сверяет
# (constant-time) и УДАЛЯЕТ cookie (одноразово). Закрывает login-CSRF (форс-логин жертвы в
# чужой аккаунт) и replay перехваченного callback-URL без соответствующей cookie (ревью).
_TG_STATE_MAX_AGE = 600
_tg_state_signer = URLSafeTimedSerializer(config.SESSION_SECRET, salt="tg-oauth-state")


def seal_tg_state(state: str) -> str:
    return _tg_state_signer.dumps(state)


def open_tg_state(sealed: str | None) -> str | None:
    if not sealed:
        return None
    try:
        val = _tg_state_signer.loads(sealed, max_age=_TG_STATE_MAX_AGE)
    except (BadSignature, Exception):  # noqa: BLE001 — подделка/просрочка → None
        return None
    return val if isinstance(val, str) else None


def verify_telegram_login(data: dict[str, str]) -> dict[str, str] | None:
    """Проверка payload Telegram Login Widget → провалидированные поля или None.
    `data` ДОЛЖЕН содержать ТОЛЬКО поля от Telegram (id/…/auth_date/hash), без посторонних
    ключей (иначе data_check_string разойдётся). Требует config.TELEGRAM_BOT_TOKEN."""
    token = config.TELEGRAM_BOT_TOKEN
    received = (data or {}).get("hash")
    if not token or not received:
        return None
    pairs = sorted(f"{k}={v}" for k, v in data.items() if k != "hash")
    data_check_string = "\n".join(pairs)
    secret_key = hashlib.sha256(token.encode()).digest()
    computed = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(computed, received):
        return None
    try:
        auth_date = int(data.get("auth_date", "0"))
    except (TypeError, ValueError):
        return None
    age = datetime.now(timezone.utc).timestamp() - auth_date
    if abs(age) > _TG_AUTH_MAX_AGE:   # просрочен ИЛИ из будущего (скос часов) → отказ
        return None
    return data
