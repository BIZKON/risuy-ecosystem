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

import csv
import io
import secrets
import uuid
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
import security
import yookassa


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
    try:
        yield
    finally:
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
async def login_form(request: Request, error: str | None = None, next: str = "/"):
    # Если уже есть валидная сессия — на дашборд (не показываем логин повторно).
    sid = auth.unsign_sid(request.cookies.get(config.COOKIE_NAME))
    if sid and await auth.load_session(sid):
        return RedirectResponse(url="/", status_code=303)

    token = secrets.token_urlsafe(32)
    resp = templates.TemplateResponse(
        request,
        "login.html",
        {"csrf_token": token, "error": _login_error_text(error), "next": _safe_next(next)},
    )
    # Кладём pre-session CSRF в подписанную cookie, привязав к токену формы.
    auth.set_login_csrf_cookie(resp, token)
    return resp


def _login_error_text(error: str | None) -> str | None:
    if not error:
        return None
    # Единый текст без user-enumeration (§3.4) — любой код ошибки → один текст.
    return "Неверный логин или пароль."


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

    ok_user = auth.verify_username(username)
    ok_pass = auth.verify_password(password)  # всегда полный argon2 (constant-time)
    if not (ok_user and ok_pass):
        await auth.register_login_failure(account)
        await db.audit(actor=account, action="login_fail", ip=ip, user_agent=ua,
                       detail={"reason": "bad_credentials"})
        return _login_redirect(error="bad", next=next_path)

    # Успех: сброс троттла, ротация sid (анти-fixation), серверная сессия.
    await auth.reset_login_throttle(account)
    sid = await auth.create_session(config.ADMIN_USERNAME)
    await db.audit(actor=config.ADMIN_USERNAME, action="login_ok", ip=ip, user_agent=ua)

    resp = RedirectResponse(url=next_path, status_code=303)
    auth.set_session_cookie(resp, sid)
    auth.clear_login_csrf(resp)
    return resp


def _login_redirect(*, error: str, next: str) -> RedirectResponse:
    qs = urlencode({"error": error, "next": next})
    return RedirectResponse(url=f"/login?{qs}", status_code=303)


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

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "counts": dict(counts),
            "conversion": conversion,
            "by_source": by_source,
            "session": session,
            "csrf_token": session.csrf_token,
            "status_labels": config.STATUS_LABELS,
            "active": "dashboard",
        },
    )


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


async def _render_dialogs(
    request: Request,
    session: auth.Session,
    *,
    selected_id=None,
    rec=None,
    thread=None,
    replied: bool = False,
    paused_flash: bool = False,
    reply_err: str | None = None,
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
    err: str | None = None,
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
        replied=bool(replied), paused_flash=bool(paused), reply_err=err,
    )


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
    err: str | None = None,
):
    rows = await db.list_products(include_archived=True)
    products = [_present_product_row(r) for r in rows]
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
    }.get(err or "")


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
            status=status_val, actor=session.actor,
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


async def _current_subscription() -> dict:
    """Текущая подписка: тариф/период из последнего оплаченного счёта + флаг отмены +
    живой расход сообщений ИИ за текущий период."""
    latest = await db.get_latest_paid_invoice()
    canceled = await db.is_subscription_canceled()
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
        "yk_failed": "Не удалось создать платёж в ЮKassa. Попробуйте позже.",
    }.get(err or "")


# ---- /subscription — текущий тариф + счётчик + история + «Выбрать тариф» ----- #
@app.get("/subscription", response_class=HTMLResponse)
async def subscription_page(
    request: Request,
    session: auth.Session = Depends(require_session),
    paid: int = 0,
    canceled: int = 0,
    err: str | None = None,
):
    sub = await _current_subscription()
    invoices = [_present_invoice(r) for r in await db.list_service_invoices()]
    return templates.TemplateResponse(
        request,
        "subscription.html",
        {
            "sub": sub,
            "plans": _plans_for_picker(sub.get("plan_key")),
            "invoices": invoices,
            "yookassa_enabled": config.YOOKASSA_ENABLED,
            "contact_url": config.SERVICE_CONTACT_URL,
            "period_days": config.SERVICE_PLAN_PERIOD_DAYS,
            "csrf_token": session.csrf_token,
            "session": session,
            "active": "subscription",
            "paid_flash": bool(paid),
            "canceled_flash": bool(canceled),
            "err": _service_err_text(err),
        },
    )


# ---- /subscription/select — выбрать тариф → счёт + платёж ЮKassa → оплата ---- #
@app.post("/subscription/select")
async def subscription_select(
    request: Request,
    session: auth.Session = Depends(require_session),
    plan_key: str = Form(""),
    csrf_token: str = Form(""),
):
    await _enforce_csrf(request, session, csrf_token)
    plan = _plan(plan_key)
    if plan is None:
        return RedirectResponse(url="/subscription?err=bad_plan", status_code=303)
    if not plan.get("payable"):
        return RedirectResponse(url="/subscription?err=not_payable", status_code=303)
    if not config.YOOKASSA_ENABLED:
        return RedirectResponse(url="/subscription?err=no_yookassa", status_code=303)

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
        period_start=start, period_end=end, plan_key=plan_key, plan_name=plan["name"],
        quota=plan["quota"], plan_amount=plan_amount, overage_count=overage_count,
        overage_amount=overage_amount, amount=amount, currency=config.SERVICE_CURRENCY,
        actor=session.actor, ip=_ip(request), user_agent=_ua(request),
    )

    host = request.headers.get("host", "")
    return_url = f"https://{host}/subscription?paid=1"
    try:
        payment = await yookassa.create_payment(
            amount=amount, currency=config.SERVICE_CURRENCY,
            description=f"{plan['name']} · {start:%d.%m.%Y}–{end:%d.%m.%Y}",
            return_url=return_url, idempotence_key=invoice_id,
            metadata={"invoice_id": invoice_id, "plan": plan_key},
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
    await db.set_subscription_canceled(
        True, actor=session.actor, ip=_ip(request), user_agent=_ua(request)
    )
    return RedirectResponse(url="/subscription?canceled=1", status_code=303)


# ---- /webhooks/yookassa — публичный вебхук (без сессии/CSRF) --------------- #
@app.post("/webhooks/yookassa")
async def yookassa_webhook(request: Request):
    """Вебхук ЮKassa. НЕ доверяем телу: берём только id платежа, ПЕРЕПРОВЕРЯЕМ через
    API ЮKassa (status=succeeded & paid) и лишь тогда отмечаем счёт оплаченным (+карта).
    Оплата возобновляет подписку — снимаем флаг отмены. Всегда 200 (иначе ретраи)."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})
    obj = (body or {}).get("object") or {}
    payment_id = obj.get("id")
    if payment_id and config.YOOKASSA_ENABLED:
        try:
            payment = await yookassa.get_payment(payment_id)
            if payment.get("status") == "succeeded" and payment.get("paid"):
                card = (((payment.get("payment_method") or {}).get("card") or {}).get("last4"))
                row = await db.mark_service_invoice_paid_by_payment(payment_id, card_last4=card)
                # Оплата возобновляет подписку — снимаем флаг отмены (если стоял).
                if row is not None and await db.is_subscription_canceled():
                    await db.set_subscription_canceled(False, actor="yookassa-webhook",
                                                       ip=None, user_agent=None)
        except Exception:
            import logging
            logging.getLogger("admin-panel").exception("yookassa webhook verify failed")
    return JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})


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
