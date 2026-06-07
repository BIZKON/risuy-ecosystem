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
  • file_id заливается ОДИН раз в служебный OPS_CHAT_ID и переиспользуется.
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

logger = logging.getLogger(__name__)

# Постоянные («перманентные») ошибки доставки → сразу failed (для аналитики «не доставлено»),
# без возврата в очередь. Сетевые/прочие — транзиентные, вернуть в очередь с потолком attempts.
_PERMANENT = (TelegramForbiddenError, TelegramNotFound, TelegramBadRequest)

# Строгий поиск плейсхолдера трекинг-ссылки в шаблоне.
_LINK_RE = re.compile(r"\{link\}")


async def run(bot: Bot, interval: int | None = None) -> None:
    """Главный цикл воркера. interval по умолчанию из config.WORKER_INTERVAL."""
    interval = interval or config.WORKER_INTERVAL
    logger.info("Воркер очереди/рассылок запущен (интервал %s c)", interval)
    while True:
        try:
            await _reclaim()
            await _drain_outbox(bot)
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
            if not await _ensure_file_ready(bot, bc):
                await db.pause_broadcast(bc["id"])
                continue
            await _send_batch(bot, bc)
        except Exception as e:  # noqa: BLE001 — одна сбойная рассылка не валит остальные
            logger.exception("Рассылка #%s: ошибка батча, продолжаем с другими: %s",
                             bc.get("id"), e)


async def _list_sending_broadcasts() -> list[dict]:
    """ВСЕ рассылки в статусе 'sending' с шаблоном/файлом-контекстом, по возрастанию id."""
    async with db.pool.acquire() as c:
        rows = await c.fetch(
            """
            select id, title, messenger, kind, body_template, recipient_count
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


async def _send_batch(bot: Bot, bc: dict) -> None:
    """Гонит ОДИН батч получателей рассылки. Resume/идемпотентность — через статусы в БД."""
    broadcast_id = bc["id"]

    # Стоп-кран: если рассылку поставили на паузу (вручную/circuit-breaker) — не берём батч.
    status = await db.get_broadcast_status(broadcast_id)
    if status not in ("sending",):
        return

    batch = await db.claim_broadcast_recipients(broadcast_id, config.BROADCAST_BATCH)
    if not batch:
        # Получателей в pending не осталось → если и sending нет, финализируем.
        await _maybe_finalize(broadcast_id)
        return

    kind = bc.get("kind") or "text"
    template = bc.get("body_template") or ""
    tg_file_id = bc.get("_tg_file_id")
    use_link = _has_link(template)
    # Футер «Отписаться» прикрепляется к КАЖДОМУ рассылочному сообщению (152-ФЗ/38-ФЗ —
    # простой отказ в самом сообщении, §5.8). on_unsub обрабатывает его идемпотентно.
    unsub_kb = messaging.unsubscribe_markup()
    # Единая трекинг-ссылка рассылки — её target_url зарегистрирован панелью отдельной
    # строкой link_tokens (broadcast_id, click_token=null). Достаём один раз на батч.
    target_url = await _broadcast_target_url(broadcast_id) if use_link else None

    for r in batch:
        rid = r["id"]
        lead_id = r["lead_id"]
        chat_id = r["tg_user_id"]
        try:
            # TOCTOU re-check перед КАЖДЫМ send: отписался/erase/consent/перехват → skipped.
            if not await db.recipient_recheck(lead_id):
                await db.mark_recipient_skipped(rid, "audience_changed")
                continue

            text = template
            if use_link and target_url:
                token = await db.ensure_click_token(rid, broadcast_id, lead_id, target_url)
                link = f"{config.BOT_PUBLIC_BASE_URL}/r/{token}" if config.BOT_PUBLIC_BASE_URL else target_url
                text = template.replace("{link}", link)
            elif use_link:
                # {link} есть, но трекинг не настроен — убираем плейсхолдер, не шлём «{link}».
                text = template.replace("{link}", "")

            if kind == "text" or not tg_file_id:
                sent = await messaging.raw_send_text(bot, chat_id, text, reply_markup=unsub_kb)
            else:
                sent = await messaging.raw_send_by_kind(
                    bot, chat_id, kind, file_id=tg_file_id, caption=text, reply_markup=unsub_kb
                )

            await db.mark_recipient_sent(rid)
            # Зеркало в тред (source='broadcast').
            await db.log_message(
                tg_user_id=chat_id,
                direction="out",
                kind=kind,
                text=text,
                file_id=tg_file_id,
                source="broadcast",
                lead_id=lead_id,
                tg_message_id=getattr(sent, "message_id", None),
            )
        except _PERMANENT as e:
            await db.mark_recipient_failed(rid, type(e).__name__)
        except Exception as e:  # noqa: BLE001 — транзиентная: вернуть в pending с потолком
            logger.warning("Рассылка #%s получатель %s: %s → возврат", broadcast_id, rid, e)
            await db.release_recipient(rid, str(e), config.MAX_SEND_ATTEMPTS)

    # После батча: circuit-breaker + обновление totals + возможная финализация.
    await _post_batch(bot, broadcast_id)


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
