"""FastAPI-приложение админ-панели лидов «Школа Лесова».

Server-rendered (Jinja2), без внешних CDN/шрифтов/JS. deny-by-default: каждый
маршрут с ПДн требует валидной серверной сессии (Depends(require_session)).
Все сайд-эффекты — POST с CSRF-токеном. Полный телефон не селектится для
списка/карточки — только хвост из SQL; полный номер раскрывается лишь в
POST /reveal (с аудитом) и POST /export-full.csv (gated, отдельный аудит).

Связь модулей:
  config   — env + fail-fast + справочники/лейблы
  db       — asyncpg pool, filter-builder, запросы (только $-параметры)
  security — заголовки/CSP/body-guard/scrub/маска/IP (без БД)
  auth     — argon2, серверные сессии, троттл, CSRF, cookie
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import re
import secrets
import uuid

import asyncpg
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from math import ceil
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, File, Form, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import StreamingResponse

import auth
import config
import db
import kb
import oauth_vk
import security
import timeweb_ai
import yookassa

from shared import money, vault


# --------------------------------------------------------------------------- #
# Управляющие исключения авторизации (ловятся хендлерами ниже).
# --------------------------------------------------------------------------- #
class AuthRedirect(Exception):
    """Нет валидной сессии → 303 на /login (deny-by-default для HTML-маршрутов)."""

    def __init__(self, next_path: str = "/") -> None:
        self.next_path = next_path


class CSRFError(Exception):
    """Невалидный/отсутствующий CSRF-токен или чужой Origin/Referer → 403."""


# --------------------------------------------------------------------------- #
# Lifespan: поднимаем/гасим asyncpg pool (как бот в init/close).
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def lifespan(_: FastAPI):
    await db.init()
    # Wave 2b: cron автосписаний рекуррента (за фиче-флагом SERVICE_RENEWAL_ENABLED;
    # при OFF — таска сразу завершается). Один процесс uvicorn → без дублей.
    import renewal
    renewal_task = asyncio.create_task(renewal.run())
    try:
        yield
    finally:
        renewal_task.cancel()
        await db.close()


app = FastAPI(
    title="Админ-панель лидов — Школа Лесова",
    docs_url=None, redoc_url=None, openapi_url=None,  # никакой авто-документации с PII-схемой
    lifespan=lifespan,
)

# Middleware-порядок: body-guard ДО всего (отбить большое тело раньше парсинга),
# заголовки — снаружи, чтобы лечь на любой ответ, включая ошибки.
app.add_middleware(security.SecurityHeadersMiddleware)
# Глобальный body-guard 64 KB; ТОЛЬКО путь загрузки файла рассылки имеет свой
# больший лимит (план §6.5) — остальные маршруты не ослабляются. Streaming-обрыв
# на превышении (для chunked) делает сам хендлер /broadcasts через read_upload_capped.
app.add_middleware(
    security.BodySizeLimitMiddleware,
    max_bytes=config.MAX_BODY_BYTES,
    # Точечные больши́е лимиты только для путей с загрузкой файла (план §6.5):
    # POST /broadcasts (разовый файл рассылки) и POST /products (файл офера каталога,
    # потолок 50 МБ = лимит Telegram-бота). Остальные маршруты держат строгий 64 KB.
    per_path_limits={
        "/broadcasts": config.MAX_UPLOAD_BYTES,
        "/products": config.MAX_PRODUCT_FILE_BYTES,
        # Загрузка файла в базу знаний (≤10 МБ) и большой промпт роли (роль/задачи/поведение)
        # не должны упираться в глобальные 64 КБ. /agents/role/<slug> — динамический, поэтому
        # точечные записи на каждый известный слаг персоны.
        "/knowledge/upload": config.MAX_KB_FILE_BYTES,
        **{f"/agents/role/{slug}": config.PERSONA_POST_MAX_BYTES for slug in config.PERSONA_PRESETS},
    },
    # Динамический путь вложения личного ответа POST /leads/{uuid}/reply — точный
    # per_path не выписать (uuid в середине), поэтому суффикс-матч. Лимит = потолок
    # файла офера (≤50 МБ Telegram); read_upload_capped в хендлере дублирует защиту.
    per_path_suffix_limits={
        # /reply несёт НЕСКОЛЬКО вложений суммарно → лимит выше пофайлового; каждый файл
        # всё равно ≤ MAX_PRODUCT_FILE_BYTES (read_upload_capped) и ≤50 МБ у бота.
        "/reply": config.MAX_REPLY_BODY_BYTES,
    },
)

templates = Jinja2Templates(directory="templates")
# Шаблоны строго экранируют HTML (autoescape по умолчанию в Jinja2Templates).


def _static_version() -> str:
    """Версия статики для cache-busting (?v=…). Макс. mtime файлов в static/ —
    меняется при каждом деплое (файлы пересобираются в образе), поэтому браузер
    гарантированно тянет СВЕЖИЕ styles.css/reply.js/thread.js, а не закэшированные
    (без этого после правок JS/CSS оператор видел бы старую версию: мёртвые кнопки,
    несвёрстанный композер). Считаем один раз на старте процесса."""
    import os
    latest = 0.0
    for root, _dirs, files in os.walk("static"):
        for f in files:
            try:
                latest = max(latest, os.path.getmtime(os.path.join(root, f)))
            except OSError:
                pass
    return str(int(latest))


# Глобал шаблонов: доступен во ВСЕХ шаблонах как {{ asset_version }} без правки контекстов.
templates.env.globals["asset_version"] = _static_version()
# Публичный сайт сервиса — для CTA «выбрать тариф» в provisioning-баннере кабинета (base.html).
templates.env.globals["service_site_url"] = config.SERVICE_SITE_URL

app.mount("/static", StaticFiles(directory="static"), name="static")


# --------------------------------------------------------------------------- #
# Хелперы запроса
# --------------------------------------------------------------------------- #
def _ip(request: Request) -> str | None:
    return security.client_ip(request)


def _ua(request: Request) -> str | None:
    ua = request.headers.get("user-agent")
    return ua[:512] if ua else None


def _same_origin(request: Request) -> bool:
    """Проверка Origin/Referer = свой хост — ВТОРИЧНЫЙ слой (основной контроль —
    CSRF-токен, §3.5).

    Источник часто НЕЛЬЗЯ достоверно определить, и это НЕ повод блокировать:
      • заголовков нет (некоторые клиенты их не шлют);
      • Origin='null' — opaque origin: Safari/WebKit при Referrer-Policy: no-referrer
        шлёт ровно это на POST-формы, плюс так делают приватные расширения;
      • значение без схемы (неразбираемо).
    Во всех этих случаях НЕ блокируем и полагаемся на токен. Блокируем ТОЛЬКО при
    ЯВНОМ несовпадении разобранного хоста — настоящий cross-site POST с чужого сайта
    несёт реальный чужой Origin и здесь отбивается (а токен он подделать не может).
    """
    target = request.headers.get("host")
    if not target:
        return True
    for hdr in ("origin", "referer"):
        val = request.headers.get(hdr)
        if not val or val == "null" or "://" not in val:
            continue  # источник не определить → доверяем CSRF-токену
        host = val.split("://", 1)[1].split("/", 1)[0]
        return host == target
    return True


async def require_session(request: Request) -> auth.Session:
    """deny-by-default Depends на каждый маршрут с ПДн.

    Cookie → unsign sid → авторитетная проверка admin_sessions (revoked/exp/idle) +
    бамп last_seen. Любой сбой → AuthRedirect на /login c next=текущий путь.
    """
    raw = request.cookies.get(config.COOKIE_NAME)
    sid = auth.unsign_sid(raw)
    if not sid:
        raise AuthRedirect(_safe_next(request.url.path))
    session = await auth.load_session(sid)
    if session is None:
        raise AuthRedirect(_safe_next(request.url.path))
    # Харденинг №2: фиксируем активный тенант запроса → каждый acquire пула проставит его
    # в app.tenant_id (RLS на leads/messages/outbox). Запрос и хендлеры — одна asyncio-задача,
    # contextvar виден сквозь Depends. Маршруты без сессии (login/health/webhook) сюда не идут.
    db.set_active_tenant(session.active_tenant_id)
    return session


def _safe_next(path: str) -> str:
    """Только локальный путь (без схемы/хоста) — иначе open-redirect через ?next=."""
    if not path or not path.startswith("/") or path.startswith("//"):
        return "/"
    return path


async def _enforce_csrf(request: Request, session: auth.Session, submitted: str | None) -> None:
    if not _same_origin(request):
        raise CSRFError()
    if not auth.check_csrf(session.csrf_token, submitted):
        raise CSRFError()


# --------------------------------------------------------------------------- #
# Парсинг query-фильтров (не-ПДн) — shareable, переживают F5, идут в экспорт.
# Возвращает (filters_dict, raw_query_form) для filter-builder и для рендера.
# --------------------------------------------------------------------------- #
def _parse_filters(request: Request, session: auth.Session) -> tuple[dict, dict]:
    qp = request.query_params
    status = qp.get("status") or None
    source = qp.get("source") or None
    messenger = qp.get("messenger") or None

    consent_raw = qp.get("consent")
    consent = None
    if consent_raw in ("1", "true", "yes"):
        consent = True
    elif consent_raw in ("0", "false", "no"):
        consent = False

    erase_raw = qp.get("erase")
    erase_pending = None
    if erase_raw in ("1", "true", "yes"):
        erase_pending = True

    # Поиск по телефону (§3.10): в URL едет ТОЛЬКО opaque-маркер qid=phone — без
    # цифр и без обратимого хеша. Сам unsalted sha256(phone) живёт в серверной
    # сессии (admin_sessions.search_phone_hash), поэтому в историю браузера/логи
    # LB/Referer обратимый ПДн-хеш не попадает. q_hash берём из сессии, не из URL.
    qid_marker = qp.get("qid")
    if qid_marker == "phone" and session.search_phone_hash:
        q_hash = session.search_phone_hash
        qid_out = "phone"
    else:
        q_hash = None
        qid_out = ""

    # Поиск по имени допустим прямо в query (имя — ПДн, но без цифр телефона;
    # держим короткое, не эхо-им в Referer за счёт no-referrer). Длину кап.
    q_name = (qp.get("qname") or "").strip()[:100] or None

    sort = qp.get("sort") or db.DEFAULT_SORT
    if sort not in ("created_desc", "updated_desc"):
        sort = db.DEFAULT_SORT

    filters = dict(
        status=status, source=source, messenger=messenger,
        consent=consent, q_hash=q_hash, q_name=q_name, erase_pending=erase_pending,
    )
    raw = dict(
        status=status or "", source=source or "", messenger=messenger or "",
        consent=consent_raw or "", erase=erase_raw or "",
        qid=qid_out, qname=q_name or "", sort=sort,
    )
    return filters, raw


def _filters_querystring(raw: dict, **overrides) -> str:
    """Собрать query-string из не-ПДн фильтров (для пагинации/экспорт-ссылок)."""
    merged = {k: v for k, v in raw.items() if v}
    merged.update({k: v for k, v in overrides.items() if v not in (None, "")})
    return urlencode(merged)


# =========================================================================== #
# Маршруты
# =========================================================================== #

# ---- /healthz — БЕЗ БД, без секретов, no-store (§4.7) ---------------------- #
@app.get("/healthz")
async def healthz() -> JSONResponse:
    return JSONResponse({"status": "ok"}, headers={"Cache-Control": "no-store"})


# ---- /login GET ----------------------------------------------------------- #
@app.get("/login", response_class=HTMLResponse)
async def login_form(
    request: Request, error: str | None = None, next: str = "/",
    signup_error: str | None = None, social_error: str | None = None,
    registered: int = 0, tab: str = "signin",
):
    # Если уже есть валидная сессия — на дашборд (не показываем логин повторно).
    sid = auth.unsign_sid(request.cookies.get(config.COOKIE_NAME))
    if sid and await auth.load_session(sid):
        return RedirectResponse(url="/", status_code=303)

    token = secrets.token_urlsafe(32)
    # Парадная: соц-вход/регистрация показываются ТОЛЬКО при включённых флагах
    # (PUBLIC_SIGNUP_ENABLED / OAUTH_*). OFF → чистый ребренд + операторский вход.
    signup_enabled = config.PUBLIC_SIGNUP_ENABLED
    tg_enabled = signup_enabled and config.OAUTH_TELEGRAM_ENABLED
    # Anti-CSRF state для Telegram-входа: кладём в data-auth-url ?st=<state> и в cookie (ниже).
    tg_state = secrets.token_urlsafe(24) if tg_enabled else ""
    tg_auth_url = (f"/auth/telegram/callback?{urlencode({'st': tg_state})}" if tg_enabled
                   else "/auth/telegram/callback")
    resp = templates.TemplateResponse(
        request,
        "login.html",
        {
            "csrf_token": token,
            "error": _login_error_text(error),
            "next": _safe_next(next),
            "signup_enabled": signup_enabled,
            "tg_enabled": tg_enabled,
            "tg_bot_username": config.TELEGRAM_BOT_USERNAME,
            "tg_auth_url": tg_auth_url,
            "vk_enabled": signup_enabled and oauth_vk.enabled(),
            "signup_password_min": config.SIGNUP_PASSWORD_MIN,
            "signup_error": _signup_error_text(signup_error),
            "social_error": _social_error_text(social_error),
            "registered_ok": bool(registered),
            # Какую вкладку показать первой (после ошибки регистрации — «Регистрация»).
            "tab": "signup" if (signup_enabled and (signup_error or tab == "signup")) else "signin",
        },
    )
    # Кладём pre-session CSRF в подписанную cookie, привязав к токену формы.
    auth.set_login_csrf_cookie(resp, token)
    # Telegram anti-CSRF state в подписанной cookie (samesite=lax: callback — cross-site GET от telegram).
    if tg_enabled:
        resp.set_cookie(TG_OAUTH_COOKIE, auth.seal_tg_state(tg_state), max_age=600,
                        httponly=True, secure=config.COOKIE_SECURE, samesite="lax", path="/")
    return resp


def _login_error_text(error: str | None) -> str | None:
    if not error:
        return None
    # Единый текст без user-enumeration (§3.4) — любой код ошибки → один текст.
    return "Неверный логин или пароль."


def _signup_error_text(code: str | None) -> str | None:
    if not code:
        return None
    return {
        "bad_email": "Укажите корректный email.",
        "bad_password": f"Пароль — от {config.SIGNUP_PASSWORD_MIN} до {config.SIGNUP_PASSWORD_MAX} символов.",
        "no_consent": "Подтвердите согласие с офертой и обработкой персональных данных.",
        "exists": "Аккаунт с таким email уже зарегистрирован — войдите.",
        "csrf": "Сессия формы устарела. Обновите страницу и попробуйте снова.",
        "disabled": "Регистрация временно недоступна.",
    }.get(code, "Не удалось зарегистрироваться. Попробуйте ещё раз.")


def _social_error_text(code: str | None) -> str | None:
    if not code:
        return None
    return {
        "tg_bad": "Не удалось подтвердить вход через Telegram. Попробуйте ещё раз.",
        "vk_bad": "Не удалось войти через ВКонтакте. Попробуйте ещё раз.",
        "disabled": "Этот способ входа временно недоступен.",
    }.get(code, "Не удалось войти. Попробуйте ещё раз.")


# ---- /login POST ---------------------------------------------------------- #
@app.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    csrf_token: str = Form(""),
    next: str = Form("/"),
):
    account = (username or "").strip().lower() or "_unknown"
    ip = _ip(request)
    ua = _ua(request)
    next_path = _safe_next(next)

    # CSRF на самом логине (pre-session cookie).
    if not auth.verify_login_csrf(request.cookies.get(auth.LOGIN_CSRF_COOKIE), csrf_token):
        return _login_redirect(error="csrf", next=next_path)

    # advisory bypass троттла из сети оператора (IP спуфится → удобство, не контроль).
    bypass = security.ip_in_cidr(ip, config.LOGIN_ALLOWLIST_CIDR)
    # Тарпит ДО проверки пароля: замедляем брут, реальный оператор всё равно войдёт.
    await auth.apply_tarpit(account, bypass=bypass)

    # Клиент входит по email → резолвим в его username (account_identities). Оператор
    # входит по username — без изменений. Не нашли email → оставляем как есть (authenticate
    # вернёт None по dummy-хешу, без user-enumeration). Только при включённой регистрации.
    login_id = (username or "").strip()
    if config.PUBLIC_SIGNUP_ENABLED and "@" in login_id and _valid_email(login_id):
        mapped = await db.resolve_username_by_email(login_id)
        if mapped:
            login_id = mapped

    # Единая аутентификация: env-админ (bootstrap-суперюзер) ИЛИ БД-юзер (admin_users).
    # Возврат (actor, role) | None; constant-time/без enumeration — внутри auth.authenticate.
    auth_result = await auth.authenticate(login_id, password)
    if auth_result is None:
        await auth.register_login_failure(account)
        await db.audit(actor=account, action="login_fail", ip=ip, user_agent=ua,
                       detail={"reason": "bad_credentials"})
        return _login_redirect(error="bad", next=next_path)
    actor, role = auth_result

    # Успех: сброс троттла, ротация sid (анти-fixation), серверная сессия с ролью.
    await auth.reset_login_throttle(account)
    sid = await auth.create_session(actor, role)
    await db.audit(actor=actor, action="login_ok", ip=ip, user_agent=ua,
                   detail={"role": role})

    resp = RedirectResponse(url=next_path, status_code=303)
    auth.set_session_cookie(resp, sid)
    auth.clear_login_csrf(resp)
    return resp


def _login_redirect(*, error: str, next: str) -> RedirectResponse:
    qs = urlencode({"error": error, "next": next})
    return RedirectResponse(url=f"/login?{qs}", status_code=303)


# =========================================================================== #
# Парадная «ИИ-Агент Про»: публичная self-serve регистрация + вход через Telegram/ВК.
# Всё за флагом PUBLIC_SIGNUP_ENABLED (+ OAUTH_*). OFF → роуты ведут на /login, UI скрыт.
# Клиентская учётка = admin_user(role='admin') + tenant(provisioning) + membership(owner);
# способ входа маппится через account_identities (db.create_client_account).
# =========================================================================== #
VK_OAUTH_COOKIE = "__Host-vk_oauth" if config.COOKIE_SECURE else "vk_oauth"
TG_OAUTH_COOKIE = "__Host-tg_oauth" if config.COOKIE_SECURE else "tg_oauth"


def _signup_redirect(error: str) -> RedirectResponse:
    return RedirectResponse(url=f"/login?{urlencode({'signup_error': error, 'tab': 'signup'})}",
                            status_code=303)


def _social_redirect(error: str) -> RedirectResponse:
    return RedirectResponse(url=f"/login?{urlencode({'social_error': error})}", status_code=303)


def _panel_base_url(request: Request) -> str:
    """Публичный базовый URL панели для OAuth redirect_uri. Приоритет — явный env
    (должен ТОЧНО совпасть с зарегистрированным во ВК); иначе derive из заголовков за LB."""
    if config.PANEL_PUBLIC_BASE_URL:
        return config.PANEL_PUBLIC_BASE_URL
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}"


async def _issue_session(actor: str, role: str, *, ip, ua, action: str) -> RedirectResponse:
    """Выдать серверную сессию (ротация sid) + cookie + аудит → redirect в кабинет."""
    sid = await auth.create_session(actor, role)
    await db.audit(actor=actor, action=action, ip=ip, user_agent=ua, detail={"role": role})
    resp = RedirectResponse(url="/", status_code=303)
    auth.set_session_cookie(resp, sid)
    auth.clear_login_csrf(resp)
    return resp


# ---- /signup/register POST (email + пароль) ------------------------------- #
@app.post("/signup/register")
async def signup_register(
    request: Request,
    email: str = Form(""),
    password: str = Form(""),
    agree_oferta: str = Form(""),
    agree_pdn: str = Form(""),
    csrf_token: str = Form(""),
):
    if not config.PUBLIC_SIGNUP_ENABLED:
        return RedirectResponse(url="/login", status_code=303)
    ip = _ip(request)
    ua = _ua(request)
    # CSRF — тот же pre-session механизм, что у логина (форма на той же странице).
    if not auth.verify_login_csrf(request.cookies.get(auth.LOGIN_CSRF_COOKIE), csrf_token):
        return _signup_redirect("csrf")
    email = (email or "").strip().lower()
    # Анти-абьюз: per-IP накопительный тарпит регистрации (РЕАЛЬНО кусается — в отличие от
    # login-тарпита, который инкрементит только register_login_failure при неуспехе входа;
    # ревью нашёл, что на /signup он был no-op). Без глобального ключа — не деградирует вход
    # операторам. Жёсткий лимит/капча на число тенантов — Фаза 2 (ревью).
    await auth.signup_tarpit_and_count(f"signup:{ip or 'noip'}")
    if not _valid_email(email):
        return _signup_redirect("bad_email")
    if not (config.SIGNUP_PASSWORD_MIN <= len(password) <= config.SIGNUP_PASSWORD_MAX):
        return _signup_redirect("bad_password")
    if agree_oferta not in _CHECKBOX_ON or agree_pdn not in _CHECKBOX_ON:
        return _signup_redirect("no_consent")
    # email уже занят → не плодим учётку, ведём на вход (signup по своей природе раскрывает
    # занятость email — это норма для регистрации; брут осложнён тарпитом выше).
    if await db.find_identity("email", email) is not None:
        return _signup_redirect("exists")
    try:
        username, _tenant = await db.create_client_account(
            provider="email", external_id=email, name=email,
            password_hash=auth.hash_password(password), verified=False, ip=ip, user_agent=ua,
        )
    except asyncpg.UniqueViolationError:
        # Гонка двойного сабмита (find_identity прошёл у обоих) → не плодим, ведём на вход.
        return _signup_redirect("exists")
    # Авто-логин в provisioning-кабинет (role='operator' своего тенанта).
    return await _issue_session(username, "operator", ip=ip, ua=ua, action="signup_login")


# ---- /auth/telegram/callback GET (Telegram Login Widget) ------------------ #
@app.get("/auth/telegram/callback")
async def auth_telegram_callback(request: Request):
    if not (config.PUBLIC_SIGNUP_ENABLED and config.OAUTH_TELEGRAM_ENABLED):
        return _social_redirect("disabled")
    ip = _ip(request)
    ua = _ua(request)

    def _finish(resp: RedirectResponse) -> RedirectResponse:
        # state одноразов: удаляем cookie; no-store — чтобы callback с hash не осел в кэше.
        resp.delete_cookie(TG_OAUTH_COOKIE, path="/")
        resp.headers["Cache-Control"] = "no-store"
        return resp

    # Anti-CSRF/anti-replay: ?st ДОЛЖЕН совпасть со state из подписанной cookie (паритет с ВК).
    # Закрывает форс-логин жертвы в чужой аккаунт и replay перехваченного URL без cookie (ревью).
    st = request.query_params.get("st", "")
    expected = auth.open_tg_state(request.cookies.get(TG_OAUTH_COOKIE))
    if not expected or not st or not secrets.compare_digest(expected, st):
        return _finish(_social_redirect("tg_bad"))

    # ТОЛЬКО поля Telegram (без посторонних query, вкл. наш st) — иначе data_check_string разойдётся.
    tg_fields = {"id", "first_name", "last_name", "username", "photo_url", "auth_date", "hash"}
    fields = {k: v for k, v in request.query_params.items() if k in tg_fields}
    data = auth.verify_telegram_login(fields)
    if data is None:
        return _finish(_social_redirect("tg_bad"))
    tg_id = str(data["id"])
    ident = await db.find_identity("telegram", tg_id)
    if ident is not None:
        await db.touch_identity_login(ident["id"])
        return _finish(await _issue_session(ident["username"], "operator", ip=ip, ua=ua, action="login_telegram"))
    # Новый клиент: лёгкий per-IP тарпит на создание (логин существующих — без тормоза).
    await auth.signup_tarpit_and_count(f"signup:{ip or 'noip'}")
    name = (f"{data.get('first_name', '')} {data.get('last_name', '')}".strip()
            or data.get("username") or f"tg:{tg_id}")
    try:
        username, _t = await db.create_client_account(
            provider="telegram", external_id=tg_id, name=name,
            password_hash=auth.hash_password(secrets.token_urlsafe(32)),  # пароля нет → неюзабельный хеш
            display_name=name, verified=True, ip=ip, user_agent=ua,
        )
    except asyncpg.UniqueViolationError:
        # Гонка двух первых входов одного tg_id → учётку создал параллельный запрос: входим в неё.
        race = await db.find_identity("telegram", tg_id)
        if race is None:
            return _finish(_social_redirect("tg_bad"))
        username = race["username"]
    return _finish(await _issue_session(username, "operator", ip=ip, ua=ua, action="signup_telegram"))


# ---- /auth/vk/start + /auth/vk/callback (VK ID OAuth2 + PKCE) ------------- #
@app.get("/auth/vk/start")
async def auth_vk_start(request: Request):
    if not (config.PUBLIC_SIGNUP_ENABLED and oauth_vk.enabled()):
        return _social_redirect("disabled")
    state = secrets.token_urlsafe(24)
    verifier, challenge = oauth_vk.make_pkce()
    redirect_uri = _panel_base_url(request) + "/auth/vk/callback"
    resp = RedirectResponse(url=oauth_vk.authorize_url(redirect_uri, state, challenge), status_code=303)
    # state+verifier — в ПОДПИСАННОЙ короткоживущей cookie. samesite=lax: ВК возвращает
    # cross-site GET-навигацией, при strict cookie бы не доехала.
    resp.set_cookie(VK_OAUTH_COOKIE, oauth_vk.seal_state(state, verifier), max_age=600,
                    httponly=True, secure=config.COOKIE_SECURE, samesite="lax", path="/")
    return resp


@app.get("/auth/vk/callback")
async def auth_vk_callback(request: Request, code: str = "", state: str = "", device_id: str = ""):
    if not (config.PUBLIC_SIGNUP_ENABLED and oauth_vk.enabled()):
        return _social_redirect("disabled")
    ip = _ip(request)
    ua = _ua(request)
    opened = oauth_vk.open_state(request.cookies.get(VK_OAUTH_COOKIE))
    # state из cookie ДОЛЖЕН совпасть с возвращённым (anti-CSRF), код обязателен.
    if not opened or not code or not secrets.compare_digest(opened[0], state or ""):
        resp = _social_redirect("vk_bad")
        resp.delete_cookie(VK_OAUTH_COOKIE, path="/")
        return resp
    _state, verifier = opened
    redirect_uri = _panel_base_url(request) + "/auth/vk/callback"
    try:
        tok = await oauth_vk.exchange_code(code, verifier, device_id, redirect_uri)
        profile = await oauth_vk.fetch_user(tok["access_token"])
    except oauth_vk.VKError:
        resp = _social_redirect("vk_bad")
        resp.delete_cookie(VK_OAUTH_COOKIE, path="/")
        return resp
    vk_id = str(tok["user_id"])
    ident = await db.find_identity("vk", vk_id)
    if ident is not None:
        await db.touch_identity_login(ident["id"])
        resp = await _issue_session(ident["username"], "operator", ip=ip, ua=ua, action="login_vk")
    else:
        await auth.signup_tarpit_and_count(f"signup:{ip or 'noip'}")  # тормоз на создание (не на логин)
        name = (f"{profile.get('first_name', '')} {profile.get('last_name', '')}".strip()
                or f"vk:{vk_id}")
        try:
            username, _t = await db.create_client_account(
                provider="vk", external_id=vk_id, name=name,
                password_hash=auth.hash_password(secrets.token_urlsafe(32)),
                display_name=name, verified=True, ip=ip, user_agent=ua,
            )
        except asyncpg.UniqueViolationError:
            # Гонка двух первых входов одного vk_id → входим в учётку, созданную параллельно.
            race = await db.find_identity("vk", vk_id)
            if race is None:
                resp = _social_redirect("vk_bad")
                resp.delete_cookie(VK_OAUTH_COOKIE, path="/")
                return resp
            username = race["username"]
        resp = await _issue_session(username, "operator", ip=ip, ua=ua, action="signup_vk")
    resp.delete_cookie(VK_OAUTH_COOKIE, path="/")
    return resp


# ---- /logout POST --------------------------------------------------------- #
@app.post("/logout")
async def logout(
    request: Request,
    session: auth.Session = Depends(require_session),
    csrf_token: str = Form(""),
):
    await _enforce_csrf(request, session, csrf_token)
    await auth.revoke_session(session.sid)
    await db.audit(actor=session.actor, action="logout", ip=_ip(request), user_agent=_ua(request))
    resp = RedirectResponse(url="/login", status_code=303)
    auth.clear_session_cookie(resp)
    return resp


# ---- / — дашборд ---------------------------------------------------------- #
@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, session: auth.Session = Depends(require_session)):
    counts = await db.dashboard_counts({})
    by_source_rows = await db.dashboard_by_source({})

    total = counts["total"] or 0
    converted = counts["converted"] or 0
    conversion = round((converted / total) * 100, 1) if total else 0.0

    by_source = [
        {"source": r["source"],
         "label": config.SOURCE_LABELS.get(r["source"], r["source"]),
         "cnt": r["cnt"]}
        for r in by_source_rows
    ]

    # Платформенная сводка (клиенты + экономика по всем тенантам) — ТОЛЬКО роль admin
    # (клиент-оператор её не видит, как и блок «Экономика сервиса»). None → шаблон скрыт.
    platform = await _platform_ctx(session.is_platform)

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "counts": dict(counts),
            "conversion": conversion,
            "by_source": by_source,
            "platform": platform,
            "session": session,
            "csrf_token": session.csrf_token,
            "status_labels": config.STATUS_LABELS,
            "active": "dashboard",
        },
    )


async def _platform_ctx(is_platform: bool) -> dict | None:
    """Сводка по всем подключённым клиентам для дашборда — ТОЛЬКО платформенный супер
    (env-админ). Деньги из db.platform_summary (µRUB) форматируем в рубли для UI. Сбой →
    None (блок скрыт, дашборд не падает). ⚠️ Гейт по личности, НЕ по role (иначе клиент-
    владелец role-операции увидел бы экономику всех клиентов — ревью)."""
    if not is_platform:
        return None
    try:
        ps = await db.platform_summary()
    except Exception:  # noqa: BLE001 — сводка не должна ронять дашборд
        import logging
        logging.getLogger("admin-panel").warning("platform_summary упал", exc_info=True)
        return None

    def rub(micro: int) -> str:
        return money.micro_to_rub_str(int(micro)) + " ₽"

    return {
        "clients": ps["clients"],
        "totals": {k: rub(v) for k, v in ps["totals"].items()},
        "margin_negative": ps["totals"]["margin"] < 0,
        "tenants": [
            {
                "name": t["name"], "status": t["status"],
                "payments": rub(t["payments"]), "charged": rub(t["charged"]),
                "cost": rub(t["cost"]), "margin": rub(t["margin"]),
                "wallet": rub(t["wallet"]), "wallet_negative": t["wallet"] < 0,
            }
            for t in ps["tenants"]
        ],
    }


# ---- /leads — список + фильтры + пагинация -------------------------------- #
@app.get("/leads", response_class=HTMLResponse)
async def leads_list(request: Request, session: auth.Session = Depends(require_session)):
    filters, raw = _parse_filters(request, session)
    sort = raw["sort"]

    try:
        page = max(1, int(request.query_params.get("page", "1")))
    except ValueError:
        page = 1
    per_page = config.PER_PAGE
    offset = (page - 1) * per_page

    total = await db.count_leads(filters)
    rows = await db.list_leads(filters, sort=sort, limit=per_page, offset=offset)
    pages = max(1, ceil(total / per_page)) if total else 1

    lead_rows = [_present_list_row(r) for r in rows]

    # base query-string без page (для пагинатора) и полный (для экспорт-форм).
    base_qs = _filters_querystring(raw)
    return templates.TemplateResponse(
        request,
        "leads.html",
        {
            "rows": lead_rows,
            "filters": raw,
            "page": page,
            "per_page": per_page,
            "total": total,
            "pages": pages,
            "base_qs": base_qs,
            "csrf_token": session.csrf_token,
            "session": session,
            "status_labels": config.STATUS_LABELS,
            "source_labels": config.SOURCE_LABELS,
            "messenger_labels": config.MESSENGER_LABELS,
            "statuses": config.STATUSES,
            "sources": config.SOURCES,
            "messengers": config.MESSENGERS,
            # Таблица лидов живёт под разделом «Диалоги» — подсвечиваем его.
            "active": "dialogs",
        },
    )


def _present_list_row(r) -> dict:
    return {
        "id": r["id"],
        "created_at": r["created_at"],
        "updated_at": r["updated_at"],
        "name": r["name"],
        "phone_masked": security.mask_phone(r["phone_tail"], r["has_phone"]),
        "messenger": r["messenger"],
        "source": r["source"],
        "status": r["status"],
        "consent": r["consent"],
        "subscribed": r["subscribed"],
        "erase_requested_at": r["erase_requested_at"],
    }


# =========================================================================== #
# ДИАЛОГИ — мессенджер-вид (список контактов с бейджем канала + чат справа).
# Тот же набор данных, что у /leads, но с превью последнего сообщения, временем и
# счётчиком «без ответа». Право-панель переиспользует чат-композер из _chat.html
# (та же форма POST /leads/{id}/reply, та же thread.js-лента) — единый источник,
# чтобы интерфейс ответа не разъезжался (грабля #14 в handoff).
# =========================================================================== #

# Иконки-подписи вложений для превью в списке (зеркалит _thread.html).
_KIND_PREVIEW = {
    "photo": "🖼 Фото", "document": "📎 Файл", "video": "🎬 Видео",
    "video_note": "⭕ Кружок", "voice": "🎤 Голосовое", "audio": "🎵 Аудио",
    "animation": "🎞 GIF", "sticker": "🩶 Стикер", "other": "Вложение",
}

# Короткие коды каналов-источников для углового бейджа карточки диалога.
_SOURCE_SHORT = {
    "vk": "ВК", "reels": "RL", "dzen": "ДЗ", "youtube": "YT",
    "max": "MAX", "other": "•",
}

_WEEKDAYS_RU = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")


def _dialog_time_label(dt) -> str:
    """Компактная отметка времени для строки диалога: сегодня → ЧЧ:ММ,
    в пределах недели → день недели, иначе → ДД.ММ. UTC (как и тред)."""
    if dt is None:
        return ""
    now = datetime.now(timezone.utc)
    if dt.date() == now.date():
        return dt.strftime("%H:%M")
    delta = (now.date() - dt.date()).days
    if 1 <= delta < 7:
        return _WEEKDAYS_RU[dt.weekday()]
    return dt.strftime("%d.%m")


def _dialog_preview(kind: str | None, text: str | None, direction: str | None) -> str:
    """Превью последнего сообщения: иконка вложения + обрезанный текст, префикс
    «Вы: » для исходящих. Текст обрезаем (CSS-эллипсис добивает по ширине)."""
    body = (text or "").strip()
    if kind and kind != "text":
        tag = _KIND_PREVIEW.get(kind, "Вложение")
        body = f"{tag} {body}".strip() if body else tag
    body = body[:90]
    if direction == "out":
        body = "Вы: " + body
    return body or "—"


def _present_dialog_row(r) -> dict:
    return {
        "id": r["id"],
        "name": r["name"],
        "messenger": r["messenger"],
        "source": r["source"],
        "source_short": _SOURCE_SHORT.get(r["source"], "•"),
        "status": r["status"],
        "phone_masked": security.mask_phone(r["phone_tail"], r["has_phone"]),
        "last_preview": _dialog_preview(r["last_kind"], r["last_text"], r["last_direction"]),
        "time_label": _dialog_time_label(r["last_at"]),
        "unread": r["unread"],
        "bot_paused": r["bot_paused"],
        "unsubscribed_at": r["unsubscribed_at"],
        "erase_requested_at": r["erase_requested_at"],
    }


def _persona_label(slug: str) -> str:
    p = config.PERSONA_PRESETS.get(slug)
    return f'{p["name"]} — {p["role"]}' if p else ""


def _effective_persona(lead_ai_persona, source, channel_map: dict, global_persona: str) -> str:
    """Какой ИИ-сотрудник РЕАЛЬНО ведёт лида (та же лестница приоритетов, что у бота):
    диалог (leads.ai_persona) > канал > глобальная настройка > дефолтный агент (Лия)."""
    for cand in (lead_ai_persona, channel_map.get(source or "", ""), global_persona):
        c = (cand or "").strip()
        if c in config.PERSONA_PRESETS:
            return c
    return "liya"  # без назначения лида ведёт дефолтный агент — Лия (администратор)


async def _persona_stats() -> dict:
    """{slug: {leads, converted, conv_pct}} — нагрузка и конверсия по сотрудникам. Каждую
    группу (ai_persona, source) относим к её ЭФФЕКТИВНОМУ сотруднику и суммируем. Снимок по
    текущим назначениям (история смен персон не отслеживается — для решений «кто конвертит»
    достаточно)."""
    rows = await db.persona_dialog_stats()
    channel_map = (await db.get_channel_personas(tuple(config.SOURCES)))["personas"]
    global_persona = (await db.get_ai_settings()).get("persona") or ""
    agg: dict[str, list[int]] = {}
    for r in rows:
        eff = _effective_persona(r["ai_persona"], r["source"], channel_map, global_persona)
        b = agg.setdefault(eff, [0, 0])
        b[0] += r["leads"]
        b[1] += r["converted"]
    return {
        slug: {"leads": v[0], "converted": v[1],
               "conv_pct": round(v[1] * 100 / v[0]) if v[0] else 0}
        for slug, v in agg.items()
    }


async def _resolve_dialog_staff(lead: dict) -> dict:
    """Кто СЕЙЧАС отвечает этому диалогу + что выбрать в селекте. Приоритет:
    leads.ai_persona (ручной выбор диалога) > канал (ai_persona__<source>) > глобальная
    настройка (раздел «ИИ-агенты») > дефолтный агент (Лия). Возвращает текущий ручной slug
    (для select), имя эффективной персоны и область её действия (для подписи)."""
    source = lead.get("source") or "other"
    lead_persona = (lead.get("ai_persona") or "").strip()
    lead_persona = lead_persona if lead_persona in config.PERSONA_PRESETS else ""
    if lead_persona:
        eff, scope = lead_persona, "выбран для этого диалога"
    else:
        ch = (await db.get_channel_personas((source,)))["personas"].get(source, "")
        if ch:
            eff = ch
            scope = f'по каналу «{config.SOURCE_LABELS.get(source, source)}»'
        elif (await db.get_ai_settings()).get("persona"):
            eff, scope = (await db.get_ai_settings())["persona"], "общая настройка"
        else:
            eff, scope = "", "по умолчанию"
    return {
        "current": lead_persona,
        "effective_name": _persona_label(eff) if eff else "Лия — ИИ-администратор",
        "scope": scope,
        "options": [{"key": k, "label": _persona_label(k)} for k in config.PERSONA_ORDER],
    }


async def _render_dialogs(
    request: Request,
    session: auth.Session,
    *,
    selected_id=None,
    rec=None,
    thread=None,
    replied: bool = False,
    paused_flash: bool = False,
    staff_flash: bool = False,
    reply_err: str | None = None,
    invoiced: bool = False,
    invoice_err: str | None = None,
):
    """Единый рендер раздела «Диалоги»: список слева + (опц.) выбранный чат справа.
    rec=None → правая панель пустая (приглашение выбрать диалог)."""
    filters, raw = _parse_filters(request, session)
    rows = await db.list_dialogs(filters, limit=config.PER_PAGE, offset=0)
    dialogs = [_present_dialog_row(r) for r in rows]
    unanswered = await db.count_unanswered_dialogs()

    ctx: dict = {
        "dialogs": dialogs,
        "filters": raw,
        "selected_id": selected_id,
        "csrf_token": session.csrf_token,
        "session": session,
        "active": "dialogs",
        "nav_dialogs_badge": unanswered,
        "statuses": config.STATUSES,
        "status_labels": config.STATUS_LABELS,
        "source_labels": config.SOURCE_LABELS,
        "messenger_labels": config.MESSENGER_LABELS,
    }
    if rec is not None:
        lead = dict(rec)
        lead["phone_masked"] = security.mask_phone(rec["phone_tail"], rec["has_phone"])
        ctx.update({
            "lead": lead,
            "thread": thread or [],
            "replied": replied,
            "paused_flash": paused_flash,
            "reply_err": _reply_err_text(reply_err),
            "refresh_sec": config.THREAD_REFRESH_SEC,
            "msg_max": config.MSG_MAX_LEN,
            "accept_attr": _reply_accept_attr(),
            "max_file_mb": config.MAX_PRODUCT_FILE_MB,
            "chat_from": "dialog",   # композер вернёт PRG на /dialogs/{id}
        })
        # «Выставить счёт» (1B): селектор оферов с ценой — только когда онлайн-оплата
        # реально работоспособна (ключи магазина школы у панели + тумблер включён).
        invoice_products = []
        if config.SHOP_PAYMENTS_CONFIGURED and await db.get_online_payments_enabled():
            invoice_products = [
                {"id": p["id"], "name": p["name"],
                 "label": f"{p['name']} — {_fmt_price(p['price'], p['currency'])}"}
                for p in await db.list_priced_products_for_invoice()
            ]
        ctx.update({
            "invoice_products": invoice_products,
            "invoiced": invoiced,
            "invoice_err": _invoice_err_text(invoice_err),
            "dialog_staff": await _resolve_dialog_staff(lead),
            "staff_flash": staff_flash,
        })
    return templates.TemplateResponse(request, "dialogs.html", ctx)


# ---- /dialogs — список диалогов (правая панель пустая) -------------------- #
@app.get("/dialogs", response_class=HTMLResponse)
async def dialogs_index(request: Request, session: auth.Session = Depends(require_session)):
    return await _render_dialogs(request, session, selected_id=None)


# ---- /dialogs/{id} — список + выбранный чат справа ------------------------ #
@app.get("/dialogs/{lead_id}", response_class=HTMLResponse)
async def dialogs_detail(
    request: Request,
    lead_id: uuid.UUID,
    session: auth.Session = Depends(require_session),
    replied: int = 0,
    paused: int = 0,
    staff: int = 0,
    err: str | None = None,
    invoiced: int = 0,
    inv_err: str | None = None,
):
    rec = await db.get_lead(lead_id)
    if rec is None:
        raise StarletteHTTPException(status_code=404, detail="Лид не найден")
    # lead_view + thread_view в аудит (как на карточке) — открытие = чтение ПДн диалога.
    await db.audit(actor=session.actor, action="lead_view", lead_id=lead_id,
                   ip=_ip(request), user_agent=_ua(request))
    thread = await _load_thread_audited(request, session, lead_id)
    return await _render_dialogs(
        request, session, selected_id=lead_id, rec=rec, thread=thread,
        replied=bool(replied), paused_flash=bool(paused), staff_flash=bool(staff),
        reply_err=err, invoiced=bool(invoiced), invoice_err=inv_err,
    )


# ---- /dialogs/{id}/persona — сменить «ИИ-сотрудника» этого диалога --------- #
@app.post("/dialogs/{lead_id}/persona")
async def dialog_set_persona(
    request: Request,
    lead_id: uuid.UUID,
    session: auth.Session = Depends(require_session),
    persona: str = Form(""),
    from_: str = Form("dialog", alias="from"),
    csrf_token: str = Form(""),
):
    """Оператор назначает диалогу конкретного ИИ-сотрудника (или «По умолчанию» = сброс).
    Перекрывает канальную и глобальную настройку для ЭТОГО лида; бот применит со следующего
    его сообщения. При выборе персоны убеждаемся, что её агент создан (cloud_ai зовёт по
    access_id) — переиспользуем общий с «Каналами» реестр (один агент на персону)."""
    await _enforce_csrf(request, session, csrf_token)
    persona = persona.strip()
    if persona and persona not in config.PERSONA_PRESETS:
        return _chat_return(lead_id, from_, err="bad_persona")
    if persona:
        try:
            await _ensure_persona_agent(persona)
        except timeweb_ai.TimewebAIError:
            import logging
            logging.getLogger("admin-panel").exception("ensure_persona_agent (диалог) не удался")
            return _chat_return(lead_id, from_, err="persona_tw")
    await db.set_lead_persona(
        lead_id, persona, actor=session.actor, ip=_ip(request), user_agent=_ua(request)
    )
    base = "/dialogs" if from_ == "dialog" else "/leads"
    return RedirectResponse(url=f"{base}/{lead_id}?staff=1#thread", status_code=303)


def _chat_return(lead_id, from_: str, *, replied: bool = False,
                 paused: bool = False, err: str | None = None) -> RedirectResponse:
    """PRG-редирект после действия в чате. from_ ∈ {dialog, card} — allow-list,
    жёстко зашитые базовые пути (НЕ open-redirect: значение не подставляется в URL)."""
    base = "/dialogs" if from_ == "dialog" else "/leads"
    params = {}
    if replied:
        params["replied"] = "1"
    if paused:
        params["paused"] = "1"
    if err:
        params["err"] = err
    qs = urlencode(params)
    suffix = f"?{qs}#thread" if qs else "#thread"
    return RedirectResponse(url=f"{base}/{lead_id}{suffix}", status_code=303)


# ---- /leads/search — POST→PRG, телефон → хеш, чистый redirect (§3.10) ----- #
@app.post("/leads/search")
async def leads_search(
    request: Request,
    session: auth.Session = Depends(require_session),
    q: str = Form(""),
    csrf_token: str = Form(""),
):
    await _enforce_csrf(request, session, csrf_token)
    q = (q or "").strip()[:100]

    params: dict[str, str] = {}
    phone_hash: str | None = None
    if q:
        digits = "".join(ch for ch in q if ch.isdigit())
        if digits:
            # Телефон → хеш на сервере, хеш кладём в СЕССИЮ (не в URL). В query-string
            # едет лишь opaque-маркер qid=phone — без цифр и без обратимого хеша (§3.10).
            phone_hash = db.phone_query_hash(q)
            if phone_hash:
                params["qid"] = "phone"
        else:
            params["qname"] = q
    # Состояние поиска по телефону — серверное; обновляем (или очищаем) на каждом
    # поиске, чтобы новый запрос/сброс не тащил старый хеш.
    await auth.set_search_phone_hash(session.sid, phone_hash)

    # Сохраняем активные не-ПДн фильтры, если переданы скрытыми полями формы.
    form = await request.form()
    for key in ("status", "source", "messenger", "consent", "sort"):
        val = (form.get(key) or "").strip()
        if val:
            params[key] = val

    qs = urlencode(params)
    return RedirectResponse(url=f"/leads?{qs}" if qs else "/leads", status_code=303)


# ---- /leads/{id} — карточка (uuid-типизация → мусор не дойдёт до SQL) ------ #
@app.get("/leads/{lead_id}", response_class=HTMLResponse)
async def lead_detail(
    request: Request,
    lead_id: uuid.UUID,
    session: auth.Session = Depends(require_session),
    saved: int = 0,
    erased: int = 0,
    replied: int = 0,
    paused: int = 0,
    err: str | None = None,
):
    rec = await db.get_lead(lead_id)
    if rec is None:
        raise StarletteHTTPException(status_code=404, detail="Лид не найден")

    # lead_view в аудит на каждое открытие (§3.6).
    await db.audit(actor=session.actor, action="lead_view", lead_id=lead_id,
                   ip=_ip(request), user_agent=_ua(request))

    thread = await _load_thread_audited(request, session, lead_id)

    return templates.TemplateResponse(
        request,
        "lead.html",
        _lead_context(request, session, rec, revealed=None, saved=bool(saved),
                      erased=bool(erased), thread=thread, replied=bool(replied),
                      paused_flash=bool(paused), reply_err=err),
    )


# ---- /leads/{id}/thread — partial-обновление треда (без полной карточки) ---- #
@app.get("/leads/{lead_id}/thread", response_class=HTMLResponse)
async def lead_thread_partial(
    request: Request,
    lead_id: uuid.UUID,
    session: auth.Session = Depends(require_session),
):
    rec = await db.get_lead(lead_id)
    if rec is None:
        raise StarletteHTTPException(status_code=404, detail="Лид не найден")
    thread = await _load_thread_audited(request, session, lead_id)
    return templates.TemplateResponse(
        request,
        "_thread.html",
        {"lead_id": lead_id, "thread": thread, "partial": True,
         "refresh_sec": config.THREAD_REFRESH_SEC},
    )


async def _load_thread_audited(request, session, lead_id):
    """Аудит thread_view fail-closed ДО чтения треда (открытие = массовое чтение ПДн
    диалога, §3 плана; как reveal/export — если INSERT упадёт, тред не отдаём)."""
    await db.audit(actor=session.actor, action="thread_view", lead_id=lead_id,
                   ip=_ip(request), user_agent=_ua(request))
    return await db.get_thread(lead_id, cap=config.THREAD_CAP)


def _reply_accept_attr() -> str:
    """accept= для <input type=file> формы ответа: расширения каталога (.pdf,.png,…) с
    точкой + MIME-маска audio/* (чтобы мобильные браузеры предложили диктофон под голос)."""
    return ",".join("." + e for e in config.REPLY_FILE_EXTS) + ",audio/*"


def _reply_err_text(err: str | None) -> str | None:
    """Текст ошибки вложения ответа (PRG ?err=... из lead_reply). None → нет ошибки."""
    return {
        "bad_file": "Тип файла не поддерживается или содержимое не совпадает с расширением.",
        "file_too_big": "Файл превышает лимит загрузки.",
        "empty_reply": "Пустой ответ: добавьте текст или вложение.",
        "bad_persona": "Неизвестный ИИ-сотрудник.",
        "persona_tw": "Не удалось подготовить агента сотрудника у ИИ-сервиса. Сотрудник не сменён — попробуйте ещё раз.",
    }.get(err or "")


def _lead_context(request, session, rec, *, revealed: str | None, saved: bool = False,
                  erased: bool = False, thread=None, replied: bool = False,
                  paused_flash: bool = False, reply_err: str | None = None) -> dict:
    lead = dict(rec)
    lead["phone_masked"] = security.mask_phone(rec["phone_tail"], rec["has_phone"])
    return {
        "lead": lead,
        "revealed": revealed,           # полный номер ТОЛЬКО при reveal-POST
        "saved": saved,
        "erased": erased,
        "replied": replied,             # флеш «ответ поставлен в очередь»
        "paused_flash": paused_flash,   # флеш переключения перехвата
        "reply_err": _reply_err_text(reply_err),  # ошибка вложения ответа (PRG)
        "thread": thread or [],
        "refresh_sec": config.THREAD_REFRESH_SEC,
        "msg_max": config.MSG_MAX_LEN,
        # Вложение личного ответа: форматы для accept= и потолок размера (UI-подсказка).
        "accept_attr": _reply_accept_attr(),
        "max_file_mb": config.MAX_PRODUCT_FILE_MB,
        "statuses": config.STATUSES,
        "status_labels": config.STATUS_LABELS,
        "source_labels": config.SOURCE_LABELS,
        "messenger_labels": config.MESSENGER_LABELS,
        "csrf_token": session.csrf_token,
        "session": session,
        "notes_max": config.NOTES_MAX_LEN,
        # Карточка лида живёт под разделом «Диалоги» — подсвечиваем его в сайдбаре.
        "active": "dialogs",
    }


# ---- /leads/{id} POST — сохранить status+notes (PRG, аудит в той же тр-ции) - #
@app.post("/leads/{lead_id}")
async def lead_update(
    request: Request,
    lead_id: uuid.UUID,
    session: auth.Session = Depends(require_session),
    status: str = Form(...),
    notes: str = Form(""),
    csrf_token: str = Form(""),
):
    await _enforce_csrf(request, session, csrf_token)

    # notes ≤ 4000 ПЕРВЫМ действием, до БД (§3.13).
    notes = (notes or "")[: config.NOTES_MAX_LEN]
    notes_val = notes if notes.strip() else None

    # Defence-in-depth: статус против allow-list (БД-слой тоже проверит).
    if status not in config.STATUSES:
        raise StarletteHTTPException(status_code=422, detail="Недопустимый статус")

    row = await db.update_lead_with_audit(
        lead_id, new_status=status, new_notes=notes_val,
        actor=session.actor, ip=_ip(request), user_agent=_ua(request),
    )
    if row is None:
        raise StarletteHTTPException(status_code=404, detail="Лид не найден")
    return RedirectResponse(url=f"/leads/{lead_id}?saved=1", status_code=303)


# ---- /leads/{id}/reveal — аудит → 200 с полным номером (НЕ redirect, §3.8) - #
@app.post("/leads/{lead_id}/reveal", response_class=HTMLResponse)
async def lead_reveal(
    request: Request,
    lead_id: uuid.UUID,
    session: auth.Session = Depends(require_session),
    csrf_token: str = Form(""),
):
    await _enforce_csrf(request, session, csrf_token)

    rec = await db.get_lead(lead_id)
    if rec is None:
        raise StarletteHTTPException(status_code=404, detail="Лид не найден")

    # Аудит ДО раскрытия (fail-closed): если INSERT упадёт — исключение, номер не отдаём.
    await db.audit(actor=session.actor, action="phone_revealed", lead_id=lead_id,
                   ip=_ip(request), user_agent=_ua(request))

    phone = await db.reveal_phone(lead_id)
    revealed = phone if phone else "—"

    resp = templates.TemplateResponse(
        request,
        "lead.html",
        _lead_context(request, session, rec, revealed=revealed),
    )
    # Жёстко гасим кэш/BFCache на ответе с полным номером.
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
    resp.headers["Pragma"] = "no-cache"
    return resp


# ---- /leads/{id}/erase — отзыв согласия → erase_requested_at (§3.9) ------- #
@app.post("/leads/{lead_id}/erase")
async def lead_erase(
    request: Request,
    lead_id: uuid.UUID,
    session: auth.Session = Depends(require_session),
    csrf_token: str = Form(""),
):
    await _enforce_csrf(request, session, csrf_token)
    row = await db.request_erase_with_audit(
        lead_id, actor=session.actor, ip=_ip(request), user_agent=_ua(request)
    )
    if row is None:
        raise StarletteHTTPException(status_code=404, detail="Лид не найден")
    return RedirectResponse(url=f"/leads/{lead_id}?erased=1", status_code=303)


# ---- /leads/{id}/bot-pause | bot-resume — перехват (§4) ------------------- #
@app.post("/leads/{lead_id}/bot-pause")
async def lead_bot_pause(
    request: Request,
    lead_id: uuid.UUID,
    session: auth.Session = Depends(require_session),
    csrf_token: str = Form(""),
    from_: str = Form("card", alias="from"),
):
    return await _set_bot_paused(request, lead_id, session, csrf_token, paused=True, from_=from_)


@app.post("/leads/{lead_id}/bot-resume")
async def lead_bot_resume(
    request: Request,
    lead_id: uuid.UUID,
    session: auth.Session = Depends(require_session),
    csrf_token: str = Form(""),
    from_: str = Form("card", alias="from"),
):
    return await _set_bot_paused(request, lead_id, session, csrf_token, paused=False, from_=from_)


async def _set_bot_paused(request, lead_id, session, csrf_token, *, paused: bool,
                          from_: str = "card"):
    await _enforce_csrf(request, session, csrf_token)
    # UPDATE одной колонки leads.bot_paused в транзакции с аудитом (bot_paused|bot_resumed).
    # Telegram панель НЕ трогает: на паузе бот сам перестаёт авто-отвечать (его проверки).
    row = await db.set_bot_paused_with_audit(
        lead_id, paused=paused, actor=session.actor,
        ip=_ip(request), user_agent=_ua(request),
    )
    if row is None:
        raise StarletteHTTPException(status_code=404, detail="Лид не найден")
    return _chat_return(lead_id, from_, paused=True)


# ---- /leads/{id}/reply — ручной ответ → INSERT в outbox (НЕ Telegram, §4) -- #
@app.post("/leads/{lead_id}/reply")
async def lead_reply(
    request: Request,
    lead_id: uuid.UUID,
    session: auth.Session = Depends(require_session),
    text: str = Form(""),
    csrf_token: str = Form(""),
    files: list[UploadFile] = File(default=[]),
    from_: str = Form("card", alias="from"),
):
    await _enforce_csrf(request, session, csrf_token)

    # Длину капим ПЕРВЫМ действием, до БД (§3.13/§5.11). plain-текст, без parse_mode.
    text = (text or "").strip()[: config.MSG_MAX_LEN]

    # НЕСКОЛЬКО вложений (файлы + голос) в одном поле files (multiple). Валидируем КАЖДОЕ
    # как файл офера (размер+ext+MIME+magic-byte, отказ exe); _read_reply_file классифицирует
    # image→photo / audio/*→voice (бот сконвертит в ogg) / иначе document. Первый отказ →
    # PRG-редирект с err-кодом. Потолок числа вложений — анти-абуз.
    attachments: list[dict] = []
    for up in (files or []):
        meta, file_err = await _read_reply_file(request, up)
        if file_err:
            return _chat_return(lead_id, from_, err=file_err)
        if meta is not None:
            attachments.append(meta)
            if len(attachments) >= config.MAX_REPLY_ATTACHMENTS:
                break

    # Ослабленный инвариант: отклоняем ТОЛЬКО когда нет ни текста, ни вложений.
    if not text and not attachments:
        return _chat_return(lead_id, from_, err="empty_reply")

    # INSERT в outbox 'queued' (по строке на текст и на каждое вложение) + аудит без байтов.
    # Реально шлёт бот; адресность (tg_user_id) и erase-фильтр он re-check'ает. Байты кладёт
    # панель (как у продуктов) → бот зальёт в OPS_CHAT_ID и проставит outbox.file_id.
    rows = await db.enqueue_manual_reply(
        lead_id, text=text, attachments=attachments, actor=session.actor,
        ip=_ip(request), user_agent=_ua(request),
    )
    if rows is None:
        # Лид не найден ИЛИ без tg_user_id (некому слать) — не молчим, говорим оператору.
        raise StarletteHTTPException(
            status_code=400, detail="Лиду нельзя написать (нет Telegram-адреса)"
        )
    return _chat_return(lead_id, from_, replied=True)


# ---- /leads/{id}/invoice — «счёт из диалога» (Phase 1B) -------------------- #
def _invoice_err_text(err: str | None) -> str | None:
    return {
        "pay_off": "Онлайн-оплата выключена или не настроена (раздел «Интеграции»).",
        "bad_product": "Выберите офер с ценой в рублях (активный, из каталога).",
        "no_tg": "У лида нет Telegram-адреса — счёт некому доставить.",
        "yk_failed": "Не удалось создать платёж. Попробуйте ещё раз или проверьте ключи оплаты.",
    }.get(err or "")


def _invoice_return(lead_id, *, invoiced: bool = False, err: str | None = None) -> RedirectResponse:
    params = {}
    if invoiced:
        params["invoiced"] = "1"
    if err:
        params["inv_err"] = err
    qs = urlencode(params)
    return RedirectResponse(url=f"/dialogs/{lead_id}{'?' + qs if qs else ''}#thread",
                            status_code=303)


@app.post("/leads/{lead_id}/invoice")
async def lead_invoice(
    request: Request,
    lead_id: uuid.UUID,
    session: auth.Session = Depends(require_session),
    product_id: str = Form(""),
    csrf_token: str = Form(""),
):
    """Оператор выставляет лиду счёт на офер: pending-заказ → платёж ЮKassa (МАГАЗИН
    ШКОЛЫ) → лиду сообщение со ссылкой на оплату через outbox (доставит БОТ — панель в
    Telegram не пишет). Подтверждение оплаты — единый вебхук (paid + converted)."""
    await _enforce_csrf(request, session, csrf_token)
    if not (config.SHOP_PAYMENTS_CONFIGURED and await db.get_online_payments_enabled()):
        return _invoice_return(lead_id, err="pay_off")
    try:
        pid = int(product_id)
    except (TypeError, ValueError):
        return _invoice_return(lead_id, err="bad_product")
    product = await db.get_product(pid)
    if (product is None or product["status"] != "active" or not product["price"]
            or product["price"] <= 0 or (product["currency"] or "RUB") != "RUB"):
        return _invoice_return(lead_id, err="bad_product")

    order = await db.create_invoice_order_with_audit(
        lead_id, pid, product["price"], "RUB",
        actor=session.actor, ip=_ip(request), user_agent=_ua(request),
    )
    if order is None:
        return _invoice_return(lead_id, err="no_tg")

    # Телефон лида — ТОЛЬКО для чека 54-ФЗ (если фискализация включена флагом env);
    # без чека сырой номер из БД не достаём вовсе (данные-минимизация).
    lead_phone = await db.reveal_phone(lead_id) if config.SHOP_RECEIPT_ENABLED else None
    runtime = await db.get_runtime_status()
    return_url = (f"https://t.me/{runtime['bot_username']}"
                  if runtime.get("bot_username") else "https://t.me")
    try:
        payment = await yookassa.create_shop_payment(
            amount=product["price"], currency="RUB",
            description=f"{product['name']} — Школа Лесова",
            return_url=return_url,
            idempotence_key=str(order["id"]),
            metadata={"kind": "order", "order_id": str(order["id"])},
            lead_phone=lead_phone,
        )
        pay_url = (payment.get("confirmation") or {}).get("confirmation_url")
        payment_id = payment.get("id")
        if not pay_url or not payment_id:
            raise yookassa.YooKassaError("нет confirmation_url/id в ответе")
    except yookassa.YooKassaError:
        import logging
        logging.getLogger("admin-panel").exception("invoice create_shop_payment failed")
        await db.set_order_status_with_audit(
            order["id"], new_status="failed",
            actor=session.actor, ip=_ip(request), user_agent=_ua(request),
        )
        return _invoice_return(lead_id, err="yk_failed")

    await db.set_order_payment_panel(order["id"], payment_id, pay_url)
    price_str = _fmt_price(product["price"], "RUB")
    await db.enqueue_invoice_message(
        lead_id, order["tg_user_id"],
        f"Счёт на оплату 🌷\n{product['name']} — {price_str}\n\n"
        f"Оплатить по ссылке (действует около часа):\n{pay_url}",
        actor=session.actor,
    )
    return _invoice_return(lead_id, invoiced=True)


# ---- /export.csv — POST, маска, аудит ДО стрима, row-cap (§3.11) ---------- #
@app.post("/export.csv")
async def export_masked(
    request: Request,
    session: auth.Session = Depends(require_session),
    csrf_token: str = Form(""),
):
    await _enforce_csrf(request, session, csrf_token)
    filters, raw = _parse_filters(request, session)

    total = await db.count_leads(filters)
    # Аудит ДО старта стрима (fail-closed): падение → 500, ни одной строки CSV.
    await db.audit(actor=session.actor, action="export", ip=_ip(request), user_agent=_ua(request),
                   detail={"filters": _audit_filters(raw), "matched": total,
                           "row_cap": config.EXPORT_ROW_CAP})

    return StreamingResponse(
        _csv_masked_rows(filters),
        media_type="text/csv; charset=utf-8",
        headers=_csv_headers("leads_export"),
    )


# ---- /export-full.csv — POST, ПОЛНЫЕ телефоны, отдельный аудит (§3.11) ---- #
@app.post("/export-full.csv")
async def export_full(
    request: Request,
    session: auth.Session = Depends(require_session),
    csrf_token: str = Form(""),
    confirm: str = Form(""),
):
    await _enforce_csrf(request, session, csrf_token)
    filters, raw = _parse_filters(request, session)

    # Требуем явное подтверждение + хотя бы один сужающий фильтр (анти mass-reveal).
    if confirm != "yes":
        raise StarletteHTTPException(status_code=400, detail="Требуется подтверждение")
    if not _has_narrowing_filter(filters):
        raise StarletteHTTPException(
            status_code=400,
            detail="Для выгрузки полных телефонов задайте хотя бы один фильтр",
        )

    total = await db.count_leads(filters)
    await db.audit(actor=session.actor, action="export_full", ip=_ip(request),
                   user_agent=_ua(request),
                   detail={"filters": _audit_filters(raw), "matched": total,
                           "row_cap": config.EXPORT_ROW_CAP})

    return StreamingResponse(
        _csv_full_rows(filters),
        media_type="text/csv; charset=utf-8",
        headers=_csv_headers("leads_export_full"),
    )


def _has_narrowing_filter(filters: dict) -> bool:
    return any(
        filters.get(k) not in (None, "")
        for k in ("status", "source", "messenger", "consent", "q_hash", "q_name", "erase_pending")
    )


def _audit_filters(raw: dict) -> dict:
    """Фильтры для аудита БЕЗ ПДн: имя поиска и phone-хеш не пишем открытым текстом."""
    out = {k: v for k, v in raw.items()
           if k in ("status", "source", "messenger", "consent", "erase", "sort") and v}
    out["q_name"] = bool(raw.get("qname"))   # факт поиска по имени, без значения
    out["q_phone"] = bool(raw.get("qid"))    # факт поиска по телефону, без хеша
    return out


def _csv_headers(prefix: str) -> dict:
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return {
        "Content-Disposition": f'attachment; filename="{prefix}_{day}.csv"',
        "Cache-Control": "no-store",
    }


async def _csv_masked_rows(filters: dict):
    header = ["created_at", "updated_at", "name", "phone_masked", "messenger",
              "source", "status", "consent", "subscribed", "guide_sent_at",
              "erase_requested_at"]
    yield _csv_line(header, bom=True)
    async for r in db.stream_export_masked(filters, row_cap=config.EXPORT_ROW_CAP):
        yield _csv_line([
            _iso(r["created_at"]), _iso(r["updated_at"]), r["name"] or "",
            security.mask_phone(r["phone_tail"], r["has_phone"]),
            r["messenger"], r["source"], r["status"],
            _yn(r["consent"]), _yn(r["subscribed"]),
            _iso(r["guide_sent_at"]), _iso(r["erase_requested_at"]),
        ])


async def _csv_full_rows(filters: dict):
    header = ["created_at", "updated_at", "name", "phone", "messenger",
              "source", "status", "consent", "subscribed", "guide_sent_at",
              "erase_requested_at"]
    yield _csv_line(header, bom=True)
    async for r in db.stream_export_full(filters, row_cap=config.EXPORT_ROW_CAP):
        yield _csv_line([
            _iso(r["created_at"]), _iso(r["updated_at"]), r["name"] or "",
            r["phone"] or "", r["messenger"], r["source"], r["status"],
            _yn(r["consent"]), _yn(r["subscribed"]),
            _iso(r["guide_sent_at"]), _iso(r["erase_requested_at"]),
        ])


def _csv_line(values: list[str], *, bom: bool = False) -> bytes:
    buf = io.StringIO()
    csv.writer(buf, quoting=csv.QUOTE_MINIMAL).writerow(values)
    text = buf.getvalue()
    if bom:
        text = "﻿" + text   # BOM для Excel-RU (§3.11)
    return text.encode("utf-8")


def _iso(dt) -> str:
    return dt.isoformat() if dt is not None else ""


def _yn(v) -> str:
    return "да" if v else "нет"


# =========================================================================== #
# ПРОДУКТЫ (каталог оферов). Переиспользуемый раздел: офер заводится ОДИН раз и
# привязывается к любому числу рассылок (broadcasts.product_id). Конструктор:
# name + kind + price/currency + caption + link + опц. файл (валидируется по
# расширению+MIME+magic-byte, ≤ MAX_PRODUCT_FILE_MB).
#
# Инвариант (panel_rw, без BOT_TOKEN): панель кладёт байты файла в products.file и
# метаданные; в Telegram заливает (file→file_tg_id в OPS_CHAT_ID) и переиспользует
# file_id — БОТ. Колонку file_tg_id панель не пишет (column-level грант).
# =========================================================================== #

def _product_kind_send(send_kind: str) -> str:
    """photo|document для офера → нормализуем способ отправки (как и у рассылок:
    image/* → photo, всё прочее → document; канон из security.sniff_product_file)."""
    return "photo" if send_kind == "photo" else "document"


def _fmt_price(price, currency: str) -> str:
    """Цена → строка для UI: «1 990 ₽» / «1 990,50 ₽». Пусто/None → '' (цена опц.).

    Разделитель тысяч — узкий пробел, дробная часть только если ненулевая (через запятую,
    как принято в RU). Знак валюты из config (RUB→₽). Чисто презентация, без БД/локали.
    """
    if price is None:
        return ""
    sign = config.PRODUCT_CURRENCY_SIGNS.get(currency, currency)
    d = Decimal(price)
    whole = int(d)
    cents = int((d - whole) * 100)
    int_str = f"{whole:,}".replace(",", " ")  # узкий неразрывный пробел между разрядами
    body = int_str if cents == 0 else f"{int_str},{cents:02d}"
    return f"{body} {sign}"


def _parse_price(raw: str) -> tuple[Decimal | None, bool]:
    """Строка цены из формы → (Decimal | None, ok). Пусто → (None, True) — цена опц.

    Принимаем запятую как десятичный разделитель и пробелы-разделители тысяч. Отрицательную
    и нечисловую отвергаем (ok=False). numeric(12,2): до 10 цифр до точки, 2 после.
    """
    s = (raw or "").strip()
    if not s:
        return None, True
    s = s.replace(" ", "").replace(" ", "").replace(",", ".")
    try:
        val = Decimal(s)
    except (InvalidOperation, ValueError):
        return None, False
    if val < 0:
        return None, False
    val = val.quantize(Decimal("0.01"))
    # numeric(12,2): целая часть ≤ 10 цифр (12 - 2). Иначе БД бы отвергла — режем заранее.
    if val >= Decimal("10000000000"):
        return None, False
    return val, True


async def _read_product_file(request: Request, upload) -> tuple[dict | None, str | None]:
    """Прочитать и провалидировать файл офера. Возвращает (file_meta | None, err_code | None).

    file_meta = {"bytes","filename","mime","send"} при валидном непустом файле; (None, None)
    если файла нет/пустой; (None, err) при отказе. Валидация: размер ≤ лимита (streaming-cap),
    расширение+MIME+magic-byte (security.sniff_product_file), отказ исполняемым/опасным.
    """
    if upload is None or not (upload.filename or ""):
        return None, None
    data = await security.read_upload_capped(upload, max_bytes=config.MAX_PRODUCT_FILE_BYTES)
    if data is None:
        return None, "file_too_big"
    if not data:
        return None, None  # пустой файл — трактуем как «без файла»
    sniffed = security.sniff_product_file(
        data, filename=upload.filename, claimed_mime=upload.content_type
    )
    if sniffed is None:
        return None, "bad_file"
    return {
        "bytes": data,
        "filename": (upload.filename or "file")[:255],
        "mime": sniffed["mime"],
        "send": _product_kind_send(sniffed["send"]),
    }, None


def _reply_kind(mime: str) -> str:
    """Способ отправки личного ответа по подтверждённому MIME (план «reply-attach»):
      image/*                → 'photo'    (фото/картинка);
      MIME ∈ REPLY_AUDIO_MIMES → 'voice'  (запись с микрофона; бот транскодит в ogg/opus,
                                            при сбое ffmpeg сам понизит до 'audio');
      иначе                  → 'document' (pdf/doc/xls/zip/… — всё прочее).
    kind кладётся в outbox.kind ЯВНО; бот уважает его при заливке (НЕ переопределяет по
    MIME — иначе voice превратился бы в document).
    """
    if mime.startswith("image/"):
        return "photo"
    if mime in config.REPLY_AUDIO_MIMES:
        return "voice"
    return "document"


async def _read_reply_file(request: Request, upload) -> tuple[dict | None, str | None]:
    """Прочитать и провалидировать вложение личного ответа. Клон _read_product_file.

    Возвращает (meta | None, err_code | None). meta = {"bytes","name","mime","kind"} при
    валидном непустом файле; (None, None) если файла нет/пустой; (None, err) при отказе.
    Валидация идентична файлу офера (размер ≤ MAX_PRODUCT_FILE_BYTES streaming-cap;
    расширение+MIME+magic-byte через security.sniff_product_file; отказ исполняемым/
    опасным). kind выводим из ПОДТВЕРЖДЁННОГО канон-MIME (_reply_kind), не из заявленного
    браузером content_type.
    """
    if upload is None or not (upload.filename or ""):
        return None, None
    data = await security.read_upload_capped(upload, max_bytes=config.MAX_PRODUCT_FILE_BYTES)
    if data is None:
        return None, "file_too_big"
    if not data:
        return None, None  # пустой файл — трактуем как «без файла»
    sniffed = security.sniff_product_file(
        data, filename=upload.filename, claimed_mime=upload.content_type
    )
    if sniffed is None:
        return None, "bad_file"
    return {
        "bytes": data,
        "name": (upload.filename or "file")[:255],
        "mime": sniffed["mime"],
        "kind": _reply_kind(sniffed["mime"]),
    }, None


# ---- /products — список-каталог ------------------------------------------- #
@app.get("/products", response_class=HTMLResponse)
async def products_list(
    request: Request,
    session: auth.Session = Depends(require_session),
    saved: int = 0,
    archived: int = 0,
    lm: int = 0,
    kassa_saved: int = 0,
    err: str | None = None,
):
    rows = await db.list_products(include_archived=True)
    products = [_present_product_row(r) for r in rows]
    # Касса (self-serve ЮKassa): статус по vault активного тенанта + URL вебхука для ЛК ЮKassa.
    kassa_secrets: list = []
    if session.active_tenant_id and vault.enabled():
        kassa_secrets = await db.list_tenant_secrets(session.active_tenant_id)
    webhook_base = config.PANEL_PUBLIC_BASE_URL or str(request.base_url).rstrip("/")
    # Выдача воронки лид-магнитом (вместо GUIDE_URL): текущий активный офер + кандидаты
    # для селектора. Бот читает app_settings; панель — единственный писатель этого ключа.
    active_lm_rec = await db.get_active_lead_magnet()
    active_lm = _present_lead_magnet(active_lm_rec) if active_lm_rec is not None else None
    lm_candidates = [
        {"id": r["id"], "name": r["name"], "has_file": r["has_file"],
         "file_ready": r["file_ready"], "has_link": bool(r["link"])}
        for r in await db.list_lead_magnet_products()
    ]
    return templates.TemplateResponse(
        request,
        "products.html",
        {
            "products": products,
            "csrf_token": session.csrf_token,
            "session": session,
            "active": "products",
            "saved": bool(saved),
            "archived": bool(archived),
            "lm_flash": bool(lm),
            "err": _product_list_err_text(err),
            "kind_labels": config.PRODUCT_KIND_LABELS,
            "active_lead_magnet": active_lm,
            "lead_magnet_candidates": lm_candidates,
            # Касса (ЮKassa) — self-serve подключение, tenant-scoped.
            "has_tenant": bool(session.active_tenant_id),
            "vault_enabled": vault.enabled(),
            "kassa": _kassa_view(kassa_secrets),
            "kassa_webhook_url": f"{webhook_base}/webhooks/yookassa",
            "value_max": config.TENANT_SECRET_VALUE_MAX,
            "kassa_saved": bool(kassa_saved),
        },
    )


def _present_lead_magnet(r) -> dict:
    """Текущий активный лид-магнит-офер воронки для индикации на /products."""
    return {
        "id": r["id"], "name": r["name"], "status": r["status"],
        "has_file": r["has_file"], "file_ready": r["file_ready"],
        "has_link": bool(r["link"]),
    }


def _product_list_err_text(err: str | None) -> str | None:
    return {
        "bad_lead_magnet": "Этот офер нельзя сделать выдачей воронки: нужен вид "
                           "«Лид-магнит», статус «Активен» и хотя бы файл или ссылка.",
        # Касса (self-serve подключение ЮKassa)
        "no_tenant": "Кабинет ещё не привязан к клиенту. Обратитесь в поддержку.",
        "no_vault": "Хранилище ключей не настроено. Обратитесь в поддержку.",
        "bad_key": "Неизвестное поле кассы.",
        "bad_shop_id": "shopId ЮKassa — только цифры (число из ЛК ЮKassa).",
        "empty": "Значение пустое — не сохранено.",
        "too_long": f"Значение длиннее {config.TENANT_SECRET_VALUE_MAX} символов.",
        "not_found": "Нечего отключать — поле не задано.",
        "kassa_saved": "",  # успех — обрабатывается отдельным флагом
    }.get(err or "")


def _kassa_view(secrets_meta: list) -> dict:
    """Презентер блока кассы (ЮKassa) в «Продуктах». Касса «настроена» = заданы ОБА ключа
    (shopId + секретный). Чистая функция: на вход метаданные секретов тенанта, на выход — данные
    для шаблона. Значения секретов НЕ раскрываются (list_tenant_secrets отдаёт только имена)."""
    known = {r["key_name"] for r in secrets_meta}
    label = dict(config.TENANT_SECRET_KEYS)
    fields = [{"key": k, "label": label.get(k, k), "is_set": k in known}
              for k in config.KASSA_SECRET_KEYS]
    return {"fields": fields, "connected": all(f["is_set"] for f in fields)}


def _present_product_row(r) -> dict:
    return {
        "id": r["id"],
        "name": r["name"],
        "kind": r["kind"],
        "price_display": _fmt_price(r["price"], r["currency"]),
        "has_file": r["has_file"],
        "file_ready": r["file_ready"],
        "has_link": bool(r["link"]),
        "file_name": r["file_name"],
        "status": r["status"],
        "created_at": r["created_at"],
    }


# ---- /products/new — конструктор (GET, пустой) ---------------------------- #
@app.get("/products/new", response_class=HTMLResponse)
async def product_new_form(
    request: Request,
    session: auth.Session = Depends(require_session),
    err: str | None = None,
):
    return templates.TemplateResponse(
        request,
        "product_form.html",
        _product_form_context(request, session, product=None, err=err),
    )


# ---- /products/{id} — конструктор (GET, правка) --------------------------- #
@app.get("/products/{product_id}", response_class=HTMLResponse)
async def product_edit_form(
    request: Request,
    product_id: int,
    session: auth.Session = Depends(require_session),
    err: str | None = None,
):
    rec = await db.get_product(product_id)
    if rec is None:
        raise StarletteHTTPException(status_code=404, detail="Продукт не найден")
    return templates.TemplateResponse(
        request,
        "product_form.html",
        _product_form_context(request, session, product=dict(rec), err=err),
    )


def _product_form_context(request, session, *, product: dict | None, err: str | None) -> dict:
    return {
        "product": product,                       # None для нового
        "csrf_token": session.csrf_token,
        "session": session,
        "active": "products",
        "err": _product_err_text(err),
        "kinds": config.PRODUCT_KINDS,
        "kind_labels": config.PRODUCT_KIND_LABELS,
        "currencies": config.PRODUCT_CURRENCIES,
        "currency_labels": config.PRODUCT_CURRENCY_LABELS,
        "name_max": config.PRODUCT_NAME_MAX_LEN,
        "caption_max": config.PRODUCT_CAPTION_MAX_LEN,
        "max_file_mb": config.MAX_PRODUCT_FILE_MB,
        "file_exts": config.PRODUCT_FILE_EXTS,
        "accept_attr": _product_accept_attr(),
    }


def _product_accept_attr() -> str:
    """Значение accept= для <input type=file>: список расширений с точкой (.pdf,.png,…)."""
    return ",".join("." + e for e in config.PRODUCT_FILE_EXTS)


def _product_err_text(err: str | None) -> str | None:
    return {
        "empty_name": "Название продукта обязательно.",
        "bad_kind": "Выберите вид продукта.",
        "bad_currency": "Недопустимая валюта.",
        "bad_price": "Цена указана неверно (число ≥ 0, до 2 знаков после запятой).",
        "bad_link": "Ссылка недопустима (нужен http/https).",
        "bad_file": "Тип файла не поддерживается или содержимое не совпадает с расширением.",
        "file_too_big": "Файл превышает лимит загрузки.",
        "need_file_or_link": "Добавьте файл и/или ссылку — нужно хотя бы одно.",
        "not_found": "Продукт не найден.",
    }.get(err or "")


# ---- /products — создать/обновить (POST, multipart) ----------------------- #
@app.post("/products")
async def product_save(
    request: Request,
    session: auth.Session = Depends(require_session),
    product_id: str = Form(""),       # пусто → создание; иначе → обновление
    name: str = Form(""),
    kind: str = Form(""),
    price: str = Form(""),
    currency: str = Form("RUB"),
    caption: str = Form(""),
    link: str = Form(""),
    clear_file: str = Form(""),       # '1' → снять текущий файл (только при обновлении)
    status: str = Form("active"),
    csrf_token: str = Form(""),
    file: UploadFile | None = File(None),
):
    await _enforce_csrf(request, session, csrf_token)

    # id целевого офера (для обновления). Нечисловой/пустой → создание.
    pid: int | None = None
    if (product_id or "").strip().isdigit():
        pid = int(product_id.strip())

    def _back(err: str) -> RedirectResponse:
        target = f"/products/{pid}" if pid is not None else "/products/new"
        return RedirectResponse(url=f"{target}?err={err}", status_code=303)

    name_val = (name or "").strip()[: config.PRODUCT_NAME_MAX_LEN]
    if not name_val:
        return _back("empty_name")
    if kind not in db._PRODUCT_KIND_SET:
        return _back("bad_kind")
    if currency not in db._PRODUCT_CURRENCY_SET:
        return _back("bad_currency")
    # status из формы: только active|archived (defence-in-depth; archived доступен и из формы).
    status_val = status if status in config.PRODUCT_STATUSES else "active"

    price_val, price_ok = _parse_price(price)
    if not price_ok:
        return _back("bad_price")

    caption_val = (caption or "").strip()[: config.PRODUCT_CAPTION_MAX_LEN] or None

    # Ссылка офера: тот же allow-list схем, что у трекинга рассылки (/r строится поверх).
    link_val: str | None = None
    if (link or "").strip():
        link_val = security.validate_target_url(link, schemes=config.LINK_URL_SCHEMES)
        if link_val is None:
            return _back("bad_link")

    # Файл: читаем+валидируем (расширение+MIME+magic-byte, размер). Пустой → нет файла.
    file_meta, file_err = await _read_product_file(request, file)
    if file_err:
        return _back(file_err)
    clear = (clear_file or "").strip() == "1" and file_meta is None

    # Инвариант «файл И/ИЛИ ссылка, но хотя бы одно». Для обновления учитываем уже
    # имеющийся файл офера, который оператор не снимает и не заменяет.
    will_have_file = file_meta is not None
    if pid is not None and not will_have_file and not clear:
        existing = await db.get_product(pid)
        if existing is None:
            return _back("not_found")
        will_have_file = bool(existing["has_file"])
    if not will_have_file and not link_val:
        return _back("need_file_or_link")

    if pid is None:
        await db.create_product_with_audit(
            name=name_val, kind=kind, price=price_val, currency=currency,
            caption=caption_val, link=link_val, file_meta=file_meta,
            status=status_val, tenant_id=session.active_tenant_id,
            actor=session.actor,
            ip=_ip(request), user_agent=_ua(request),
        )
        return RedirectResponse(url="/products?saved=1", status_code=303)

    row = await db.update_product_with_audit(
        pid, name=name_val, kind=kind, price=price_val, currency=currency,
        caption=caption_val, link=link_val, file_meta=file_meta, clear_file=clear,
        status=status_val, actor=session.actor,
        ip=_ip(request), user_agent=_ua(request),
    )
    if row is None:
        raise StarletteHTTPException(status_code=404, detail="Продукт не найден")
    return RedirectResponse(url="/products?saved=1", status_code=303)


# ---- /products/{id}/archive — архивировать (обычный CSRF) ----------------- #
@app.post("/products/{product_id}/archive")
async def product_archive(
    request: Request,
    product_id: int,
    session: auth.Session = Depends(require_session),
    csrf_token: str = Form(""),
):
    await _enforce_csrf(request, session, csrf_token)
    result = await db.archive_product_with_audit(
        product_id, actor=session.actor, ip=_ip(request), user_agent=_ua(request),
    )
    if result is None:
        raise StarletteHTTPException(status_code=404, detail="Продукт не найден")
    # conflict (уже в архиве) трактуем мягко — всё равно ведём в список с флешем.
    return RedirectResponse(url="/products?archived=1", status_code=303)


# ---- /products/set-lead-magnet — назначить/снять выдачу воронки (CSRF) ----- #
# Лид-магнит-офер ЗАМЕНЯЕТ GUIDE_URL-заглушку в выдаче воронки (решение владельца).
# Панель — единственный писатель app_settings['active_lead_magnet_product_id']; бот
# его читает (get_active_lead_magnet_product) и валидирует повторно. Обычное действие
# (не отправка): CSRF, без step-up. product_id пустой → снять (фолбэк на GUIDE_URL).
# Путь без {id}, чтобы один POST покрывал и назначение, и снятие.
@app.post("/products/set-lead-magnet")
async def product_set_lead_magnet(
    request: Request,
    session: auth.Session = Depends(require_session),
    product_id: str = Form(""),
    csrf_token: str = Form(""),
):
    await _enforce_csrf(request, session, csrf_token)
    pid: int | None = None
    if (product_id or "").strip().isdigit():
        pid = int(product_id.strip())
    result = await db.set_active_lead_magnet_with_audit(
        pid, actor=session.actor, ip=_ip(request), user_agent=_ua(request),
    )
    if result == "bad_product":
        return RedirectResponse(url="/products?err=bad_lead_magnet", status_code=303)
    return RedirectResponse(url="/products?lm=1", status_code=303)


# ── Слой C: self-serve подключение кассы клиента (ЮKassa) в «Продуктах» ────────
# Tenant-scoped, БЕЗ _require_admin: клиент сам вводит shopId + секретный ключ СВОЕГО магазина
# ЮKassa → tenant-vault. Принимаются ТОЛЬКО ключи кассы (KASSA_SECRET_KEY_SET). AAD шифрования —
# {tenant_id}:{key_name} (как /keys/channels; бот расшифровывает тем же AAD). Бот тенанта берёт
# их как creds для create_payment → приём оплаты за продукты клиента на ЕГО магазин.
@app.post("/products/payments/connect")
async def kassa_connect(
    request: Request,
    session: auth.Session = Depends(require_session),
    key_name: str = Form(""),
    value: str = Form(""),
    csrf_token: str = Form(""),
):
    await _enforce_csrf(request, session, csrf_token)
    if not session.active_tenant_id:
        return RedirectResponse(url="/products?err=no_tenant", status_code=303)
    if not vault.enabled():
        return RedirectResponse(url="/products?err=no_vault", status_code=303)
    key_name = key_name.strip()
    if key_name not in config.KASSA_SECRET_KEY_SET:
        return RedirectResponse(url="/products?err=bad_key", status_code=303)
    value = value.strip()
    if not value:
        return RedirectResponse(url="/products?err=empty", status_code=303)
    if len(value) > config.TENANT_SECRET_VALUE_MAX:
        return RedirectResponse(url="/products?err=too_long", status_code=303)
    # shopId ЮKassa — числовой (как в ЛК). Валидируем, чтобы не сохранить мусор → «настроено,
    # но платёж не создаётся» (BasicAuth по shopId упадёт). Секретный ключ — произвольная строка.
    if key_name == "shop_yookassa_shop_id" and not value.isdigit():
        return RedirectResponse(url="/products?err=bad_shop_id", status_code=303)
    ct, nonce, ver = vault.encrypt(value, aad=f"{session.active_tenant_id}:{key_name}")
    del value  # plaintext дальше не живёт
    await db.upsert_tenant_secret(
        session.active_tenant_id, key_name, ct, nonce, ver,
        actor=session.actor, ip=_ip(request), user_agent=_ua(request),
    )
    return RedirectResponse(url="/products?kassa_saved=1", status_code=303)


@app.post("/products/payments/disconnect")
async def kassa_disconnect(
    request: Request,
    session: auth.Session = Depends(require_session),
    key_name: str = Form(""),
    csrf_token: str = Form(""),
):
    await _enforce_csrf(request, session, csrf_token)
    if not session.active_tenant_id:
        return RedirectResponse(url="/products?err=no_tenant", status_code=303)
    key_name = key_name.strip()
    if key_name not in config.KASSA_SECRET_KEY_SET:
        return RedirectResponse(url="/products?err=bad_key", status_code=303)
    ok = await db.delete_tenant_secret(
        session.active_tenant_id, key_name,
        actor=session.actor, ip=_ip(request), user_agent=_ua(request),
    )
    return RedirectResponse(url="/products?kassa_saved=1" if ok else "/products?err=not_found", status_code=303)


# =========================================================================== #
# РАССЫЛКИ (план §5,§6,§7). Композер + CRUD заявки + аналитика.
#
# Инвариант: панель НЕ шлёт в Telegram. «Запуск» = перевод broadcasts.status в
# 'queued' (бот подхватит, материализует получателей единым WHERE и разошлёт).
# Запуск — мощное действие: confirm + hard-cap + step-up пароль + аудит (§7.1).
# =========================================================================== #

def _parse_audience(form) -> dict:
    """Собрать фильтр аудитории из формы композера (подмножество, не сырой SQL).

    messenger ограничен tg (max — disabled-задел). source/status — против allow-list.
    exclude_unsubscribed — чекбокс, ВКЛ по умолчанию (приходит 'on'/отсутствует).
    """
    messenger = (form.get("messenger") or "tg").strip()
    if messenger not in db.BROADCAST_MESSENGERS:
        messenger = "tg"
    source = (form.get("source") or "").strip() or None
    if source is not None and source not in config.SOURCES:
        source = None
    status = (form.get("status") or "").strip() or None
    if status is not None and status not in config.STATUSES:
        status = None
    # Чекбокс «исключить отписанных», вкл по умолчанию. Когда форма явно отправлена
    # (hidden audience_submitted присутствует), значение чекбокса авторитетно:
    # present → True, absent → оператор СНЯЛ галку → False. Если маркера нет
    # (не из композера) — безопасный дефолт True.
    if form.get("audience_submitted") is not None:
        exclude_unsub = form.get("exclude_unsubscribed") is not None
    else:
        exclude_unsub = True
    return {
        "messenger": messenger,
        "source": source,
        "status": status,
        "exclude_unsubscribed": bool(exclude_unsub),
    }


# ---- /broadcasts — список + сводная аналитика ----------------------------- #
@app.get("/broadcasts", response_class=HTMLResponse)
async def broadcasts_list(request: Request, session: auth.Session = Depends(require_session)):
    try:
        page = max(1, int(request.query_params.get("page", "1")))
    except ValueError:
        page = 1
    per_page = config.PER_PAGE
    offset = (page - 1) * per_page

    total = await db.count_broadcasts()
    rows = await db.list_broadcasts(limit=per_page, offset=offset)
    pages = max(1, ceil(total / per_page)) if total else 1

    return templates.TemplateResponse(
        request,
        "broadcasts.html",
        {
            "rows": [dict(r) for r in rows],
            "page": page, "per_page": per_page, "total": total, "pages": pages,
            "base_qs": "",
            "csrf_token": session.csrf_token,
            "session": session,
            "active": "broadcasts",
            "status_labels": config.STATUS_LABELS,
            "broadcast_status_labels": BROADCAST_STATUS_LABELS,
            "messenger_labels": config.MESSENGER_LABELS,
        },
    )


# ---- /broadcasts/new — композер (GET) ------------------------------------- #
@app.get("/broadcasts/new", response_class=HTMLResponse)
async def broadcast_new_form(
    request: Request,
    session: auth.Session = Depends(require_session),
    err: str | None = None,
):
    # Предпросмотр количества по дефолтной аудитории (tg + consent + не отписан).
    default_audience = {"messenger": "tg", "source": None, "status": None,
                        "exclude_unsubscribed": True}
    estimate = await db.count_broadcast_audience(default_audience)
    # Каталог активных оферов для селектора «прикрепить продукт» (опц.).
    product_rows = await db.list_products_for_select()
    products = [_present_product_option(r) for r in product_rows]
    return templates.TemplateResponse(
        request,
        "broadcast_new.html",
        {
            "csrf_token": session.csrf_token,
            "session": session,
            "active": "broadcasts",
            "estimate": estimate,
            "err": _broadcast_err_text(err),
            "sources": config.SOURCES, "source_labels": config.SOURCE_LABELS,
            "statuses": config.STATUSES, "status_labels": config.STATUS_LABELS,
            "msg_max": config.MSG_MAX_LEN, "caption_max": config.CAPTION_MAX_LEN,
            "max_recipients": config.MAX_BROADCAST_RECIPIENTS,
            "max_upload_mb": config.MAX_UPLOAD_BYTES // (1024 * 1024),
            "products": products,
            "product_kind_labels": config.PRODUCT_KIND_LABELS,
            "currency_signs": config.PRODUCT_CURRENCY_SIGNS,
        },
    )


def _present_product_option(r) -> dict:
    """Опция офера для селектора композера + данные для предпросмотра (без байт)."""
    return {
        "id": r["id"],
        "name": r["name"],
        "kind": r["kind"],
        "price_display": _fmt_price(r["price"], r["currency"]),
        "caption": r["caption"],
        "has_link": bool(r["link"]),
        "has_file": r["has_file"],
        "file_name": r["file_name"],
    }


def _broadcast_err_text(err: str | None) -> str | None:
    return {
        "empty_body": "Текст рассылки обязателен.",
        "too_long": "Текст превышает лимит Telegram.",
        "bad_link": "Ссылка для трекинга недопустима (нужен http/https).",
        "bad_file": "Тип файла не поддерживается.",
        "file_too_big": "Файл превышает лимит загрузки.",
        "rate": "Слишком много черновиков создано за час. Подождите.",
        "raw_link_in_body": "В тексте нельзя использовать сырые ссылки/разметку — "
                            "трекинг-ссылка подставляется через {link}.",
    }.get(err or "")


# ---- /broadcasts — создать черновик (POST, с файлом) ---------------------- #
@app.post("/broadcasts")
async def broadcast_create(
    request: Request,
    session: auth.Session = Depends(require_session),
    title: str = Form(""),
    body_template: str = Form(""),
    target_url: str = Form(""),
    csrf_token: str = Form(""),
    file: UploadFile | None = File(None),
):
    await _enforce_csrf(request, session, csrf_token)

    # Анти-флуд черновиков (§6.5).
    if await db.count_recent_draft_broadcasts(within_hours=1) >= config.BROADCAST_DRAFT_MAX_PER_HOUR:
        return _broadcast_new_redirect("rate")

    form = await request.form()
    audience = _parse_audience(form)

    title_val = (title or "").strip()[:200] or None
    body = (body_template or "").strip()
    if not body:
        return _broadcast_new_redirect("empty_body")

    # parse_mode запрещён (§5.11): отвергаем сырые ссылки/markdown/html в теле —
    # трекинг подставляется ТОЛЬКО через плейсхолдер {link}.
    if _has_raw_link_or_markup(body):
        return _broadcast_new_redirect("raw_link_in_body")

    # Длина: с файлом текст идёт подписью (caption ≤1024), без файла — текст ≤4096.
    has_file = file is not None and (file.filename or "")
    max_len = config.CAPTION_MAX_LEN if has_file else config.MSG_MAX_LEN
    if len(body) > max_len:
        return _broadcast_new_redirect("too_long")

    # Трекинг-ссылка: target_url валидируем allow-list'ом схем (дублируется в /r бота).
    link_url: str | None = None
    if (target_url or "").strip():
        link_url = security.validate_target_url(target_url, schemes=config.LINK_URL_SCHEMES)
        if link_url is None:
            return _broadcast_new_redirect("bad_link")
        # Если ссылка задана — в теле ОБЯЗАН быть {link} (иначе её никто не увидит).
        if "{link}" not in body:
            body = body + "\n{link}"

    # Файл рассылки: ту же контентную проверку, что и у продуктов (расширение+MIME+
    # magic-byte + отказ исполняемым, security.sniff_product_file) — НЕ доверяем
    # заявленному браузером content_type (он спуфится: HTML/SVG/EXE под image/png).
    # Читаем потоком с потолком ДО sniff (sniff требует уже прочитанные байты, §6.5),
    # затем kind (photo/document) выводим из ПОДТВЕРЖДЁННОГО magic-byte send, а не из
    # клиентского MIME. Доп. сужение: подтверждённый канон-MIME обязан быть и в
    # UPLOAD_MIME_ALLOW — у разовой рассылки набор форматов уже, чем у каталога оферов.
    file_meta: dict | None = None
    kind = "text"
    if has_file:
        data = await security.read_upload_capped(file, max_bytes=config.MAX_UPLOAD_BYTES)
        if data is None:
            return _broadcast_new_redirect("file_too_big")
        if data:
            sniff = security.sniff_product_file(
                data, filename=file.filename, claimed_mime=file.content_type
            )
            if sniff is None or sniff["mime"] not in config.UPLOAD_MIME_ALLOW:
                return _broadcast_new_redirect("bad_file")
            file_meta = {"filename": (file.filename or "file")[:255],
                         "mime": sniff["mime"], "bytes": data}
            kind = sniff["send"]  # photo|document — из подтверждённого magic-byte, не из заявленного MIME
        # пустой файл (0 байт) — игнорируем, остаётся text

    # Опц. привязка офера из каталога (broadcasts.product_id). Невалидный/несуществующий/
    # архивный id молча игнорируем (рассылка всё равно создаётся) — db-слой ещё раз
    # проверит «active» в той же транзакции и не свяжет мусор.
    product_id_val: int | None = None
    pid_raw = (form.get("product_id") or "").strip()
    if pid_raw.isdigit():
        product_id_val = int(pid_raw)

    estimate = await db.count_broadcast_audience(audience)
    bid = await db.create_broadcast_with_audit(
        title=title_val, messenger=audience["messenger"], kind=kind,
        body_template=body, audience=audience, recipient_estimate=estimate,
        file_meta=file_meta, target_url=link_url, product_id=product_id_val,
        tenant_id=session.active_tenant_id,
        actor=session.actor, ip=_ip(request), user_agent=_ua(request),
    )
    return RedirectResponse(url=f"/broadcasts/{bid}", status_code=303)


def _broadcast_new_redirect(err: str) -> RedirectResponse:
    return RedirectResponse(url=f"/broadcasts/new?err={err}", status_code=303)


def _has_raw_link_or_markup(body: str) -> bool:
    """Отвергаем сырые URL/markdown/html в теле — трекинг идёт через {link} (§5.11).

    Грубо: http(s):// или www. или markdown [..](..) или html-теги <a/<b/<i/<code.
    Плейсхолдер {link} разрешён (подставляется воркером, не сырая ссылка).
    """
    low = body.lower()
    if "http://" in low or "https://" in low or "www." in low or "tg://" in low:
        return True
    if "](" in body:                                    # markdown-ссылка [text](url)
        return True
    if "<a" in low or "<b>" in low or "<i>" in low or "<code" in low or "<pre" in low:
        return True
    return False


# ---- /broadcasts/{id} — аналитика (4 честных метрики, §6.1) --------------- #
@app.get("/broadcasts/{broadcast_id}", response_class=HTMLResponse)
async def broadcast_detail(
    request: Request,
    broadcast_id: int,
    session: auth.Session = Depends(require_session),
    queued: int = 0,
    canceled: int = 0,
    resumed: int = 0,
    product: int = 0,
    err: str | None = None,
):
    rec = await db.get_broadcast(broadcast_id)
    if rec is None:
        raise StarletteHTTPException(status_code=404, detail="Рассылка не найдена")

    # broadcast_view в аудит (получатели — ПДн), на каждое открытие аналитики.
    await db.audit(actor=session.actor, action="broadcast_view",
                   ip=_ip(request), user_agent=_ua(request),
                   detail={"broadcast_id": broadcast_id})

    # Прикреплённый офер (если есть) + активные оферы для смены на черновике.
    bound_product = None
    if rec["product_id"] is not None:
        prec = await db.get_product(rec["product_id"])
        if prec is not None:
            bound_product = {
                "id": prec["id"], "name": prec["name"], "kind": prec["kind"],
                "price_display": _fmt_price(prec["price"], prec["currency"]),
                "has_file": prec["has_file"], "has_link": bool(prec["link"]),
                "status": prec["status"],
            }
    product_options = []
    if rec["status"] == "draft":
        product_options = [_present_product_option(r)
                           for r in await db.list_products_for_select()]

    stats = await db.broadcast_recipient_stats(broadcast_id)
    link = await db.broadcast_link(broadcast_id)
    clicks = await db.broadcast_click_count(broadcast_id) if link else 0
    unsubs = await db.broadcast_unsub_count(broadcast_id)

    recips = await db.list_broadcast_recipients(broadcast_id, limit=500, offset=0)
    recip_rows = [{
        "name": r["name"],
        "phone_masked": security.mask_phone(r["phone_tail"], r["has_phone"]),
        "tg_user_id": r["tg_user_id"],
        "status": r["status"], "error": r["error"], "sent_at": r["sent_at"],
        "clicked": r["clicked"],
    } for r in recips]

    sent = stats["sent"] or 0
    ctr = round((clicks / sent) * 100, 1) if (link and sent) else None

    return templates.TemplateResponse(
        request,
        "broadcast_detail.html",
        {
            "b": dict(rec),
            "stats": dict(stats),
            "clicks": clicks, "unsubs": unsubs, "ctr": ctr,
            "has_link": bool(link),
            "link_url": link["target_url"] if link else None,
            "recipients": recip_rows,
            "queued": bool(queued), "canceled": bool(canceled),
            "resumed": bool(resumed), "product_flash": bool(product),
            "err": _broadcast_send_err_text(err),
            "csrf_token": session.csrf_token,
            "session": session,
            "active": "broadcasts",
            "status_labels": config.STATUS_LABELS,
            "broadcast_status_labels": BROADCAST_STATUS_LABELS,
            "max_recipients": config.MAX_BROADCAST_RECIPIENTS,
            "bound_product": bound_product,
            "product_options": product_options,
            "product_kind_labels": config.PRODUCT_KIND_LABELS,
        },
    )


def _broadcast_send_err_text(err: str | None) -> str | None:
    return {
        "confirm": "Запуск не подтверждён.",
        "stepup": "Неверный пароль. Запуск отклонён.",
        "cap": "Аудитория превышает лимит — введите точное число получателей для подтверждения.",
        "cap_mismatch": "Введённое число не совпадает с числом получателей.",
        "conflict": "Рассылка уже запущена или отменена.",
        "empty": "В аудитории нет ни одного получателя.",
        "bad_product": "Выбранный продукт недоступен (архивирован или удалён).",
    }.get(err or "")


# ---- /broadcasts/{id}/send — ПОДТВЕРЖДЕНИЕ + cap + step-up (§7.1) ---------- #
@app.post("/broadcasts/{broadcast_id}/send")
async def broadcast_send(
    request: Request,
    broadcast_id: int,
    session: auth.Session = Depends(require_session),
    confirm: str = Form(""),
    confirm_count: str = Form(""),
    password: str = Form(""),
    csrf_token: str = Form(""),
):
    await _enforce_csrf(request, session, csrf_token)

    rec = await db.get_broadcast(broadcast_id)
    if rec is None:
        raise StarletteHTTPException(status_code=404, detail="Рассылка не найдена")

    # 1) Явное подтверждение (паттерн export_full).
    if confirm != "yes":
        return _broadcast_detail_redirect(broadcast_id, "confirm")

    # 2) Step-up: повторный пароль оператора (constant-time argon2). Режет угнанную
    #    куку/открытую вкладку. Дёшево, переживает рестарт (§7.1).
    if not auth.verify_password(password):
        return _broadcast_detail_redirect(broadcast_id, "stepup")

    # Считаем фактический размер аудитории ТЕМ ЖЕ фильтром, что бот возьмёт snapshot'ом.
    # audience_filter приходит из jsonb СТРОКОЙ (кодек не зарегистрирован) — декодируем.
    audience = db.decode_audience(rec["audience_filter"])
    count = await db.count_broadcast_audience(audience)
    if count <= 0:
        return _broadcast_detail_redirect(broadcast_id, "empty")

    # 3) Hard-cap: сверх лимита требуем точное число эхом (как «введите сумму прописью»).
    if count > config.MAX_BROADCAST_RECIPIENTS:
        if not (confirm_count or "").strip():
            return _broadcast_detail_redirect(broadcast_id, "cap")
        try:
            if int(confirm_count.strip()) != count:
                return _broadcast_detail_redirect(broadcast_id, "cap_mismatch")
        except ValueError:
            return _broadcast_detail_redirect(broadcast_id, "cap_mismatch")

    # 4) Атомарный перевод draft→queued (0 строк → уже запущена). recipient_count
    #    пишется ДО старта + в аудит broadcast_send. Бот не берёт, пока не queued+count.
    result = await db.queue_broadcast_with_audit(
        broadcast_id, recipient_count=count, actor=session.actor,
        ip=_ip(request), user_agent=_ua(request),
    )
    if result is None:
        raise StarletteHTTPException(status_code=404, detail="Рассылка не найдена")
    if result == "conflict":
        return _broadcast_detail_redirect(broadcast_id, "conflict")
    return RedirectResponse(url=f"/broadcasts/{broadcast_id}?queued=1", status_code=303)


# ---- /broadcasts/{id}/resume — возобновить ПРИОСТАНОВЛЕННУЮ (paused→sending) - #
# Мощное действие (заново запускает исходящие): CSRF + step-up пароль, как /send.
# Закрывает терминальный тупик 'paused' от circuit-breaker / «файл не готов» (§5.5/§5.9):
# без этого пути транзиентный всплеск замораживал рассылку навсегда.
@app.post("/broadcasts/{broadcast_id}/resume")
async def broadcast_resume(
    request: Request,
    broadcast_id: int,
    session: auth.Session = Depends(require_session),
    confirm: str = Form(""),
    password: str = Form(""),
    csrf_token: str = Form(""),
):
    await _enforce_csrf(request, session, csrf_token)

    rec = await db.get_broadcast(broadcast_id)
    if rec is None:
        raise StarletteHTTPException(status_code=404, detail="Рассылка не найдена")

    # Подтверждение + step-up пароль (constant-time argon2) — как при запуске.
    if confirm != "yes":
        return _broadcast_detail_redirect(broadcast_id, "confirm")
    if not auth.verify_password(password):
        return _broadcast_detail_redirect(broadcast_id, "stepup")

    result = await db.resume_broadcast_with_audit(
        broadcast_id, actor=session.actor, ip=_ip(request), user_agent=_ua(request),
    )
    if result is None:
        raise StarletteHTTPException(status_code=404, detail="Рассылка не найдена")
    if result == "conflict":
        return _broadcast_detail_redirect(broadcast_id, "conflict")
    return RedirectResponse(url=f"/broadcasts/{broadcast_id}?resumed=1", status_code=303)


# ---- /broadcasts/{id}/cancel — отмена (обычный CSRF, обратимо до старта) --- #
@app.post("/broadcasts/{broadcast_id}/cancel")
async def broadcast_cancel(
    request: Request,
    broadcast_id: int,
    session: auth.Session = Depends(require_session),
    csrf_token: str = Form(""),
):
    await _enforce_csrf(request, session, csrf_token)
    result = await db.cancel_broadcast_with_audit(
        broadcast_id, actor=session.actor, ip=_ip(request), user_agent=_ua(request),
    )
    if result is None:
        raise StarletteHTTPException(status_code=404, detail="Рассылка не найдена")
    if result == "conflict":
        return _broadcast_detail_redirect(broadcast_id, "conflict")
    return RedirectResponse(url=f"/broadcasts/{broadcast_id}?canceled=1", status_code=303)


# ---- /broadcasts/{id}/product — привязать/сменить/снять офер на черновике ----- #
# Обычное действие (не отправка): CSRF, без step-up. Только для draft (после queued
# состав сообщения зафиксирован). product_id='' → отвязать. db-слой ещё раз проверяет
# draft + active-офер в одной транзакции (defence-in-depth).
@app.post("/broadcasts/{broadcast_id}/product")
async def broadcast_set_product(
    request: Request,
    broadcast_id: int,
    session: auth.Session = Depends(require_session),
    product_id: str = Form(""),
    csrf_token: str = Form(""),
):
    await _enforce_csrf(request, session, csrf_token)
    pid: int | None = None
    if (product_id or "").strip().isdigit():
        pid = int(product_id.strip())
    result = await db.set_broadcast_product_with_audit(
        broadcast_id, pid, actor=session.actor, ip=_ip(request), user_agent=_ua(request),
    )
    if result is None:
        raise StarletteHTTPException(status_code=404, detail="Рассылка не найдена")
    if result == "conflict":
        return _broadcast_detail_redirect(broadcast_id, "conflict")
    if result == "bad_product":
        return _broadcast_detail_redirect(broadcast_id, "bad_product")
    return RedirectResponse(url=f"/broadcasts/{broadcast_id}?product=1", status_code=303)


def _broadcast_detail_redirect(broadcast_id: int, err: str) -> RedirectResponse:
    return RedirectResponse(url=f"/broadcasts/{broadcast_id}?err={err}", status_code=303)


# Подписи статусов рассылки для UI (канон со схемой broadcasts.status).
BROADCAST_STATUS_LABELS = {
    "draft": "Черновик",
    "queued": "В очереди",
    "sending": "Отправляется",
    "paused": "Пауза",
    "done": "Завершена",
    "canceled": "Отменена",
}


# =========================================================================== #
# ПЛАТЕЖИ (раздел «Платежи», Phase 1A). Ручной учёт продаж: оператор фиксирует
# заказ (лид опц. + офер опц. + сумма + статус) → orders (source='manual'); дашборд
# выручки + лента заказов. Бот в 1A не участвует (онлайн-оплата = Phase 1B). Зеркалит
# каталог продуктов: CSRF, PRG, аудит в той же транзакции.
# =========================================================================== #

def _present_order_row(r) -> dict:
    return {
        "id": r["id"],
        "amount_display": _fmt_price(r["amount"], r["currency"]),
        "status": r["status"],
        "source": r["source"],
        "note": r["note"],
        "created_at": r["created_at"],
        "paid_at": r["paid_at"],
        "lead_id": r["lead_id"],
        "lead_name": r["lead_name"],
        "product_name": r["product_name"],
    }


def _order_err_text(err: str | None) -> str | None:
    return {
        "bad_amount": "Сумма указана неверно (число ≥ 0, до 2 знаков после запятой).",
        "bad_status": "Недопустимый статус заказа.",
        "bad_currency": "Недопустимая валюта.",
        "not_found": "Заказ не найден.",
    }.get(err or "")


# ---- /payments — дашборд выручки + лента заказов + форма записи продажи ----- #
@app.get("/payments", response_class=HTMLResponse)
async def payments_list(
    request: Request,
    session: auth.Session = Depends(require_session),
    saved: int = 0,
    updated: int = 0,
    err: str | None = None,
):
    try:
        page = max(1, int(request.query_params.get("page", "1")))
    except ValueError:
        page = 1
    per_page = config.PER_PAGE
    offset = (page - 1) * per_page

    summary = await db.revenue_summary()
    total = await db.count_orders()
    rows = await db.list_orders(limit=per_page, offset=offset)
    orders = [_present_order_row(r) for r in rows]

    # Селекторы формы «Записать продажу»: последние лиды + активные оферы.
    lead_opts = [
        {"id": r["id"], "name": r["name"], "created_at": r["created_at"]}
        for r in await db.list_recent_leads_for_select()
    ]
    product_opts = [
        {"id": r["id"], "name": r["name"],
         "price_display": _fmt_price(r["price"], r["currency"])}
        for r in await db.list_products_for_select()
    ]

    # Сумму выводим со знаком ₽ (MVP-допущение единой валюты, см. db.revenue_summary).
    rub = config.PRODUCT_CURRENCY_SIGNS["RUB"]
    summary_view = {
        "paid_total": f"{_fmt_amount(summary['paid_total'])} {rub}",
        "paid_30d": f"{_fmt_amount(summary['paid_30d'])} {rub}",
        "paid_7d": f"{_fmt_amount(summary['paid_7d'])} {rub}",
        "refunded_total": f"{_fmt_amount(summary['refunded_total'])} {rub}",
        "paid_count": summary["paid_count"],
        "pending_count": summary["pending_count"],
        "refunded_count": summary["refunded_count"],
        "total_count": summary["total_count"],
    }

    base_qs = ""
    return templates.TemplateResponse(
        request,
        "payments.html",
        {
            "orders": orders,
            "summary": summary_view,
            "page": page,
            "per_page": per_page,
            "total": total,
            "base_qs": base_qs,
            "lead_opts": lead_opts,
            "product_opts": product_opts,
            "statuses": config.ORDER_STATUSES_MANUAL,
            "status_labels": config.ORDER_STATUS_LABELS,
            "source_labels": config.ORDER_SOURCE_LABELS,
            "currencies": config.PRODUCT_CURRENCIES,
            "currency_labels": config.PRODUCT_CURRENCY_LABELS,
            "note_max": config.ORDER_NOTE_MAX_LEN,
            "csrf_token": session.csrf_token,
            "session": session,
            "active": "payments",
            "saved": bool(saved),
            "updated": bool(updated),
            "err": _order_err_text(err),
        },
    )


def _fmt_amount(value) -> str:
    """Сумма без знака валюты: «1 990» / «1 990,50» (узкий неразрывный пробел)."""
    if value is None:
        return "0"
    d = Decimal(value)
    whole = int(d)
    cents = int((d - whole) * 100)
    int_str = f"{whole:,}".replace(",", " ")
    return int_str if cents == 0 else f"{int_str},{cents:02d}"


# ---- /payments — записать продажу (POST, manual) -------------------------- #
@app.post("/payments")
async def payment_create(
    request: Request,
    session: auth.Session = Depends(require_session),
    lead_id: str = Form(""),
    product_id: str = Form(""),
    amount: str = Form(""),
    currency: str = Form("RUB"),
    status: str = Form("paid"),
    note: str = Form(""),
    mark_converted: str = Form(""),
    csrf_token: str = Form(""),
):
    await _enforce_csrf(request, session, csrf_token)

    amount_val, amount_ok = _parse_price(amount)
    if not amount_ok or amount_val is None:
        return RedirectResponse(url="/payments?err=bad_amount", status_code=303)
    if status not in config.ORDER_STATUSES:
        return RedirectResponse(url="/payments?err=bad_status", status_code=303)
    if currency not in db._PRODUCT_CURRENCY_SET:
        return RedirectResponse(url="/payments?err=bad_currency", status_code=303)

    # Лид/офер опциональны; парсим типобезопасно (мусор → None, не до SQL).
    lead_uuid = None
    if (lead_id or "").strip():
        try:
            lead_uuid = uuid.UUID(lead_id.strip())
        except ValueError:
            lead_uuid = None
    pid: int | None = int(product_id.strip()) if (product_id or "").strip().isdigit() else None
    note_val = (note or "").strip()[: config.ORDER_NOTE_MAX_LEN] or None
    mark_conv = (mark_converted or "").strip() == "1"

    await db.create_order_with_audit(
        lead_id=lead_uuid, product_id=pid, amount=amount_val, currency=currency,
        status=status, note=note_val, mark_converted=mark_conv,
        tenant_id=session.active_tenant_id,
        actor=session.actor, ip=_ip(request), user_agent=_ua(request),
    )
    return RedirectResponse(url="/payments?saved=1", status_code=303)


# ---- /payments/{id}/status — возврат/правка статуса (POST) ----------------- #
@app.post("/payments/{order_id}/status")
async def payment_set_status(
    request: Request,
    order_id: uuid.UUID,
    session: auth.Session = Depends(require_session),
    status: str = Form(...),
    csrf_token: str = Form(""),
):
    await _enforce_csrf(request, session, csrf_token)
    if status not in config.ORDER_STATUSES:
        return RedirectResponse(url="/payments?err=bad_status", status_code=303)
    row = await db.set_order_status_with_audit(
        order_id, new_status=status, actor=session.actor,
        ip=_ip(request), user_agent=_ua(request),
    )
    if row is None:
        return RedirectResponse(url="/payments?err=not_found", status_code=303)
    return RedirectResponse(url="/payments?updated=1", status_code=303)


# =========================================================================== #
# ПОДПИСКА / БИЛЛИНГ ПО ТАРИФАМ (раздел «Подписка», модель НЕЙРОАГЕНТОВ). Метрика =
# сообщения ИИ (Лия) за период. Тарифы — в config (квота + overage). Текущий тариф/
# период = последний ОПЛАЧЕННЫЙ счёт; превышение прошлого периода доначисляется в
# следующий счёт. «Выбрать тариф» → счёт 'pending' + платёж ЮKassa → confirmation_url.
# Вебхук перепроверяет платёж, ставит paid + карту, снимает флаг отмены.
# =========================================================================== #

def _plan(key: str | None) -> dict | None:
    return config.SERVICE_PLANS.get(key) if key else None


def _plan_amount(plan: dict | None):
    """Decimal цены тарифа (или None для договорного)."""
    if plan and plan.get("price") is not None:
        return Decimal(str(plan["price"]))
    return None


def _next_period_from(end_date):
    """(start, end) следующего периода: продлеваем от хвоста, иначе с сегодня."""
    today = datetime.now(timezone.utc).date()
    start = end_date if (end_date and end_date > today) else today
    return start, start + timedelta(days=config.SERVICE_PLAN_PERIOD_DAYS)


def _meter(used: int, quota):
    """Счётчик: в квоте / превышение / осталось + доли для полосы (grey + orange)."""
    if quota is None:
        return {"used": used, "quota": None, "in_quota": used, "over": 0,
                "remaining": None, "pct": 0, "over_pct": 0}
    in_quota = min(used, quota)
    over = max(0, used - quota)
    remaining = max(0, quota - used)
    total = max(used, quota) or 1
    return {"used": used, "quota": quota, "in_quota": in_quota, "over": over,
            "remaining": remaining,
            "pct": round(in_quota * 100 / total), "over_pct": round(over * 100 / total)}


async def _current_subscription(tenant_id) -> dict:
    """Текущая подписка АКТИВНОГО ТЕНАНТА: тариф/период из последнего оплаченного счёта +
    флаг отмены + живой расход сообщений ИИ за период. get_latest_paid_invoice/count_ai_messages
    скоупятся по app.tenant_id (RLS, выставлен require_session); флаг отмены — per-tenant."""
    latest = await db.get_latest_paid_invoice()
    canceled = await db.is_subscription_canceled(tenant_id)
    today = datetime.now(timezone.utc).date()
    if latest is None:
        return {"exists": False, "active": False, "canceled": canceled,
                "plan_key": None, "meter": _meter(0, None)}
    expired = latest["period_end"] < today
    # Расход за текущий период: до now (если идёт) или до конца периода (если истёк).
    end_cap = None if not expired else latest["period_end"]
    used = await db.count_ai_messages(latest["period_start"], end_cap)
    return {
        "exists": True,
        "active": (not expired) and (not canceled),
        "canceled": canceled,
        "expired": expired,
        "plan_key": latest["plan_key"],
        "plan_name": latest["plan_name"],
        "amount_display": _fmt_amount(latest["amount"]) + " ₽",
        "period_start": latest["period_start"],
        "period_end": latest["period_end"],
        "meter": _meter(used, latest["quota"]),
    }


def _plans_for_picker(current_key: str | None) -> list[dict]:
    out = []
    for k in config.SERVICE_PLAN_ORDER:
        p = config.SERVICE_PLANS[k]
        out.append({"key": k, "is_current": (k == current_key), **p})
    return out


def _present_invoice(r) -> dict:
    """Строка истории: период + использование (живой расчёт) + транзакция."""
    quota = r["quota"]
    used = r["used"]
    over = max(0, used - quota) if quota is not None else 0
    remaining = max(0, quota - used) if quota is not None else None
    return {
        "id": r["id"],
        "period_start": r["period_start"], "period_end": r["period_end"],
        "plan_name": r["plan_name"],
        "used": used, "quota": quota, "remaining": remaining, "over": over,
        "amount_display": _fmt_amount(r["amount"]) + " ₽",
        "status": r["status"], "paid_at": r["paid_at"], "card_last4": r["card_last4"],
    }


def _service_err_text(err: str | None) -> str | None:
    return {
        "bad_plan": "Неизвестный тариф.",
        "not_payable": "Этот тариф оформляется по заявке — нажмите «Оставить заявку».",
        "no_yookassa": "Онлайн-оплата выключена: не заданы ключи ЮKassa (YOOKASSA_SHOP_ID / YOOKASSA_SECRET_KEY).",
        "bad_email": "Укажите корректный email — на него ЮKassa пришлёт чек (54-ФЗ).",
        "no_tenant": "Сначала выберите клиента (раздел «Клиенты»).",
        "bad_amount": "Сумма пополнения вне допустимых границ.",
        "yk_failed": "Не удалось создать платёж в ЮKassa. Попробуйте позже.",
    }.get(err or "")


# ---- Публичная оплата подписки с сайта сервиса (info.pro-agent-ai.ru) -------- #
# Форма pay.html живёт на статическом сайте сервиса (без сессии/CSRF — внешний источник,
# как вебхук). Поля формы валидируем здесь; сумму берём С СЕРВЕРА (из тарифа), не из формы.
_CHECKBOX_ON = {"on", "1", "true", "yes"}
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _valid_email(value: str) -> bool:
    return bool(value) and len(value) <= 254 and _EMAIL_RE.match(value) is not None


def _service_receipt(email: str, description: str, amount) -> dict | None:
    """Чек 54-ФЗ для платежа подписки (опц.): включён флагом + есть email → одна
    позиция-услуга, электронный чек на email. Иначе None (платёж без чека, как было)."""
    if not config.SERVICE_RECEIPT_ENABLED:
        return None
    return {
        "customer": {"email": email},
        "items": [{
            "description": description[:128],
            "quantity": "1.00",
            "amount": {"value": yookassa.amount_str(amount), "currency": config.SERVICE_CURRENCY},
            "vat_code": config.SERVICE_VAT_CODE,
            "payment_subject": "service",
            "payment_mode": "full_payment",
        }],
    }


def _blended_token_price() -> Decimal:
    """Смешанная цена ₽/млн токенов: used_tokens Timeweb не делит вход/выход, поэтому
    (1-share)·вход + share·выход (тарифы и share — config/env)."""
    share = Decimal(str(config.AI_OUT_TOKENS_SHARE))
    return (Decimal(str(config.AI_PRICE_IN_RUB_PER_M)) * (1 - share)
            + Decimal(str(config.AI_PRICE_OUT_RUB_PER_M)) * share)


async def _ai_economics(is_platform: bool) -> dict | None:
    """Экономика сервиса — ТОЛЬКО платформенный супер (env-админ): клиент-владелец/операторы
    этого видеть не должны. Таблица расходов по агентам: РЕАЛЬНЫЙ used_tokens каждого
    агента Timeweb × смешанная цена → себестоимость; против выручки подписки → маржа;
    + баланс аккаунта. Всё в try — сбой Timeweb-API не должен ронять страницу (None → скрыт).
    ⚠️ Гейт по личности, НЕ по role (ревью)."""
    if not is_platform or not config.TIMEWEB_AI_ENABLED:
        return None
    try:
        agents = await timeweb_ai.list_agents()
        fin = await timeweb_ai.account_finances()
    except timeweb_ai.TimewebAIError:
        return None
    price = _blended_token_price()
    rows, used_total = [], 0
    for a in agents:
        used = int(a.get("used_tokens") or 0)
        used_total += used
        rows.append({
            "name": a.get("name") or f"агент {a.get('id')}",
            "used_tokens": used,
            "cost": _fmt_amount((Decimal(used) / Decimal(1_000_000) * price).quantize(Decimal("0.01"))),
        })
    cost = (Decimal(used_total) / Decimal(1_000_000) * price).quantize(Decimal("0.01"))
    revenue = Decimal(str(await db.service_revenue_total() or 0))
    margin = (revenue - cost).quantize(Decimal("0.01"))
    balance, hours_left, monthly = fin.get("balance"), fin.get("hours_left"), fin.get("monthly_cost")
    return {
        "rows": rows,
        "used_tokens": used_total,
        "price_in": _fmt_amount(Decimal(str(config.AI_PRICE_IN_RUB_PER_M))),
        "price_out": _fmt_amount(Decimal(str(config.AI_PRICE_OUT_RUB_PER_M))),
        "price_blended": _fmt_amount(price.quantize(Decimal("0.01"))),
        "out_share_pct": int(Decimal(str(config.AI_OUT_TOKENS_SHARE)) * 100),
        "cost": _fmt_amount(cost),
        "revenue": _fmt_amount(revenue),
        "margin": _fmt_amount(margin),
        "margin_negative": margin < 0,
        "balance": _fmt_amount(Decimal(str(balance))) if balance is not None else None,
        "monthly_cost": _fmt_amount(Decimal(str(monthly))) if monthly is not None else None,
        "days_left": round(float(hours_left) / 24, 1) if hours_left else None,
    }


# ---- /subscription — текущий тариф + счётчик + история + «Выбрать тариф» ----- #
@app.get("/subscription", response_class=HTMLResponse)
async def subscription_page(
    request: Request,
    session: auth.Session = Depends(require_session),
    paid: int = 0,
    canceled: int = 0,
    detached: int = 0,
    err: str | None = None,
):
    sub = await _current_subscription(session.active_tenant_id)
    invoices = [_present_invoice(r) for r in await db.list_service_invoices()]
    return templates.TemplateResponse(
        request,
        "subscription.html",
        {
            "sub": sub,
            "plans": _plans_for_picker(sub.get("plan_key")),
            "invoices": invoices,
            "yookassa_enabled": config.YOOKASSA_ENABLED,
            "receipt_required": config.SERVICE_RECEIPT_ENABLED,
            "contact_url": config.SERVICE_CONTACT_URL,
            "period_days": config.SERVICE_PLAN_PERIOD_DAYS,
            "csrf_token": session.csrf_token,
            "session": session,
            "active": "subscription",
            "paid_flash": bool(paid),
            "canceled_flash": bool(canceled),
            "detached_flash": bool(detached),
            "err": _service_err_text(err),
            "economics": await _ai_economics(session.is_platform),
            # Wave 2a: кошелёк тенанта (топап + история платежей платформы + отвязка карты)
            "wallet": await _wallet_ctx(session),
        },
    )


async def _wallet_ctx(session: auth.Session) -> dict | None:
    """Кошелёк активного тенанта для раздела «Подписка». None — тенант не выбран."""
    tid = session.active_tenant_id
    if not tid:
        return None
    balance = await db.get_wallet_balance(tid)
    pays = await db.list_platform_payments(tid, limit=15)
    return {
        "balance": money.micro_to_rub_str(balance),
        "balance_negative": balance < 0,
        "topup_min": config.WALLET_TOPUP_MIN_RUB,
        "topup_max": config.WALLET_TOPUP_MAX_RUB,
        "receipt_required": config.SERVICE_RECEIPT_ENABLED,
        "saved_method": bool(await db.get_saved_payment_method(tid)),
        "payments": [
            {
                "type": "Пополнение кошелька" if p["type"] == "topup" else "Подписка",
                "amount": money.micro_to_rub_str(int(p["amount_microrub"])),
                "status": p["status"],
                "created_at": p["created_at"],
            }
            for p in pays
        ],
    }


# ---- /subscription/select — выбрать тариф → счёт + платёж ЮKassa → оплата ---- #
@app.post("/subscription/select")
async def subscription_select(
    request: Request,
    session: auth.Session = Depends(require_session),
    plan_key: str = Form(""),
    email: str = Form(""),
    csrf_token: str = Form(""),
):
    await _enforce_csrf(request, session, csrf_token)
    # service_invoices под RLS → счёт привязывается к активному тенанту; без него не создаём.
    if not session.active_tenant_id:
        return RedirectResponse(url="/subscription?err=no_tenant", status_code=303)
    plan = _plan(plan_key)
    if plan is None:
        return RedirectResponse(url="/subscription?err=bad_plan", status_code=303)
    if not plan.get("payable"):
        return RedirectResponse(url="/subscription?err=not_payable", status_code=303)
    if not config.YOOKASSA_ENABLED:
        return RedirectResponse(url="/subscription?err=no_yookassa", status_code=303)
    # Фискальный магазин (54-ФЗ): чек обязателен → нужен корректный email покупателя.
    email = email.strip()
    if config.SERVICE_RECEIPT_ENABLED and not _valid_email(email):
        return RedirectResponse(url="/subscription?err=bad_email", status_code=303)

    # Превышение ПРОШЛОГО (текущего оплаченного) периода → доначисляем в этот счёт.
    latest = await db.get_latest_paid_invoice()
    overage_count = 0
    overage_amount = Decimal("0")
    start_from = None
    if latest is not None:
        prev_plan = _plan(latest["plan_key"]) or {}
        prev_quota = latest["quota"]
        if prev_quota is not None:
            prev_used = await db.count_ai_messages(latest["period_start"], latest["period_end"])
            overage_count = max(0, prev_used - prev_quota)
        over_price = prev_plan.get("overage") or 0
        overage_amount = (Decimal(str(over_price)) * overage_count).quantize(Decimal("0.01"))
        start_from = latest["period_end"]

    start, end = _next_period_from(start_from)
    plan_amount = _plan_amount(plan)
    amount = (plan_amount + overage_amount).quantize(Decimal("0.01"))

    invoice_id = await db.create_period_invoice(
        tenant_id=session.active_tenant_id,
        period_start=start, period_end=end, plan_key=plan_key, plan_name=plan["name"],
        quota=plan["quota"], plan_amount=plan_amount, overage_count=overage_count,
        overage_amount=overage_amount, amount=amount, currency=config.SERVICE_CURRENCY,
        actor=session.actor, ip=_ip(request), user_agent=_ua(request),
    )

    host = request.headers.get("host", "")
    return_url = f"https://{host}/subscription?paid=1"
    description = f"{plan['name']} · {start:%d.%m.%Y}–{end:%d.%m.%Y}"
    try:
        payment = await yookassa.create_payment(
            amount=amount, currency=config.SERVICE_CURRENCY,
            description=description,
            return_url=return_url, idempotence_key=invoice_id,
            # Wave 4: kind+tenant_id → вебхук активирует subscriptions + начислит
            # included_credits (метеринг по тарифу). invoice_id/plan — для legacy UI.
            # Wave 2b: email в metadata → сохранится в subscriptions.receipt_email для
            # чеков 54-ФЗ будущих безакцептных автосписаний.
            metadata={"invoice_id": invoice_id, "plan": plan_key,
                      "kind": "platform_subscription",
                      "tenant_id": str(session.active_tenant_id) if session.active_tenant_id else "",
                      "email": email},
            receipt=_service_receipt(email, description, amount),
            # Wave 2b: сохранить способ оплаты для автопродления (рекуррент включён).
            save_payment_method=True,
        )
    except yookassa.YooKassaError:
        import logging
        logging.getLogger("admin-panel").exception("yookassa create_payment failed")
        return RedirectResponse(url="/subscription?err=yk_failed", status_code=303)

    pid = payment.get("id")
    conf_url = (payment.get("confirmation") or {}).get("confirmation_url")
    if pid:
        await db.attach_yookassa_payment(invoice_id, pid)
    if not conf_url:
        return RedirectResponse(url="/subscription?err=yk_failed", status_code=303)
    return RedirectResponse(url=conf_url, status_code=303)


# ---- /subscription/cancel — отменить подписку (флаг в app_settings) -------- #
@app.post("/subscription/cancel")
async def subscription_cancel(
    request: Request,
    session: auth.Session = Depends(require_session),
    csrf_token: str = Form(""),
):
    await _enforce_csrf(request, session, csrf_token)
    if not session.active_tenant_id:
        return RedirectResponse(url="/subscription?err=no_tenant", status_code=303)
    await db.set_subscription_canceled(
        session.active_tenant_id, True,
        actor=session.actor, ip=_ip(request), user_agent=_ua(request)
    )
    return RedirectResponse(url="/subscription?canceled=1", status_code=303)


# ---- /service/subscribe — ПУБЛИЧНАЯ оплата подписки с сайта сервиса --------- #
@app.post("/service/subscribe")
async def service_subscribe(
    request: Request,
    plan: str = Form(""),
    email: str = Form(""),
    agree_oferta: str = Form(""),
    agree_pdn: str = Form(""),
    persona: str = Form(""),
):
    """Публичная форма оплаты с info.pro-agent-ai.ru/pay.html (БЕЗ сессии/CSRF — внешний
    источник, как вебхук). Сумму берём С СЕРВЕРА (из тарифа), НЕ из формы — защита от подмены.
    Email — только в чек (минимизация ПДн): не логируем, не кладём в metadata.
    152-ФЗ: оба согласия (оферта + обработка ПДн) обязательны. Ошибки → назад на pay.html?err=."""
    site = config.SERVICE_SITE_URL

    def _back(err: str) -> RedirectResponse:
        return RedirectResponse(url=f"{site}/pay.html?err={err}", status_code=303)

    plan_obj = _plan(plan)
    if plan_obj is None or not plan_obj.get("payable"):
        return _back("bad_plan")
    email = (email or "").strip()
    if not _valid_email(email):
        return _back("bad_email")
    if agree_oferta not in _CHECKBOX_ON or agree_pdn not in _CHECKBOX_ON:
        return _back("no_consent")
    if not config.YOOKASSA_ENABLED:
        return _back("no_yookassa")

    amount = _plan_amount(plan_obj)
    description = f"Подписка «ИИ-Агент Про» — {plan_obj['name']}"
    # Метка «ИИ-сотрудника» с витрины (опциональна): только из белого списка персон —
    # уходит в metadata платежа, чтобы видеть, какой образ реально продаёт.
    persona = persona.strip() if persona.strip() in config.PERSONA_PRESETS else ""
    metadata = {"kind": "service_landing", "plan": plan}
    if persona:
        metadata["persona"] = persona
    import logging
    lg = logging.getLogger("admin-panel")
    try:
        payment = await yookassa.create_payment(
            amount=amount, currency=config.SERVICE_CURRENCY,
            description=description, return_url=f"{site}/pay-success.html",
            idempotence_key=uuid.uuid4().hex,
            metadata=metadata,
            receipt=_service_receipt(email, description, amount),
        )
    except yookassa.YooKassaError:
        lg.exception("service_subscribe create_payment failed")
        return _back("yk_failed")

    conf_url = (payment.get("confirmation") or {}).get("confirmation_url")
    if not conf_url:
        lg.warning("service_subscribe no confirmation_url (status=%s)", payment.get("status"))
        return _back("yk_failed")
    lg.info("service_subscribe payment created id=%s plan=%s", payment.get("id"), plan)
    return RedirectResponse(url=conf_url, status_code=303)


# ---- /webhooks/yookassa — публичный вебхук (без сессии/CSRF) --------------- #
@app.post("/webhooks/yookassa")
async def yookassa_webhook(request: Request):
    """ЕДИНЫЙ вебхук ЮKassa для ОБОИХ магазинов (подписка агентства + продажи школы, 1B):
    оба ЛК шлют payment.succeeded на этот URL. НЕ доверяем телу: берём только id платежа,
    ветку выбираем матчем по СВОЕЙ БД (id среди orders.provider_payment_id → заказ школы,
    иначе → счёт подписки) и ПЕРЕПРОВЕРЯЕМ платёж через API кредами СВОЕГО магазина
    (status=succeeded & paid). Заказ: paid + лид converted + «спасибо» через outbox
    (доставит бот). Подписка: как раньше. Всегда 200 (иначе провайдер ретраит)."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})
    obj = (body or {}).get("object") or {}
    payment_id = obj.get("id")
    if not payment_id:
        return JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})
    # Дедуп повторных доставок (Wave 2a, ТЗ §5.3): ЮKassa ретраит уведомления.
    # Повтор уже виденного payment_id → 200 сразу, без повторной обработки
    # (кошелёк/заказ не зачтутся дважды; сами ветки тоже идемпотентны — оборона в глубину).
    event_key = f"yookassa:{payment_id}:{(body or {}).get('event') or 'payment'}"
    if not await db.webhook_event_new(event_key, (body or {}).get("event"), body or {}):
        return JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})
    processed_ok = True
    try:
        if config.SHOP_PAYMENTS_CONFIGURED and await db.order_exists_for_payment(payment_id):
            # Ветка ЗАКАЗА школы (платёж создан ботом-кнопкой или панелью-«счётом»).
            payment = await yookassa.get_shop_payment(payment_id)
            if payment.get("status") == "succeeded" and payment.get("paid"):
                await db.mark_order_paid_by_payment(payment_id)
        elif config.YOOKASSA_ENABLED:
            # Магазин платформы: топап кошелька (Wave 2a) ИЛИ счёт подписки (legacy).
            payment = await yookassa.get_payment(payment_id)
            meta = payment.get("metadata") or {}
            if meta.get("kind") == "platform_topup" and meta.get("tenant_id"):
                if payment.get("status") == "succeeded" and payment.get("paid"):
                    await db.mark_topup_succeeded(meta["tenant_id"], payment_id, payment)
            elif meta.get("kind") == "platform_subscription" and meta.get("tenant_id"):
                # Wave 4: оплата тарифа → (1) legacy service_invoice paid (UI-витрина
                # /subscription) + (2) активация subscriptions + included_credits в
                # кошелёк (источник тарифа для метеринга). Обе системы наполняются.
                if payment.get("status") == "succeeded" and payment.get("paid"):
                    sub_tenant = meta["tenant_id"]
                    card = (((payment.get("payment_method") or {}).get("card") or {}).get("last4"))
                    row = await db.mark_service_invoice_paid_by_payment(
                        payment_id, tenant_id=sub_tenant, card_last4=card)
                    if row is not None and await db.is_subscription_canceled(sub_tenant):
                        await db.set_subscription_canceled(sub_tenant, False, actor="yookassa-webhook",
                                                           ip=None, user_agent=None)
                    amount_micro = money.rub_to_micro(
                        (payment.get("amount") or {}).get("value") or "0")
                    # Wave 2b: сохранённый способ оплаты + email для авточеков рекуррента.
                    pm = payment.get("payment_method") or {}
                    pm_id = pm.get("id") if pm.get("saved") else None
                    await db.activate_subscription_from_payment(
                        meta["tenant_id"], meta.get("plan") or "", payment_id,
                        amount_micro, config.SERVICE_PLAN_PERIOD_DAYS,
                        payment_method_id=pm_id, receipt_email=meta.get("email") or None)
            elif meta.get("kind") == "platform_subscription_renewal" and meta.get("tenant_id"):
                # Wave 2b: безакцептное автосписание (cron) → ПРОДЛЕВАЕТ существующую
                # подписку (renew_subscription: UPDATE период + included_credits,
                # идемпотентно по payment_id), НЕ создаёт новую строку.
                if payment.get("status") == "succeeded" and payment.get("paid"):
                    amount_micro = money.rub_to_micro(
                        (payment.get("amount") or {}).get("value") or "0")
                    sub_id = meta.get("subscription_id")
                    if sub_id:
                        await db.renew_subscription(
                            meta["tenant_id"], sub_id, payment_id,
                            amount_micro, config.SERVICE_PLAN_PERIOD_DAYS)
            elif (payment.get("status") == "succeeded" and payment.get("paid")
                  and meta.get("tenant_id")):
                # Legacy-фолбэк счёта подписки БЕЗ нового kind, но С tenant_id в metadata
                # (старые/ручные платежи). service_invoices под RLS → tenant обязателен;
                # платежи лендинга (service_landing) счёт не создают, а platform_subscription
                # обработан выше — поэтому без tenant_id тут отмечать нечего (no-op).
                lt = meta["tenant_id"]
                card = (((payment.get("payment_method") or {}).get("card") or {}).get("last4"))
                row = await db.mark_service_invoice_paid_by_payment(
                    payment_id, tenant_id=lt, card_last4=card)
                # Оплата возобновляет подписку — снимаем флаг отмены (если стоял).
                if row is not None and await db.is_subscription_canceled(lt):
                    await db.set_subscription_canceled(lt, False, actor="yookassa-webhook",
                                                       ip=None, user_agent=None)
    except Exception:
        processed_ok = False
        import logging
        logging.getLogger("admin-panel").exception("yookassa webhook verify failed")
    try:
        await db.webhook_event_done(event_key, processed_ok)
    except Exception:
        pass
    return JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})


# =========================================================================== #
# Раздел «ИИ-агенты» (/agents): управление авто-ответами Лии БЕЗ редеплоя бота.
# Панель пишет настройки в app_settings (ai_enabled/ai_agent_id/ai_fallback_text),
# бот читает их поверх env. Токен Timeweb AI остаётся в env бота (секрет, не в БД),
# поэтому тест-вызов из панели здесь не делаем — у панели токена нет (см. handoff).
# =========================================================================== #
def _present_liya_msg(r) -> dict:
    text = (r["text"] or "").strip()
    return {
        "id": r["id"],
        "lead_id": r["lead_id"],
        "lead_name": (r["lead_name"] or "").strip() or "Без имени",
        "text": text or "—",
        "created_at": r["created_at"],
    }


def _agents_err_text(err: str | None) -> str | None:
    return {
        "bad_agent_id": "ID агента содержит пробелы или непечатаемые символы. "
                        "Скопируйте его из админки провайдера без правок.",
        "bad_backend": "Неизвестный бэкенд ИИ.",
        "bad_model": "ID модели содержит пробелы или непечатаемые символы. "
                     "Возьмите его из списка моделей вашего шлюза.",
        "bad_gateway_url": "Базовый URL шлюза должен начинаться с http:// или https:// и "
                           "не содержать пробелов (напр. https://api.timeweb.ai/v1).",
    }.get(err or "")


@app.get("/agents", response_class=HTMLResponse)
async def agents_page(
    request: Request,
    session: auth.Session = Depends(require_session),
    saved: int = 0,
    err: str | None = None,
    preset: str | None = None,
):
    _require_admin(session)  # глобальные настройки Школы (app_settings) — только платформе; клиент → /my-agent
    ai = await db.get_ai_settings()
    since = datetime.now(timezone.utc) - timedelta(days=config.AI_ACTIVITY_WINDOW_DAYS)
    act = await db.ai_activity_summary(since)
    recent = [_present_liya_msg(r) for r in await db.list_liya_messages(limit=20)]
    backends = [
        {"key": k, "label": config.AI_BACKENDS[k], "is_current": k == ai["backend"]}
        for k in config.AI_BACKEND_ORDER
    ]
    # «Должности» (пресеты): ?preset=<slug> предзаполняет промпт каркасом ДО сохранения
    # (PRG-чисто, без JS — клик по ссылке-шаблону перерисовывает форму). Невалидный slug
    # тихо игнорируем. Выбранная persona уезжает в форму и сохранится обычным POST.
    preset_key = preset if preset in config.PERSONA_PRESETS else None
    if preset_key:
        ai = {**ai,
              "system_prompt": config.PERSONA_PRESETS[preset_key]["prompt"],
              "persona": preset_key}
    personas = [
        {"key": k, "label": f'{config.PERSONA_PRESETS[k]["name"]} — {config.PERSONA_PRESETS[k]["role"]}',
         "is_current": k == ai["persona"]}
        for k in config.PERSONA_ORDER
    ]
    persona_label = next((p["label"] for p in personas if p["is_current"]), "")
    # Обзор ИИ-сотрудников по ролям: статус (агент создан? кастомизирован? есть знания?)
    # + нагрузка/конверсия (счётчик диалогов на сотрудника — для решений «кого развивать»).
    stats = await _persona_stats()
    role_cards = []
    for k in config.PERSONA_ORDER:
        r = await db.get_persona_role(k)
        p = config.PERSONA_PRESETS[k]
        s = stats.get(k, {"leads": 0, "converted": 0, "conv_pct": 0})
        role_cards.append({
            "slug": k, "name": p["name"], "role": p["role"],
            "agent_ready": bool(r["access_id"]),
            "has_knowledge": bool((r["knowledge"] or "").strip()),
            "customized": not r["is_default"],
            "leads": s["leads"], "converted": s["converted"], "conv_pct": s["conv_pct"],
        })
    return templates.TemplateResponse(
        request,
        "agents.html",
        {
            "ai": ai,
            "backends": backends,
            "default_fallback": config.AI_DEFAULT_FALLBACK,
            "default_model": config.AI_DEFAULT_MODEL,
            "default_gateway_url": config.AI_DEFAULT_GATEWAY_URL,
            "activity": {"total": act["total"], "recent": act["recent"],
                         "last_at": act["last_at"]},
            "window_days": config.AI_ACTIVITY_WINDOW_DAYS,
            "agent_id_max": config.AI_AGENT_ID_MAX,
            "fallback_max": config.AI_FALLBACK_MAX,
            "model_max": config.AI_MODEL_MAX,
            "gateway_url_max": config.AI_GATEWAY_URL_MAX,
            "system_prompt_max": config.AI_SYSTEM_PROMPT_MAX,
            "personas": personas,
            "persona_label": persona_label,
            "preset_applied": bool(preset_key),
            "role_cards": role_cards,
            "recent_messages": recent,
            "csrf_token": session.csrf_token,
            "session": session,
            "active": "agents",
            "saved": bool(saved),
            "err": _agents_err_text(err),
        },
    )


def _is_token_like(s: str) -> bool:
    """Идентификатор без пробелов и непечатаемого (agent_id / model). Точную валидность
    проверит провайдер при вызове (ошибка → фолбэк); тут режем явный мусор."""
    return s.isascii() and not any(c.isspace() for c in s)


@app.post("/agents")
async def agents_save(
    request: Request,
    session: auth.Session = Depends(require_session),
    enabled: str = Form(""),
    backend: str = Form(""),
    agent_id: str = Form(""),
    model: str = Form(""),
    gateway_base_url: str = Form(""),
    system_prompt: str = Form(""),
    fallback: str = Form(""),
    persona: str = Form(""),
    csrf_token: str = Form(""),
):
    _require_admin(session)  # запись глобальных app_settings — только платформе (анти-кросс-тенант)
    await _enforce_csrf(request, session, csrf_token)
    backend = backend.strip()
    if backend not in config.AI_BACKENDS:
        return RedirectResponse(url="/agents?err=bad_backend", status_code=303)
    agent_id = agent_id.strip()[: config.AI_AGENT_ID_MAX]
    model = model.strip()[: config.AI_MODEL_MAX]
    gateway_base_url = gateway_base_url.strip()[: config.AI_GATEWAY_URL_MAX]
    system_prompt = system_prompt.strip()[: config.AI_SYSTEM_PROMPT_MAX]
    fallback = fallback.strip()[: config.AI_FALLBACK_MAX]
    if agent_id and not _is_token_like(agent_id):
        return RedirectResponse(url="/agents?err=bad_agent_id", status_code=303)
    if model and not _is_token_like(model):
        return RedirectResponse(url="/agents?err=bad_model", status_code=303)
    # gateway URL (если задан) — http(s) без пробелов; пусто → бот возьмёт дефолт.
    if gateway_base_url and (
        not gateway_base_url.startswith(("http://", "https://"))
        or any(c.isspace() for c in gateway_base_url)
    ):
        return RedirectResponse(url="/agents?err=bad_gateway_url", status_code=303)
    # Persona — метка-«должность» из белого списка ("" = своя настройка); мусор молча в "".
    persona = persona.strip() if persona.strip() in config.PERSONA_PRESETS else ""
    await db.set_ai_settings(
        enabled=bool(enabled), backend=backend, agent_id=agent_id, model=model,
        gateway_base_url=gateway_base_url, system_prompt=system_prompt, fallback=fallback,
        actor=session.actor, ip=_ip(request), user_agent=_ua(request), persona=persona,
    )
    return RedirectResponse(url="/agents?saved=1", status_code=303)


# ---- /agents/role/{slug} — управление ИИ-сотрудником роли (промпт + знания + RAG) ---- #
@app.get("/agents/role/{slug}", response_class=HTMLResponse)
async def agent_role_page(
    request: Request,
    slug: str,
    session: auth.Session = Depends(require_session),
    saved: int = 0,
    warn: int = 0,
    err: str | None = None,
):
    _require_admin(session)  # «ИИ-сотрудники» Школы (глобальный реестр персон) — только платформе
    if slug not in config.PERSONA_PRESETS:
        raise StarletteHTTPException(status_code=404, detail="Роль не найдена")
    preset = config.PERSONA_PRESETS[slug]
    role = await db.get_persona_role(slug)
    s = (await _persona_stats()).get(slug, {"leads": 0, "converted": 0, "conv_pct": 0})
    # Статус векторных баз знаний агента (read-only): только если токен ИИ есть И агент создан.
    kb_count, tw_error = None, False
    if config.TIMEWEB_AI_ENABLED and role["nid"]:
        try:
            agent = await timeweb_ai.get_agent(int(role["nid"]))
            kb_count = len(agent.get("knowledge_bases_ids") or [])
        except (timeweb_ai.TimewebAIError, ValueError):
            tw_error = True
    return templates.TemplateResponse(
        request,
        "agent_role.html",
        {
            "slug": slug, "name": preset["name"], "role_title": preset["role"],
            "role": role["role"], "tasks": role["tasks"], "behavior": role["behavior"],
            "knowledge": role["knowledge"],
            "is_default": role["is_default"],
            "agent_ready": bool(role["access_id"]),
            "access_tail": role["access_id"][-6:] if role["access_id"] else "",
            "best_practices": config.PERSONA_BEST_PRACTICES.get(slug, []),
            "leads": s["leads"], "converted": s["converted"], "conv_pct": s["conv_pct"],
            "role_max": config.PERSONA_ROLE_MAX,
            "tasks_max": config.PERSONA_TASKS_MAX,
            "behavior_max": config.PERSONA_BEHAVIOR_MAX,
            "knowledge_max": config.PERSONA_KNOWLEDGE_MAX,
            "tw_enabled": config.TIMEWEB_AI_ENABLED,
            "kb_count": kb_count, "tw_error": tw_error,
            "saved": bool(saved), "push_warn": bool(warn),
            "err": _agent_role_err_text(err),
            "csrf_token": session.csrf_token, "session": session, "active": "agents",
        },
    )


@app.post("/agents/role/{slug}")
async def agent_role_save(
    request: Request,
    slug: str,
    session: auth.Session = Depends(require_session),
    role: str = Form(""),
    tasks: str = Form(""),
    behavior: str = Form(""),
    knowledge: str = Form(""),
    csrf_token: str = Form(""),
):
    """Сохранить роль + задачи + правила поведения + базу знаний роли. Эффективный промпт
    (склейка) пишется в реестр (его берёт gateway-бэкенд и создание агента) и, если агент роли
    уже создан, пушится на живого cloud-ai агента (PATCH). Сбой пуша не теряет сохранённое → warn."""
    _require_admin(session)  # запись глобального реестра персон Школы — только платформе
    await _enforce_csrf(request, session, csrf_token)
    if slug not in config.PERSONA_PRESETS:
        raise StarletteHTTPException(status_code=404, detail="Роль не найдена")
    role = role.strip()[: config.PERSONA_ROLE_MAX]
    tasks = tasks.strip()[: config.PERSONA_TASKS_MAX]
    behavior = behavior.strip()[: config.PERSONA_BEHAVIOR_MAX]
    knowledge = knowledge.strip()[: config.PERSONA_KNOWLEDGE_MAX]
    if not (role or behavior):
        return RedirectResponse(url=f"/agents/role/{slug}?err=empty", status_code=303)
    prompt = _persona_effective_prompt(role, tasks, behavior, knowledge)
    await db.set_persona_role(
        slug, role=role, tasks=tasks, behavior=behavior, knowledge=knowledge, prompt=prompt,
        actor=session.actor, ip=_ip(request), user_agent=_ua(request),
    )
    saved_role = await db.get_persona_role(slug)
    if config.TIMEWEB_AI_ENABLED and saved_role["nid"]:
        try:
            await timeweb_ai.set_system_prompt(int(saved_role["nid"]), prompt)
        except (timeweb_ai.TimewebAIError, ValueError):
            import logging
            logging.getLogger("admin-panel").exception("push persona-role prompt failed")
            return RedirectResponse(url=f"/agents/role/{slug}?warn=1", status_code=303)
    return RedirectResponse(url=f"/agents/role/{slug}?saved=1", status_code=303)


# =========================================================================== #
# Раздел «Базы знаний» (/knowledge) — ОБУЧЕНИЕ Лии: правка системного промпта ЖИВОГО
# cloud-ai агента (главный бесплатный рычаг) + статус баз знаний (RAG, платная вектор-БД).
# Панель ходит в Timeweb API под аккаунт-токеном (config.TIMEWEB_AI_TOKEN, env панели).
# Нет токена → раздел показывает подсказку. Базы знаний здесь НЕ провижионим (платно).
# =========================================================================== #
def _present_agent(a: dict) -> dict:
    settings = a.get("settings") or {}
    aid_full = a.get("access_id") or ""
    return {
        "id": a.get("id"),
        "name": (a.get("name") or "").strip() or f"Агент {a.get('id')}",
        # White-label: клиент видит брендовое имя движка, а не реальную модель «под капотом».
        "model": config.AI_BRAND_MODEL,
        "status": a.get("status"),
        "prompt": settings.get("system_prompt") or "",
        "access_tail": aid_full[-6:] if aid_full else "—",
        "kb_count": len(a.get("knowledge_bases_ids") or []),
        "web_search": bool(a.get("is_web_search_enabled")),
        "used_tokens": a.get("used_tokens"),
    }


def _knowledge_err_text(err: str | None) -> str | None:
    return {
        "kb_off": "Загрузка недоступна: не задан EMBEDDER_URL в окружении панели.",
        "kb_nofile": "Выберите файл для загрузки.",
        "kb_ext": "Поддерживаются только файлы txt, md, csv, pdf.",
        "kb_big": f"Файл слишком большой (лимит {config.MAX_KB_FILE_BYTES // 1024 // 1024} МБ).",
        "kb_empty": "В файле не нашлось текста для загрузки.",
        "kb_embed": "Не удалось обработать файл (эмбеддер недоступен или ошибка чтения). Попробуйте ещё раз.",
        "kb_nodoc": "Документ не найден.",
    }.get(err or "")


@app.get("/knowledge", response_class=HTMLResponse)
async def knowledge_page(
    request: Request,
    session: auth.Session = Depends(require_session),
    kb_saved: int = 0,
    err: str | None = None,
):
    _require_admin(session)  # база знаний Школы (глобальный pgvector + app_settings) — только платформе
    # Раздел теперь — только своя база знаний (загрузка файлов). Промпт/инструкции агента
    # живут в «ИИ-Агенты» → карточка роли (/agents/role/<slug>).
    kb_docs = await db.kb_list_documents()
    kb_enabled = await db.get_kb_enabled()
    return templates.TemplateResponse(
        request,
        "knowledge.html",
        {
            "csrf_token": session.csrf_token,
            "session": session,
            "active": "knowledge",
            "err": _knowledge_err_text(err),
            # RF-RAG — своя база знаний (загрузка файлов в pgvector)
            "kb_docs": kb_docs,
            "kb_enabled": kb_enabled,
            "embedder_enabled": config.EMBEDDER_ENABLED,
            "kb_roles": config.PERSONA_PRESETS,
            "kb_saved": kb_saved,
            "kb_max_mb": config.MAX_KB_FILE_BYTES // 1024 // 1024,
        },
    )


# ── RF-RAG: своя база знаний (загрузка файлов в pgvector) ── #
@app.post("/knowledge/toggle")
async def knowledge_toggle(
    request: Request,
    session: auth.Session = Depends(require_session),
    kb_enabled: str = Form(""),
    csrf_token: str = Form(""),
):
    """Тумблер поиска по базе знаний (app_settings['kb_enabled'], бот читает его при retrieval)."""
    _require_admin(session)  # глобальный app_settings Школы — только платформе
    await _enforce_csrf(request, session, csrf_token)
    await db.set_kb_enabled(
        kb_enabled.strip() == "1",
        actor=session.actor, ip=_ip(request), user_agent=_ua(request),
    )
    return RedirectResponse(url="/knowledge", status_code=303)


@app.post("/knowledge/upload")
async def knowledge_upload(
    request: Request,
    session: auth.Session = Depends(require_session),
    title: str = Form(""),
    role: str = Form(""),
    csrf_token: str = Form(""),
    file: UploadFile | None = File(None),
):
    """Файл (txt/md/csv/pdf) → текст → чанки → эмбеддинг (TEI) → pgvector. role '' = общая
    справка (все роли). Эмбеддер должен быть задан в env панели (EMBEDDER_URL)."""
    _require_admin(session)  # загрузка в базу знаний Школы — только платформе
    await _enforce_csrf(request, session, csrf_token)
    if not config.EMBEDDER_ENABLED:
        return RedirectResponse(url="/knowledge?err=kb_off", status_code=303)
    if file is None or not (file.filename or ""):
        return RedirectResponse(url="/knowledge?err=kb_nofile", status_code=303)
    fname = file.filename
    ext = ("." + fname.rsplit(".", 1)[-1].lower()) if "." in fname else ""
    if ext not in config.KB_ALLOWED_EXT:
        return RedirectResponse(url="/knowledge?err=kb_ext", status_code=303)
    data = await security.read_upload_capped(file, max_bytes=config.MAX_KB_FILE_BYTES)
    if data is None:
        return RedirectResponse(url="/knowledge?err=kb_big", status_code=303)
    if not data:
        return RedirectResponse(url="/knowledge?err=kb_nofile", status_code=303)
    try:
        text = kb.extract_text(fname, data)
        chunks = kb.chunk_text(text)
        if not chunks:
            return RedirectResponse(url="/knowledge?err=kb_empty", status_code=303)
        embeddings = await kb.embed_passages(chunks)
    except kb.KBError:
        import logging
        logging.getLogger("admin-panel").exception("kb upload failed")
        return RedirectResponse(url="/knowledge?err=kb_embed", status_code=303)
    role = role.strip()
    if role and role not in config.PERSONA_PRESETS:
        role = ""
    doc_title = (title.strip() or fname)[: config.KB_TITLE_MAX]
    n = await db.kb_insert_document(
        title=doc_title, source=fname[:200], role_tag=role, content=text,
        chunks=chunks, embeddings=embeddings,
        tenant_id=session.active_tenant_id,
        actor=session.actor, ip=_ip(request), user_agent=_ua(request),
    )
    return RedirectResponse(url=f"/knowledge?kb_saved={n}", status_code=303)


@app.post("/knowledge/delete")
async def knowledge_delete(
    request: Request,
    session: auth.Session = Depends(require_session),
    doc_id: str = Form(""),
    csrf_token: str = Form(""),
):
    """Удалить документ базы знаний (каскад чистит чанки)."""
    _require_admin(session)  # удаление из базы знаний Школы — только платформе
    await _enforce_csrf(request, session, csrf_token)
    doc_id = doc_id.strip()
    if not doc_id:
        return RedirectResponse(url="/knowledge?err=kb_nodoc", status_code=303)
    try:
        await db.kb_delete_document(
            doc_id, actor=session.actor, ip=_ip(request), user_agent=_ua(request)
        )
    except Exception:
        import logging
        logging.getLogger("admin-panel").exception("kb delete failed")
        return RedirectResponse(url="/knowledge?err=kb_nodoc", status_code=303)
    return RedirectResponse(url="/knowledge", status_code=303)


# =========================================================================== #
# Раздел «Интеграции» (/integrations) — статус-борд интеграций (read-only) +
# редактируемая ссылка-гайд (ЗАКРЫТИЕ GUIDE_URL-заглушки через app_settings). Статус =
# get_ai_settings (что знает панель) + get_runtime_status (НЕ-секретный снимок бота).
# Ссылку-гайд бот читает ПОВЕРХ env без редеплоя (get_effective_guide_url).
# =========================================================================== #
def _integrations_err_text(err: str | None) -> str | None:
    return {
        "bad_url": "Ссылка должна начинаться с http:// или https:// и быть без пробелов "
                   f"(до {config.GUIDE_URL_MAX} символов).",
    }.get(err or "")


@app.get("/integrations", response_class=HTMLResponse)
async def integrations_page(
    request: Request,
    session: auth.Session = Depends(require_session),
    saved: int = 0,
    err: str | None = None,
):
    _require_admin(session)  # интеграции Школы (глобальный app_settings: гайд/оплата/токены) — только платформе
    ai = await db.get_ai_settings()
    runtime = await db.get_runtime_status()
    guide_override = await db.get_guide_url_setting()
    # Эффективная ссылка-гайд = override из панели ИЛИ env-фолбэк бота (что бот и выдаст).
    guide_effective = guide_override or runtime.get("guide_url_env") or ""
    # Какой токен нужен ТЕКУЩЕМУ бэкенду ИИ — для честного статуса «ключ ИИ задан?».
    ai_token_ok = (runtime["gateway_token_set"] if ai["backend"] == "gateway"
                   else runtime["agent_token_set"])
    return templates.TemplateResponse(
        request,
        "integrations.html",
        {
            "ai": ai,
            "ai_backend_label": config.AI_BACKENDS.get(ai["backend"], ai["backend"]),
            "ai_token_ok": ai_token_ok,
            "runtime": runtime,
            "guide_override": guide_override,
            "guide_effective": guide_effective,
            "guide_url_max": config.GUIDE_URL_MAX,
            # Онлайн-оплата продаж (1B): ключи магазина школы нужны ОБОИМ концам.
            "pay_enabled": await db.get_online_payments_enabled(),
            "pay_panel_keys": config.SHOP_PAYMENTS_CONFIGURED,
            "pay_bot_keys": runtime.get("shop_yookassa_set", False),
            "csrf_token": session.csrf_token,
            "session": session,
            "active": "integrations",
            "saved": bool(saved),
            "err": _integrations_err_text(err),
        },
    )


@app.post("/integrations/guide-url")
async def integrations_set_guide_url(
    request: Request,
    session: auth.Session = Depends(require_session),
    guide_url: str = Form(""),
    csrf_token: str = Form(""),
):
    _require_admin(session)  # глобальный app_settings['guide_url'] Школы — только платформе
    await _enforce_csrf(request, session, csrf_token)
    # Пусто → снять переопределение (бот фолбэчит на env GUIDE_URL).
    result = await db.set_guide_url_with_audit(
        guide_url.strip() or None,
        actor=session.actor, ip=_ip(request), user_agent=_ua(request),
    )
    if result == "bad_url":
        return RedirectResponse(url="/integrations?err=bad_url", status_code=303)
    return RedirectResponse(url="/integrations?saved=1", status_code=303)


@app.post("/integrations/payments")
async def integrations_set_payments(
    request: Request,
    session: auth.Session = Depends(require_session),
    enabled: str = Form(""),
    csrf_token: str = Form(""),
):
    """Тумблер онлайн-оплаты (1B): app_settings['online_payments_enabled'] — бот гейтит
    кнопку «Купить», панель — «счёт из диалога». Дефолт ВЫКЛ (включается явно)."""
    _require_admin(session)  # глобальный app_settings['online_payments_enabled'] Школы — только платформе
    await _enforce_csrf(request, session, csrf_token)
    await db.set_online_payments_with_audit(
        bool(enabled), actor=session.actor, ip=_ip(request), user_agent=_ua(request),
    )
    return RedirectResponse(url="/integrations?saved=1", status_code=303)


# =========================================================================== #
# Раздел «Каналы» (/channels) — read-only атрибуция по площадке (source): лиды,
# конверсия, conv% + генератор deep-link'ов воронки (t.me/<bot_username>?start=<source>)
# + статус гейт-канала. Имя бота — из runtime-снимка (бот публикует на старте). Нет POST.
# =========================================================================== #
# Площадки для deep-link'ов: VALID_SOURCES бота, КРОМЕ 'other' (дефолтный «прочий» бакет —
# отдельная ссылка избыточна; неизвестный ?start= в боте всё равно падает в 'other').
_DEEPLINK_SOURCES = tuple(s for s in config.SOURCES if s != "other")


def _present_attribution(rows) -> dict:
    """Презентер атрибуции: строки по источникам (+ conv%) и итоги. leads=0 → conv%=0."""
    items, total_leads, total_conv = [], 0, 0
    for r in rows:
        leads, conv = r["leads"], r["converted"]
        total_leads += leads
        total_conv += conv
        items.append({
            "source": r["source"],
            "label": config.SOURCE_LABELS.get(r["source"], r["source"]),
            "leads": leads,
            "converted": conv,
            "conv_pct": round(conv / leads * 100, 1) if leads else 0.0,
        })
    overall_pct = round(total_conv / total_leads * 100, 1) if total_leads else 0.0
    # ключ rows, НЕ items: в Jinja `attribution.items` разрешается в метод dict.items(), не в ключ.
    return {"rows": items, "total_leads": total_leads,
            "total_converted": total_conv, "overall_pct": overall_pct}


def _channels_err_text(err: str | None) -> str | None:
    return {
        # платформенный вид (персоны по каналам)
        "bad_source": "Неизвестный канал.",
        "bad_persona": "Неизвестный ИИ-сотрудник.",
        "tw": "Не удалось создать агента персоны у ИИ-сервиса. Проверьте токен ИИ в окружении "
              "панели и повторите — назначение не сохранено.",
        # client-вид (self-serve подключение каналов)
        "no_tenant": "Кабинет ещё не привязан к клиенту. Обратитесь в поддержку.",
        "no_vault": "Хранилище ключей не настроено. Обратитесь в поддержку.",
        "bad_key": "Неизвестное поле канала.",
        "bad_gid": "ID сообщества ВК — только цифры, без минуса, букв и ссылок.",
        "empty": "Значение пустое — не сохранено.",
        "too_long": f"Значение длиннее {config.TENANT_SECRET_VALUE_MAX} символов.",
        "not_found": "Нечего отключать — поле не задано.",
    }.get(err or "")


def _channel_cards_view(secrets_meta: list) -> list[dict]:
    """Презентер карточек подключения каналов (client-вид «Каналы»). Канал «подключён», когда
    заданы ВСЕ его секреты в vault. Чистая функция (тестируема смоуком без БД): на вход —
    метаданные секретов тенанта (db.list_tenant_secrets), на выход — данные для шаблона."""
    known = {r["key_name"] for r in secrets_meta}
    label = dict(config.TENANT_SECRET_KEYS)
    cards = []
    for c in config.CHANNEL_CONNECT_CARDS:
        fields = [{"key": k, "label": label.get(k, k), "is_set": k in known} for k in c["secret_keys"]]
        cards.append({
            "key": c["key"],
            "title": c["title"],
            "guide": c["guide"],
            "fields": fields,
            "connected": all(f["is_set"] for f in fields),
        })
    return cards


@app.get("/channels", response_class=HTMLResponse)
async def channels_page(
    request: Request,
    session: auth.Session = Depends(require_session),
    saved_persona: int = 0,
    saved: int = 0,
    err: str | None = None,
):
    # Ролевой раздел (Слой C, решение владельца): КЛИЕНТ (operator) подключает свои каналы
    # (self-serve, tenant-scoped); ПЛАТФОРМА видит атрибуцию по площадкам + персоны на канал
    # (глобальный app_settings Школы). Глобально-пишущий POST /channels/persona — под is_platform.
    if not session.is_platform:
        secrets_meta: list = []
        if session.active_tenant_id and vault.enabled():
            secrets_meta = await db.list_tenant_secrets(session.active_tenant_id)
        return templates.TemplateResponse(
            request,
            "channels.html",
            {
                "active": "channels",
                "session": session,
                "csrf_token": session.csrf_token,
                "is_platform": False,
                "has_tenant": bool(session.active_tenant_id),
                "vault_enabled": vault.enabled(),
                "channel_cards": _channel_cards_view(secrets_meta),
                "value_max": config.TENANT_SECRET_VALUE_MAX,
                "support_url": _safe_support_url(config.SUPPORT_URL),
                "notifier_username": config.NOTIFIER_BOT_USERNAME,
                "saved_flash": bool(saved),
                "err": _channels_err_text(err),
            },
        )
    # ── Платформенный вид (без изменений) ──
    attribution = _present_attribution(await db.attribution_by_source())
    clicks = await db.total_link_clicks()
    runtime = await db.get_runtime_status()
    username = runtime["bot_username"]
    deep_links = [
        {"source": s, "label": config.SOURCE_LABELS.get(s, s),
         "url": f"https://t.me/{username}?start={s}"}
        for s in _DEEPLINK_SOURCES
    ] if username else []
    # «ИИ-сотрудник на канал»: назначения по ВСЕМ источникам (вкл. 'other' — лиды без метки).
    cp = await db.get_channel_personas(tuple(config.SOURCES))
    persona_options = [
        {"key": k, "label": f'{config.PERSONA_PRESETS[k]["name"]} — {config.PERSONA_PRESETS[k]["role"]}'}
        for k in config.PERSONA_ORDER
    ]
    channel_staff = [
        {"source": s, "label": config.SOURCE_LABELS.get(s, s),
         "persona": cp["personas"].get(s, "")}
        for s in config.SOURCES
    ]
    # Слой C: платформа тоже подключает каналы — ЗА активного клиента (решение владельца). Карточки
    # скоупятся по session.active_tenant_id (тот же self-serve путь, что у клиента); connect/disconnect
    # пишут в vault активного тенанта. Для дефолт-тенанта (Школа) VK/MAX через мультиплекс не поднимается.
    plat_cards: list = []
    if session.active_tenant_id and vault.enabled():
        plat_cards = _channel_cards_view(await db.list_tenant_secrets(session.active_tenant_id))
    return templates.TemplateResponse(
        request,
        "channels.html",
        {
            "is_platform": True,
            "vault_enabled": vault.enabled(),
            "channel_cards": plat_cards,
            "value_max": config.TENANT_SECRET_VALUE_MAX,
            "notifier_username": config.NOTIFIER_BOT_USERNAME,
            "saved_flash": bool(saved),
            "attribution": attribution,
            "clicks": clicks,
            "runtime": runtime,
            "bot_username": username,
            "deep_links": deep_links,
            "channel_staff": channel_staff,
            "persona_options": persona_options,
            "saved_persona": bool(saved_persona),
            "err": _channels_err_text(err),
            "csrf_token": session.csrf_token,
            "session": session,
            "active": "channels",
        },
    )


@app.post("/channels/persona")
async def channels_set_persona(
    request: Request,
    session: auth.Session = Depends(require_session),
    source: str = Form(""),
    persona: str = Form(""),
    csrf_token: str = Form(""),
):
    """Назначить «ИИ-сотрудника» каналу. Персона выбрана впервые → панель создаёт под неё
    СВОЕГО cloud-ai агента через API (один на персону, реестр ai_persona_agent__<slug>) и
    каналу прописывается его access_id + промпт-каркас. Пустая персона — сброс («как у всех»).
    Бот подхватывает per-канальные ключи со следующего сообщения лида, без редеплоя."""
    _require_admin(session)  # запись глобальных канальных ключей app_settings Школы — только платформе
    await _enforce_csrf(request, session, csrf_token)
    source = source.strip()
    if source not in config.SOURCES:
        return RedirectResponse(url="/channels?err=bad_source", status_code=303)
    persona = persona.strip()
    if persona and persona not in config.PERSONA_PRESETS:
        return RedirectResponse(url="/channels?err=bad_persona", status_code=303)

    agent_access_id = ""
    prompt = ""
    if persona:
        prompt = config.PERSONA_PRESETS[persona]["prompt"]
        try:
            agent_access_id = await _ensure_persona_agent(persona)
        except timeweb_ai.TimewebAIError:
            return RedirectResponse(url="/channels?err=tw", status_code=303)

    await db.set_channel_persona(
        source=source, persona=persona, agent_access_id=agent_access_id, prompt=prompt,
        actor=session.actor, ip=_ip(request), user_agent=_ua(request),
    )
    return RedirectResponse(url="/channels?saved_persona=1", status_code=303)


# ── Слой C: self-serve подключение каналов (client-вид «Каналы») ──────────────
# Tenant-scoped, БЕЗ _require_admin: клиент сам подключает свои каналы. Пишем ТОЛЬКО в его
# tenant-vault (upsert_tenant_secret) — никаких глобальных app_settings (анти-кросс-тенант).
# Принимаем лишь канальные ключи (CHANNEL_SECRET_KEY_SET). AAD шифрования — {tenant_id}:{key_name},
# как в /keys: бот расшифровывает с тем же AAD (db.get_tenant_secret).
@app.post("/channels/connect")
async def channels_connect(
    request: Request,
    session: auth.Session = Depends(require_session),
    key_name: str = Form(""),
    value: str = Form(""),
    csrf_token: str = Form(""),
):
    await _enforce_csrf(request, session, csrf_token)
    if not session.active_tenant_id:
        return RedirectResponse(url="/channels?err=no_tenant", status_code=303)
    if not vault.enabled():
        return RedirectResponse(url="/channels?err=no_vault", status_code=303)
    key_name = key_name.strip()
    if key_name not in config.CHANNEL_SECRET_KEY_SET:
        return RedirectResponse(url="/channels?err=bad_key", status_code=303)
    value = value.strip()
    if not value:
        return RedirectResponse(url="/channels?err=empty", status_code=303)
    if len(value) > config.TENANT_SECRET_VALUE_MAX:
        return RedirectResponse(url="/channels?err=too_long", status_code=303)
    # Зеркалим жёсткий гейт бота (_reconcile_vk: gid.isdigit()): иначе нечисловой ID («club123»,
    # «-123», ссылка) сохранится, карточка покажет «настроен», но VK-поллер молча не стартует.
    if key_name == "vk_group_id" and not value.isdigit():
        return RedirectResponse(url="/channels?err=bad_gid", status_code=303)
    ct, nonce, ver = vault.encrypt(value, aad=f"{session.active_tenant_id}:{key_name}")
    del value  # plaintext дальше не живёт (ни в БД, ни в аудите, ни в логах)
    await db.upsert_tenant_secret(
        session.active_tenant_id, key_name, ct, nonce, ver,
        actor=session.actor, ip=_ip(request), user_agent=_ua(request),
    )
    return RedirectResponse(url="/channels?saved=1", status_code=303)


@app.post("/channels/disconnect")
async def channels_disconnect(
    request: Request,
    session: auth.Session = Depends(require_session),
    key_name: str = Form(""),
    csrf_token: str = Form(""),
):
    await _enforce_csrf(request, session, csrf_token)
    if not session.active_tenant_id:
        return RedirectResponse(url="/channels?err=no_tenant", status_code=303)
    key_name = key_name.strip()
    if key_name not in config.CHANNEL_SECRET_KEY_SET:
        return RedirectResponse(url="/channels?err=bad_key", status_code=303)
    ok = await db.delete_tenant_secret(
        session.active_tenant_id, key_name,
        actor=session.actor, ip=_ip(request), user_agent=_ua(request),
    )
    return RedirectResponse(url="/channels?saved=1" if ok else "/channels?err=not_found", status_code=303)


def _persona_effective_prompt(role: str, tasks: str, behavior: str, knowledge: str) -> str:
    """Системный промпт агента роли = роль + задачи + правила поведения + (опц.) база знаний
    промптом. Пустые секции опускаются. Знания подаются как факты, по которым агент обязан
    отвечать (РФ-комплаентный «RAG промптом»)."""
    role = (role or "").strip()
    tasks = (tasks or "").strip()
    behavior = (behavior or "").strip()
    knowledge = (knowledge or "").strip()
    parts: list[str] = []
    if role:
        parts.append(role)
    if tasks:
        parts.append(f"## Твои задачи:\n{tasks}")
    if behavior:
        parts.append(f"## Правила поведения (что можно и нельзя):\n{behavior}")
    if knowledge:
        parts.append(f"## База знаний — отвечай СТРОГО по этим фактам, не выдумывай:\n{knowledge}")
    return "\n\n".join(parts)


async def _ensure_persona_agent(slug: str) -> str:
    """access_id cloud-ai агента персоны: из реестра (один агент на персону) или создаём
    через API + сохраняем оба id (access для вызова, числовой для PATCH) и эффективный промпт.
    Берём АКТУАЛЬНЫЙ промпт роли (инструкция+знания владельца), иначе каркас пресета.
    Нет токена ИИ → "" (канал/per-lead тогда полагается на промпт для gateway-бэкенда).
    TimewebAIError (сбой создания) пробрасывается вызывающему — он решает, что показать."""
    existing = await db.get_persona_agent(slug)
    if existing:
        return existing
    if not config.TIMEWEB_AI_ENABLED:
        return ""
    role = await db.get_persona_role(slug)
    prompt = _persona_effective_prompt(role["role"], role["tasks"], role["behavior"], role["knowledge"])
    preset = config.PERSONA_PRESETS[slug]
    created = await timeweb_ai.create_agent(
        f'{preset["name"]} — {preset["role"]}', prompt, model_id=config.PERSONA_AGENT_MODEL_ID,
    )
    await db.save_persona_agent(slug, created["access_id"], created.get("id"), prompt)
    return created["access_id"]


def _agent_role_err_text(err: str | None) -> str | None:
    return {
        "empty": "Инструкция роли не может быть пустой — опишите, кто этот сотрудник и как отвечает.",
        "tw": "Не удалось обратиться к ИИ-сервису. Проверьте токен ИИ в окружении панели.",
    }.get(err or "")


# =========================================================================== #
# Раздел «Команда» (/team) — управление УЧЁТКАМИ ВСЕЙ ПЛАТФОРМЫ (admin_users без скоупа
# по тенанту: листинг/создание/смена роли/сброс пароля любого юзера). Поэтому ПЛАТФОРМЕННЫЙ-
# ONLY: гейт по личности env-админа (is_platform), НЕ по role. Self-serve клиент (operator
# своего кабинета) сюда не должен попадать — иначе сброс пароля владельца другого клиента
# (ревью, critical). env-админ — bootstrap-суперюзер вне admin_users.
# =========================================================================== #
def _require_admin(session: auth.Session) -> None:
    """Гейт платформенных разделов: ТОЛЬКО env-админ (is_platform). Любой БД-юзер → 403
    (пункта меню он и так не видит). ⚠️ По личности, НЕ по role='admin' (ревью, critical)."""
    if not session.is_platform:
        raise StarletteHTTPException(status_code=403, detail="Только для администратора платформы")


def _valid_team_username(u: str) -> bool:
    return (config.TEAM_USERNAME_MIN <= len(u) <= config.TEAM_USERNAME_MAX
            and re.match(config.TEAM_USERNAME_RE, u) is not None)


def _present_admin_user(u) -> dict:
    return {
        "username": u["username"], "role": u["role"], "active": u["active"],
        "created_at": u["created_at"], "created_by": u["created_by"],
    }


def _team_saved_text(saved: str | None) -> str | None:
    return {
        "created": "Оператор создан. Передайте ему логин и пароль.",
        "role": "Роль обновлена.",
        "active": "Статус обновлён.",
        "password": "Пароль сброшен. Передайте оператору новый пароль.",
    }.get(saved or "")


def _team_err_text(err: str | None) -> str | None:
    return {
        "bad_username": f"Логин: {config.TEAM_USERNAME_MIN}–{config.TEAM_USERNAME_MAX} символов, "
                        "строчные латинские буквы/цифры/дефис/подчёркивание.",
        "reserved": "Этот логин зарезервирован системой. Выберите другой.",
        "bad_password": f"Пароль: минимум {config.TEAM_PASSWORD_MIN} символов.",
        "bad_role": "Недопустимая роль.",
        "exists": "Оператор с таким логином уже есть.",
        "not_found": "Оператор не найден.",
        "self_deactivate": "Нельзя деактивировать собственную учётную запись.",
    }.get(err or "")


@app.get("/team", response_class=HTMLResponse)
async def team_page(
    request: Request,
    session: auth.Session = Depends(require_session),
    saved: str | None = None,
    err: str | None = None,
):
    _require_admin(session)
    users = [_present_admin_user(u) for u in await db.list_admin_users()]
    return templates.TemplateResponse(
        request,
        "team.html",
        {
            "users": users,
            "roles": [{"key": r, "label": config.TEAM_ROLE_LABELS[r]} for r in config.TEAM_ROLES],
            "role_labels": config.TEAM_ROLE_LABELS,
            "default_role": config.TEAM_DEFAULT_ROLE,
            "env_admin": config.ADMIN_USERNAME,
            "username_min": config.TEAM_USERNAME_MIN,
            "username_max": config.TEAM_USERNAME_MAX,
            "password_min": config.TEAM_PASSWORD_MIN,
            "me": session.actor,
            "csrf_token": session.csrf_token,
            "session": session,
            "active": "team",
            "saved": _team_saved_text(saved),
            "err": _team_err_text(err),
        },
    )


@app.post("/team")
async def team_create(
    request: Request,
    session: auth.Session = Depends(require_session),
    username: str = Form(""),
    password: str = Form(""),
    role: str = Form(""),
    csrf_token: str = Form(""),
):
    _require_admin(session)
    await _enforce_csrf(request, session, csrf_token)
    uname = (username or "").strip().lower()
    role = (role or "").strip()
    if not _valid_team_username(uname):
        return RedirectResponse(url="/team?err=bad_username", status_code=303)
    if uname == (config.ADMIN_USERNAME or "").lower():   # совпал с env-админом → зарезервировано
        return RedirectResponse(url="/team?err=reserved", status_code=303)
    if role not in config.TEAM_ROLES:
        return RedirectResponse(url="/team?err=bad_role", status_code=303)
    if not (config.TEAM_PASSWORD_MIN <= len(password) <= config.TEAM_PASSWORD_MAX):
        return RedirectResponse(url="/team?err=bad_password", status_code=303)
    result = await db.create_admin_user_with_audit(
        uname, auth.hash_password(password), role,
        actor=session.actor, ip=_ip(request), user_agent=_ua(request),
    )
    if result == "exists":
        return RedirectResponse(url="/team?err=exists", status_code=303)
    return RedirectResponse(url="/team?saved=created", status_code=303)


@app.post("/team/{username}/role")
async def team_set_role(
    request: Request,
    username: str,
    session: auth.Session = Depends(require_session),
    role: str = Form(""),
    csrf_token: str = Form(""),
):
    _require_admin(session)
    await _enforce_csrf(request, session, csrf_token)
    role = (role or "").strip()
    if role not in config.TEAM_ROLES:
        return RedirectResponse(url="/team?err=bad_role", status_code=303)
    ok = await db.set_admin_user_role_with_audit(
        username.lower(), role, actor=session.actor, ip=_ip(request), user_agent=_ua(request),
    )
    return RedirectResponse(url="/team?saved=role" if ok else "/team?err=not_found", status_code=303)


@app.post("/team/{username}/active")
async def team_set_active(
    request: Request,
    username: str,
    session: auth.Session = Depends(require_session),
    active: str = Form(""),
    csrf_token: str = Form(""),
):
    _require_admin(session)
    await _enforce_csrf(request, session, csrf_token)
    uname = username.lower()
    want_active = active == "1"
    # Запрет самодеактивации: env-админ — сеть безопасности, но db-админ мог бы потерять вход.
    if not want_active and uname == (session.actor or "").lower():
        return RedirectResponse(url="/team?err=self_deactivate", status_code=303)
    ok = await db.set_admin_user_active_with_audit(
        uname, want_active, actor=session.actor, ip=_ip(request), user_agent=_ua(request),
    )
    return RedirectResponse(url="/team?saved=active" if ok else "/team?err=not_found", status_code=303)


@app.post("/team/{username}/password")
async def team_reset_password(
    request: Request,
    username: str,
    session: auth.Session = Depends(require_session),
    password: str = Form(""),
    csrf_token: str = Form(""),
):
    _require_admin(session)
    await _enforce_csrf(request, session, csrf_token)
    if not (config.TEAM_PASSWORD_MIN <= len(password) <= config.TEAM_PASSWORD_MAX):
        return RedirectResponse(url="/team?err=bad_password", status_code=303)
    ok = await db.set_admin_user_password_with_audit(
        username.lower(), auth.hash_password(password),
        actor=session.actor, ip=_ip(request), user_agent=_ua(request),
    )
    return RedirectResponse(url="/team?saved=password" if ok else "/team?err=not_found", status_code=303)


# =========================================================================== #
# Раздел «Профиль» — личный кабинет пользователя (профиль, безопасность, способы
# входа, кабинет, поддержка). Все операции — над СВОЕЙ учёткой (actor==username),
# поэтому БЕЗ is_platform-гейта (это собственный кабинет). env-админ (платформенный
# супер вне admin_users) видит профиль read-only: правка имени/пароля недоступна.
# =========================================================================== #
def _safe_support_url(url: str) -> str:
    """Ссылка «Поддержка» — только из allow-list схем (https/tg/mailto), иначе пусто.
    Защита от javascript:/data: в href. Значение приходит из env владельца (SUPPORT_URL)."""
    u = (url or "").strip()
    return u if u.startswith(config.SUPPORT_URL_SCHEMES) else ""


def _account_saved_text(code: str | None) -> str | None:
    return {
        "profile": "Профиль обновлён.",
        "password": "Пароль изменён.",
        "sessions": "Все другие сеансы завершены — на остальных устройствах нужно войти заново.",
    }.get(code or "")


def _account_err_text(code: str | None) -> str | None:
    return {
        "bad_name": f"Имя — до {config.ACCOUNT_DISPLAY_NAME_MAX} символов.",
        "no_identity": "Для этой учётной записи правка профиля недоступна.",
        "platform_pwd": "Пароль администратора платформы задаётся через переменные окружения, не здесь.",
        "bad_current": "Текущий пароль неверный.",
        "bad_password": f"Новый пароль — от {config.ACCOUNT_PASSWORD_MIN} до {config.ACCOUNT_PASSWORD_MAX} символов.",
        "mismatch": "Новый пароль и повтор не совпадают.",
        "not_found": "Учётная запись не найдена.",
    }.get(code or "")


@app.get("/account", response_class=HTMLResponse)
async def account_page(
    request: Request,
    session: auth.Session = Depends(require_session),
    saved: str | None = None,
    err: str | None = None,
):
    is_platform = session.is_platform
    acct = await db.get_account(session.actor)          # None для env-админа (вне admin_users)
    idents = await db.list_account_identities(session.actor)
    by_provider = {r["provider"]: r for r in idents}
    email_ident = by_provider.get("email")
    # Имя в шапке: первое непустое display_name среди личностей.
    display_name = next((r["display_name"] for r in idents if r["display_name"]), None)
    # Имя живёт в account_identities → правка возможна ТОЛЬКО при наличии личности
    # (team-оператор из /team имеет строку admin_users БЕЗ личности — имени негде храниться).
    can_edit_name = (not is_platform) and bool(idents)
    # Пароль живёт в admin_users → доступен любому БД-юзеру (вкл. team-оператора), кроме env-админа.
    can_edit_password = (acct is not None) and (not is_platform)

    # Способы входа: все 3 канала со статусом. Реальная привязка ВК/ТГ к существующему
    # аккаунту появится с включением OAuth (handoff A) — сейчас карта read-only (флаги OFF).
    tg_on = config.PUBLIC_SIGNUP_ENABLED and config.OAUTH_TELEGRAM_ENABLED
    vk_on = config.PUBLIC_SIGNUP_ENABLED and oauth_vk.enabled()
    login_methods = [
        {"provider": "email", "label": config.ACCOUNT_PROVIDER_LABELS["email"],
         "connected": email_ident is not None,
         "value": email_ident["external_id"] if email_ident else None,
         "verified": bool(email_ident["verified"]) if email_ident else False,
         "available": True},
        {"provider": "telegram", "label": config.ACCOUNT_PROVIDER_LABELS["telegram"],
         "connected": "telegram" in by_provider,
         "value": by_provider["telegram"]["display_name"] if "telegram" in by_provider else None,
         "verified": "telegram" in by_provider,
         "available": tg_on},
        {"provider": "vk", "label": config.ACCOUNT_PROVIDER_LABELS["vk"],
         "connected": "vk" in by_provider,
         "value": by_provider["vk"]["display_name"] if "vk" in by_provider else None,
         "verified": "vk" in by_provider,
         "available": vk_on},
    ]

    return templates.TemplateResponse(
        request,
        "account.html",
        {
            "active": "account",
            "session": session,
            "csrf_token": session.csrf_token,
            "is_platform": is_platform,
            "can_edit_name": can_edit_name,
            "can_edit_password": can_edit_password,
            "login": session.actor,
            "role_label": ("Администратор платформы" if is_platform else "Владелец кабинета"),
            "display_name": display_name or "",
            "email": email_ident["external_id"] if email_ident else None,
            "email_verified": bool(email_ident["verified"]) if email_ident else False,
            "created_at": acct["created_at"] if acct else None,
            "login_methods": login_methods,
            "linking_hint": not (tg_on and vk_on),
            "tenant_name": session.active_tenant_name,
            "tenant_status": session.active_tenant_status,
            # «Кабинет»/кошелёк показываем ТОЛЬКО клиенту (его собственный тенант). У env-админа
            # active_tenant — произвольный чужой клиент → на ЛИЧНОМ /account его не выводим
            # (он управляет клиентами в /tenants /subscription). Тариф из глобального service_invoices
            # (без tenant_id/RLS) на /account НЕ показываем — иначе клиент B видел бы тариф клиента A.
            "wallet": (await _wallet_ctx(session)) if not is_platform else None,
            "support_url": _safe_support_url(config.SUPPORT_URL),
            "password_min": config.ACCOUNT_PASSWORD_MIN,
            "name_max": config.ACCOUNT_DISPLAY_NAME_MAX,
            "saved": _account_saved_text(saved),
            "err": _account_err_text(err),
        },
    )


@app.post("/account/profile")
async def account_update_profile(
    request: Request,
    session: auth.Session = Depends(require_session),
    display_name: str = Form(""),
    csrf_token: str = Form(""),
):
    await _enforce_csrf(request, session, csrf_token)
    if session.is_platform:                              # env-админ: личности нет, править нечего
        return RedirectResponse(url="/account?err=no_identity", status_code=303)
    name = (display_name or "").strip()
    if len(name) > config.ACCOUNT_DISPLAY_NAME_MAX:
        return RedirectResponse(url="/account?err=bad_name", status_code=303)
    ok = await db.set_account_display_name_with_audit(
        session.actor, name, ip=_ip(request), user_agent=_ua(request),
    )
    return RedirectResponse(url="/account?saved=profile" if ok else "/account?err=no_identity", status_code=303)


@app.post("/account/password")
async def account_change_password(
    request: Request,
    session: auth.Session = Depends(require_session),
    current_password: str = Form(""),
    new_password: str = Form(""),
    confirm_password: str = Form(""),
    csrf_token: str = Form(""),
):
    await _enforce_csrf(request, session, csrf_token)
    # env-админ: пароль платформенного супера живёт в переменных окружения, не в БД.
    if session.is_platform:
        return RedirectResponse(url="/account?err=platform_pwd", status_code=303)
    # Подтверждение личности: текущий пароль ДОЛЖЕН сойтись (constant-time внутри authenticate).
    verified = await auth.authenticate(session.actor, current_password)
    if verified is None or verified[0] != session.actor:
        await db.audit(actor=session.actor, action="account_password_fail",
                       ip=_ip(request), user_agent=_ua(request), detail={"reason": "bad_current"})
        return RedirectResponse(url="/account?err=bad_current", status_code=303)
    if not (config.ACCOUNT_PASSWORD_MIN <= len(new_password) <= config.ACCOUNT_PASSWORD_MAX):
        return RedirectResponse(url="/account?err=bad_password", status_code=303)
    if new_password != confirm_password:
        return RedirectResponse(url="/account?err=mismatch", status_code=303)
    ok = await db.change_own_password_with_audit(
        session.actor, auth.hash_password(new_password),
        ip=_ip(request), user_agent=_ua(request),
    )
    return RedirectResponse(url="/account?saved=password" if ok else "/account?err=not_found", status_code=303)


@app.post("/account/sessions/revoke-all")
async def account_revoke_sessions(
    request: Request,
    session: auth.Session = Depends(require_session),
    csrf_token: str = Form(""),
):
    await _enforce_csrf(request, session, csrf_token)
    # «Выйти на всех устройствах»: ревокаем ВСЕ сессии актора КРОМЕ текущей — пользователь
    # остаётся залогинен здесь, прочие устройства выкидываются. Только свои сессии (actor).
    await db.revoke_all_sessions_with_audit(
        session.actor, keep_sid=session.sid, ip=_ip(request), user_agent=_ua(request),
    )
    return RedirectResponse(url="/account?saved=sessions", status_code=303)


# =========================================================================== #
# Раздел КЛИЕНТА «Мой ИИ-сотрудник» (/my-agent) — per-tenant конфиг ИИ в tenant_settings.
# Клиент (operator своего тенанта) правит ТОЛЬКО инструкции ИИ-сотрудника + текст-фолбэк +
# тумблер; пишется в tenant_settings (RLS, скоуп активного тенанта сессии). Бот читает их
# в мультиплексе (get_tenant_ai_overrides) и шлёт как system-сообщение per-request. Инфра
# (движок/agent_id/модель) клиенту НЕ показываем (white-label) и НЕ трогаем — провижининг
# владельца. Глобальные настройки Школы живут отдельно в /agents (платформенный супер).
# =========================================================================== #
def _my_agent_saved_text(saved: str | None) -> str | None:
    return {
        "settings": "Настройки ИИ-сотрудника сохранены.",
        "escalation": "Настройки эскалации сохранены.",
    }.get(saved or "")


def _my_agent_err_text(err: str | None) -> str | None:
    return {
        "no_tenant": "Кабинет ещё не привязан к клиенту. Обратитесь в поддержку.",
        "bad_prompt": "Инструкции слишком длинные — сократите текст.",
        "bad_fallback": "Сообщение-заглушка слишком длинное.",
        "bad_chat": "ID Telegram-чата должен быть числом вида -1002576119452.",
        "bad_topic": "ID темы форума должен быть числом.",
        "esc_no_chat": "Чтобы включить эскалацию, укажите ID Telegram-чата менеджеров.",
    }.get(err or "")


@app.get("/my-agent", response_class=HTMLResponse)
async def my_agent_page(
    request: Request,
    session: auth.Session = Depends(require_session),
    saved: str | None = None,
    err: str | None = None,
):
    tid = session.active_tenant_id
    cfg = await db.get_tenant_ai_config(tid) if tid else {
        "enabled": True, "system_prompt": "", "fallback": "", "provisioned": False}
    esc = await db.get_tenant_escalation_config(tid) if tid else {
        "enabled": False, "chat_id": "", "topic_id": ""}
    return templates.TemplateResponse(
        request,
        "my_agent.html",
        {
            "active": "my_agent",
            "session": session,
            "csrf_token": session.csrf_token,
            "has_tenant": bool(tid),
            "tenant_name": session.active_tenant_name,
            "tenant_status": session.active_tenant_status,
            "enabled": cfg["enabled"],
            "system_prompt": cfg["system_prompt"],
            "fallback": cfg["fallback"],
            "provisioned": cfg["provisioned"],
            "default_fallback": config.AI_DEFAULT_FALLBACK,
            "prompt_max": config.TENANT_AI_PROMPT_MAX,
            "fallback_max": config.AI_FALLBACK_MAX,
            # Блок «Эскалация» (Слой A): клиент задаёт свой TG-чат менеджеров.
            "esc_enabled": esc["enabled"],
            "esc_chat_id": esc["chat_id"],
            "esc_topic_id": esc["topic_id"],
            "notifier_username": config.NOTIFIER_BOT_USERNAME,
            "support_url": _safe_support_url(config.SUPPORT_URL),
            "saved": _my_agent_saved_text(saved),
            "err": _my_agent_err_text(err),
        },
    )


@app.post("/my-agent")
async def my_agent_save(
    request: Request,
    session: auth.Session = Depends(require_session),
    enabled: str = Form(""),
    system_prompt: str = Form(""),
    fallback: str = Form(""),
    csrf_token: str = Form(""),
):
    await _enforce_csrf(request, session, csrf_token)
    tid = session.active_tenant_id
    if not tid:                                          # team-оператор без membership / легаси-сессия
        return RedirectResponse(url="/my-agent?err=no_tenant", status_code=303)
    # Длины режем до сохранения (как /agents): пользовательский ввод не должен распухать БД.
    system_prompt = system_prompt.strip()[: config.TENANT_AI_PROMPT_MAX]
    fallback = fallback.strip()[: config.AI_FALLBACK_MAX]
    await db.set_tenant_ai_config(
        tid, enabled=bool(enabled), system_prompt=system_prompt, fallback=fallback,
        actor=session.actor, ip=_ip(request), user_agent=_ua(request),
    )
    return RedirectResponse(url="/my-agent?saved=settings", status_code=303)


@app.post("/my-agent/escalation")
async def my_agent_escalation(
    request: Request,
    session: auth.Session = Depends(require_session),
    esc_enabled: str = Form(""),
    chat_id: str = Form(""),
    topic_id: str = Form(""),
    csrf_token: str = Form(""),
):
    """Адрес эскалации горячего лида (Слой A): TG-чат менеджеров клиента + опц. тема + тумблер →
    tenant_settings (бот читает get_tenant_escalation). chat_id/topic — числа; включение требует
    chat_id (иначе слать некуда)."""
    await _enforce_csrf(request, session, csrf_token)
    tid = session.active_tenant_id
    if not tid:
        return RedirectResponse(url="/my-agent?err=no_tenant", status_code=303)
    chat_id = chat_id.strip()
    topic_id = topic_id.strip()
    enabled = bool(esc_enabled)
    if chat_id and not re.match(config.ESCALATION_CHAT_ID_RE, chat_id):
        return RedirectResponse(url="/my-agent?err=bad_chat", status_code=303)
    if topic_id and not topic_id.isdigit():
        return RedirectResponse(url="/my-agent?err=bad_topic", status_code=303)
    if enabled and not chat_id:                          # включили без адреса — слать некуда
        return RedirectResponse(url="/my-agent?err=esc_no_chat", status_code=303)
    await db.set_tenant_escalation_config(
        tid, enabled=enabled, chat_id=chat_id, topic_id=topic_id,
        actor=session.actor, ip=_ip(request), user_agent=_ua(request),
    )
    return RedirectResponse(url="/my-agent?saved=escalation", status_code=303)


# =========================================================================== #
# Раздел КЛИЕНТА «Триггеры» (/triggers) — движок триггеров (Слой B). Клиент создаёт триггеры
# (стоп-слова/намерение/кол-во сообщений/документы) → действие (уведомить менеджеров в свою
# TG-группу через бот-нотификатор + готовый ответ клиенту). Пишется в tenant_triggers (RLS,
# скоуп активного тенанта). Бот применяет (bot-telegram/triggers.py). Эталон UX — «Нейроагенты».
# =========================================================================== #
def _triggers_saved_text(saved: str | None) -> str | None:
    return {"added": "Триггер добавлен.", "deleted": "Триггер удалён."}.get(saved or "")


def _triggers_err_text(err: str | None) -> str | None:
    return {
        "no_tenant": "Кабинет ещё не привязан к клиенту. Обратитесь в поддержку.",
        "bad_type": "Неизвестный тип триггера.",
        "bad_action": "Неизвестное действие.",
        "bad_chat": "ID Telegram-чата должен быть числом вида -1002576119452.",
        "bad_topic": "ID темы форума должен быть числом.",
        "no_reply": "Для этого действия нужен текст ответа клиенту.",
        "no_stopwords": "Добавьте хотя бы одно стоп-слово.",
        "no_intent": "Опишите, в каком случае срабатывает триггер.",
        "bad_count": f"Количество сообщений — число от 1 до {config.TRIGGER_MSG_COUNT_MAX}.",
        "too_many": f"Достигнут лимит триггеров ({config.TRIGGER_MAX_PER_TENANT}).",
        "not_found": "Триггер не найден.",
    }.get(err or "")


def _present_trigger(r) -> dict:
    return {
        "id": str(r["id"]), "type": r["type"], "action": r["action"],
        "action_label": config.TRIGGER_ACTION_LABELS.get(r["action"], r["action"]),
        "stopwords": list(r["stopwords"] or []),
        "intent_desc": r["intent_desc"] or "", "msg_count": r["msg_count"],
        "notify_chat_id": r["notify_chat_id"] or "", "notify_topic_id": r["notify_topic_id"],
        "reply_text": r["reply_text"] or "", "enabled": r["enabled"],
    }


@app.get("/triggers", response_class=HTMLResponse)
async def triggers_page(
    request: Request,
    session: auth.Session = Depends(require_session),
    saved: str | None = None,
    err: str | None = None,
):
    tid = session.active_tenant_id
    rows = await db.list_tenant_triggers(tid) if tid else []
    by_type: dict[str, list] = {t: [] for t in config.TRIGGER_TYPE_ORDER}
    for r in rows:
        if r["type"] in by_type:
            by_type[r["type"]].append(_present_trigger(r))
    sections = [{"type": t, "label": config.TRIGGER_TYPE_LABELS[t], "items": by_type[t]}
                for t in config.TRIGGER_TYPE_ORDER]
    # Префилл TG-чата из блока «Эскалация» (чтобы клиент не вводил id повторно).
    esc = await db.get_tenant_escalation_config(tid) if tid else {"chat_id": ""}
    return templates.TemplateResponse(
        request,
        "triggers.html",
        {
            "active": "triggers",
            "session": session,
            "csrf_token": session.csrf_token,
            "has_tenant": bool(tid),
            "sections": sections,
            "actions": [{"key": k, "label": config.TRIGGER_ACTION_LABELS[k]}
                        for k in config.TRIGGER_ACTION_ORDER],
            "default_chat_id": esc.get("chat_id") or "",
            "notifier_username": config.NOTIFIER_BOT_USERNAME,
            "stopword_len_max": config.TRIGGER_STOPWORD_LEN_MAX,
            "intent_max": config.TRIGGER_INTENT_MAX,
            "reply_max": config.TRIGGER_REPLY_MAX,
            "count_max": config.TRIGGER_MSG_COUNT_MAX,
            "support_url": _safe_support_url(config.SUPPORT_URL),
            "saved": _triggers_saved_text(saved),
            "err": _triggers_err_text(err),
        },
    )


def _parse_stopwords(raw: str) -> list[str]:
    """Стоп-слова из текстового поля: разделители — запятая/перевод строки. Тримминг, дедуп с
    сохранением порядка, отбрасывание пустых, кап длины каждого и общего числа."""
    seen, out = set(), []
    for part in re.split(r"[,\n]", raw or ""):
        w = part.strip()[: config.TRIGGER_STOPWORD_LEN_MAX]
        if w and w.lower() not in seen:
            seen.add(w.lower())
            out.append(w)
        if len(out) >= config.TRIGGER_STOPWORDS_MAX:
            break
    return out


@app.post("/triggers/add")
async def triggers_add(
    request: Request,
    session: auth.Session = Depends(require_session),
    trigger_type: str = Form(""),
    action: str = Form(""),
    stopwords: str = Form(""),
    intent_desc: str = Form(""),
    msg_count: str = Form(""),
    notify_chat_id: str = Form(""),
    notify_topic_id: str = Form(""),
    reply_text: str = Form(""),
    csrf_token: str = Form(""),
):
    await _enforce_csrf(request, session, csrf_token)
    tid = session.active_tenant_id
    if not tid:
        return RedirectResponse(url="/triggers?err=no_tenant", status_code=303)
    if trigger_type not in config.TRIGGER_TYPE_LABELS:
        return RedirectResponse(url="/triggers?err=bad_type", status_code=303)
    if action not in config.TRIGGER_ACTION_LABELS:
        return RedirectResponse(url="/triggers?err=bad_action", status_code=303)
    chat = notify_chat_id.strip()
    if not re.match(config.ESCALATION_CHAT_ID_RE, chat):
        return RedirectResponse(url="/triggers?err=bad_chat", status_code=303)
    topic_raw = notify_topic_id.strip()
    if topic_raw and not topic_raw.isdigit():
        return RedirectResponse(url="/triggers?err=bad_topic", status_code=303)
    topic = int(topic_raw) if topic_raw else None
    reply = reply_text.strip()[: config.TRIGGER_REPLY_MAX]
    if action in ("notify_reply_continue", "notify_reply_pause") and not reply:
        return RedirectResponse(url="/triggers?err=no_reply", status_code=303)
    # Условие по типу:
    words: list[str] = []
    intent = ""
    count: int | None = None
    if trigger_type == "stopwords":
        words = _parse_stopwords(stopwords)
        if not words:
            return RedirectResponse(url="/triggers?err=no_stopwords", status_code=303)
    elif trigger_type == "intent":
        intent = intent_desc.strip()[: config.TRIGGER_INTENT_MAX]
        if not intent:
            return RedirectResponse(url="/triggers?err=no_intent", status_code=303)
    elif trigger_type == "message_count":
        if not (msg_count.strip().isdigit() and 1 <= int(msg_count) <= config.TRIGGER_MSG_COUNT_MAX):
            return RedirectResponse(url="/triggers?err=bad_count", status_code=303)
        count = int(msg_count)
    if await db.count_tenant_triggers(tid) >= config.TRIGGER_MAX_PER_TENANT:
        return RedirectResponse(url="/triggers?err=too_many", status_code=303)
    await db.create_tenant_trigger(
        tid, type_=trigger_type, action=action, stopwords=words, intent_desc=intent,
        msg_count=count, notify_chat_id=chat, notify_topic_id=topic, reply_text=reply,
        actor=session.actor, ip=_ip(request), user_agent=_ua(request),
    )
    return RedirectResponse(url="/triggers?saved=added", status_code=303)


@app.post("/triggers/delete")
async def triggers_delete(
    request: Request,
    session: auth.Session = Depends(require_session),
    trigger_id: str = Form(""),
    csrf_token: str = Form(""),
):
    await _enforce_csrf(request, session, csrf_token)
    tid = session.active_tenant_id
    if not tid:
        return RedirectResponse(url="/triggers?err=no_tenant", status_code=303)
    try:
        ok = await db.delete_tenant_trigger(
            tid, trigger_id, actor=session.actor, ip=_ip(request), user_agent=_ua(request))
    except Exception:  # noqa: BLE001 — кривой uuid и т.п.
        ok = False
    return RedirectResponse(url="/triggers?saved=deleted" if ok else "/triggers?err=not_found",
                            status_code=303)


# =========================================================================== #
# Обработчики исключений (§3.12) — generic-ответы, скрабинг ПДн в stdout.
# =========================================================================== #
@app.exception_handler(AuthRedirect)
async def _auth_redirect_handler(_: Request, exc: AuthRedirect):
    qs = urlencode({"next": exc.next_path}) if exc.next_path and exc.next_path != "/" else ""
    url = f"/login?{qs}" if qs else "/login"
    return RedirectResponse(url=url, status_code=303)


@app.exception_handler(CSRFError)
async def _csrf_handler(request: Request, _: CSRFError):
    return _error_page(request, 403, "Запрос отклонён (CSRF). Обновите страницу и повторите.")


@app.exception_handler(RequestValidationError)
async def _validation_handler(request: Request, _: RequestValidationError):
    # Невалидный path/form (напр. /leads/not-a-uuid) → generic 422, НЕ 500 и без
    # эха значений полей. Acceptance: GET /leads/not-a-uuid → 422, не 500.
    return _error_page(request, 422, "Некорректные данные запроса.")


@app.exception_handler(StarletteHTTPException)
async def _http_exc_handler(request: Request, exc: StarletteHTTPException):
    # Не раскрываем detail с возможным контекстом; маппим на дружелюбный текст.
    messages = {
        400: "Некорректный запрос.",
        403: "Доступ запрещён.",
        404: "Не найдено.",
        405: "Метод не поддерживается.",
        413: "Слишком большой запрос.",
        422: "Некорректные данные запроса.",
    }
    msg = messages.get(exc.status_code, "Ошибка запроса.")
    return _error_page(request, exc.status_code, msg)


@app.exception_handler(Exception)
async def _unhandled_handler(request: Request, exc: Exception):
    # Полный traceback — только в server-side stdout (РФ-инфра), со скрабингом ПДн.
    import logging
    import traceback
    tb = security.scrub_pii("".join(traceback.format_exception(exc)))
    logging.getLogger("admin-panel").error("Unhandled error on %s:\n%s", request.url.path, tb)
    # Браузеру — generic, БЕЗ traceback/SQL/значений/DSN.
    return _error_page(request, 500, "Внутренняя ошибка. Мы уже разбираемся.")


def _error_page(request: Request, status_code: int, message: str) -> Response:
    """Generic-страница ошибки. HTML для браузера, JSON для прочего."""
    accept = request.headers.get("accept", "")
    if "text/html" in accept:
        try:
            resp = templates.TemplateResponse(
                request, "error.html",
                {"status_code": status_code, "message": message},
                status_code=status_code,
            )
            resp.headers["Cache-Control"] = "no-store"
            return resp
        except Exception:
            pass
    return JSONResponse({"detail": message}, status_code=status_code,
                        headers={"Cache-Control": "no-store"})


# --------------------------------------------------------------------------- #
# Reseller-платформа Wave 1 (ТЗ docs/reseller-platform-tz.md):
#   /tenants — выбор активного клиента (тенанта) сессии;
#   /keys    — «Ключи»: write-only секреты тенанта (vault, AES-GCM).
# Значения секретов НИКОГДА не логируются и не рендерятся (критерий §8.5).
# --------------------------------------------------------------------------- #

def _keys_err_text(err: str | None) -> str | None:
    return {
        "no_tenant": "Сначала выберите клиента (раздел «Клиенты»).",
        "no_vault": "Хранилище ключей не настроено: задайте VAULT_MASTER_KEY в env панели.",
        "bad_key": "Неизвестное имя ключа.",
        "empty": "Значение пустое — ключ не сохранён.",
        "too_long": f"Значение длиннее {config.TENANT_SECRET_VALUE_MAX} символов.",
        "not_found": "Такого ключа нет — удалять нечего.",
    }.get(err or "")


@app.get("/tenants", response_class=HTMLResponse)
async def tenants_page(
    request: Request,
    session: auth.Session = Depends(require_session),
    switched: int = 0,
):
    rows = await db.list_tenants_for(session.actor, session.role)
    return templates.TemplateResponse(
        request,
        "tenants.html",
        {
            "csrf_token": session.csrf_token,
            "session": session,
            "active": "tenants",
            "tenants": rows,
            "switched_flash": bool(switched),
        },
    )


@app.post("/tenants/switch")
async def tenants_switch(
    request: Request,
    session: auth.Session = Depends(require_session),
    tenant_id: str = Form(""),
    csrf_token: str = Form(""),
):
    await _enforce_csrf(request, session, csrf_token)
    tenant_id = tenant_id.strip()
    if not tenant_id or not await db.tenant_accessible(session.actor, session.role, tenant_id):
        # Чужой/несуществующий тенант — молча назад к списку (без подтверждения догадок).
        return RedirectResponse(url="/tenants", status_code=303)
    await db.set_session_tenant(session.sid, tenant_id)
    await db.audit(
        actor=session.actor, action="tenant_switch",
        ip=_ip(request), user_agent=_ua(request), detail={"tenant_id": tenant_id},
    )
    return RedirectResponse(url="/tenants?switched=1", status_code=303)


# ---- /usage — «Расход»: лента списаний ИИ из кошелька (Wave 3, ТЗ §6) ------- #
# Клиент видит ТОЛЬКО charged: себестоимость и множитель в контекст шаблона не
# попадают вовсе (db.list_usage их не выбирает). Платформенная экономика — блок
# «Экономика сервиса» в «Подписке» (только роль admin).
_USAGE_KIND_LABELS = {
    "llm": "Ответ ИИ",
    "embedding": "База знаний",
    "message": "Сообщение ИИ",
    "other": "Другое",
}


def _usage_volume(kind: str, units: dict) -> str:
    """Человекочитаемый объём операции из units (jsonb)."""
    if kind == "message":
        return "1 сообщение"
    total = units.get("tokens_total") or 0
    if total:
        return f"{int(total):,} ткн".replace(",", " ")
    return "—"


@app.get("/usage", response_class=HTMLResponse)
async def usage_page(
    request: Request,
    session: auth.Session = Depends(require_session),
):
    rows_ctx: list[dict] = []
    daily_ctx: list[dict] = []
    balance = 0
    ai_blocked = False
    if session.active_tenant_id:
        tid = session.active_tenant_id
        balance = await db.get_wallet_balance(tid)
        ai_blocked = await db.is_tenant_ai_blocked(tid)
        for r in await db.list_usage(tid, limit=100):
            units = r["units"] if isinstance(r["units"], dict) else json.loads(r["units"] or "{}")
            rows_ctx.append({
                "occurred_at": r["occurred_at"],
                "kind_label": _USAGE_KIND_LABELS.get(r["kind"], r["kind"]),
                "volume": _usage_volume(r["kind"], units),
                "charged": money.micro_to_rub_str(int(r["charged_microrub"])),
                "balance_after": money.micro_to_rub_str(int(r["balance_after_microrub"])),
                "balance_negative": int(r["balance_after_microrub"]) < 0,
            })
        daily_rows = await db.usage_daily(tid, days=14)
        daily_ctx = [
            {
                "day": d["day"],
                "ops": int(d["ops"]),
                "charged": money.micro_to_rub_str(int(d["charged_microrub"] or 0)),
            }
            for d in daily_rows
        ]
        charged_14d_micro = sum(int(d["charged_microrub"] or 0) for d in daily_rows)
    else:
        charged_14d_micro = 0
    return templates.TemplateResponse(
        request,
        "usage.html",
        {
            "csrf_token": session.csrf_token,
            "session": session,
            "active": "usage",
            "rows": rows_ctx,
            "daily": daily_ctx,
            "balance": money.micro_to_rub_str(balance),
            "balance_negative": balance < 0,
            "charged_14d": money.micro_to_rub_str(charged_14d_micro),
            "ai_blocked": ai_blocked,
        },
    )


@app.get("/keys", response_class=HTMLResponse)
async def keys_page(
    request: Request,
    session: auth.Session = Depends(require_session),
    saved: int = 0,
    deleted: int = 0,
    err: str | None = None,
):
    secrets_meta: list = []
    if session.active_tenant_id and vault.enabled():
        secrets_meta = await db.list_tenant_secrets(session.active_tenant_id)
    known = {r["key_name"] for r in secrets_meta}
    return templates.TemplateResponse(
        request,
        "keys.html",
        {
            "csrf_token": session.csrf_token,
            "session": session,
            "active": "keys",
            "vault_enabled": vault.enabled(),
            "secret_keys": config.TENANT_SECRET_KEYS,
            "secrets_meta": secrets_meta,
            "known_keys": known,
            "saved_flash": bool(saved),
            "deleted_flash": bool(deleted),
            "err": _keys_err_text(err),
        },
    )


@app.post("/keys")
async def keys_set(
    request: Request,
    session: auth.Session = Depends(require_session),
    key_name: str = Form(""),
    value: str = Form(""),
    csrf_token: str = Form(""),
):
    await _enforce_csrf(request, session, csrf_token)
    if not session.active_tenant_id:
        return RedirectResponse(url="/keys?err=no_tenant", status_code=303)
    if not vault.enabled():
        return RedirectResponse(url="/keys?err=no_vault", status_code=303)
    key_name = key_name.strip()
    if key_name not in config.TENANT_SECRET_KEY_SET:
        return RedirectResponse(url="/keys?err=bad_key", status_code=303)
    value = value.strip()
    if not value:
        return RedirectResponse(url="/keys?err=empty", status_code=303)
    if len(value) > config.TENANT_SECRET_VALUE_MAX:
        return RedirectResponse(url="/keys?err=too_long", status_code=303)
    ct, nonce, ver = vault.encrypt(value, aad=f"{session.active_tenant_id}:{key_name}")
    del value  # plaintext дальше этой строки не живёт (ни в БД, ни в аудите, ни в логах)
    await db.upsert_tenant_secret(
        session.active_tenant_id, key_name, ct, nonce, ver,
        actor=session.actor, ip=_ip(request), user_agent=_ua(request),
    )
    return RedirectResponse(url="/keys?saved=1", status_code=303)


@app.post("/keys/delete")
async def keys_delete(
    request: Request,
    session: auth.Session = Depends(require_session),
    key_name: str = Form(""),
    csrf_token: str = Form(""),
):
    await _enforce_csrf(request, session, csrf_token)
    if not session.active_tenant_id:
        return RedirectResponse(url="/keys?err=no_tenant", status_code=303)
    ok = await db.delete_tenant_secret(
        session.active_tenant_id, key_name.strip(),
        actor=session.actor, ip=_ip(request), user_agent=_ua(request),
    )
    return RedirectResponse(url="/keys?deleted=1" if ok else "/keys?err=not_found", status_code=303)


# --------------------------------------------------------------------------- #
# Reseller Wave 2a: пополнение кошелька (топап) + отвязка карты автопродления.
# Деньги ВНУТРЬ платформы (billing); списания наружу — Wave 3 (metering).
# --------------------------------------------------------------------------- #
@app.post("/wallet/topup")
async def wallet_topup(
    request: Request,
    session: auth.Session = Depends(require_session),
    amount: str = Form(""),
    email: str = Form(""),
    csrf_token: str = Form(""),
):
    await _enforce_csrf(request, session, csrf_token)
    if not session.active_tenant_id:
        return RedirectResponse(url="/subscription?err=no_tenant", status_code=303)
    if not config.YOOKASSA_ENABLED:
        return RedirectResponse(url="/subscription?err=no_yookassa", status_code=303)
    try:
        amount_micro = money.rub_to_micro(amount)
    except Exception:
        return RedirectResponse(url="/subscription?err=bad_amount", status_code=303)
    if not (config.WALLET_TOPUP_MIN_RUB * money.MICRO <= amount_micro
            <= config.WALLET_TOPUP_MAX_RUB * money.MICRO):
        return RedirectResponse(url="/subscription?err=bad_amount", status_code=303)
    email = email.strip()
    if config.SERVICE_RECEIPT_ENABLED and not _valid_email(email):
        return RedirectResponse(url="/subscription?err=bad_email", status_code=303)

    tid = session.active_tenant_id
    idem = f"topup:{tid}:{uuid.uuid4().hex}"
    row_id = await db.create_platform_payment(
        tid, type_="topup", amount_microrub=amount_micro, idempotence_key=idem)
    host = request.headers.get("host", "")
    description = "Пополнение кошелька «ИИ-Агент Про»"
    amount_str = money.micro_to_amount_str(amount_micro)
    try:
        payment = await yookassa.create_payment(
            amount=amount_str, currency="RUB",
            description=description,
            return_url=f"https://{host}/subscription?paid=1",
            idempotence_key=row_id,                      # наш id строки = Idempotence-Key
            metadata={"kind": "platform_topup", "tenant_id": str(tid), "payment_row_id": row_id},
            receipt=_service_receipt(email, description, amount_str),
        )
    except yookassa.YooKassaError:
        import logging
        logging.getLogger("admin-panel").exception("wallet topup create_payment failed")
        return RedirectResponse(url="/subscription?err=yk_failed", status_code=303)
    pid = payment.get("id")
    conf_url = (payment.get("confirmation") or {}).get("confirmation_url")
    if pid:
        await db.attach_platform_payment_yk(row_id, tid, pid)
    if not conf_url:
        return RedirectResponse(url="/subscription?err=yk_failed", status_code=303)
    await db.audit(actor=session.actor, action="wallet_topup_create",
                   ip=_ip(request), user_agent=_ua(request),
                   detail={"tenant_id": str(tid), "amount_microrub": amount_micro})
    return RedirectResponse(url=conf_url, status_code=303)


@app.post("/subscription/detach-card")
async def subscription_detach_card(
    request: Request,
    session: auth.Session = Depends(require_session),
    csrf_token: str = Form(""),
):
    """«Отвязать карту»: требование ЮKassa к рекурренту — покупатель отключает
    автопродление сам, без поддержки. Стираем сохранённый способ оплаты подписок тенанта."""
    await _enforce_csrf(request, session, csrf_token)
    if not session.active_tenant_id:
        return RedirectResponse(url="/subscription?err=no_tenant", status_code=303)
    await db.detach_payment_method(
        session.active_tenant_id,
        actor=session.actor, ip=_ip(request), user_agent=_ua(request),
    )
    return RedirectResponse(url="/subscription?detached=1", status_code=303)
