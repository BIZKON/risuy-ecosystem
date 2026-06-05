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

from fastapi import Depends, FastAPI, Form, Request
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
app.add_middleware(security.BodySizeLimitMiddleware, max_bytes=config.MAX_BODY_BYTES)

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
):
    rec = await db.get_lead(lead_id)
    if rec is None:
        raise StarletteHTTPException(status_code=404, detail="Лид не найден")

    # lead_view в аудит на каждое открытие (§3.6).
    await db.audit(actor=session.actor, action="lead_view", lead_id=lead_id,
                   ip=_ip(request), user_agent=_ua(request))

    return templates.TemplateResponse(
        request,
        "lead.html",
        _lead_context(request, session, rec, revealed=None, saved=bool(saved), erased=bool(erased)),
    )


def _lead_context(request, session, rec, *, revealed: str | None, saved: bool = False,
                  erased: bool = False) -> dict:
    lead = dict(rec)
    lead["phone_masked"] = security.mask_phone(rec["phone_tail"], rec["has_phone"])
    return {
        "lead": lead,
        "revealed": revealed,           # полный номер ТОЛЬКО при reveal-POST
        "saved": saved,
        "erased": erased,
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
