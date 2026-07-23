"""T-1D-2: тарификация эмбеддингов (self-host TEI `intfloat/multilingual-e5-base`).

Закрывает утечку §7.5: индексация базы знаний, RAG-ретрив и память агента считали эмбеддинги
БЕСПЛАТНО. Списание идёт через ЕДИНУЮ точку charge_usage (shared/metering.py) с
kind='embedding' + resource='embedding' → наценка resource_pricing['embedding']=3.000.

⚠️ ОБЪЁМ. TEI `/embed` возвращает ГОЛЫЙ массив векторов — ни `usage`, ни prompt_tokens
(в отличие от LLM-провайдеров). Токенайзера в репозитории нет. Поэтому объём ОЦЕНИВАЕМ по
длине текста (символы / CHARS_PER_TOKEN) и КЛАМПИМ по max_seq_len модели: TEI запущен с
`--auto-truncate` (docs/rag-embedder-vm.md) и МОЛЧА режет вход длиннее 512 токенов — платить
за отрезанный хвост нельзя. Оценка помечена в units как `tokens_est` (не `tokens_total`),
чтобы аналитика не путала её с точным счётчиком LLM.

⚠️ ИНЕРТНОСТЬ БЕЗ ЦЕНЫ (канон bot-telegram/ai.py + metering_worker.py, guardrail ТЗ §10
«цены не выдумываем»): нет строки model_prices для эмбеддера → рейт-лимитированный log.error
и ВЫХОД БЕЗ вызова charge_usage. НИКОГДА не звать charge_usage с нулевой себестоимостью:
он запишет строку леджера с charged=0 и unique(idempotence_key) НАВСЕГДА заблокирует
корректное списание по этому ключу (повтор вернёт нулевую dup-строку).
"""
from __future__ import annotations

import hashlib
import logging
import time
from decimal import Decimal

from shared.metering import charge_usage
from shared.money import ceil_mul

log = logging.getLogger("embed_metering")

# Ключ строки model_prices (решение владельца #3). Себестоимость — амортизация своей VM,
# вендорного тарифа за токен у self-host TEI не существует; цифру вписывает владелец (T-1D-1).
EMBED_PROVIDER = "timeweb-tei"
EMBED_MODEL = "multilingual-e5-base"

CHARS_PER_TOKEN = 3          # оценка для русского в multilingual-sentencepiece токенайзере e5
MAX_TOKENS_PER_TEXT = 512    # max_seq_len e5-base; TEI --auto-truncate режет длиннее МОЛЧА
_PRICE_WARN_INTERVAL = 3600  # рейт-лимит «нет цены» (как _PRICE_WARN_INTERVAL в ai.py)
_last_warn = 0.0

# Кэш цены (и ОТСУТСТВИЯ цены) — эмбеддинг-списание висит на ГОРЯЧЕМ пути ответа бота
# (эмбеддинг запроса на каждое входящее с RAG). Без кэша каждое сообщение стоило бы лишнего
# SELECT в model_prices — в т.ч. когда цены нет и списание всё равно инертно. TTL короткий:
# свежевписанная цена (T-1D-1) начнёт применяться не позже чем через _PRICE_TTL секунд.
_PRICE_TTL = 60.0
_price_cache: tuple[float, int | None] | None = None


def reset_price_cache() -> None:
    """Сбросить кэш цены (тесты/смоук: цена вписывается по ходу прогона)."""
    global _price_cache
    _price_cache = None


def est_tokens(text: str) -> int:
    """Оценка токенов по длине текста (счётчика TEI не даёт), с клампом по max_seq_len."""
    n = len(text or "")
    if n <= 0:
        return 0
    return min(-(-n // CHARS_PER_TOKEN), MAX_TOKENS_PER_TEXT)   # ceil-деление без float


async def _price_in_per_1k(conn) -> int | None:
    """Себестоимость эмбеддера в µRUB за 1000 токенов из model_prices (версионируемая).
    Фильтр `effective_from <= now()` — в отличие от прочих читателей model_prices, чтобы
    строка с будущей датой не начала применяться немедленно (канон billing_token_rate)."""
    global _price_cache
    now = time.monotonic()
    if _price_cache is not None and now - _price_cache[0] < _PRICE_TTL:
        return _price_cache[1]
    row = await conn.fetchrow(
        "select price_in_microrub_per_1k as pin from model_prices "
        "where provider = $1 and model = $2 and effective_from <= now() "
        "order by effective_from desc limit 1",
        EMBED_PROVIDER, EMBED_MODEL)
    val = int(row["pin"]) if row is not None else None
    _price_cache = (now, val)
    return val


def _warn_no_price() -> None:
    global _last_warn
    now = time.monotonic()
    if now - _last_warn >= _PRICE_WARN_INTERVAL:
        _last_warn = now
        log.error(
            "model_prices: нет строки %s/%s — эмбеддинги НЕ тарифицируются (T-1D-1 не закрыт). "
            "Расход идёт бесплатно; впишите цену, списание включится само.",
            EMBED_PROVIDER, EMBED_MODEL)


async def charge_embedding(conn, tenant_id, texts, *, scope: str) -> bool:
    """Списать эмбеддинги за `texts` (список строк, реально отправленных в TEI).

    scope — точка вызова (query|passage|memory|kb): различает событие в idempotence_key.
    Идемпотентность: `emb:{tenant}:{scope}:{sha1(контент)[:16]}` — повтор ТОГО ЖЕ контента
    тем же тенантом в той же точке не списывается дважды (защита от ретраев; одинаковый
    текст = одинаковый вектор, кэшируемо).

    allow_negative=True: TEI уже отработал и ресурс потрачен — отказать в списании нельзя.
    Возвращает True — списано; False — инертно (нет цены / пустой объём). Исключения НЕ гасит:
    гашение — задача вызывающей обёртки (db.charge_embedding), чтобы метеринг не ломал UX.
    """
    if not tenant_id or not texts:
        return False
    tokens = sum(est_tokens(t) for t in texts)
    if tokens <= 0:
        return False
    price = await _price_in_per_1k(conn)
    if price is None:
        _warn_no_price()
        return False                      # ИНЕРТНО: ключ НЕ сожжён — впишут цену, начнём списывать
    cost = ceil_mul(tokens * price, Decimal("0.001"))    # µRUB/1k × токены / 1000, округление вверх
    if cost <= 0:
        return False                      # нулевая себестоимость — ключ не жжём (см. докстринг)
    ref = hashlib.sha1("\n".join(texts).encode("utf-8")).hexdigest()[:16]
    await charge_usage(
        conn, tenant_id, cost,
        {"kind": "embedding", "resource": "embedding",
         "provider": EMBED_PROVIDER, "model": EMBED_MODEL,
         "units": {"tokens_est": tokens, "texts": len(texts),
                   "chars": sum(len(t or "") for t in texts)}},
        f"emb:{tenant_id}:{scope}:{ref}",
        allow_negative=True,
    )
    return True
