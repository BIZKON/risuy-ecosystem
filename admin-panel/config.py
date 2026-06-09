"""Конфигурация админ-панели лидов. Всё из переменных окружения — секреты не в коде.

Зеркалит паттерн bot-telegram/config.py: один helper _req() + fail-fast.
Дополнительно — реальные guard'ы на формат секретов (§3.14 плана), а не только
их наличие: панель должна падать на старте, если ADMIN_PASSWORD_HASH не argon2-PHC
или SESSION_SECRET слишком короткий. Это единственная защита от «забыли задать env».
"""
import os


def _req(name: str) -> str:
    """Обязательная переменная: пусто/отсутствует → падаем на старте (как в боте)."""
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(f"Не задана обязательная переменная окружения: {name}")
    return val


def _opt_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as e:
        raise RuntimeError(f"Переменная {name} должна быть целым числом, получено: {raw!r}") from e


def _opt_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# --- DSN роли panel_rw (НЕ owner-DSN бота). Тот же кластер Managed PG, ru-1. ---
DATABASE_URL = _req("DATABASE_URL")

# --- Секреты ---
# Подпись session-cookie (tamper-evidence для opaque sid). Минимум 32 байта.
SESSION_SECRET = _req("SESSION_SECRET")
if len(SESSION_SECRET) < 32:
    raise RuntimeError(
        "SESSION_SECRET слишком короткий: нужно ≥32 символов "
        f"(сейчас {len(SESSION_SECRET)}). Сгенерируйте: python -c \"import secrets;print(secrets.token_urlsafe(48))\""
    )

# Логин единственного оператора (не секрет, но обязателен; сравнивается constant-time).
ADMIN_USERNAME = _req("ADMIN_USERNAME")

# Argon2id PHC-строка (хеш, НЕ пароль). Генерится офлайн, в репозиторий не коммитим.
ADMIN_PASSWORD_HASH = _req("ADMIN_PASSWORD_HASH")
if not ADMIN_PASSWORD_HASH.startswith("$argon2"):
    raise RuntimeError(
        "ADMIN_PASSWORD_HASH должен быть argon2-PHC строкой ($argon2id$v=19$...). "
        "Похоже, передан plain-пароль. Сгенерируйте хеш офлайн через argon2-cffi."
    )

# --- Опциональная сеть оператора: advisory bypass per-IP троттла (§3.4). ---
# IP легко спуфится за LB → это удобство, не security-контроль.
LOGIN_ALLOWLIST_CIDR = os.environ.get("LOGIN_ALLOWLIST_CIDR", "")

# --- Параметры сессий (§3.2) ---
SESSION_IDLE_MIN = _opt_int("SESSION_IDLE_MIN", 30)     # скользящий idle-таймаут, минуты
SESSION_MAX_HOURS = _opt_int("SESSION_MAX_HOURS", 8)    # жёсткий потолок жизни сессии, часы

# --- Cookie ---
# Безусловно Secure по умолчанию: браузер↔LB всегда HTTPS на *.twc1.net (§3.3/§4.6),
# даже если контейнер видит plain HTTP. Снять (=0) можно только для локальной отладки по http.
COOKIE_SECURE = _opt_bool("COOKIE_SECURE", True)

# __Host- префикс требует Secure+Path=/+без Domain. Если Secure отключён (локальная
# отладка по http), браузер отвергнет __Host- cookie → используем обычное имя.
COOKIE_NAME = "__Host-session" if COOKIE_SECURE else "session"

# --- Порт health/uvicorn (Timeweb App Platform пробрасывает свой $PORT). ---
PORT = _opt_int("PORT", 8080)

# --- Лимиты (§3.13) ---
MAX_BODY_BYTES = _opt_int("MAX_BODY_BYTES", 64 * 1024)   # тело запроса > 64 KB → 413
NOTES_MAX_LEN = 4000                                     # обрезка notes первым действием хендлера
EXPORT_ROW_CAP = _opt_int("EXPORT_ROW_CAP", 50_000)      # hard row-cap на CSV-экспорт
PER_PAGE = 50                                            # пагинация списка (объёмы малы)

# --- Переписка / рассылки (план §3,§5,§6,§7) ---
# Лимиты сообщений Telegram (отвергаем сверх лимита ДО постановки в очередь, §5.11):
#   текст/реплай ≤4096; caption у файла ≤1024. parse_mode НЕ используется — всё plain.
MSG_MAX_LEN = _opt_int("MSG_MAX_LEN", 4096)              # ручной ответ / текст рассылки без файла
CAPTION_MAX_LEN = _opt_int("CAPTION_MAX_LEN", 1024)      # подпись к файлу рассылки (TG-лимит)
THREAD_CAP = _opt_int("THREAD_CAP", 200)                 # лента треда: последние N сообщений
THREAD_REFRESH_SEC = _opt_int("THREAD_REFRESH_SEC", 15)  # meta-refresh карточки лида (no-JS)

# Загрузка файла рассылки: отдельный лимит ТОЛЬКО для POST /broadcasts (НЕ ослаблять
# глобальный MAX_BODY_BYTES). Бот первичной заливкой в служебный чат получит file_id (§6.5).
MAX_UPLOAD_BYTES = _opt_int("MAX_UPLOAD_BYTES", 10 * 1024 * 1024)   # ~10 MB, в пределах bot-upload
# Allow-list типов файла рассылки (картинки/документы) — не произвольные байты (§6.5).
UPLOAD_MIME_ALLOW = (
    "image/jpeg", "image/png", "image/gif", "image/webp",
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
)

# ── Каталог продуктов (оферов) — раздел «Продукты» (schema_products.sql) ──────
# Лимит размера файла офера. Бот заливает file→file_tg_id в OPS_CHAT_ID и шлёт во
# все рассылки; жёсткий потолок Telegram-бота — 50 МБ (и фото, и документ). Это
# СОБСТВЕННЫЙ лимит для POST /products (не путать с MAX_UPLOAD_BYTES рассылки): офер
# хранится переиспользуемо и может быть тяжелее разового файла рассылки. per-path
# body-guard и streaming-cap хендлера используют именно это значение.
MAX_PRODUCT_FILE_MB = _opt_int("MAX_PRODUCT_FILE_MB", 50)        # потолок файла офера, МБ (≤50 у бота)
MAX_PRODUCT_FILE_BYTES = MAX_PRODUCT_FILE_MB * 1024 * 1024

# Личный ответ может нести НЕСКОЛЬКО вложений (файлы + голос) — каждое уйдёт отдельным
# сообщением (Telegram не комбинирует документ+голос). MAX_REPLY_ATTACHMENTS — потолок
# числа вложений в одном ответе (анти-абуз). MAX_REPLY_BODY_BYTES — лимит ВСЕГО тела
# POST /leads/{id}/reply (несколько файлов суммарно); каждый файл всё равно ≤
# MAX_PRODUCT_FILE_BYTES (read_upload_capped) и ≤50 МБ у бота. Тело спулится на диск
# (Starlette), память не раздувает.
MAX_REPLY_ATTACHMENTS = _opt_int("MAX_REPLY_ATTACHMENTS", 10)
MAX_REPLY_BODY_BYTES = _opt_int("MAX_REPLY_BODY_MB", 100) * 1024 * 1024

# Виды офера — синхронны CHECK products_kind_chk в db/schema_products.sql. Порядок =
# порядок в select конструктора (lead_magnet первым: с него начинается воронка).
PRODUCT_KINDS = ("lead_magnet", "tripwire", "main")
PRODUCT_KIND_LABELS = {
    "lead_magnet": "Лид-магнит",
    "tripwire": "Трипваер",
    "main": "Основной продукт",
}
# Статусы офера — синхронны CHECK products_status_chk. archived скрывает из выбора.
PRODUCT_STATUSES = ("active", "archived")
PRODUCT_STATUS_LABELS = {"active": "Активен", "archived": "В архиве"}

# Валюты для показа рядом с ценой (RUB по умолчанию в схеме). Узкий allow-list —
# валюта идёт в текст рассылки/карточку, не в эквайринг (оплата внешняя через /r).
PRODUCT_CURRENCIES = ("RUB", "USD", "EUR")
PRODUCT_CURRENCY_LABELS = {"RUB": "₽ (RUB)", "USD": "$ (USD)", "EUR": "€ (EUR)"}
PRODUCT_CURRENCY_SIGNS = {"RUB": "₽", "USD": "$", "EUR": "€"}

# Длина подписи/описания офера. caption идёт В РАССЫЛКУ и/или как подпись к файлу;
# когда офер с файлом — реальный потолок Telegram-подписи это CAPTION_MAX_LEN (1024),
# но текст офера может уходить и отдельным сообщением, поэтому держим до MSG_MAX_LEN,
# а «файл → ≤1024» проверяет уже воркёр/композер при сборке сообщения.
PRODUCT_NAME_MAX_LEN = 200
PRODUCT_CAPTION_MAX_LEN = _opt_int("PRODUCT_CAPTION_MAX_LEN", 4096)

# Allow-list форматов файла ОФЕРА (шире, чем у разовой рассылки): картинки → photo,
# документы/архивы/медиа → document. Ключ — нормализованное расширение (без точки),
# значение — допустимые MIME (lower, без параметров). Браузеры присылают разные MIME
# на один тип (особенно office/zip), поэтому MIME сверяем множеством, а финально тип
# подтверждаем magic-byte'ами (security.sniff_product_file). Исполняемые/скриптовые
# расширения СЮДА НЕ входят — всё, чего нет в таблице, отвергается (deny-by-default).
#   ⚠️ Держать синхронно с таблицей сигнатур security._PRODUCT_SIGNATURES.
PRODUCT_FILE_TYPES: dict[str, dict] = {
    # — Картинки → отправляются как photo —
    "jpg":  {"send": "photo", "mimes": ("image/jpeg",)},
    "jpeg": {"send": "photo", "mimes": ("image/jpeg",)},
    "png":  {"send": "photo", "mimes": ("image/png",)},
    "webp": {"send": "photo", "mimes": ("image/webp",)},
    "gif":  {"send": "photo", "mimes": ("image/gif",)},
    # — Документы / таблицы / презентации → document —
    "pdf":  {"send": "document", "mimes": ("application/pdf",)},
    "doc":  {"send": "document", "mimes": ("application/msword",)},
    "docx": {"send": "document", "mimes": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/zip", "application/octet-stream")},
    "xls":  {"send": "document", "mimes": ("application/vnd.ms-excel",)},
    "xlsx": {"send": "document", "mimes": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/zip", "application/octet-stream")},
    "ppt":  {"send": "document", "mimes": ("application/vnd.ms-powerpoint",)},
    "pptx": {"send": "document", "mimes": (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/zip", "application/octet-stream")},
    "zip":  {"send": "document", "mimes": ("application/zip", "application/octet-stream")},
    "txt":  {"send": "document", "mimes": ("text/plain", "application/octet-stream")},
    # — Популярное медиа → document (как и в рассылках: единый file_id) —
    "mp4":  {"send": "document", "mimes": ("video/mp4", "application/mp4")},
    "mp3":  {"send": "document", "mimes": ("audio/mpeg", "audio/mp3")},
    # — Голос/аудио для ЛИЧНОГО ответа оператора (не каталог оферов) → бот шлёт
    #   как voice (после транскодинга в ogg/opus) или audio (fallback). Расширения
    #   нужны здесь, чтобы magic-byte валидатор (security.sniff_product_file) узнавал
    #   запись с микрофона. Классификацию voice vs document даёт _read_reply_file по
    #   REPLY_AUDIO_MIMES — НЕ поле "send" (оно тут номинально document, реальный kind
    #   ставит хендлер ответа). Браузерная запись приходит как webm/ogg (Chrome/FF) или
    #   mp4/m4a (Safari); octet-stream допускаем — финально подтвердит magic-byte.
    "webm": {"send": "document", "mimes": ("audio/webm", "video/webm", "application/octet-stream")},
    "ogg":  {"send": "document", "mimes": ("audio/ogg", "application/ogg", "application/octet-stream")},
    "m4a":  {"send": "document", "mimes": ("audio/mp4", "audio/x-m4a", "application/octet-stream")},
}
# Удобный плоский набор допустимых расширений (для accept= в форме и быстрых проверок).
PRODUCT_FILE_EXTS = tuple(PRODUCT_FILE_TYPES.keys())

# ── Личный ответ оператора лиду: вложение файла + голосовое (план «reply-attach») ──
# Реюзаем PRODUCT_FILE_TYPES (картинки/доки) для валидации; голос — отдельный набор
# MIME ниже. Классификация kind в _read_reply_file: image/* → 'photo';
# MIME ∈ REPLY_AUDIO_MIMES → 'voice' (бот транскодит в ogg/opus, при сбое ffmpeg →
# 'audio'); иначе → 'document'. Канон-MIME сверяется magic-byte'ом, поэтому набор
# минимален: ровно те типы, что отдаёт запись с микрофона в браузере.
REPLY_AUDIO_MIMES = ("audio/webm", "audio/mp4", "audio/ogg", "video/webm",
                     "application/ogg", "audio/x-m4a")
# Расширения голоса/аудио — подмножество PRODUCT_FILE_TYPES, для accept= в форме ответа.
REPLY_AUDIO_EXTS = ("webm", "ogg", "m4a")
# accept= для <input type=file> формы ответа: картинки/доки из каталога + голос.
# (audio/* добавляем как MIME-маску, чтобы мобильные браузеры предложили диктофон.)
REPLY_FILE_EXTS = PRODUCT_FILE_EXTS

# Hard-cap на размер аудитории рассылки (§7.1): сверх — требуем точный confirm_count эхом.
MAX_BROADCAST_RECIPIENTS = _opt_int("MAX_BROADCAST_RECIPIENTS", 5000)
# Анти-флуд черновиков рассылок (§6.5): не больше N создаётся за окно (счётчик в БД).
BROADCAST_DRAFT_MAX_PER_HOUR = _opt_int("BROADCAST_DRAFT_MAX_PER_HOUR", 20)

# allow-list схем target_url для трекинг-ссылки (defence-in-depth: и на записи в панели,
# и на чтении в /r бота, §6.3). Редирект /r/<token> живёт в БОТЕ, не здесь.
LINK_URL_SCHEMES = ("http", "https")

# --- 152-ФЗ (из landing/privacy.html §6.5, landing/consent.html §6) ---
ERASE_AFTER_DAYS = _opt_int("ERASE_AFTER_DAYS", 30)      # срок обезличивания после отзыва согласия

# --- Справочники (defence-in-depth; значения совпадают со схемой/ботом) ---
# status валидируется против STATUSES в хендлере UPDATE (§2 плана).
STATUSES = ("new", "guide_sent", "nurturing", "converted", "lost")
SOURCES = ("reels", "dzen", "youtube", "vk", "max", "other")
MESSENGERS = ("tg", "max")

# Человекочитаемые подписи для UI (бейджи, фильтры, дашборд).
STATUS_LABELS = {
    "new": "Новый",
    "guide_sent": "Гайд выдан",
    "nurturing": "Прогрев",
    "converted": "Купил",
    "lost": "Потерян",
}
SOURCE_LABELS = {
    "reels": "Reels",
    "dzen": "Дзен",
    "youtube": "YouTube",
    "vk": "VK",
    "max": "MAX",
    "other": "Другое",
}
MESSENGER_LABELS = {"tg": "Telegram", "max": "MAX"}

# ── Платежи / заказы (раздел «Платежи», schema_orders.sql) ───────────────────
# Статусы заказа — синхронны CHECK orders_status_chk. Валюты переиспользуем из
# каталога (PRODUCT_CURRENCIES/SIGNS выше). Источник оплаты: manual в 1A; в 1B
# добавятся yookassa/telegram_stars (их пишет бот из вебхука).
ORDER_STATUSES = ("paid", "pending", "refunded", "failed")
ORDER_STATUS_LABELS = {
    "paid": "Оплачен",
    "pending": "Ожидает",
    "refunded": "Возврат",
    "failed": "Не прошёл",
}
ORDER_SOURCES = ("manual", "yookassa", "telegram_stars")
ORDER_SOURCE_LABELS = {
    "manual": "Вручную",
    "yookassa": "ЮKassa",
    "telegram_stars": "Telegram Stars",
}
# Статусы, которые оператор может выставить руками в 1A (refund/возврат + правка).
ORDER_STATUSES_MANUAL = ("paid", "pending", "refunded", "failed")
ORDER_NOTE_MAX_LEN = 500
# Потолок суммы заказа (defence-in-depth поверх numeric(12,2)): целая часть ≤ 10 цифр.
ORDER_AMOUNT_MAX = 10_000_000_000

# ── Биллинг сервиса (раздел «Подписка», schema_service.sql) ──────────────────
# B2B-абонентка школа→агентство по ТАРИФАМ (модель НЕЙРОАГЕНТОВ): у каждого тарифа
# квота сообщений ИИ/период + цена сверх квоты (overage). Метрика = сообщения,
# сгенерированные ИИ (messages.source='liya'). Превышение доначисляется к следующему
# счёту. Тарифы заданы здесь (агентство правит код; школа-оператор не меняет).
SERVICE_PLAN_PERIOD_DAYS = _opt_int("SERVICE_PLAN_PERIOD_DAYS", 30)  # длина периода, дней
SERVICE_CURRENCY = "RUB"

# Каталог тарифов. price/overage — в рублях (int/float); quota — сообщений ИИ/период.
# payable=False (Индивидуальный) → не оплачивается онлайн, ведёт на заявку (SERVICE_CONTACT_URL).
# price_display/subtitle/features — маркетинговые строки карточки (как на скринах).
SERVICE_PLANS = {
    "econom": {
        "name": "Эконом", "payable": True, "price": 3750, "quota": 500, "overage": 7.5,
        "price_display": "2 250 ₽ / 3 750 ₽",
        "subtitle": "Два тарифа для небольших компаний",
        "features": ["300 / 500 сообщений ИИ", "7,5 ₽ за сообщение сверх тарифа",
                     "Без ограничений по количеству каналов"],
    },
    "start": {
        "name": "Стартовый", "payable": True, "price": 7500, "quota": 1500, "overage": 5,
        "price_display": "7 500 ₽ в месяц",
        "subtitle": "Для тех, кто хочет автоматизировать общение с клиентами",
        "features": ["1500 сообщений ИИ в месяц", "5 ₽ за дополнительное сообщение ИИ"],
    },
    "custom": {
        "name": "Индивидуальный", "payable": False, "price": None, "quota": None, "overage": None,
        "price_display": "Цена договорная",
        "subtitle": "Для предприятий: автоматизировать коммуникации, снизить затраты на поддержку и увеличить продажи",
        "features": ["Приоритетная поддержка", "Неограниченное дообучение ИИ", "Личный менеджер",
                     "Стоимость сообщения от 1 ₽", "От 6000 сообщений в месяц"],
        "cta": "Оставить заявку",
    },
}
SERVICE_PLAN_ORDER = ("econom", "start", "custom")
SERVICE_PLAN_KEYS = tuple(SERVICE_PLANS.keys())
# Куда ведёт «Оставить заявку» / «Связаться с техподдержкой» (опц.; пусто → mailto оператора).
SERVICE_CONTACT_URL = os.environ.get("SERVICE_CONTACT_URL", "")

SERVICE_INVOICE_STATUSES = ("pending", "paid", "canceled")
SERVICE_INVOICE_STATUS_LABELS = {
    "pending": "Ожидает оплаты",
    "paid": "Оплачен",
    "canceled": "Отменён",
}
# app_settings-ключ флага отмены подписки (панель пишет, бот не использует).
SERVICE_CANCEL_SETTING_KEY = "service_subscription_canceled"

# ЮKassa (онлайн-оплата подписки). Секреты — ТОЛЬКО env (shopId + secretKey из ЛК
# ЮKassa). Нет ключей → онлайн-оплата выключена (кнопка «Оплатить» неактивна).
YOOKASSA_SHOP_ID = os.environ.get("YOOKASSA_SHOP_ID", "")
YOOKASSA_SECRET_KEY = os.environ.get("YOOKASSA_SECRET_KEY", "")
YOOKASSA_API_BASE = os.environ.get("YOOKASSA_API_BASE", "https://api.yookassa.ru/v3")
YOOKASSA_ENABLED = bool(YOOKASSA_SHOP_ID and YOOKASSA_SECRET_KEY)

# ── ИИ-ассистент Лия (раздел «ИИ-агенты») — настройки в app_settings ──────────
# Панель ПИШЕТ ключи, бот ЧИТАЕТ их ПОВЕРХ env (bot-telegram/ai.py). Управление БЕЗ
# редеплоя: тумблер авто-ответов, id агента Timeweb, текст фолбэка. Токен Timeweb AI
# НИКОГДА не в app_settings/БД (секрет) — остаётся в env бота; панель его не видит и
# не хранит. Дефолты ниже — для показа «эффективного» значения, когда строки ещё нет.
AI_ENABLED_SETTING_KEY = "ai_enabled"         # ""/"1": глобальный тумблер авто-ответов Лии
AI_AGENT_ID_SETTING_KEY = "ai_agent_id"       # переопределяет env AGENT_ID бота
AI_FALLBACK_SETTING_KEY = "ai_fallback_text"  # переопределяет хардкод-фолбэк бота
# Дефолт фолбэка ДОЛЖЕН совпадать с bot-telegram/ai.py::_FALLBACK (показываем как
# «эффективный», если своя строка не задана). Меняешь тут — поправь и там.
AI_DEFAULT_FALLBACK = (
    "Ой, сейчас не получается ответить 🌷\n"
    "Напиши, пожалуйста, менеджеру: lesovschool@yandex.ru"
)
AI_AGENT_ID_MAX = 200   # потолок длины agent_id (UUID-подобный идентификатор Timeweb)
AI_FALLBACK_MAX = 600   # потолок длины своего фолбэка
AI_ACTIVITY_WINDOW_DAYS = _opt_int("AI_ACTIVITY_WINDOW_DAYS", 30)  # окно метрики «ответов Лии»

# Бэкенд ИИ: какой движок отвечает клиентам.
#  • cloud_ai — агент Timeweb (Лия): нативный /call по AGENT_ID, есть RAG/MCP, контекст
#    хранится на сервере (parent_message_id). Системный промпт настраивается на агенте.
#  • gateway  — Timeweb AI Gateway: ПРЯМАЯ работа с моделью (DeepSeek и др.) по OpenAI-
#    совместимому /v1/chat/completions, БЕЗ агента/RAG. Системный промпт задаётся здесь.
#    Ключ Gateway (отдельный, не аккаунт-токен) — ТОЛЬКО в env бота (AI_GATEWAY_TOKEN).
AI_BACKEND_SETTING_KEY = "ai_backend"               # "cloud_ai" | "gateway"
AI_MODEL_SETTING_KEY = "ai_model"                   # ID модели для gateway, напр. deepseek-v4-pro
AI_GATEWAY_URL_SETTING_KEY = "ai_gateway_base_url"  # base URL OpenAI-совместимого шлюза
AI_SYSTEM_PROMPT_SETTING_KEY = "ai_system_prompt"   # системный промпт (работает для gateway)

AI_BACKENDS = {
    "cloud_ai": "Агент Timeweb (Лия)",
    "gateway": "AI Gateway · DeepSeek",
}
AI_BACKEND_ORDER = ("cloud_ai", "gateway")
AI_DEFAULT_BACKEND = "cloud_ai"   # дефолт сохраняет текущее поведение (бот жив)
# Дефолты gateway. Точный base URL и ID моделей — из ЛК AI Gateway (вкладки «API-ключи»/модели);
# «DeepSeek V4 Pro» обычно deepseek-v4-pro. Меняется в панели без редеплоя.
AI_DEFAULT_GATEWAY_URL = "https://api.timeweb.ai/v1"
AI_DEFAULT_MODEL = "deepseek-v4-pro"
AI_MODEL_MAX = 100
AI_GATEWAY_URL_MAX = 300
AI_SYSTEM_PROMPT_MAX = 4000
