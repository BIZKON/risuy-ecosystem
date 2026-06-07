"""Security-примитивы без БД: заголовки, CSP, body-guard, скрабинг ПДн, маска.

Здесь только чистые функции и middleware-классы. Состояние сессий/CSRF/троттла —
в auth.py (с БД). Делёж нужен, чтобы exception-handler (§3.12) и заголовки (§3.7)
не тянули за собой пул.
"""
from __future__ import annotations

import ipaddress
import re

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

import config

# --------------------------------------------------------------------------- #
# Security-заголовки (§3.7). Строгий CSP без unsafe-inline: ни inline <script>,
# ни onclick. CSS — отдельный /static/styles.css. img data: — для inline-точек
# маски/иконок без внешних запросов. Применяются на КАЖДЫЙ ответ.
# --------------------------------------------------------------------------- #
_CSP = (
    "default-src 'none'; "
    "base-uri 'none'; "
    "form-action 'self'; "
    "frame-ancestors 'none'; "
    "img-src 'self' data:; "
    "style-src 'self'; "
    "script-src 'self'; "
    "connect-src 'self'"
)

_STATIC_HEADERS = {
    "Content-Security-Policy": _CSP,
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
    "X-Robots-Tag": "noindex, nofollow, noarchive",
}


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Навешивает security-заголовки. HSTS — только когда работаем под HTTPS
    (COOKIE_SECURE=1), иначе по http браузер всё равно его игнорирует, а слать
    смысла нет. Cache-Control: no-store ставится по умолчанию на всё, КРОМЕ
    статики (CSS можно кэшировать) — страницы/ответы с ПДн не должны оседать
    в BFCache/прокси (§3.7/§3.8).
    """

    async def dispatch(self, request: Request, call_next):
        response: Response = await call_next(request)
        for k, v in _STATIC_HEADERS.items():
            response.headers.setdefault(k, v)
        if config.COOKIE_SECURE:
            response.headers.setdefault(
                "Strict-Transport-Security",
                "max-age=63072000; includeSubDomains; preload",
            )
        # Статике (только /static/*) разрешаем кэш; всему остальному — no-store.
        # setdefault, чтобы НЕ затирать более строгий Cache-Control, который мог
        # выставить хендлер (напр. reveal: "no-store, no-cache, must-revalidate, private").
        if not request.url.path.startswith("/static/"):
            response.headers.setdefault("Cache-Control", "no-store")
            response.headers.setdefault("Pragma", "no-cache")
        return response


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    """Body-size guard (§3.13): Content-Length > лимита → 413 ДО парсинга формы.
    Дёшево отбивает попытки забить память multipart-парсера. Тело без
    Content-Length (chunked) тут не ловим — для HTML-форм за LB он всегда есть.

    per_path_limits — точечные исключения (план §6.5): путь загрузки файла рассылки
    POST /broadcasts имеет СВОЙ больший лимит (MAX_UPLOAD_BYTES), не ослабляя
    глобальный для всех остальных маршрутов. Совпадение по точному request.url.path.
    Для chunked-без-Content-Length глобальный лимит здесь не срабатывает (как и
    раньше) — streaming-обрыв на превышении делает сам хендлер при чтении UploadFile.
    """

    def __init__(self, app, max_bytes: int,
                 per_path_limits: dict[str, int] | None = None) -> None:
        super().__init__(app)
        self.max_bytes = max_bytes
        self.per_path_limits = per_path_limits or {}

    async def dispatch(self, request: Request, call_next):
        limit = self.per_path_limits.get(request.url.path, self.max_bytes)
        cl = request.headers.get("content-length")
        if cl is not None:
            try:
                if int(cl) > limit:
                    return JSONResponse(
                        {"detail": "Тело запроса слишком большое."},
                        status_code=413,
                        headers={"Cache-Control": "no-store"},
                    )
            except ValueError:
                return JSONResponse(
                    {"detail": "Некорректный Content-Length."},
                    status_code=400,
                    headers={"Cache-Control": "no-store"},
                )
        return await call_next(request)


# --------------------------------------------------------------------------- #
# IP из X-Forwarded-For — ADVISORY only (§3.6/§4.6). За LB Timeweb внутренний IP
# балансировщика не пиннится; XFF доверяем лишь для записи в аудит, НЕ для auth.
# Берём первый (левый) адрес цепочки, валидируем как ip; мусор → None.
# --------------------------------------------------------------------------- #
def client_ip(request: Request) -> str | None:
    xff = request.headers.get("x-forwarded-for")
    if xff:
        first = xff.split(",")[0].strip()
        try:
            ipaddress.ip_address(first)
            return first
        except ValueError:
            pass
    real = request.headers.get("x-real-ip")
    if real:
        try:
            ipaddress.ip_address(real.strip())
            return real.strip()
        except ValueError:
            pass
    client = request.client
    return client.host if client else None


def ip_in_cidr(ip: str | None, cidr: str) -> bool:
    """advisory: IP оператора в разрешённой сети (bypass троттла). IP спуфится → удобство."""
    if not ip or not cidr:
        return False
    try:
        return ipaddress.ip_address(ip) in ipaddress.ip_network(cidr, strict=False)
    except ValueError:
        return False


# --------------------------------------------------------------------------- #
# Скрабинг ПДн для логов/ошибок (§3.12). В stdout (РФ-инфра) полный traceback ОК,
# но из него вычищаем то, что похоже на телефон/email, чтобы случайно не осадить
# ПДн в Timeweb-логах открытым текстом. Грубо, best-effort, поверх generic-ответа.
# --------------------------------------------------------------------------- #
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?7|8)?[\s\-()]*\d[\d\s\-()]{8,}\d")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
# Куски DSN с паролем: postgres://user:PASSWORD@host
_DSN_RE = re.compile(r"(?i)(postgres(?:ql)?://[^:/@\s]+:)[^@/\s]+(@)")


def scrub_pii(text: str) -> str:
    if not text:
        return text
    text = _DSN_RE.sub(r"\1***\2", text)
    text = _EMAIL_RE.sub("«email»", text)
    text = _PHONE_RE.sub("«phone»", text)
    return text


# --------------------------------------------------------------------------- #
# Маска телефона по двум последним цифрам (§3.8). Презентация В ДОПОЛНЕНИЕ к тому,
# что список/карточка вообще не селектят полный номер — хвост приходит из SQL.
# has_phone=False → телефона нет (показываем «—»).
# --------------------------------------------------------------------------- #
def mask_phone(tail: str | None, has_phone: bool) -> str:
    if not has_phone:
        return "—"
    tail = (tail or "").rjust(2, "·")[-2:]
    return f"+7 ··· ··-{tail}"


# --------------------------------------------------------------------------- #
# Валидация target_url трекинг-ссылки (план §6.3, defence-in-depth). Допускаем
# только http/https с непустым host, без управляющих символов и пробелов. ТА ЖЕ
# проверка дублируется в обработчике /r бота на чтении — не доверяем «панель уже
# проверила». Возвращает нормализованный URL или None (тогда хендлер отвергает).
# --------------------------------------------------------------------------- #
def validate_target_url(raw: str | None, *, schemes: tuple[str, ...]) -> str | None:
    from urllib.parse import urlparse

    if not raw:
        return None
    url = raw.strip()
    if not url or len(url) > 2048:
        return None
    # Никаких управляющих символов/пробелов/переводов строк внутри URL.
    if any(ord(ch) < 0x20 or ch in " \t\r\n" for ch in url):
        return None
    if url.startswith("//"):           # protocol-relative → отвергаем
        return None
    try:
        p = urlparse(url)
    except ValueError:
        return None
    if p.scheme.lower() not in schemes:
        return None
    if not p.netloc:                   # пустой host (напр. http:///x) → отвергаем
        return None
    return url


async def read_upload_capped(upload, *, max_bytes: int, chunk: int = 64 * 1024) -> bytes | None:
    """Прочитать UploadFile с потолком (план §6.5): streaming-обрыв на превышении.

    НЕ доверяем Content-Length (для chunked его нет, заголовок можно соврать) —
    читаем потоком, складываем размер, при превышении возвращаем None (хендлер →
    413). Пустой файл → b"" (длина 0). Память ограничена max_bytes + один chunk.
    """
    buf = bytearray()
    while True:
        part = await upload.read(chunk)
        if not part:
            break
        buf.extend(part)
        if len(buf) > max_bytes:
            return None
    return bytes(buf)
