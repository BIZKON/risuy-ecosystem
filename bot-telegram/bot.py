"""Точка входа Telegram-бота.

Бот работает на long-polling. Рядом поднимаем крошечный HTTP-сервер на $PORT — его ждёт
Timeweb App Platform (проксирует 80/443 на порт контейнера). На том же сервере живёт
публичный трекинг-редирект /r/<token> (клик по ссылке рассылки → лог + 302 на target_url).

Фоновые таски рядом с polling (все по образцу nurture.run, прогресс в БД → переживают редеплой):
  • nurture.run    — прогрев (не трогаем; +2 фильтра в его SQL).
  • worker.run     — дренаж outbox (точечные ответы оператора) + исполнение рассылок.
  • retention.run  — обезличивание ПДн по отзыву согласия + TTL переписки (152-ФЗ).
"""
import asyncio
import logging
import re
import time
from urllib.parse import urlparse

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.types import BotCommand
from aiohttp import web

import config
import db
import nurture
import retention
import worker
from handlers import router
from messaging import LoggingMiddleware

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("bot")

# ── Трекинг-редирект /r/<token> ──────────────────────────────────────────────
# Токен — secrets.token_urlsafe(16) → алфавит [A-Za-z0-9_-]. Валидируем ДО любого SELECT:
# мусор / %00 / гигантская строка → 404 без обращения к БД.
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
_ALLOWED_SCHEMES = {"http", "https"}

# Лёгкий in-memory per-IP rate-limit от флуда клик-логов превью-ботами/сканерами
# (single-instance — этого достаточно; редирект всё равно отдаём, режем только запись лога).
_RL_WINDOW = 10.0          # окно, сек
_RL_MAX = 30               # макс. кликов с одного IP за окно (для записи лога)
_rl_hits: dict[str, list[float]] = {}


def _safe_target(url: str) -> bool:
    """Defence-in-depth (§6.3): пускаем ТОЛЬКО http/https с непустым host и без protocol-relative.

    Дублирует allow-list, который панель применяет на записи. target_url берётся ИЗ БД,
    query клиента игнорируется. javascript:/data:/file: и '//host' отвергаются.
    """
    if not url or url.startswith("//"):
        return False
    try:
        p = urlparse(url)
    except Exception:  # noqa: BLE001
        return False
    return p.scheme in _ALLOWED_SCHEMES and bool(p.netloc)


def _rl_allow_log(ip: str | None) -> bool:
    """True, если клик с этого IP можно залогировать (не превышен лимит окна)."""
    if not ip:
        return True
    now = time.monotonic()
    hits = [t for t in _rl_hits.get(ip, ()) if now - t < _RL_WINDOW]
    if len(hits) >= _RL_MAX:
        _rl_hits[ip] = hits
        return False
    hits.append(now)
    _rl_hits[ip] = hits
    # Гигиена памяти: периодически чистим протухшие ключи (дёшево, без отдельной таски).
    if len(_rl_hits) > 10000:
        for k in list(_rl_hits.keys()):
            if all(now - t >= _RL_WINDOW for t in _rl_hits[k]):
                _rl_hits.pop(k, None)
    return True


def _sec_headers(resp: web.StreamResponse) -> web.StreamResponse:
    """Ручные заголовки на 302/404 (голый aiohttp-сервер их сам не ставит)."""
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


async def _redirect(request: web.Request) -> web.StreamResponse:
    """Публичный GET /r/{token}: лог клика fire-and-forget → 302 на target_url из БД.

    Всё в try/except: при любой ошибке отдаём 404 (или редирект, если уже знаем target).
    В лог пишем только префикс токена, БЕЗ сырого UA/IP в текст. Редирект важнее лога:
    при недоступности пула всё равно отдаём 302.
    """
    token = request.match_info.get("token", "")
    if not _TOKEN_RE.match(token):
        return _sec_headers(web.Response(status=404, text="not found"))

    try:
        row = await db.get_link_token(token)
    except Exception:  # noqa: BLE001 — БД недоступна
        logger.warning("/r: ошибка чтения токена %s…", token[:6], exc_info=True)
        return _sec_headers(web.Response(status=404, text="not found"))

    if row is None:
        return _sec_headers(web.Response(status=404, text="not found"))

    target = row["target_url"]
    # Повторная проверка target ПЕРЕД редиректом (не доверяем «панель проверила», §6.3).
    if not _safe_target(target):
        logger.error("/r: небезопасный target у токена %s… — инцидент", token[:6])
        return _sec_headers(web.Response(status=404, text="not found"))

    # Лог клика — fire-and-forget с коротким таймаутом; per-IP rate-limit от флуда.
    ip = _client_ip(request)
    if _rl_allow_log(ip):
        ua = request.headers.get("User-Agent")
        asyncio.create_task(_log_click_safe(token, row["broadcast_id"], row["lead_id"], ua, ip))

    return _sec_headers(web.HTTPFound(target))


def _client_ip(request: web.Request) -> str | None:
    """Best-effort IP за прокси Timeweb (X-Forwarded-For), advisory — не security-контроль."""
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        return xff.split(",")[0].strip()[:64]
    peer = request.remote
    return peer[:64] if peer else None


async def _log_click_safe(token, broadcast_id, lead_id, ua, ip) -> None:
    """Обёртка лога клика: короткий таймаут, никогда не бросает (редирект уже отдан)."""
    try:
        await asyncio.wait_for(
            db.log_link_click(token, broadcast_id, lead_id, ua, ip), timeout=3.0
        )
    except Exception:  # noqa: BLE001
        logger.warning("/r: клик не залогирован (token=%s…)", str(token)[:6])


async def _health(_request: web.Request) -> web.Response:
    return web.Response(text="ok")


async def _start_health() -> web.AppRunner:
    app = web.Application()
    app.router.add_get("/", _health)
    app.router.add_get("/health", _health)
    app.router.add_get("/r/{token}", _redirect)  # публичный трекинг-редирект (§6.2)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", config.PORT)
    await site.start()
    logger.info("HTTP-сервер (health + /r) на порту %s", config.PORT)
    return runner


async def main() -> None:
    await db.init()
    health = await _start_health()

    if config.TELEGRAM_PROXY:
        # Прячем креды прокси в логе — печатаем только host:port.
        logger.info("Telegram через прокси: %s", config.TELEGRAM_PROXY.rsplit("@", 1)[-1])
        bot = Bot(token=config.BOT_TOKEN, session=AiohttpSession(proxy=config.TELEGRAM_PROXY))
    else:
        bot = Bot(token=config.BOT_TOKEN)
    dp = Dispatcher()
    # Лог входящих ДО роутинга — ловит всё вне фильтров состояния. ТОЛЬКО на message
    # (не callback_query — нажатия кнопок не переписка). Ошибки лога изолированы внутри.
    dp.message.outer_middleware(LoggingMiddleware())
    dp.include_router(router)

    nurture_task = asyncio.create_task(nurture.run(bot))
    worker_task = asyncio.create_task(worker.run(bot))
    retention_task = asyncio.create_task(retention.run())
    try:
        # ЕДИНСТВЕННЫЙ set_my_commands со ВСЕМИ командами меню (§5.8): второй вызов сотрёт
        # /start из меню (механизм запуска воронки). /start — запуск; /stop — отписка.
        await bot.set_my_commands([
            BotCommand(command="start", description="Начать заново 🌷"),
            BotCommand(command="stop", description="Отписаться от рассылок"),
        ])
        await bot.delete_webhook(drop_pending_updates=True)
        # Публикуем НЕ-секретный снимок конфигурации в app_settings (bot_username для
        # deep-link'ов панели + статус интеграций). Сбой изолируем — статус-борд не
        # критичен, бот должен подняться в любом случае.
        try:
            me = await bot.get_me()
            await db.publish_runtime_status(
                bot_username=me.username or "",
                gate_channel_url=config.CHANNEL_URL,
                guide_url_env=config.GUIDE_URL,
                proxy_set=bool(config.TELEGRAM_PROXY),
                agent_token_set=bool(config.TIMEWEB_AI_TOKEN),
                gateway_token_set=bool(config.AI_GATEWAY_TOKEN),
                public_base_url=config.BOT_PUBLIC_BASE_URL,
                shop_yookassa_set=config.SHOP_PAYMENTS_CONFIGURED,
            )
            logger.info("Статус рантайма опубликован в app_settings (bot @%s)", me.username)
        except Exception as e:  # noqa: BLE001 — публикация статуса не должна валить старт
            logger.warning("Не удалось опубликовать статус рантайма: %s", e)
        logger.info("Бот запущен на long-polling")
        await dp.start_polling(bot)
    finally:
        nurture_task.cancel()
        worker_task.cancel()
        retention_task.cancel()
        await health.cleanup()
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
