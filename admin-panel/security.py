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
    "img-src 'self' data: blob:; "    # blob: — превью выбранной картинки-вложения (object URL в reply.js)
    "media-src 'self' blob:; "        # blob: — переслушать записанное голосовое ДО отправки (<audio>)
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
    # microphone=(self): разрешаем запись голоса в форме ответа оператора (диктофон в
    # браузере панели). geolocation/camera по-прежнему полностью запрещены.
    "Permissions-Policy": "geolocation=(), microphone=(self), camera=()",
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

    per_path_suffix_limits — суффикс-исключения для ДИНАМИЧЕСКИХ путей, где точный
    путь не выписать заранее: напр. POST /leads/{uuid}/reply (вложение в личный ответ)
    оканчивается на '/reply'. Точный per_path_limits для него не сработал бы (uuid в
    середине), поэтому отдельный суффикс-матч. Точное совпадение приоритетнее суффикса.
    Для chunked-без-Content-Length глобальный лимит здесь не срабатывает (как и
    раньше) — streaming-обрыв на превышении делает сам хендлер при чтении UploadFile.
    """

    def __init__(self, app, max_bytes: int,
                 per_path_limits: dict[str, int] | None = None,
                 per_path_suffix_limits: dict[str, int] | None = None) -> None:
        super().__init__(app)
        self.max_bytes = max_bytes
        self.per_path_limits = per_path_limits or {}
        # tuple для стабильного порядка проверки суффиксов (первый матч выигрывает).
        self.per_path_suffix_limits = tuple((per_path_suffix_limits or {}).items())

    def _limit_for(self, path: str) -> int:
        # 1) точное совпадение пути (приоритет); 2) суффикс динамического пути; 3) дефолт.
        exact = self.per_path_limits.get(path)
        if exact is not None:
            return exact
        for suffix, lim in self.per_path_suffix_limits:
            if path.endswith(suffix):
                return lim
        return self.max_bytes

    async def dispatch(self, request: Request, call_next):
        limit = self._limit_for(request.url.path)
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


# --------------------------------------------------------------------------- #
# Валидация файла ОФЕРА по magic-byte (каталог продуктов). НЕ доверяем имени и
# заявленному MIME — проверяем СОДЕРЖИМОЕ по сигнатуре, затем сверяем расширение и
# MIME с allow-list'ом из config (тройной контроль: ext ∧ mime ∧ magic). Это режет
# подмену исполняемого/скрипта под видом .pdf/.png и «голый octet-stream».
#
# Чистая Python-проверка по префиксу байтов — БЕЗ libmagic/python-magic (их нет в
# slim-образе, новые зависимости не добавляем). Покрывает заявленные форматы; чего
# нет в таблице — отвергается (deny-by-default). Office/zip-семейство (docx/xlsx/
# pptx/zip) делит сигнатуру PK\x03\x04 — для них достаточно «это ZIP-контейнер» +
# расширение из allow-list (различать ooxml по mimetype внутри архива здесь избыточно
# и хрупко; расширение уже в allow-list, а отправляется всё как document).
# --------------------------------------------------------------------------- #

# Явный отказ опасным/исполняемым/скриптовым сигнатурам — даже если кто-то добавит
# такое расширение в allow-list по ошибке, magic-проверка не даст это пронести.
_DANGEROUS_SIGNATURES: tuple[bytes, ...] = (
    b"MZ",            # PE/DOS (.exe/.dll/.scr)
    b"\x7fELF",       # ELF (Linux-бинарь)
    b"\xca\xfe\xba\xbe",  # Mach-O fat / Java class
    b"\xfe\xed\xfa\xce",  # Mach-O 32
    b"\xfe\xed\xfa\xcf",  # Mach-O 64
    b"#!",            # шебанг скрипта (#!/bin/sh, #!/usr/bin/env ...)
    b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",  # OLE2 (legacy doc/xls/ppt) — НЕ принимаем как exe-носитель
)

# Карта «сигнатура → набор расширений, которым она соответствует». Проверка:
#   1) содержимое матчит ОДНУ из сигнатур;
#   2) заявленное расширение входит в множество допустимых для этой сигнатуры;
#   3) расширение есть в config.PRODUCT_FILE_TYPES и MIME — в его mimes.
# Для txt сигнатуры нет (произвольный текст) → отдельная эвристика «печатаемый текст».
_IMG_EXTS = frozenset({"jpg", "jpeg", "png", "webp", "gif"})
_ZIP_OOXML_EXTS = frozenset({"zip", "docx", "xlsx", "pptx"})
_OLE_EXTS = frozenset({"doc", "xls", "ppt"})
# Аудио/голос личного ответа оператора (запись с микрофона). У каждого свой контейнер:
# webm → EBML-заголовок; ogg → 'OggS'; m4a → ISO-BMFF box 'ftyp' на offset 4 (как mp4).
# Сигнатуры проверяются в _content_matches_ext отдельными ветками (как webp/mp4/txt).
_AUDIO_EXTS = frozenset({"webm", "ogg", "m4a"})

# (offset, magic-bytes, допустимые расширения). Первый матч выигрывает.
_PRODUCT_SIGNATURES: tuple[tuple[int, bytes, frozenset[str]], ...] = (
    (0, b"\xff\xd8\xff", frozenset({"jpg", "jpeg"})),                 # JPEG
    (0, b"\x89PNG\r\n\x1a\n", frozenset({"png"})),                    # PNG
    (0, b"GIF87a", frozenset({"gif"})),                              # GIF
    (0, b"GIF89a", frozenset({"gif"})),
    # WEBP: 'RIFF' .... 'WEBP' (проверяем оба маркера в sniff'е отдельно из-за дыры в 4 байта)
    (0, b"%PDF-", frozenset({"pdf"})),                               # PDF
    (0, b"PK\x03\x04", _ZIP_OOXML_EXTS),                            # ZIP / OOXML (docx/xlsx/pptx)
    (0, b"PK\x05\x06", _ZIP_OOXML_EXTS),                            # пустой ZIP
    (0, b"PK\x07\x08", _ZIP_OOXML_EXTS),                            # spanned ZIP
    (0, b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", _OLE_EXTS),             # OLE2 legacy doc/xls/ppt
    (0, b"ID3", frozenset({"mp3"})),                                # MP3 с ID3-тегом
    (0, b"\xff\xfb", frozenset({"mp3"})),                           # MP3 frame (MPEG-1 L3)
    (0, b"\xff\xf3", frozenset({"mp3"})),
    (0, b"\xff\xf2", frozenset({"mp3"})),
    # MP4/ISO-BMFF: на offset 4 идёт 'ftyp' (проверяем в sniff'е отдельно).
)


def normalize_ext(filename: str | None) -> str:
    """Расширение из имени файла → нижний регистр без точки ('' если нет)."""
    if not filename or "." not in filename:
        return ""
    return filename.rsplit(".", 1)[-1].strip().lower()


def _looks_like_text(data: bytes) -> bool:
    """Грубая эвристика «это печатаемый текст» для .txt: нет NUL, мало control-байт.

    Декодируем как UTF-8 (наиболее частый случай) с запасом cp1251 — если не вышло,
    проверяем долю непечатаемых в первом килобайте. NUL-байт → точно не текст.
    """
    if not data:
        return True  # пустой .txt допустим (валидатор размера отдельно)
    sample = data[:4096]
    if b"\x00" in sample:
        return False
    try:
        sample.decode("utf-8")
        return True
    except UnicodeDecodeError:
        pass
    # Доля «странных» control-байт (кроме \t\n\r) в выборке.
    allowed_ctrl = {0x09, 0x0A, 0x0D}
    bad = sum(1 for b in sample if b < 0x20 and b not in allowed_ctrl)
    return bad / len(sample) < 0.10


def sniff_product_file(
    data: bytes, *, filename: str | None, claimed_mime: str | None
) -> dict | None:
    """Подтвердить файл офера по СОДЕРЖИМОМУ. Возвращает dict или None (отказ).

    На вход — уже прочитанные байты (см. read_upload_capped), имя и заявленный MIME.
    Тройной контроль (ext ∧ mime ∧ magic), затем отказ опасным сигнатурам. Возврат:
      {"ext", "mime", "send"} — нормализованное расширение, MIME для хранения
      (берём канон из allow-list, НЕ доверяем браузерному), способ отправки photo|document.
    None — формат не из allow-list / содержимое не совпало с расширением / опасное.

    config импортируется внутри (модуль security не должен тянуть тяжёлые справочники
    на верхнем уровне; config уже импортирован процессом — это просто доступ к атрибуту).
    """
    ext = normalize_ext(filename)
    spec = config.PRODUCT_FILE_TYPES.get(ext)
    if spec is None:
        return None  # расширение не в allow-list

    mime = (claimed_mime or "").split(";")[0].strip().lower()
    # MIME сверяем с allow-list расширения. Пустой/неизвестный браузерный MIME
    # допускаем ТОЛЬКО если octet-stream разрешён для этого ext (zip/office/txt/mp*),
    # т.к. финально тип подтвердят magic-byte'ы. Иначе — отказ.
    allowed_mimes = spec["mimes"]
    if mime and mime not in allowed_mimes:
        return None
    if not mime and "application/octet-stream" not in allowed_mimes:
        return None

    # Опасные сигнатуры — жёсткий отказ ДО позитивных проверок (кроме OLE2, который
    # легитимен для doc/xls/ppt: его пропускаем к позитивной ветке ниже).
    head = data[:16]
    if not (ext in _OLE_EXTS):
        for sig in _DANGEROUS_SIGNATURES:
            if head.startswith(sig):
                return None

    if not _content_matches_ext(data, ext):
        return None

    return {"ext": ext, "mime": allowed_mimes[0], "send": spec["send"]}


def _content_matches_ext(data: bytes, ext: str) -> bool:
    """Magic-byte: содержимое соответствует заявленному расширению (из allow-list)."""
    # txt: сигнатуры нет — эвристика печатаемого текста.
    if ext == "txt":
        return _looks_like_text(data)
    # webp: RIFF....WEBP (маркер на offset 8).
    if ext == "webp":
        return len(data) >= 12 and data[0:4] == b"RIFF" and data[8:12] == b"WEBP"
    # mp4/m4a: ISO-BMFF box 'ftyp' на offset 4 (....ftyp). m4a — тот же контейнер,
    # что mp4 (Safari пишет голос с микрофона как audio/mp4), поэтому проверка общая.
    if ext in ("mp4", "m4a"):
        return len(data) >= 12 and data[4:8] == b"ftyp"
    # webm: EBML-заголовок (Matroska-контейнер) — Chrome/Firefox пишут голос как audio/webm.
    if ext == "webm":
        return data[:4] == b"\x1aE\xdf\xa3"
    # ogg: 'OggS' capture pattern на offset 0 (Firefox/Chrome могут писать в ogg/opus).
    if ext == "ogg":
        return data[:4] == b"OggS"
    # Остальное — по таблице префиксных сигнатур.
    for offset, sig, exts in _PRODUCT_SIGNATURES:
        if ext in exts and data[offset:offset + len(sig)] == sig:
            return True
    return False
