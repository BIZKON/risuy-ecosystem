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
from datetime import datetime, timezone
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
    per_path_limits={"/broadcasts": config.MAX_UPLOAD_BYTES},
)

templates = Jinja2Templates(directory="templates")
# Шаблоны строго экранируют HTML (autoescape по умолчанию в Jinja2Templates).

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
    """Проверка Origin/Referer = свой хост (доп. слой к CSRF-токену, §3.5).

    Сверяем host:port. Если ни Origin, ни Referer не пришли — НЕ блокируем
    (некоторые клиенты их не шлют), полагаемся на CSRF-токен как основной контроль.
    """
    target = request.headers.get("host")
    if not target:
        return True
    for hdr in ("origin", "referer"):
        val = request.headers.get(hdr)
        if not val:
            continue
        # Грубо вытащим host из URL без внешних либ.
        try:
            after_scheme = val.split("://", 1)[1]
        except IndexError:
            return False
        host = after_scheme.split("/", 1)[0]
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
                      paused_flash=bool(paused)),
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


def _lead_context(request, session, rec, *, revealed: str | None, saved: bool = False,
                  erased: bool = False, thread=None, replied: bool = False,
                  paused_flash: bool = False) -> dict:
    lead = dict(rec)
    lead["phone_masked"] = security.mask_phone(rec["phone_tail"], rec["has_phone"])
    return {
        "lead": lead,
        "revealed": revealed,           # полный номер ТОЛЬКО при reveal-POST
        "saved": saved,
        "erased": erased,
        "replied": replied,             # флеш «ответ поставлен в очередь»
        "paused_flash": paused_flash,   # флеш переключения перехвата
        "thread": thread or [],
        "refresh_sec": config.THREAD_REFRESH_SEC,
        "msg_max": config.MSG_MAX_LEN,
        "statuses": config.STATUSES,
        "status_labels": config.STATUS_LABELS,
        "source_labels": config.SOURCE_LABELS,
        "messenger_labels": config.MESSENGER_LABELS,
        "csrf_token": session.csrf_token,
        "session": session,
        "notes_max": config.NOTES_MAX_LEN,
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
):
    return await _set_bot_paused(request, lead_id, session, csrf_token, paused=True)


@app.post("/leads/{lead_id}/bot-resume")
async def lead_bot_resume(
    request: Request,
    lead_id: uuid.UUID,
    session: auth.Session = Depends(require_session),
    csrf_token: str = Form(""),
):
    return await _set_bot_paused(request, lead_id, session, csrf_token, paused=False)


async def _set_bot_paused(request, lead_id, session, csrf_token, *, paused: bool):
    await _enforce_csrf(request, session, csrf_token)
    # UPDATE одной колонки leads.bot_paused в транзакции с аудитом (bot_paused|bot_resumed).
    # Telegram панель НЕ трогает: на паузе бот сам перестаёт авто-отвечать (его проверки).
    row = await db.set_bot_paused_with_audit(
        lead_id, paused=paused, actor=session.actor,
        ip=_ip(request), user_agent=_ua(request),
    )
    if row is None:
        raise StarletteHTTPException(status_code=404, detail="Лид не найден")
    return RedirectResponse(url=f"/leads/{lead_id}?paused=1#thread", status_code=303)


# ---- /leads/{id}/reply — ручной ответ → INSERT в outbox (НЕ Telegram, §4) -- #
@app.post("/leads/{lead_id}/reply")
async def lead_reply(
    request: Request,
    lead_id: uuid.UUID,
    session: auth.Session = Depends(require_session),
    text: str = Form(""),
    csrf_token: str = Form(""),
):
    await _enforce_csrf(request, session, csrf_token)

    # Длину капим ПЕРВЫМ действием, до БД (§3.13/§5.11). plain-текст, без parse_mode.
    text = (text or "").strip()[: config.MSG_MAX_LEN]
    if not text:
        raise StarletteHTTPException(status_code=400, detail="Пустой ответ")

    # INSERT в outbox 'queued' + аудит manual_reply ({len}, без текста). Реально шлёт
    # бот; адресность (tg_user_id) и erase-фильтр он re-check'ает перед отправкой.
    outbox_id = await db.enqueue_manual_reply(
        lead_id, text=text, actor=session.actor,
        ip=_ip(request), user_agent=_ua(request),
    )
    if outbox_id is None:
        # Лид не найден ИЛИ без tg_user_id (некому слать) — не молчим, говорим оператору.
        raise StarletteHTTPException(
            status_code=400, detail="Лиду нельзя написать (нет Telegram-адреса)"
        )
    return RedirectResponse(url=f"/leads/{lead_id}?replied=1#thread", status_code=303)


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
        },
    )


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

    # Файл: allow-list mime + streaming-cap (не доверяем Content-Length, §6.5).
    file_meta: dict | None = None
    kind = "text"
    if has_file:
        mime = (file.content_type or "").split(";")[0].strip().lower()
        if mime not in config.UPLOAD_MIME_ALLOW:
            return _broadcast_new_redirect("bad_file")
        data = await security.read_upload_capped(file, max_bytes=config.MAX_UPLOAD_BYTES)
        if data is None:
            return _broadcast_new_redirect("file_too_big")
        if data:
            file_meta = {"filename": (file.filename or "file")[:255], "mime": mime,
                         "bytes": data}
            kind = _kind_for_mime(mime)
        # пустой файл (0 байт) — игнорируем, остаётся text

    estimate = await db.count_broadcast_audience(audience)
    bid = await db.create_broadcast_with_audit(
        title=title_val, messenger=audience["messenger"], kind=kind,
        body_template=body, audience=audience, recipient_estimate=estimate,
        file_meta=file_meta, target_url=link_url,
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


def _kind_for_mime(mime: str) -> str:
    if mime.startswith("image/"):
        return "photo"
    return "document"


# ---- /broadcasts/{id} — аналитика (4 честных метрики, §6.1) --------------- #
@app.get("/broadcasts/{broadcast_id}", response_class=HTMLResponse)
async def broadcast_detail(
    request: Request,
    broadcast_id: int,
    session: auth.Session = Depends(require_session),
    queued: int = 0,
    canceled: int = 0,
    resumed: int = 0,
    err: str | None = None,
):
    rec = await db.get_broadcast(broadcast_id)
    if rec is None:
        raise StarletteHTTPException(status_code=404, detail="Рассылка не найдена")

    # broadcast_view в аудит (получатели — ПДн), на каждое открытие аналитики.
    await db.audit(actor=session.actor, action="broadcast_view",
                   ip=_ip(request), user_agent=_ua(request),
                   detail={"broadcast_id": broadcast_id})

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
            "resumed": bool(resumed),
            "err": _broadcast_send_err_text(err),
            "csrf_token": session.csrf_token,
            "session": session,
            "active": "broadcasts",
            "status_labels": config.STATUS_LABELS,
            "broadcast_status_labels": BROADCAST_STATUS_LABELS,
            "max_recipients": config.MAX_BROADCAST_RECIPIENTS,
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
