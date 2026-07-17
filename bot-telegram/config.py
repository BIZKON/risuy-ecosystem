"""Конфигурация Telegram-бота. Всё из переменных окружения — секреты не в коде."""
import logging
import os


def _req(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(f"Не задана обязательная переменная окружения: {name}")
    return val


BOT_TOKEN = _req("BOT_TOKEN")
DATABASE_URL = _req("DATABASE_URL")

# Канал для гейта подписки. Бот ДОЛЖЕН быть админом канала, иначе проверка не сработает.
# Надёжнее числовой id вида -100xxxxxxxxxx (работает и для приватных каналов), чем @username.
# Числовую строку приводим к int, чтобы get_chat_member получил корректный chat_id.
_channel_id_raw = _req("CHANNEL_ID")
CHANNEL_ID = int(_channel_id_raw) if _channel_id_raw.lstrip("-").isdigit() else _channel_id_raw
# Ссылка для подписки, которую видит пользователь, напр. https://t.me/risuysdushoy
CHANNEL_URL = _req("CHANNEL_URL")

# Ссылка на гайд (GetCourse) — то, что бот отдаёт после прохождения воронки.
GUIDE_URL = _req("GUIDE_URL")

# Ссылка на политику обработки ПДн (необязательно, но желательно для 152-ФЗ).
PRIVACY_URL = os.environ.get("PRIVACY_URL", "")

# Имя оператора для текста согласия «Клуба предпринимателей» (152-ФЗ ст.9 + ФЗ-38).
# Подставляется в shared.club.build_club_consent_text(...) ТОЛЬКО как фолбэк, если
# funnel-config тенанта (панель, «Интеграции», operator_name) не задан — приоритет у
# него (см. handlers._club_start). Дефолт — пустая строка: реальное юрлицо тенанта
# нельзя захардкодить фейком, "оператор" в build_club_consent_text подставит handlers.
CLUB_OPERATOR_NAME = os.environ.get("CLUB_OPERATOR_NAME", "")

# file_id видео-кружка Насти (необязательно). Записывается заранее, см. README.
VIDEO_NOTE_FILE_ID = os.environ.get("VIDEO_NOTE_FILE_ID", "")

# Прокси для Telegram API: нужен, если api.telegram.org недоступен напрямую
# из дата-центра (РФ-хостинг часто не достукивается до Telegram).
# Формат: socks5://user:pass@host:port или http://user:pass@host:port.
# Пусто = подключаться к Telegram напрямую (поведение по умолчанию).
TELEGRAM_PROXY = os.environ.get("TELEGRAM_PROXY", "")

# AI-ассистент Лия — агент Timeweb Cloud (cloud-ai). Без этих значений AI-ответы
# отключены (бот отдаёт мягкий фолбэк). Только из env, в репозиторий не коммитим.
AGENT_ID = os.environ.get("AGENT_ID", "")
TIMEWEB_AI_TOKEN = os.environ.get("TIMEWEB_AI_TOKEN", "")

# Ключ Timeweb AI Gateway (OpenAI-совместимый шлюз к моделям, напр. DeepSeek). ОТДЕЛЬНЫЙ
# ключ из ЛК AI Gateway (вкладка «API-ключи»), НЕ аккаунт-токен и НЕ ключ агента. Нужен,
# только если бэкенд ИИ переключён на «gateway» в панели (app_settings['ai_backend']).
# base URL и модель берутся из app_settings (панель), ключ — секрет, только из env.
AI_GATEWAY_TOKEN = os.environ.get("AI_GATEWAY_TOKEN", "")

# ── Wave 5: промпт ИИ-сотрудника из ПАНЕЛИ через OpenAI-совместимый эндпоинт агента ──
# База OpenAI-совместимого API cloud-ai агента (ОТДЕЛЬНЫЙ хост от management-API!).
# Нативный /call (api.timeweb.cloud) НЕ принимает промпт per-request — берёт его жёстко
# из настроек агента. OpenAI-эндпоинт /cloud-ai/agents/{id}/v1/chat/completions ПРИНИМАЕТ
# messages[] с role:"system" и переопределяет промпт В КАЖДОМ запросе → промпт из панели
# (app_settings) доезжает до Лии без PATCH/редеплоя. Док Timeweb: хост agent.timeweb.cloud
# (тот же токен TIMEWEB_AI_TOKEN). На жёсткий сбой ai.ask_ai фолбэчит на нативный /call
# (Школа не молчит, §8.7) — поэтому смена хоста env-настраиваема без правки кода.
TIMEWEB_AI_OPENAI_BASE = os.environ.get(
    "TIMEWEB_AI_OPENAI_BASE", "https://agent.timeweb.cloud/api/v1"
)

# ── RF-RAG: эмбеддер TEI (self-host, intfloat/multilingual-e5-base, 768-dim) ──
# URL нашего TEI-сервиса на VM (напр. http://10.0.0.5:8080 во внутренней сети или
# http://<vm-ip>:8080 за фаерволом). Пусто → RAG выключен полностью: retrieval не
# выполняется, бот отвечает как раньше (см. kb.py — гейт). EMBEDDER_TOKEN — опц. Bearer,
# если TEI закрыт reverse-proxy с проверкой токена (у самого TEI auth нет). Только из env.
EMBEDDER_URL = os.environ.get("EMBEDDER_URL", "")
EMBEDDER_TOKEN = os.environ.get("EMBEDDER_TOKEN", "")

# ── Онлайн-оплата продаж школы — ЮKassa, МАГАЗИН ШКОЛЫ (Phase 1B) ────────────
# ОТДЕЛЬНАЯ пара ключей от магазина подписки в панели (деньги лидов идут ШКОЛЕ,
# биллинг сервиса — агентству; это разные магазины ЮKassa). Бот создаёт платёж по
# клику «Купить»; подтверждение ловит вебхук ПАНЕЛИ (единый URL в ЛК обоих магазинов).
# Ключи не заданы → онлайн-оплата выключена: кнопка «Купить» не показывается,
# «счёт из диалога» в панели недоступен. Те же ключи добавляются и в env панели
# (перепроверка платежа в вебхуке). Вписывает ВЛАДЕЛЕЦ через twc-set-env.sh.
SHOP_YOOKASSA_SHOP_ID = os.environ.get("SHOP_YOOKASSA_SHOP_ID", "")
SHOP_YOOKASSA_SECRET_KEY = os.environ.get("SHOP_YOOKASSA_SECRET_KEY", "")
SHOP_PAYMENTS_CONFIGURED = bool(SHOP_YOOKASSA_SHOP_ID and SHOP_YOOKASSA_SECRET_KEY)
YOOKASSA_API_BASE = os.environ.get("YOOKASSA_API_BASE", "https://api.yookassa.ru/v3")
# Чек 54-ФЗ: включать, если у боевого магазина школы включена фискализация (иначе
# create_payment без receipt отвергается). Дефолт ВЫКЛ. vat_code 1 = без НДС (УСН/НПД).
SHOP_RECEIPT_ENABLED = os.environ.get("SHOP_RECEIPT_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}
SHOP_VAT_CODE = int(os.environ.get("SHOP_VAT_CODE", "1"))
# TTL переиспользования pending-заказа при повторном клике «Купить», минуты: та же
# ссылка на оплату вместо нового платежа (анти-двойное списание). Платёж ЮKassa
# живёт ~1 час — TTL держим заметно меньше.
ORDER_REUSE_MINUTES = int(os.environ.get("ORDER_REUSE_MINUTES", "30"))
# Просроченные pending-заказы онлайн-оплаты → failed (часов; чистит retention-цикл).
ORDER_STALE_HOURS = int(os.environ.get("ORDER_STALE_HOURS", "24"))

# ── Метеринг (Wave 3, ТЗ §5.2) ────────────────────────────────────────────────
def _env_int(name: str, default: int, *, minimum: int | None = None) -> int:
    """int из env с фолбэком на дефолт при мусоре (не валим импорт → нет crash-loop)."""
    raw = (os.environ.get(name) or "").strip()
    try:
        val = int(raw) if raw else default
    except ValueError:
        logging.getLogger(__name__).warning(
            "%s=%r не int — беру дефолт %s", name, raw, default)
        val = default
    return max(val, minimum) if minimum is not None else val


def _env_float(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").strip().replace(",", ".")
    try:
        return float(raw) if raw else default
    except ValueError:
        logging.getLogger(__name__).warning(
            "%s=%r не float — беру дефолт %s", name, raw, default)
        return default


# Тенант env-бота (Школа): его tenant_id резолвится по slug при старте и пишется
# во все вставки tenant-scoped таблиц явно. DEFAULT в БД на этих колонках пока
# стоит (переходник, DECISIONS п.12) и СНИМЕТСЯ отдельной миграцией Wave 3 (3d).
DEFAULT_TENANT_SLUG = os.environ.get("DEFAULT_TENANT_SLUG", "lesov-school")
# Период снапшот-воркера used_tokens, сек (зажат снизу 30: METERING_INTERVAL=0
# дал бы горячий цикл — молотьба БД и Timeweb API). Дельта списывается
# идемпотентно — частота влияет только на гранулярность строк леджера.
METERING_INTERVAL = _env_int("METERING_INTERVAL", 300, minimum=30)
# Доля выходных токенов в смешанной цене (used_tokens не делит вход/выход).
# 0.5 — прод-факт для thinking-моделей (DECISIONS п.5). ⚠️ Тот же ключ есть в env
# панели (блок «Экономика сервиса»); держите ОБА значения равными, иначе
# себестоимость метеринга разойдётся с цифрой маржи в панели.
AI_OUT_TOKENS_SHARE = _env_float("AI_OUT_TOKENS_SHARE", 0.5)
# База management-API Timeweb (снапшоты used_tokens агентов; тот же токен TIMEWEB_AI_TOKEN).
TIMEWEB_API_BASE = os.environ.get("TIMEWEB_API_BASE", "https://api.timeweb.cloud/api/v1")

# Wave 5: сколько последних сообщений диалога подмешивать как историю в OpenAI-эндпоинт
# агента (контекст диалога, который раньше держал серверный parent_message_id нативного
# /call). 0 → без истории (только system-промпт + текущий вопрос). Зажат снизу 0.
AI_HISTORY_MESSAGES = _env_int("AI_HISTORY_MESSAGES", 10, minimum=0)

# СП-2-память: суммаризировать диалог каждые N входящих (0 → выключить запись памяти);
# top-k сводок и порог косинусной дистанции при ретриве памяти.
MEMORY_SUMMARIZE_EVERY = _env_int("MEMORY_SUMMARIZE_EVERY", 10, minimum=0)
MEMORY_TOP_K = _env_int("MEMORY_TOP_K", 3, minimum=1)
MEMORY_MAX_DISTANCE = float(os.environ.get("MEMORY_MAX_DISTANCE", "0.55"))

# Порт для health-эндпоинта (Timeweb App Platform проксирует сюда). Бот работает на long-polling.
PORT = int(os.environ.get("PORT", "8080"))

# Тайминги прогрева в секундах от момента выдачи гайда.
FOLLOW_UP_DELAYS = [
    int(os.environ.get("FOLLOW_UP_1_DELAY", str(60 * 60))),          # +1 час
    int(os.environ.get("FOLLOW_UP_2_DELAY", str(60 * 60 * 24))),     # +1 день
    int(os.environ.get("FOLLOW_UP_3_DELAY", str(60 * 60 * 24 * 3))), # +3 дня
]

# ── Очередь и рассылки (worker.run) ──────────────────────────────────────────
# Единый process-wide rate-limit на ВЕСЬ исходящий трафик одного Bot (воронка,
# Лия, прогрев, outbox, рассылки) — общий лимит Telegram ~30/с. Берём 15/с с
# headroom под интерактив; рассылка уступает приоритет интерактиву (§5.3 плана).
BROADCAST_RATE = float(os.environ.get("BROADCAST_RATE", "15"))   # msg/s, token-bucket
BROADCAST_BATCH = int(os.environ.get("BROADCAST_BATCH", "50"))   # claim-батч получателей за тик
OUTBOX_BATCH = int(os.environ.get("OUTBOX_BATCH", "20"))         # claim-батч точечных ответов за тик
MAX_SEND_ATTEMPTS = int(os.environ.get("MAX_SEND_ATTEMPTS", "3"))   # потолок попыток на получателя рассылки
OUTBOX_MAX_ATTEMPTS = int(os.environ.get("OUTBOX_MAX_ATTEMPTS", "10"))  # потолок попыток на запись outbox
OUTBOX_MAX_AGE_HOURS = int(os.environ.get("OUTBOX_MAX_AGE_HOURS", "24"))  # старше — в failed (не висеть вечно)
# Reclaim застрявших 'sending' (краш/редеплой). 10 мин > rolling-overlap Timeweb (§5.5).
RECLAIM_AFTER_SECONDS = int(os.environ.get("RECLAIM_AFTER_SECONDS", str(60 * 10)))
WORKER_INTERVAL = int(os.environ.get("WORKER_INTERVAL", "5"))     # период цикла воркера, сек
# Circuit-breaker молодого бота: при доле failed среди первых N > порога — авто-пауза (§5.9).
CB_MIN_SAMPLE = int(os.environ.get("CB_MIN_SAMPLE", "20"))       # сколько отправок накопить перед оценкой
CB_FAIL_RATIO = float(os.environ.get("CB_FAIL_RATIO", "0.30"))   # доля failed для авто-паузы

# Служебный чат для первичной заливки файла рассылки (НЕ первый получатель, §5.6).
# Тот же чат используется для первичной заливки файла ПРОДУКТА (каталог оферов):
# бот заливает products.file ОДИН раз сюда → products.file_tg_id, дальше переиспользует.
# Гарантированно доступный chat_id (админ/служебная группа). Пусто = файловые
# рассылки/продукты не материализуются (текстовые/ссылочные работают).
OPS_CHAT_ID_raw = os.environ.get("OPS_CHAT_ID", "")
OPS_CHAT_ID = int(OPS_CHAT_ID_raw) if OPS_CHAT_ID_raw.lstrip("-").isdigit() else (OPS_CHAT_ID_raw or None)

# ── A3: эскалация горячего лида менеджерам (карточка в ТГ-группу/тему) ──────────
# Лия в ответе ставит маркер [[ESCALATE]]{json}[[/ESCALATE]] (после квалификации/по запросу) →
# бот вырезает маркер из ответа клиенту и постит карточку лида в группу менеджеров (опц. в тему
# форума MANAGER_TOPIC_ID). Бот ДОЛЖЕН быть участником/админом группы. Пусто → эскалация OFF
# (маркер всё равно вырезается, чтобы клиент его не видел). Дедуп — одна карточка на лид.
MANAGER_GROUP_ID_raw = os.environ.get("MANAGER_GROUP_ID", "")
MANAGER_GROUP_ID = int(MANAGER_GROUP_ID_raw) if MANAGER_GROUP_ID_raw.lstrip("-").isdigit() else None
MANAGER_TOPIC_ID_raw = os.environ.get("MANAGER_TOPIC_ID", "")
MANAGER_TOPIC_ID = int(MANAGER_TOPIC_ID_raw) if MANAGER_TOPIC_ID_raw.isdigit() else None
MANAGER_ESCALATION_ENABLED = MANAGER_GROUP_ID is not None
# Единый сервис-бот-уведомитель (Слой B): ОДИН бот на платформу постит карточки эскалаций/
# триггеров в группы менеджеров клиентов (клиент добавляет ЕГО в свою группу + вставляет id).
# Пусто → фолбэк на разговорный бот тенанта (текущая эскалация Школы не ломается до провижининга).
# Создаётся владельцем у BotFather, кладётся в env (twc-set-env.sh 201859 NOTIFIER_BOT_TOKEN=…).
NOTIFIER_BOT_TOKEN = os.environ.get("NOTIFIER_BOT_TOKEN", "").strip()
# Базовый URL админ-панели — для ссылки «открыть диалог и ответить» в карточке эскалации
# ({PANEL_BASE_URL}/dialogs/<lead_id>). Пусто → панель-ссылку в карточку не кладём (остаётся
# только прямой tg://user). Без завершающего слэша.
PANEL_BASE_URL = os.environ.get("PANEL_BASE_URL", "").rstrip("/")


# ── ГРАНИЦА РАССЫЛОК: канал гейта НИКОГДА не получает контент рассылок ─────────
# Канал (@lesov_art_school) бот использует ТОЛЬКО для проверки подписки нового
# человека — это даёт нативный рост канала. Любые рассылки (текст/ссылка/файл/
# продукт) идут ИСКЛЮЧИТЕЛЬНО в личные чаты лидов:
#   • адресаты материализуются строго `select … from leads` (db._AUDIENCE_WHERE) —
#     канал в leads не лежит, личный tg_user_id > 0, id канала = -100… → коллизия
#     адресата невозможна структурно;
#   • единственная точка, где бот шлёт контент в НЕ-лид-чат, — первичная заливка
#     файла в OPS_CHAT_ID ради переиспользуемого file_id. Если оператор по ошибке
#     укажет туда канал гейта, файл засветился бы в канале. Запрещаем инвариантом:
#     при совпадении OPS_CHAT_ID с каналом (по числовому id ИЛИ @username) —
#     файловую заливку ОТКЛЮЧАЕМ (OPS_CHAT_ID=None) и громко логируем. Текстовые/
#     ссылочные рассылки при этом продолжают работать. OPS_CHAT_ID может быть
#     личным чатом оператора с ботом ИЛИ приватной группой — но не каналом гейта.
def _channel_aliases(channel_id, channel_url) -> set:
    """Все идентификаторы канала гейта: числовой id и @username (нижний регистр)."""
    aliases: set = set()
    if isinstance(channel_id, int):
        aliases.add(channel_id)
    elif isinstance(channel_id, str) and channel_id:
        aliases.add(channel_id.lstrip("@").lower())
    if channel_url and "t.me/" in channel_url:
        uname = channel_url.rstrip("/").rsplit("/", 1)[-1].lstrip("@").lower()
        if uname:
            aliases.add(uname)
    return aliases


def is_gate_channel(chat, channel_id=None, channel_url=None) -> bool:
    """True, если chat указывает на канал гейта (по числовому id или @username).

    Граница рассылок: канал гейта не может быть адресатом/служебным чатом рассылок.
    channel_id/channel_url по умолчанию берутся из модуля (передаются явно в тестах).
    """
    if chat is None:
        return False
    cid = CHANNEL_ID if channel_id is None else channel_id
    curl = CHANNEL_URL if channel_url is None else channel_url
    aliases = _channel_aliases(cid, curl)
    if isinstance(chat, int):
        return chat in aliases
    return chat.lstrip("@").lower() in aliases


if OPS_CHAT_ID is not None and is_gate_channel(OPS_CHAT_ID):
    logging.getLogger(__name__).error(
        "OPS_CHAT_ID совпадает с каналом гейта — файловая заливка ОТКЛЮЧЕНА: "
        "рассылки не должны светиться в канале (он только для проверки подписки). "
        "Укажи ЛИЧНЫЙ chat_id оператора с ботом или ПРИВАТНУЮ группу."
    )
    OPS_CHAT_ID = None

# ── Каталог продуктов (оферов) ────────────────────────────────────────────────
# Лимит файла продукта = жёсткий потолок Telegram Bot API на отправку (50 МБ).
# Панель валидирует размер/тип файла ДО записи байтов в products.file (расширение+
# MIME+magic-byte, отказ исполняемым) — здесь дублируем верхнюю границу как защиту
# на стороне бота перед заливкой в OPS_CHAT_ID (битый/слишком большой файл не валит
# воркер, продукт просто не получает file_tg_id и логируется). Меняется редко.
MAX_PRODUCT_FILE_MB = int(os.environ.get("MAX_PRODUCT_FILE_MB", "50"))
MAX_PRODUCT_FILE_BYTES = MAX_PRODUCT_FILE_MB * 1024 * 1024
# Сколько продуктов-офферов заливать за один тик воркера (обычно их единицы — каталог
# мал, заливка однократна; батч держим маленьким, чтобы не занимать bucket надолго).
PRODUCT_UPLOAD_BATCH = int(os.environ.get("PRODUCT_UPLOAD_BATCH", "5"))
# Потолок попыток заливки файла офера (симметрично OUTBOX_MAX_ATTEMPTS/MAX_SEND_ATTEMPTS):
# валидный-по-magic, но отвергаемый Telegram файл не должен переселектироваться вечно
# каждым тиком (5с), тратя токен бакета и засоряя OPS_CHAT_ID. После N неудач офер
# выпадает из очереди заливки (products.upload_attempts >= лимит), file_tg_id остаётся
# null → рассылка-продукт с таким файлом уйдёт на паузу (см. worker._prepare_product_broadcast).
PRODUCT_UPLOAD_MAX_ATTEMPTS = int(os.environ.get("PRODUCT_UPLOAD_MAX_ATTEMPTS", "5"))

# ── Вложения в личный ответ оператора лиду (outbox-заливка) ───────────────────
# Паттерн клонирован с продуктовой заливки: панель кладёт байты в outbox.file_bytes,
# воркёр (_drain_outbox_uploads ДО _drain_outbox) льёт их в OPS_CHAT_ID и проставляет
# file_id, после чего штатный _drain_outbox шлёт лиду по file_id. Кэп размера —
# существующий MAX_PRODUCT_FILE_BYTES (тот же потолок Telegram 50 МБ, не дублируем).
# Сколько вложений заливать за один тик воркера (личных ответов с файлом обычно единицы;
# батч держим маленьким, чтобы не занимать bucket надолго — как у продуктов).
OUTBOX_UPLOAD_BATCH = int(os.environ.get("OUTBOX_UPLOAD_BATCH", "5"))
# Потолок попыток заливки вложения (симметрично PRODUCT_UPLOAD_MAX_ATTEMPTS): валидный-по-
# magic, но отвергаемый Telegram файл не должен переселектироваться вечно каждым тиком (5с),
# тратя токен бакета и засоряя OPS_CHAT_ID. После N неудач исходящее выпадает из очереди
# заливки (outbox.upload_attempts >= лимит), file_id остаётся null → личный ответ с этим
# вложением на паузе (текстовая часть/иные ответы не затронуты — см. worker._drain_outbox).
OUTBOX_UPLOAD_MAX_ATTEMPTS = int(os.environ.get("OUTBOX_UPLOAD_MAX_ATTEMPTS", "5"))
# Путь к ffmpeg для транскода голосового (запись с микрофона → ogg/opus voice). Если ffmpeg
# упал/отсутствует — воркёр откатывает kind='voice' → 'audio' и шлёт исходник как файл.
FFMPEG_BIN = os.environ.get("FFMPEG_BIN", "ffmpeg")
# Таймаут одного запуска ffmpeg-транскода голосового, сек (короткий клип; не вешаем тик воркера).
VOICE_TRANSCODE_TIMEOUT = int(os.environ.get("VOICE_TRANSCODE_TIMEOUT", "15"))

# Публичный базовый URL БОТА (его поддомен на Timeweb) — на нём живёт трекинг-редирект
# /r/<token>. Ссылка {link} в рассылке строится как BOT_PUBLIC_BASE_URL + /r/<click_token>.
# Пусто = плейсхолдер {link} не подставляется (рассылка без трекинга всё равно идёт).
BOT_PUBLIC_BASE_URL = os.environ.get("BOT_PUBLIC_BASE_URL", "").rstrip("/")

# TTL абсолютной чистки текста переписки (самый объёмный ПДн-поток), дни. §6.4.
MESSAGES_TTL_DAYS = int(os.environ.get("MESSAGES_TTL_DAYS", "90"))
# Срок обезличивания по отзыву согласия (erase_requested_at + N дней). Совпадает с панелью/privacy.
ERASE_AFTER_DAYS = int(os.environ.get("ERASE_AFTER_DAYS", "30"))
# Период цикла retention-обезличивания, сек (раз в час достаточно).
RETENTION_INTERVAL = int(os.environ.get("RETENTION_INTERVAL", str(60 * 60)))

# Партнёрский реф-поток: анти-абьюз (лёгкий гард).
REF_RATELIMIT_HOURS = int(os.environ.get("REF_RATELIMIT_HOURS", "24"))
REF_RATELIMIT_MAX = int(os.environ.get("REF_RATELIMIT_MAX", "3"))
