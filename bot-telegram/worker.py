"""Фоновый воркер: дренаж outbox (точечные ответы) + исполнение рассылок.

Запускается рядом с nurture.run одной asyncio-таской (см. bot.py). Образец цикла —
nurture.run: while True → tick → sleep, любая ошибка тика логируется и не валит цикл.

Жёсткие правила (план §5):
  • Соединение asyncpg НИКОГДА не держится через await send: claim — короткая tx с
    commit, send — без открытой транзакции (через messaging-bucket), запись результата —
    ещё одна короткая tx. Это спасает пул max_size=10 от голодания polling-воронки.
  • ВСЕ отправки идут через messaging._rate_limited_call (общий token-bucket + 429).
  • Бот НЕ доверяет панели: неотменяемый WHERE «кому можно» и при материализации, и
    re-check перед КАЖДЫМ send (TOCTOU отписки/erase/перехвата).
  • Прогресс в БД (статусы), не в FSM → переживает редеплой Timeweb. reclaim застрявших
    'sending' через RECLAIM_AFTER_SECONDS. Гарантия: at-least-once с редким дублем.
  • file_id заливается ОДИН раз в служебный OPS_CHAT_ID и переиспользуется (рассылки И
    продукты-оферы каталога: см. _drain_product_uploads).
  • per-recipient {link} подставляется воркером в момент отправки.
  • circuit-breaker: доля failed среди первых N > порога → авто-пауза рассылки + ops-лог.
"""
import asyncio
import logging
import re

from aiogram import Bot
from aiogram.exceptions import (
    TelegramForbiddenError,
    TelegramBadRequest,
    TelegramNotFound,
)

import config
import db
import messaging
import texts

logger = logging.getLogger(__name__)

# Постоянные («перманентные») ошибки доставки → сразу failed (для аналитики «не доставлено»),
# без возврата в очередь. Сетевые/прочие — транзиентные, вернуть в очередь с потолком attempts.
_PERMANENT = (TelegramForbiddenError, TelegramNotFound, TelegramBadRequest)

# Строгий поиск плейсхолдера трекинг-ссылки в шаблоне.
_LINK_RE = re.compile(r"\{link\}")

# C3: футер отписки для VK/MAX-рассылок (у каналов нет inline-кнопки «Отписаться» как в TG —
# отписка по ключевому слову, обрабатывается в multiplex._vk_respond/_max_respond).
# ⚠️ 152-ФЗ-ИНВАРИАНТ: слово, которое тут ИНСТРУКТИРУЕМ ответить (СТОП), ОБЯЗАНО входить в
# multiplex._UNSUB_WORDS — иначе лид физически не сможет отписаться. Меняешь слово — синхронно там.
_CHANNEL_UNSUB_FOOTER = "\n\n—\nЧтобы больше не получать рассылку, ответьте: СТОП"


async def run(bot: Bot, interval: int | None = None) -> None:
    """Главный цикл воркера. interval по умолчанию из config.WORKER_INTERVAL."""
    interval = interval or config.WORKER_INTERVAL
    logger.info("Воркер очереди/рассылок запущен (интервал %s c)", interval)
    while True:
        try:
            await _reclaim()
            await _drain_outbox_uploads(bot)  # байты вложения ответа → file_id (ДО отправки)
            await _drain_outbox(bot)
            await _drain_platform_notify(bot)  # уведомления владельцу/партнёрам (тенант/бриф)
            await _drain_outbox_channels()    # C3: ответы оператора в VK/MAX (живой канальный бот)
            await _drain_product_uploads(bot)
            await _run_broadcasts(bot)
        except Exception as e:  # noqa: BLE001 — цикл не должен падать
            logger.exception("Ошибка в цикле воркера: %s", e)
        await asyncio.sleep(interval)


# ── Reclaim застрявших claimed-строк (краш/редеплой) ─────────────────────────
async def _reclaim() -> None:
    n1 = await db.reclaim_stuck_outbox(config.RECLAIM_AFTER_SECONDS)
    n2 = await db.reclaim_stuck_recipients(config.RECLAIM_AFTER_SECONDS)
    if n1 or n2:
        logger.info("Reclaim: вернул в очередь outbox=%s recipients=%s", n1, n2)


# ── OUTBOX: однократная заливка ВЛОЖЕНИЯ операторского ответа в служебный чат ──
async def _drain_outbox_uploads(bot: Bot) -> None:
    """Заливает вложения личных ответов оператора (outbox.file_bytes есть, file_id нет) в
    OPS_CHAT_ID и сохраняет file_id (с обнулением байтов) — клон _drain_product_uploads, но
    для outbox. Дальше _drain_outbox шлёт лиду по готовому file_id (как раньше для рассылок).

    Заливка ОДИН раз. Отдельный «тик» СТРОГО ПЕРЕД _drain_outbox (см. run): пока байты не
    превратились в file_id, строка не подхватится отправкой (claim_outbox шлёт по file_id).
    Голосовое (kind='voice') до заливки конвертим в ogg/opus; если ffmpeg упал — деградируем
    в kind='audio' и шлём исходник как аудио (бот НЕ падает). Любая ошибка по одному вложению
    логируется и НЕ валит остальные/воркер — строка подождёт следующего тика (до кэпа попыток).
    """
    if config.OPS_CHAT_ID is None:
        return  # некуда заливать — вложения подождут настройки OPS_CHAT_ID
    items = await db.list_outbox_pending_upload(
        config.OUTBOX_UPLOAD_BATCH, config.OUTBOX_UPLOAD_MAX_ATTEMPTS
    )
    for it in items:
        outbox_id = it["id"]
        raw = it.get("file_bytes")
        if not raw:
            continue  # гонка: байты уже обнулены другим путём
        content = bytes(raw)
        # Защита на стороне бота: не льём файл сверх лимита Telegram. Панель валидирует ДО
        # записи, но битую/огромную строку считаем неудачной попыткой (инкремент attempts) —
        # по достижении кэпа вложение выпадет из очереди и воркёр не зациклится.
        if len(content) > config.MAX_PRODUCT_FILE_BYTES:
            logger.error(
                "Outbox #%s: вложение %s Б превышает лимит %s МБ — попытка заливки засчитана",
                outbox_id, len(content), config.MAX_PRODUCT_FILE_MB,
            )
            await db.bump_outbox_upload_attempt(
                outbox_id, f"file too big: {len(content)} bytes > {config.MAX_PRODUCT_FILE_BYTES}"
            )
            continue
        # ВАЖНО: kind берём из строки (панель проставила явно), НЕ из kind_for_mime — иначе
        # voice превратился бы в document и потерял нативный голосовой пузырь.
        kind = it["kind"]
        if kind == "voice":
            # Запись микрофона → ogg/opus. Сбой ffmpeg — не фатален: шлём исходник как audio.
            try:
                content = await messaging.transcode_voice(content, it.get("file_mime"))
            except Exception as e:  # noqa: BLE001 — деградация voice→audio, бот не падает
                logger.warning(
                    "Outbox #%s: транскод voice не удался (%s) → шлём как audio", outbox_id, e
                )
                kind = "audio"
        try:
            _msg, file_id = await messaging.upload_file_to_chat(
                bot, config.OPS_CHAT_ID, kind,
                content=content, filename=it.get("file_name"),
                mime=it.get("file_mime"), caption=None,
            )
        except Exception as e:  # noqa: BLE001 — одно вложение не валит остальные/воркер
            logger.error("Outbox #%s: заливка вложения не удалась: %s", outbox_id, e)
            await db.bump_outbox_upload_attempt(outbox_id, f"{type(e).__name__}: {e}")
            continue
        # Передаём ФИНАЛЬНЫЙ kind: при voice→audio fallback строка получит 'audio', иначе
        # _drain_outbox послал бы send_voice с audio-file_id (Telegram отвергнет).
        await db.set_outbox_file_id(outbox_id, file_id, kind)  # file_id + kind + обнуление байтов
        logger.info("Outbox #%s: вложение залито (kind=%s), file_id получен", outbox_id, kind)


# ── OUTBOX: точечные ответы оператора ────────────────────────────────────────
async def _drain_outbox(bot: Bot) -> None:
    items = await db.claim_outbox(config.OUTBOX_BATCH)  # короткая tx, соединение отдано
    for it in items:
        item_id = it["id"]
        tg_user_id = it["tg_user_id"]
        try:
            # Re-check адресности перед send (§5.10): нет адреса / отозвал согласие на ПДн.
            skip = await db.outbox_recheck_address(tg_user_id)
            if skip:
                await db.mark_outbox_failed(item_id, skip)
                continue

            kind = it.get("kind") or "text"
            text = it.get("text")
            file_id = it.get("file_id")
            if kind == "text" or not file_id:
                sent = await messaging.raw_send_text(bot, tg_user_id, text or "")
            else:
                sent = await messaging.raw_send_by_kind(
                    bot, tg_user_id, kind, file_id=file_id, caption=text
                )

            await db.mark_outbox_sent(item_id)
            # Зеркало операторского ответа в тред (§3): source='manual'.
            await db.log_message(
                tg_user_id=tg_user_id,
                direction="out",
                kind=kind,
                text=text,
                file_id=file_id,
                source="manual",
                tg_message_id=getattr(sent, "message_id", None),
            )
        except _PERMANENT as e:
            logger.info("Outbox %s: перманентная ошибка (%s) → failed", item_id, type(e).__name__)
            await db.mark_outbox_failed(item_id, f"{type(e).__name__}")
        except Exception as e:  # noqa: BLE001 — транзиентная: вернуть в очередь с потолком
            logger.warning("Outbox %s: транзиентная ошибка %s → возврат в очередь", item_id, e)
            await db.release_outbox(
                item_id, str(e), config.OUTBOX_MAX_ATTEMPTS, config.OUTBOX_MAX_AGE_HOURS
            )


# ── Дренаж очереди уведомлений владельцу/партнёрам (platform_notify) ───────────
async def _drain_platform_notify(bot: Bot) -> None:
    """Дренаж platform_notify. ⚠️ ПРИНЦИПИАЛЬНО через РАЗГОВОРНЫЙ бот (bot), НЕ через
    notifier.get_notifier_bot(): Telegram шлёт личку только тем, кто сам НАЧАЛ диалог с ботом,
    а владелец/партнёр стартуют именно разговорный бот через /whoami — не notifier-бот."""
    items = await db.claim_platform_notify(config.OUTBOX_BATCH)
    for it in items:
        try:
            await messaging.raw_send_text(bot, it["chat_id"], it["text"])
            await db.mark_platform_notify_sent(it["id"])
        except Exception as e:  # noqa: BLE001 — одно уведомление не валит остальные/воркер
            await db.mark_platform_notify_failed(it["id"], str(e))


# ── OUTBOX каналов VK/MAX (C3): ответ оператора через живой канальный бот ──────
async def _channel_send_media(cbot, messenger: str, addr: int, kind: str, text: str,
                              content: bytes, filename: str) -> bool:
    """Отправка медиа-вложения в VK/MAX через драйвер (байты напрямую — без TG file_id-стейджинга).
    kind — photo|document|voice|audio (из строки outbox/рассылки). Возвращает успех доставки (bool)."""
    import max_driver
    import vk_driver
    if messenger == "vk":
        if vk_driver.vk_media_type_for_kind(kind) == "photo":
            return await cbot.send_photo(addr, content, caption=text, filename=filename)
        return await cbot.send_document(addr, content, filename=filename, caption=text)
    if messenger == "max":
        return await cbot.send_media(addr, media_type=max_driver.max_media_type_for_kind(kind),
                                     content=content, caption=text, filename=filename)
    return False


async def _drain_outbox_channels() -> None:
    """C3: дренаж outbox для НЕ-tg каналов (vk/max). Отправка через ЖИВОЙ канальный бот тенанта
    из реестра multiplex (тот же процесс) — текст или медиа байтами. TG-путь (_drain_outbox) не
    трогаем. Канал не поднят / нет адреса → возврат в очередь / failed (как TG-ветка)."""
    import multiplex  # лениво: исключить любой риск цикла импорта (общий процесс через bot.py)
    items = await db.claim_outbox_channels(config.OUTBOX_BATCH)
    for it in items:
        item_id = it["id"]
        messenger = it["messenger"]
        tid = it["tenant_id"]
        addr = it.get("reply_address")
        ctx = db.current_tenant_id.set(tid)   # мультитенантно: лог/резолв в правильного тенанта
        try:
            if it.get("erase_requested_at") is not None:
                await db.mark_outbox_failed(item_id, "erased")
                continue
            if not addr:
                # vk: нет vk_user_id; max: ещё не знаем chat_id (лид не писал в личку) — слать некуда.
                await db.mark_outbox_failed(item_id, "no_address")
                continue
            cbot = multiplex.get_channel_bot(tid, messenger)
            if cbot is None:
                # Канал не поднят (не настроен / только что рестарт) — вернуть в очередь, повторим.
                await db.release_outbox(item_id, "channel bot offline",
                                        config.OUTBOX_MAX_ATTEMPTS, config.OUTBOX_MAX_AGE_HOURS)
                continue
            kind = it.get("kind") or "text"
            text = it.get("text") or ""
            raw = it.get("file_bytes")
            if kind == "text" or not raw:
                ok = await cbot.send(int(addr), text)
            else:
                ok = await _channel_send_media(cbot, messenger, int(addr), kind, text,
                                               bytes(raw), it.get("file_name") or "file")
            if not ok:
                # Драйвер сообщил о неуспехе (VK error-в-теле / MAX HTTP≠200 / сеть). НЕ помечаем 'sent'
                # при недоставке — возвращаем в очередь (транзиент, потолок attempts → failed). #1 аудита.
                await db.release_outbox(item_id, "канал: доставка не удалась",
                                        config.OUTBOX_MAX_ATTEMPTS, config.OUTBOX_MAX_AGE_HOURS)
                continue
            await db.mark_outbox_sent(item_id)
            # Зеркало операторского ответа в тред (source='manual'), как в TG-ветке.
            await db.log_message(tg_user_id=int(addr), messenger=messenger, direction="out",
                                 kind=kind, text=text, source="manual", lead_id=str(it["lead_id"]))
        except Exception as e:  # noqa: BLE001 — транзиентная: вернуть в очередь с потолком
            logger.warning("Outbox(%s) %s: %s → возврат в очередь", messenger, item_id, e)
            await db.release_outbox(item_id, str(e), config.OUTBOX_MAX_ATTEMPTS,
                                    config.OUTBOX_MAX_AGE_HOURS)
        finally:
            db.current_tenant_id.reset(ctx)


# ── КАТАЛОГ ПРОДУКТОВ: однократная заливка файла офера в служебный чат ────────
async def _drain_product_uploads(bot: Bot) -> None:
    """Заливает файлы продуктов-оферов (products.file есть, file_tg_id нет) в OPS_CHAT_ID
    и сохраняет file_tg_id (с обнулением байтов) — как broadcast_files, но для каталога.

    Заливка ОДИН раз: дальше file_id переиспользуется во всех рассылках/выдачах воронки.
    Отдельный «тик» рядом с дренажем outbox (см. run): дёшев (каталог мал, очередь обычно
    пуста — частичный индекс products_pending_upload_idx), не трогает материализацию/«кому
    слать». Любая ошибка по одному оферу логируется и НЕ валит остальные/воркер — продукт
    просто останется без file_tg_id до следующего тика.
    """
    if config.OPS_CHAT_ID is None:
        return  # некуда заливать — файловые оферы подождут настройки OPS_CHAT_ID
    items = await db.list_products_pending_upload(
        config.PRODUCT_UPLOAD_BATCH, config.PRODUCT_UPLOAD_MAX_ATTEMPTS
    )
    for it in items:
        product_id = it["id"]
        content = it.get("file")
        if not content:
            continue  # гонка: байты уже обнулены другим путём
        # Защита на стороне бота: не пытаемся залить файл сверх лимита Telegram (50 МБ).
        # Панель валидирует размер ДО записи, но битую/огромную строку считаем неудачной
        # попыткой (инкремент attempts) — чтобы по достижении кэпа офер выпал из очереди и
        # воркёр не зацикливался на неотправляемом файле (TelegramBadRequest/слишком большой).
        if len(content) > config.MAX_PRODUCT_FILE_BYTES:
            logger.error(
                "Продукт #%s: файл %s Б превышает лимит %s МБ — попытка заливки засчитана",
                product_id, len(content), config.MAX_PRODUCT_FILE_MB,
            )
            await db.bump_product_upload_attempt(
                product_id, f"file too big: {len(content)} bytes > {config.MAX_PRODUCT_FILE_BYTES}"
            )
            continue
        kind = messaging.kind_for_mime(it.get("file_mime"))
        try:
            _msg, file_id = await messaging.upload_file_to_chat(
                bot, config.OPS_CHAT_ID, kind,
                content=bytes(content), filename=it.get("file_name"),
                mime=it.get("file_mime"), caption=None,
            )
        except Exception as e:  # noqa: BLE001 — один офер не валит остальные/воркер
            logger.error("Продукт #%s: заливка файла не удалась: %s", product_id, e)
            await db.bump_product_upload_attempt(product_id, f"{type(e).__name__}: {e}")
            continue
        await db.set_product_file_id(product_id, file_id)  # file_id + обнуление байтов
        logger.info("Продукт #%s: файл залит, file_id получен", product_id)


# ── РАССЫЛКИ ─────────────────────────────────────────────────────────────────
async def _run_broadcasts(bot: Bot) -> None:
    """За тик: материализует ОДНУ новую queued-рассылку (если есть) и продвигает на ОДИН
    батч КАЖДУЮ активную 'sending'-рассылку (round-robin, без head-of-line).

    Один батч на рассылку за тик оставляет event-loop живым для polling-воронки и делит
    пропускную способность между параллельными рассылками честно; следующий батч уедет на
    следующем тике (sleep между тиками = headroom интерактиву).
    """
    # 1) Новая заявка: материализуем (детерминированный snapshot до первой отправки).
    bc = await db.claim_broadcast_to_send()
    if bc is not None:
        broadcast_id = bc["id"]
        logger.info("Рассылка #%s взята в работу (kind=%s)", broadcast_id, bc.get("kind"))
        count = await db.materialize_recipients(broadcast_id)
        await db.set_broadcast_recipient_count(broadcast_id, count)
        logger.info("Рассылка #%s: материализовано получателей=%s", broadcast_id, count)

    # 2) Продвигаем ВСЕ 'sending'-рассылки (включая только что материализованную и те, что
    #    остались после редеплоя/паузы→возобновления) — по одному батчу на каждую.
    await _continue_sending_broadcasts(bot)


async def _continue_sending_broadcasts(bot: Bot) -> None:
    """Догоняет ВСЕ рассылки в статусе 'sending' — по одному батчу на каждую за тик.

    ⚠️FIX (head-of-line между рассылками): раньше обслуживалась ровно ОДНА (lowest-id)
    'sending'-рассылка за тик, и если у неё застряли строки в 'sending' (краш/редеплой),
    тик уходил на её бесплодную пере-проверку, а сёстры стояли до reclaim (10 мин). Теперь
    итерируем ВЕСЬ список: каждая получает свой батч; застрявшая (нет pending, есть not-yet-
    reclaim 'sending') просто отдаёт пустой claim и НЕ блокирует остальных, self-heal'ясь
    после reclaim. Список снимаем один раз за тик — новые queued подхватятся следующим тиком.
    """
    for bc in await _list_sending_broadcasts():
        try:
            ready = await _prepare_broadcast(bot, bc)
            if ready == "pause":
                await db.pause_broadcast(bc["id"])
                continue
            if ready == "wait":
                # Контент ещё не готов к доставке, но это транзиентно (файл офера
                # дозальётся _drain_product_uploads в этом же цикле) — НЕ пауза, просто
                # пропускаем рассылку на этом тике и берём её снова на следующем.
                continue
            await _send_batch(bot, bc)
        except Exception as e:  # noqa: BLE001 — одна сбойная рассылка не валит остальные
            logger.exception("Рассылка #%s: ошибка батча, продолжаем с другими: %s",
                             bc.get("id"), e)


async def _prepare_broadcast(bot: Bot, bc: dict) -> str:
    """Готовит контекст доставки рассылки и решает, можно ли её гнать на этом тике.

    Возвращает:
      • "ready" — можно слать (контекст разложен в bc: _tg_file_id и/или _product);
      • "wait"  — контент транзиентно не готов (файл офера ещё заливается) → пропустить
                  тик, не паузить (дозальётся в том же цикле _drain_product_uploads);
      • "pause" — постоянная проблема конфигурации (нет OPS_CHAT_ID для заливки, пустой
                  broadcast_files, продукт удалён/без файла-и-ссылки) → на паузу.

    Две ветки: рассылка-ПРОДУКТ (broadcasts.product_id задан — доставляем офер из каталога,
    файл лежит на products) и обычная файловая/текстовая рассылка (файл на broadcast_files,
    прежнее поведение _ensure_file_ready без изменений).
    """
    if bc.get("product_id") is not None:
        return await _prepare_product_broadcast(bc)
    return "ready" if await _ensure_file_ready(bot, bc) else "pause"


async def _prepare_product_broadcast(bc: dict) -> str:
    """Готовит доставку офера-продукта рассылки. Кладёт _product (и _tg_file_id, если есть).

    Решения:
      • Продукт удалён/недоступен (product_id повис на ON DELETE SET NULL гонкой) → pause.
      • Есть file_tg_id → ready (файл уже залит, переиспользуем).
      • Файл задуман (file_mime), но ещё не залит:
          – заливка реально идёт (OPS_CHAT_ID задан, байты ещё есть, попыток < кэпа) → wait
            (транзиентно: _drain_product_uploads доберёт в этом же цикле);
          – заливка невозможна/исчерпана (нет OPS_CHAT_ID ИЛИ байты пропали без file_tg_id
            ИЛИ upload_attempts >= кэпа — битый/слишком большой файл) → pause, чтобы рассылка
            не висела ВЕЧНО в 'sending' на 0% (оператор увидит паузу, поправит файл/OPS_CHAT_ID
            и сделает /resume). Это закрывает мягкий вечный 'wait' (грабля §finding 7).
      • Файла не задумано, но есть link → ready (офер со ссылкой/текстом).
      • Совсем пустой офер (ни файла, ни ссылки) → pause: слать нечего (панель такого не
        даёт сохранить, но бот не доверяет — защищаемся).
    """
    product = await db.get_broadcast_product(bc["id"])
    if product is None:
        logger.error("Рассылка #%s: product_id задан, но продукт не найден", bc["id"])
        return "pause"
    bc["_product"] = product
    file_tg_id = product.get("file_tg_id")
    if file_tg_id:
        bc["_tg_file_id"] = file_tg_id
        return "ready"
    # Файла ещё нет. Если офер задуман с файлом — решаем wait vs pause по тому, может ли
    # заливка вообще завершиться (иначе вечный 'wait' заморозил бы рассылку в 'sending').
    if product.get("file_mime"):
        can_upload = (
            config.OPS_CHAT_ID is not None
            and product.get("has_file_bytes")
            and (product.get("upload_attempts") or 0) < config.PRODUCT_UPLOAD_MAX_ATTEMPTS
        )
        if can_upload:
            logger.info("Рассылка #%s: файл офера ещё не залит — ждём заливку", bc["id"])
            return "wait"
        # Заливка не сможет завершиться (нет OPS_CHAT_ID / байты пропали / попытки исчерпаны).
        if product.get("link"):
            # Есть запасная ссылка — доставим офер без файла, чем держать рассылку мёртвой.
            logger.warning(
                "Рассылка #%s: файл офера не заливается (attempts=%s, ops_chat=%s) — "
                "доставляем без файла по ссылке офера",
                bc["id"], product.get("upload_attempts"), config.OPS_CHAT_ID is not None,
            )
            return "ready"
        logger.error(
            "Рассылка #%s: файл офера не заливается и запасной ссылки нет (attempts=%s) → пауза",
            bc["id"], product.get("upload_attempts"),
        )
        return "pause"
    if product.get("link"):
        return "ready"  # офер только со ссылкой/текстом — доставляем без файла
    logger.error("Рассылка #%s: у офера нет ни файла, ни ссылки — слать нечего", bc["id"])
    return "pause"


async def _list_sending_broadcasts() -> list[dict]:
    """ВСЕ рассылки в статусе 'sending' с шаблоном/файлом-контекстом, по возрастанию id."""
    async with db.pool.acquire() as c:
        rows = await c.fetch(
            """
            select id, title, messenger, kind, body_template, recipient_count, product_id,
                   tenant_id
            from broadcasts
            where status = 'sending'
            order by id
            """
        )
    return [dict(r) for r in rows]


async def _ensure_file_ready(bot: Bot, bc: dict) -> bool:
    """Гарантирует, что у файловой рассылки есть tg_file_id (заливка в служебный чат).

    Возвращает True, если рассылку можно гнать (текстовая — всегда True; файловая — после
    успешной заливки). Кладёт tg_file_id обратно в bc для переиспользования в батче.
    """
    kind = (bc.get("kind") or "text")
    if kind == "text":
        return True
    fr = await db.get_broadcast_file(bc["id"])
    if fr is None:
        logger.error("Рассылка #%s: kind=%s, но broadcast_files пуст", bc["id"], kind)
        return False
    # C3: VK/MAX — НЕ стейджим в OPS_CHAT (TG file_id неприменим). Шлём байты per-recipient
    # канальным драйвером. bytes остаются в broadcast_files (set_broadcast_file_id для них не зовём).
    if (bc.get("messenger") or "tg") != "tg":
        content = fr.get("bytes")
        if not content:
            logger.error("Рассылка #%s (%s): bytes пусты, медиа недоступно", bc["id"], bc.get("messenger"))
            return False
        bc["_file_bytes"] = bytes(content)
        bc["_file_name"] = fr.get("filename") or "file"
        return True
    if fr.get("tg_file_id"):
        bc["_tg_file_id"] = fr["tg_file_id"]
        return True
    # Первичная заливка — строго в служебный чат (НЕ первому получателю, §5.6).
    if config.OPS_CHAT_ID is None:
        logger.error("Рассылка #%s: нет OPS_CHAT_ID для заливки файла", bc["id"])
        return False
    content = fr.get("bytes")
    if not content:
        logger.error("Рассылка #%s: bytes пусты и tg_file_id нет", bc["id"])
        return False
    try:
        _msg, file_id = await messaging.upload_file_to_chat(
            bot, config.OPS_CHAT_ID, kind,
            content=bytes(content), filename=fr.get("filename"),
            mime=fr.get("mime"), caption=None,
        )
    except Exception as e:  # noqa: BLE001
        logger.error("Рассылка #%s: заливка файла не удалась: %s", bc["id"], e)
        return False
    await db.set_broadcast_file_id(fr["id"], file_id)  # проставляет file_id + обнуляет bytes
    bc["_tg_file_id"] = file_id
    logger.info("Рассылка #%s: файл залит, file_id получен", bc["id"])
    return True


def _has_link(template: str) -> bool:
    return bool(_LINK_RE.search(template or ""))


def _delivery_for_broadcast(bc: dict) -> tuple[str, str, str | None]:
    """Параметры доставки рассылки, посчитанные ОДИН раз на батч: (kind, template, file_id).

    Две ветки:
      • Рассылка-ПРОДУКТ (bc['_product'] разложен _prepare_product_broadcast): kind выводим
        из file_mime офера (фото/документ) при наличии file_tg_id, иначе text. Шаблон —
        ТЕКСТ ОПЕРАТОРА (bc['body_template'], композер его требует непустым) как основа, к
        нему снизу добавляем подпись офера (caption+название+цена, texts.product_caption),
        чтобы не терять написанное оператором (контракт UI: «текст задавайте слева»). Если у
        офера есть link — добавляем {link}; URL для него берёт _send_batch напрямую из
        офера (product.link), а если у рассылки задан собственный target_url — он перекрывает
        (см. _delivery_target_url). {link} НЕ дублируем, если оператор уже вписал его в текст.
      • Обычная рассылка: прежнее поведение — kind/шаблон/файл из самой рассылки, {link}
        управляется наличием плейсхолдера в body_template (панель/композер).
    """
    product = bc.get("_product")
    if product is not None:
        file_id = bc.get("_tg_file_id")  # = product.file_tg_id, проставлен в _prepare_*
        kind = messaging.kind_for_mime(product.get("file_mime")) if file_id else "text"
        # Основа — текст оператора; подпись офера добавляем отдельным блоком под ним.
        operator_body = (bc.get("body_template") or "").strip()
        offer_caption = texts.product_caption(product)
        parts = [p for p in (operator_body, offer_caption) if p]
        template = "\n\n".join(parts)
        # Ссылка офера: добавляем {link} только если у офера есть ссылка И плейсхолдер
        # ещё не вписал сам оператор (иначе получили бы двойную ссылку).
        if product.get("link") and not _has_link(template):
            template = (template + "\n{link}") if template else "{link}"
        return kind, template, file_id
    # Обычная рассылка — без изменений.
    return (bc.get("kind") or "text"), (bc.get("body_template") or ""), bc.get("_tg_file_id")


async def _send_batch(bot: Bot, bc: dict) -> None:
    """Гонит ОДИН батч получателей рассылки. Resume/идемпотентность — через статусы в БД."""
    broadcast_id = bc["id"]

    # Стоп-кран: если рассылку поставили на паузу (вручную/circuit-breaker) — не берём батч.
    status = await db.get_broadcast_status(broadcast_id)
    if status not in ("sending",):
        return

    # C3: канал рассылки. Для VK/MAX — берём ЖИВОЙ канальный бот тенанта из реестра multiplex.
    # Если канал не поднят (не настроен / рестарт) — НЕ клеймим батч (не жжём attempts), ждём тик.
    messenger = bc.get("messenger") or "tg"
    cbot = None
    if messenger != "tg":
        import multiplex
        cbot = multiplex.get_channel_bot(bc.get("tenant_id"), messenger)
        if cbot is None:
            logger.info("Рассылка #%s: канал %s не поднят — ждём", broadcast_id, messenger)
            return

    batch = await db.claim_broadcast_recipients(broadcast_id, config.BROADCAST_BATCH)
    if not batch:
        # Получателей в pending не осталось → если и sending нет, финализируем.
        await _maybe_finalize(broadcast_id)
        return

    # Контент доставки считаем ОДИН раз на батч (рассылка-продукт vs обычная рассылка).
    # «Кому слать»/материализация/статусы ниже — общий путь, НЕ дублируется и НЕ меняется.
    kind, template, tg_file_id = _delivery_for_broadcast(bc)
    use_link = _has_link(template)
    # Клавиатура (раз на батч): обязательный футер «Отписаться» (152-ФЗ/38-ФЗ, §5.8;
    # on_unsub идемпотентен) + опциональный ряд «Купить за X ₽» (Phase 1B) — ТОЛЬКО для
    # рассылки-продукта с рублёвой ценой при включённом тумблере панели и вписанных
    # SHOP-ключах. Выключено/не продукт → ровно прежняя клавиатура из одной «Отписаться».
    buy_id, buy_label = None, None
    product = bc.get("_product")
    if (product and product.get("price") and product["price"] > 0
            and (product.get("currency") or "RUB") == "RUB"
            and config.SHOP_PAYMENTS_CONFIGURED
            and await db.is_online_payments_enabled()):
        buy_id = product["id"]
        buy_label = texts.buy_button(product["price"], product.get("currency"))
    unsub_kb = messaging.broadcast_markup(buy_product_id=buy_id, buy_label=buy_label)
    # Целевой URL для {link}. Источник: единая трекинг-ссылка рассылки (link_tokens,
    # click_token=null, регистрирует панель из поля target_url композера). Для рассылки-
    # ПРОДУКТА, если панель не зарегистрировала собственный target_url, берём ссылку из
    # самого офера (product.link) — иначе {link} офера терялся бы. Достаём раз на батч.
    target_url = await _delivery_target_url(bc) if use_link else None

    for r in batch:
        rid = r["id"]
        lead_id = r["lead_id"]
        # Адрес доставки: tg → tg_user_id; vk/max → reply_address (vk_user_id / max_chat_id).
        addr = r["tg_user_id"] if messenger == "tg" else r.get("reply_address")
        try:
            # TOCTOU re-check перед КАЖДЫМ send (по адресу НУЖНОГО канала): отписка/erase/перехват.
            if not await db.recipient_recheck(lead_id, messenger):
                await db.mark_recipient_skipped(rid, "audience_changed")
                continue
            if not addr:
                await db.mark_recipient_skipped(rid, "no_address")
                continue

            text = template
            if use_link and target_url:
                token = await db.ensure_click_token(rid, broadcast_id, lead_id, target_url)
                link = f"{config.BOT_PUBLIC_BASE_URL}/r/{token}" if config.BOT_PUBLIC_BASE_URL else target_url
                text = template.replace("{link}", link)
            elif use_link:
                # {link} есть, но трекинг не настроен — убираем плейсхолдер, не шлём «{link}».
                text = template.replace("{link}", "")

            if messenger == "tg":
                if kind == "text" or not tg_file_id:
                    sent = await messaging.raw_send_text(bot, addr, text, reply_markup=unsub_kb)
                else:
                    sent = await messaging.raw_send_by_kind(
                        bot, addr, kind, file_id=tg_file_id, caption=text, reply_markup=unsub_kb
                    )
                tg_message_id = getattr(sent, "message_id", None)
            else:
                # VK/MAX: футер отписки (нет inline-кнопки как в TG); текст или медиа байтами.
                # Драйверы НЕ бросают, но ВОЗВРАЩАЮТ успех (bool) → ветвим как TG (#1 аудита).
                ch_text = text + _CHANNEL_UNSUB_FOOTER
                if kind == "text" or not bc.get("_file_bytes"):
                    ok = await cbot.send(int(addr), ch_text)
                else:
                    ok = await _channel_send_media(cbot, messenger, int(addr), kind, ch_text,
                                                   bc["_file_bytes"], bc.get("_file_name") or "file")
                if not ok:
                    # Недоставка (VK error / MAX HTTP≠200 / сеть). НЕ помечаем 'sent' — иначе статус
                    # врёт и circuit-breaker слеп для каналов. Возврат в pending (транзиент, потолок
                    # attempts → failed); доля failed снова осмысленна для _post_batch.
                    await db.release_recipient(rid, "канал: доставка не удалась", config.MAX_SEND_ATTEMPTS)
                    continue
                tg_message_id = None

            await db.mark_recipient_sent(rid)
            # Зеркало в тред (source='broadcast').
            await db.log_message(
                tg_user_id=int(addr),
                messenger=messenger,
                direction="out",
                kind=kind,
                text=text,
                file_id=(tg_file_id if messenger == "tg" else None),
                source="broadcast",
                lead_id=lead_id,
                tg_message_id=tg_message_id,
            )
        except _PERMANENT as e:
            await db.mark_recipient_failed(rid, type(e).__name__)
        except Exception as e:  # noqa: BLE001 — транзиентная: вернуть в pending с потолком
            logger.warning("Рассылка #%s получатель %s: %s → возврат", broadcast_id, rid, e)
            await db.release_recipient(rid, str(e), config.MAX_SEND_ATTEMPTS)

    # После батча: circuit-breaker + обновление totals + возможная финализация.
    await _post_batch(bot, broadcast_id)


async def _delivery_target_url(bc: dict) -> str | None:
    """Целевой URL для подстановки в {link}, посчитанный раз на батч.

    Приоритет: target_url, который панель зарегистрировала строкой link_tokens этой
    рассылки (общий путь, работает и для обычной, и для продукт-рассылки с заданным в
    композере target_url). Если такой строки нет, а это рассылка-ПРОДУКТ со своей
    ссылкой — фолбэк на product.link (его панель в link_tokens НЕ кладёт; без этого
    фолбэка ссылка офера не доставлялась). Когда оба заданы, побеждает target_url
    рассылки — оператор намеренно переопределил ссылку офера на уровне рассылки.
    """
    registered = await _broadcast_target_url(bc["id"])
    if registered:
        return registered
    product = bc.get("_product")
    if product is not None:
        link = (product.get("link") or "").strip()
        if link:
            return link
    return None


async def _broadcast_target_url(broadcast_id: int) -> str | None:
    """target_url единой трекинг-ссылки рассылки (строка link_tokens без click_token)."""
    async with db.pool.acquire() as c:
        return await c.fetchval(
            "select target_url from link_tokens "
            "where broadcast_id = $1 and lead_id is null order by created_at limit 1",
            broadcast_id,
        )


async def _post_batch(bot: Bot, broadcast_id: int) -> None:
    counts = await db.broadcast_counts(broadcast_id)
    totals = {"sent": counts["sent"], "failed": counts["failed"], "skipped": counts["skipped"]}
    await db.update_broadcast_totals(broadcast_id, totals)

    # Circuit-breaker: на ранней стадии (накоплено >= CB_MIN_SAMPLE) высокая доля failed → пауза.
    done = counts["sent"] + counts["failed"]
    if done >= config.CB_MIN_SAMPLE and counts["failed"] / max(done, 1) > config.CB_FAIL_RATIO:
        logger.error(
            "Рассылка #%s: circuit-breaker (failed=%s/%s > %.0f%%) → авто-пауза",
            broadcast_id, counts["failed"], done, config.CB_FAIL_RATIO * 100,
        )
        await db.pause_broadcast(broadcast_id)
        await _ops_alert(
            bot,
            f"Рассылка #{broadcast_id} остановлена авто-стоп-краном: "
            f"доля недоставленных {counts['failed']}/{done} превысила "
            f"{int(config.CB_FAIL_RATIO * 100)}%. Проверьте сегмент/контент.",
        )
        return

    await _maybe_finalize(broadcast_id)


async def _maybe_finalize(broadcast_id: int) -> None:
    """Если не осталось pending/sending — переводим в done с итогами."""
    counts = await db.broadcast_counts(broadcast_id)
    if counts["pending"] == 0 and counts["sending"] == 0:
        totals = {"sent": counts["sent"], "failed": counts["failed"], "skipped": counts["skipped"]}
        await db.finalize_broadcast(broadcast_id, totals)
        logger.info("Рассылка #%s завершена: %s", broadcast_id, totals)


async def _ops_alert(bot: Bot, text: str) -> None:
    """Best-effort ops-уведомление в служебный чат. Никогда не валит воркер."""
    if config.OPS_CHAT_ID is None:
        logger.warning("OPS-алерт (нет OPS_CHAT_ID): %s", text)
        return
    try:
        # Лёгкий прямой вызов без зеркалирования в тред (служебка, не диалог с лидом).
        await messaging.raw_send_text(bot, config.OPS_CHAT_ID, text)
    except Exception:  # noqa: BLE001
        logger.warning("Не удалось отправить ops-алерт: %s", text, exc_info=True)
