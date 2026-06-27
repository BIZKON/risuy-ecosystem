"""Фоновый прогрев/дожим. Раз в минуту два прохода:
  • School (дефолт-тенант) — 3 касания от guide_sent_at (выдача лид-магнита). НЕ меняем.
  • Прочие тенанты (item B) — per-tenant дожим: конфиг в tenant_settings (nurture_enabled +
    nurture_steps), якорь = время ПОСЛЕДНЕГО ВХОДЯЩЕГО лида (молчит → касание; ответил → серия
    перезапускается, т.к. касание «протухает» относительно нового входящего). Те же шаги уходят
    по ВСЕМ поднятым каналам тенанта (TG/VK/MAX) — бот канала из мультиплекса. Прогресс — в
    follow_up_*_at (переживает рестарт). Стоп: отписка/ручная пауза/эскалация/конверсия (в SQL get_due_*)."""
import asyncio
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError

import config
import db
import messaging
import multiplex
import texts

logger = logging.getLogger(__name__)

# School (дефолт-тенант): (номер, колонка, задержка_сек, текст). НЕ трогаем — якорь guide_sent_at.
_FOLLOW_UPS = [
    (1, "follow_up_1_at", config.FOLLOW_UP_DELAYS[0], texts.FOLLOW_UP_1),
    (2, "follow_up_2_at", config.FOLLOW_UP_DELAYS[1], texts.FOLLOW_UP_2),
    (3, "follow_up_3_at", config.FOLLOW_UP_DELAYS[2], texts.FOLLOW_UP_3),
]
# Колонки касаний по позиции шага дожима тенанта (до 3 — по числу колонок).
_TENANT_COLS = ["follow_up_1_at", "follow_up_2_at", "follow_up_3_at"]
# Каналы дожима тенанта: те же nurture_steps уходят по каждому ПОДНЯТОМУ каналу тенанта.
_NURTURE_CHANNELS = ("tg", "vk", "max")


async def _send_touch(bot, messenger: str, addr: int, text: str, lead_id) -> None:
    """Отправить одно касание дожима по каналу + зеркало в «Диалоги» (source='nurture').
    TG — через общий token-bucket/429-ретрай messaging.send_text (он же логирует; бросает
    TelegramForbiddenError при блокировке — ловит вызывающий). VK/MAX — драйвер bot.send(addr,text)
    (адрес ответа: vk_user_id / max_chat_id) + ручной лог по lead_id (адрес MAX ≠ identity)."""
    if messenger == "tg":
        await messaging.send_text(bot, int(addr), text, source="nurture")
        return
    if await bot.send(int(addr), text):
        await db.log_message(tg_user_id=int(addr), messenger=messenger, direction="out",
                             text=text, source="nurture", lead_id=str(lead_id))
    else:
        logger.warning("Дожим %s: касание лиду lead=%s addr=%s не доставлено (драйвер вернул False)",
                       messenger, lead_id, addr)


async def run(bot: Bot, interval: int = 60) -> None:
    logger.info("Прогрев/дожим запущен (интервал %s c)", interval)
    while True:
        try:
            await _tick(bot)            # School (дефолт-тенант) — как раньше
        except Exception as e:
            logger.exception("Ошибка в цикле прогрева (School): %s", e)
        try:
            await _tick_tenants()       # item B: per-tenant дожим
        except Exception as e:
            logger.exception("Ошибка в цикле дожима (тенанты): %s", e)
        await asyncio.sleep(interval)


async def _tick(bot: Bot) -> None:
    for n, col, delay, text in _FOLLOW_UPS:
        # get_due_followups уже отфильтровал unsubscribed_at/bot_paused (§4) — на паузе/
        # после отписки лид сюда не попадёт, касание НЕ помечается отправленным (resume бесплатный).
        for tg_user_id in await db.get_due_followups(col, delay):
            try:
                # Через общий token-bucket + единый 429-ретрай + зеркало в тред (source='nurture').
                await messaging.send_text(bot, tg_user_id, text, source="nurture")
            except TelegramForbiddenError:
                logger.info("Пользователь %s заблокировал бота — пропускаем касание %s", tg_user_id, n)
            except Exception as e:
                logger.warning("Касание %s для %s не доставлено: %s", n, tg_user_id, e)
            finally:
                # Помечаем отправленным в любом случае, чтобы не зацикливаться на одном лиде.
                await db.mark_followup_sent(col, tg_user_id)


async def _tick_tenants() -> None:
    """Дожим по каждому активному НЕ-дефолт тенанту с включённым дожимом, ПО ВСЕМ его поднятым
    каналам (TG/VK/MAX). School исключаем — его обслуживает _tick (свой якорь guide_sent_at).
    Дефолт-тенант вне мультиплекса → ботов в реестре нет (get_channel_bot=None), но явный пропуск
    надёжнее и дешевле. Те же nurture_steps уходят по каждому каналу, где у тенанта поднят бот."""
    default = db.default_tenant_id()
    for t in await db.list_active_tenants():
        tid = t["id"]
        if tid == default:
            continue
        try:
            cfg = await db.get_tenant_nurture(tid)
        except Exception as e:  # noqa: BLE001 — один тенант не должен ронять остальных
            logger.warning("Дожим: не прочитал конфиг тенанта %s: %s", tid, e)
            continue
        if not cfg["enabled"]:
            continue
        # Касание логируется в messages под ЭТИМ тенантом (зеркало в «Диалоги»): выставляем
        # tenant-контекст на время отправок тенанта и сбрасываем после (не течёт в School/др.).
        token = db.current_tenant_id.set(tid)
        try:
            for messenger in _NURTURE_CHANNELS:
                bot = multiplex.get_channel_bot(tid, messenger)
                if bot is None:
                    continue  # канал тенанта не поднят (не настроен/рестарт) — следующий тик
                prev_col = None  # шаги — цепочка: шаг N якорится на касании N-1 (анти-залп + порядок)
                for idx, step in enumerate(cfg["steps"]):
                    col = _TENANT_COLS[idx]
                    for row in await db.get_due_tenant_followups(
                        tid, col, step["delay_seconds"], prev_col=prev_col, messenger=messenger
                    ):
                        lead_id, addr = row["lead_id"], row["addr"]
                        try:
                            await _send_touch(bot, messenger, addr, step["text"], lead_id)
                        except TelegramForbiddenError:  # только TG-ветка _send_touch (vk/max → bool, не бросают)
                            logger.info("Дожим %s/%s: лид %s заблокировал бота — пропуск касания %s",
                                        tid, messenger, lead_id, idx + 1)
                        except Exception as e:  # noqa: BLE001
                            logger.warning("Дожим %s/%s: касание %s лиду %s не доставлено: %s",
                                           tid, messenger, idx + 1, lead_id, e)
                        finally:
                            # mark в finally — best-effort (паритет School): сбой доставки касание не
                            # ретраит (анти-залп/анти-спам), но новый входящий сбросит серию.
                            await db.mark_tenant_followup_sent(tid, col, lead_id)
                    prev_col = col  # следующий шаг якорится на ЭТОЙ колонке (кумулятивная пауза)
        finally:
            db.current_tenant_id.reset(token)
