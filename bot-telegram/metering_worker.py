"""Снапшот-воркер метеринга (Wave 3, ТЗ §5.2; DECISIONS п.5, 16–18).

Каждые config.METERING_INTERVAL секунд (образец цикла — nurture.run: ошибка тика
логируется и не валит цикл):

 1) GET /cloud-ai/agents (ОДИН вызов на всех агентов аккаунта) → used_tokens;
 2) для каждого агента из реестра tenant_agents — дельта против последнего
    снапшота agent_token_snapshots, ПОД advisory-lock на agent_id (защита от
    гонки двух экземпляров воркера на rolling-деплое — финдинг ревью №4):
      • поле used_tokens отсутствует в ответе API → агент ПРОПУСКАЕТСЯ (НЕ 0:
        иначе глитч API дал бы ложный сброс счётчика — финдинг №1);
      • used_tokens == 0 при ненулевом prev → подозрение на глитч, тик
        пропускается (счётчик живого агента в 0 не падает; реальный сброс
        подтвердится следующим чтением);
      • первый снапшот = baseline, исторический расход НЕ списывается;
      • счётчик уменьшился (агент пересоздан) → новый baseline + warning;
      • дельта > 0 и план cost_multiplier → СНАПШОТ + СПИСАНИЕ в ОДНОЙ
        транзакции (атомарно, под advisory-lock); idempotence_key =
        "ca:{agent_id}:{prev_taken_at}" — повтор после краха безопасен;
      • дельта > 0, но цены модели нет в model_prices → снапшот НЕ пишется
        (дельта копится до вписывания цены из ЛК), алерт рейт-лимитирован;
      • план per_message → снапшот пишется как СВЕРКА, без списания;
    агент аккаунта БЕЗ строки в реестре пропускается (никому не списывается);
 3) per_message-планы: списание за каждое исходящее сообщение Лии
    (messages: source='liya', direction='out'), idempotence_key = "msg:{id}".
    Первый скан тенанта = baseline (hwm = max(id) истории Лии БЕЗ списания —
    иначе вся история затарифицировалась бы задним числом, финдинг №2);
    cost=0 в v1 (DECISIONS п.17). Скан + продвижение hwm — ОДНА транзакция
    (атомарность: краш не оставит списания без hwm → нет ложного рескана).
    Граница created_at < now()−grace отсекает гонку bigserial-коммитов.

Все списания — ПОСТФАКТУМ (токены у Timeweb уже потрачены) → allow_negative=True.
prepaid-тенант с балансом ≤ 0 получает флаг ai_wallet_blocked (Лия — мягкая пауза,
handlers.on_free_text) + ops-алерт. Тенант без плана (Школа до Wave 4) НЕ
блокируется; тенант по умолчанию (Школа) не блокируется НИКОГДА, даже получив
план — §8.7 (DECISIONS п.18), вместо блокировки шлётся ops-алерт.

Сетевые вызовы (HTTP Timeweb, ops-алерты Telegram) — ВНЕ удержанного соединения
пула (правило db.init: «соединение не держится через await send»; финдинг №7).
"""
import asyncio
import logging
import re
import time

import aiohttp
from aiogram import Bot

import config
import db
import messaging
from shared.metering import blended_price_per_token, charge_usage, get_tenant_plan
from shared.money import ceil_mul, micro_to_rub_str

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=30)
_MODELS_TTL = 3600.0          # кэш каталога моделей (id → public_name), сек
_ALERT_INTERVAL = 3600.0      # рейт-лимит повторных алертов (нет цены / минус), сек
_MSG_SCAN_BATCH = 500         # сообщений per_message-скана за тик на тенанта
_MSG_GRACE_SECONDS = 60       # не тарифицируем сообщения моложе grace (гонка bigserial-коммитов)
_ADVISORY_NS = 719            # namespace advisory-локов метеринга (произвольный, см. _process_agent_delta)

_models_cache: dict[int, str] = {}
_models_fetched_at: float = 0.0
_alerted: dict[str, float] = {}


def _slug(name: str) -> str:
    """'DeepSeek V4 Pro Thinking' → 'deepseek-v4-pro-thinking' (ключ model_prices)."""
    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")


def _alert_due(key: str) -> bool:
    now = time.monotonic()
    if now - _alerted.get(key, 0.0) > _ALERT_INTERVAL:
        _alerted[key] = now
        return True
    return False


async def run(bot: Bot, interval: int | None = None) -> None:
    """Главный цикл снапшот-воркера. interval по умолчанию из config.METERING_INTERVAL."""
    interval = interval or config.METERING_INTERVAL
    logger.info("Метеринг-воркер запущен (интервал %s c)", interval)
    while True:
        try:
            await _tick(bot)
        except Exception as e:  # noqa: BLE001 — цикл не должен падать
            logger.exception("Ошибка тика метеринга: %s", e)
        await asyncio.sleep(interval)


async def _tick(bot: Bot) -> None:
    await _snapshot_agents(bot)
    await _scan_per_message(bot)


# ── Шаги 1–2: снапшоты used_tokens и дельта-списания ─────────────────────────
async def _snapshot_agents(bot: Bot) -> None:
    async with db.pool.acquire() as c:
        registry = await c.fetch("select agent_id, tenant_id from tenant_agents")
    if not registry:
        return
    if not config.TIMEWEB_AI_TOKEN:
        if _alert_due("no-token"):
            logger.error("Метеринг: пуст TIMEWEB_AI_TOKEN — снапшоты used_tokens невозможны")
        return

    agents = {a["id"]: a for a in await _fetch_json("/cloud-ai/agents", "agents")}
    alerts: list[tuple[str, str]] = []

    for r in registry:
        agent_id = int(r["agent_id"])
        tenant = r["tenant_id"]
        a = agents.get(agent_id)
        if a is None:
            if _alert_due(f"gone:{agent_id}"):
                logger.warning("Метеринг: агент %s из реестра не найден в аккаунте", agent_id)
            continue
        # Финдинг №1: поле used_tokens может отсутствовать (частичная деградация
        # API / переименование ключа). НЕ трактуем как 0 — иначе на следующем тике
        # настоящий счётчик дал бы дельту = вся история и двойное списание.
        raw_used = a.get("used_tokens")
        if raw_used is None:
            if _alert_due(f"nofield:{agent_id}"):
                logger.warning(
                    "Метеринг: в ответе нет used_tokens для агента %s — тик пропущен", agent_id)
            continue
        used_now = int(raw_used)
        try:
            # HTTP-резолв модели — ВНЕ соединения пула (финдинг №7).
            model_slug = await _agent_model_slug(a)
            alert = await _process_agent_delta(agent_id, tenant, used_now, model_slug)
            if alert:
                alerts.append(alert)
        except Exception:  # noqa: BLE001 — один агент не валит остальных
            logger.exception("Метеринг: сбой обработки агента %s", agent_id)

    # ops-алерты — ВНЕ удержанного соединения, рейт-лимитированы.
    for key, text in alerts:
        if _alert_due(key):
            await _ops_alert(bot, text)


async def _process_agent_delta(
    agent_id: int, tenant, used_now: int, model_slug: str
) -> tuple[str, str] | None:
    """Обрабатывает дельту used_tokens агента под advisory-lock. Возвращает (key, text)
    для ops-алерта (шлёт вызывающий ВНЕ conn) либо None. Снапшот и списание — в ОДНОЙ
    транзакции (атомарность + lock против гонки двух воркеров, финдинг №4)."""
    async with db.pool.acquire() as conn:
        async with conn.transaction():
            # Сериализуем обработку одного агента между экземплярами воркера:
            # prev читается ПОД локом → второй воркер увидит уже обновлённый prev
            # и посчитает корректную дельту (а не потеряет её на схлопнутом ключе).
            await conn.execute(
                "select pg_advisory_xact_lock($1, $2)", _ADVISORY_NS, agent_id
            )
            prev = await conn.fetchrow(
                "select used_tokens, taken_at from agent_token_snapshots "
                "where agent_id = $1 order by taken_at desc limit 1",
                agent_id,
            )
            if prev is None:
                await conn.execute(
                    "insert into agent_token_snapshots (agent_id, tenant_id, used_tokens) "
                    "values ($1, $2, $3)",
                    agent_id, tenant, used_now,
                )
                logger.info("Метеринг: baseline агента %s = %s токенов (без списания)",
                            agent_id, used_now)
                return None

            prev_used = int(prev["used_tokens"])
            if used_now == prev_used:
                return None
            if used_now < prev_used:
                # Финдинг №1: 0 при ненулевом prev — почти наверняка глитч API,
                # а не реальный сброс (счётчик живого агента не падает в 0; id
                # агентов не переиспользуются). НЕ пишем baseline — ждём след. чтения.
                if used_now == 0:
                    if _alert_due(f"glitch:{agent_id}"):
                        logger.warning(
                            "Метеринг: used_tokens агента %s = 0 при prev=%s — похоже на "
                            "глитч API, тик пропущен (baseline НЕ сброшен)",
                            agent_id, prev_used)
                    return None
                # Ненулевое уменьшение = реальный сброс (пересоздание) → новый baseline.
                await conn.execute(
                    "insert into agent_token_snapshots (agent_id, tenant_id, used_tokens) "
                    "values ($1, $2, $3)",
                    agent_id, tenant, used_now,
                )
                logger.warning("Метеринг: счётчик агента %s уменьшился (%s → %s) — новый baseline",
                               agent_id, prev_used, used_now)
                return None

            plan = await get_tenant_plan(conn, tenant)
            if plan["billing_mode"] == "per_message":
                # Сверка себестоимости per_message-тенанта: снапшот пишем, не списываем.
                await conn.execute(
                    "insert into agent_token_snapshots (agent_id, tenant_id, used_tokens) "
                    "values ($1, $2, $3)",
                    agent_id, tenant, used_now,
                )
                return None

            price = await conn.fetchrow(
                "select price_in_microrub_per_1k as pin, price_out_microrub_per_1k as pout "
                "from model_prices where provider = 'timeweb-cloud-ai' and model = $1 "
                "order by effective_from desc limit 1",
                model_slug,
            )
            if price is None:
                # Цену не выдумываем (guardrail §10). Снапшот НЕ пишем — дельта
                # докопится и спишется, когда владелец впишет тариф из ЛК.
                logger.error(
                    "Метеринг: нет цены модели %r (timeweb-cloud-ai) в model_prices — "
                    "дельта агента %s (%s токенов) НЕ списана, копится. Впишите тариф из ЛК.",
                    model_slug, agent_id, used_now - prev_used,
                )
                return (f"price:{model_slug}",
                        f"Метеринг: нет цены модели «{model_slug}» — "
                        f"расход агента {agent_id} копится без списания.")

            delta = used_now - prev_used
            cost = ceil_mul(
                delta,
                blended_price_per_token(price["pin"], price["pout"], config.AI_OUT_TOKENS_SHARE),
            )
            await conn.execute(
                "insert into agent_token_snapshots (agent_id, tenant_id, used_tokens) "
                "values ($1, $2, $3)",
                agent_id, tenant, used_now,
            )
            row = await charge_usage(
                conn, tenant, cost,
                {
                    "kind": "llm", "provider": "timeweb-cloud-ai", "model": model_slug,
                    "units": {"tokens_total": delta},
                    "request_id": f"agent:{agent_id}",
                },
                f"ca:{agent_id}:{prev['taken_at'].isoformat()}",
                allow_negative=True,
            )
            logger.info(
                "Метеринг: агент %s, дельта %s токенов → списано %s ₽ (баланс %s ₽)",
                agent_id, delta,
                micro_to_rub_str(int(row["charged_microrub"])),
                micro_to_rub_str(int(row["balance_after_microrub"])),
            )
            return await _maybe_block_wallet(conn, tenant, plan)


# ── Шаг 3: per_message-планы — списание за исходящие сообщения Лии ────────────
async def _scan_per_message(bot: Bot) -> None:
    async with db.pool.acquire() as c:
        tenants = await c.fetch(
            """
            select distinct s.tenant_id
            from subscriptions s join plans p on p.id = s.plan_id
            where p.billing_mode = 'per_message'
              and s.status in ('trialing', 'active', 'past_due')
            """
        )
    for t in tenants:
        try:
            alert = await _scan_tenant_messages(t["tenant_id"])
            if alert and _alert_due(alert[0]):
                await _ops_alert(bot, alert[1])
        except Exception:  # noqa: BLE001 — один тенант не валит остальных
            logger.exception("Метеринг: сбой per_message-скана тенанта %s", t["tenant_id"])


async def _scan_tenant_messages(tenant) -> tuple[str, str] | None:
    """Списывает per_message-тенанту за новые исходящие сообщения Лии. Скан +
    продвижение hwm — ОДНА транзакция (атомарность против ложного рескана, финдинг №3).
    Возвращает (key, text) для ops-алерта либо None."""
    async with db.pool.acquire() as conn:
        async with conn.transaction():
            plan = await get_tenant_plan(conn, tenant)
            # Финдинг minor: тенант мог попасть в выборку по старой per_message-подписке,
            # но НОВЕЙШИЙ его план — уже cost_multiplier. Не тарифицируем за сообщения и
            # НЕ трогаем hwm (иначе сообщения «съелись» бы по 0 ₽ навсегда).
            if plan["billing_mode"] != "per_message":
                return None

            hwm_raw = await conn.fetchval(
                "select value from tenant_settings "
                "where tenant_id = $1 and key = 'metering_msg_hwm'",
                tenant,
            )
            if hwm_raw is None:
                # Финдинг №2: первый скан = baseline. Отсекаем ВСЮ существующую
                # историю Лии (её токены либо уже учтены снапшотами на прошлом
                # плане, либо относятся к до-подписочному периоду) — без списания.
                baseline = int(await conn.fetchval(
                    "select coalesce(max(id), 0) from messages "
                    "where tenant_id = $1 and source = 'liya' and direction = 'out'",
                    tenant,
                ) or 0)
                await conn.execute(
                    "insert into tenant_settings (tenant_id, key, value) "
                    "values ($1, 'metering_msg_hwm', $2) "
                    "on conflict (tenant_id, key) do update set value = $2, updated_at = now()",
                    tenant, str(baseline),
                )
                logger.info("Метеринг: per_message baseline тенанта %s = msg.id %s (без списания)",
                            tenant, baseline)
                return None

            hwm = int(hwm_raw)
            msgs = await conn.fetch(
                "select id from messages "
                "where tenant_id = $1 and source = 'liya' and direction = 'out' "
                "  and id > $2 and created_at < now() - make_interval(secs => $3) "
                "order by id limit $4",
                tenant, hwm, _MSG_GRACE_SECONDS, _MSG_SCAN_BATCH,
            )
            if not msgs:
                return None
            for m in msgs:
                await charge_usage(
                    conn, tenant, 0,
                    {
                        "kind": "message", "provider": None, "model": None,
                        "units": {"messages": 1}, "request_id": f"message:{m['id']}",
                    },
                    f"msg:{m['id']}",
                    allow_negative=True,
                )
            await conn.execute(
                "insert into tenant_settings (tenant_id, key, value) "
                "values ($1, 'metering_msg_hwm', $2) "
                "on conflict (tenant_id, key) do update set value = $2, updated_at = now()",
                tenant, str(msgs[-1]["id"]),
            )
            logger.info("Метеринг: тенант %s — списано %s сообщений Лии", tenant, len(msgs))
            return await _maybe_block_wallet(conn, tenant, plan)


# ── Общее ─────────────────────────────────────────────────────────────────────
async def _maybe_block_wallet(conn, tenant, plan: dict) -> tuple[str, str] | None:
    """prepaid-тенант ушёл в ноль/минус → мягкая пауза Лии + ops-алерт.
    Блокировка решается по ТЕКУЩЕМУ балансу кошелька (а не по balance_after
    возможной dup-строки — финдинг №3). Работает на том же conn (без второго
    acquire — финдинг №6). Тенант по умолчанию (Школа) НЕ блокируется никогда,
    даже получив план — §8.7 (финдинг №8): вместо паузы шлём ops-алерт.
    Возвращает (key, text) для ops-алерта либо None."""
    if not plan["prepaid"]:
        return None
    if tenant == db.default_tenant_id():
        return (f"school-plan:{tenant}",
                "⚠️ У тенанта по умолчанию (Школа) появился платный план — метеринг "
                "НЕ блокирует ИИ Школы (§8.7). Проверьте, что это намеренно (Wave 4).")
    # T-1B-4: доступные средства = доступный пул (истёкший период игнорируется) + аванс.
    bal = await conn.fetchval(
        "select (case when included_period_end > now() then included_microrub else 0 end) "
        "       + topup_microrub from credit_wallets where tenant_id = $1", tenant)
    if bal is None or int(bal) > 0:
        return None
    await db.set_ai_wallet_blocked(tenant, True, conn=conn)
    return (f"blocked:{tenant}",
            "Кошелёк тенанта пуст — ИИ-ответы на паузе. Клиенту нужно пополнить "
            "кошелёк в кабинете («Подписка»).")


async def _agent_model_slug(agent: dict) -> str:
    """Слаг модели агента для model_prices: model_id → public_name каталога →
    'deepseek-v4-pro-thinking'. Каталог кэшируется на час. Зовётся ВНЕ conn пула."""
    global _models_fetched_at, _models_cache
    model_id = agent.get("model_id")
    now = time.monotonic()
    if not _models_cache or now - _models_fetched_at > _MODELS_TTL:
        models = await _fetch_json("/cloud-ai/models", "models")
        _models_cache = {m["id"]: (m.get("public_name") or m.get("name") or "")
                         for m in models}
        _models_fetched_at = now
    return _slug(_models_cache.get(model_id) or f"model-{model_id}")


async def _fetch_json(path: str, key: str) -> list[dict]:
    """GET management-API Timeweb (Bearer TIMEWEB_AI_TOKEN). Бросает на не-200/битом JSON."""
    url = f"{config.TIMEWEB_API_BASE.rstrip('/')}/{path.lstrip('/')}"
    headers = {"authorization": f"Bearer {config.TIMEWEB_AI_TOKEN}"}
    async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as session:
        async with session.get(url, headers=headers) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Timeweb API {path}: HTTP {resp.status}")
            data = await resp.json()
    items = data.get(key)
    if not isinstance(items, list):
        raise RuntimeError(f"Timeweb API {path}: нет списка {key!r} в ответе")
    return items


async def _ops_alert(bot: Bot, text: str) -> None:
    """Best-effort ops-уведомление в служебный чат. Никогда не валит воркер.
    Вызывается ВНЕ удержанного соединения пула (финдинг №7)."""
    if config.OPS_CHAT_ID is None:
        logger.warning("OPS-алерт метеринга (нет OPS_CHAT_ID): %s", text)
        return
    try:
        await messaging.raw_send_text(bot, config.OPS_CHAT_ID, text)
    except Exception:  # noqa: BLE001
        logger.warning("Не удалось отправить ops-алерт метеринга: %s", text, exc_info=True)
