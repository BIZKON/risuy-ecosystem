"""RF-RAG: эмбеддинг запроса (self-host TEI, e5-base) + retrieval из pgvector.

Без OpenAI и без managed-KB: эмбеддер — наш TEI на VM (intfloat/multilingual-e5-base,
768-dim), вектор-стор — pgvector на том же кластере, где leads/orders. Данные не
покидают РФ-инфру.

ГЕЙТ (RAG аддитивен, по умолчанию ВЫКЛ): если не задан EMBEDDER_URL, или в панели
выключен kb_enabled, или база пустая, или любой сбой — retrieve_context() вернёт ""
и бот ответит ровно как раньше (без подмешивания справки). RAG не должен ни ломать
ответ, ни задерживать его при недоступном эмбеддере.

e5-семейство ТРЕБУЕТ префиксы: запрос — "query: …", документ — "passage: …"
(ингест ставит passage-префикс сам). Без них качество retrieval резко падает.
"""
import logging

import aiohttp

import config
import db

logger = logging.getLogger(__name__)

_QUERY_PREFIX = "query: "          # обязательный префикс e5 для запроса
_EMBED_TIMEOUT = aiohttp.ClientTimeout(total=10)


async def embed_query(text: str) -> list[float] | None:
    """Эмбеддинг ПОЛЬЗОВАТЕЛЬСКОГО запроса через TEI. None — если эмбеддер не настроен
    или недоступен (тогда retrieval пропускаем, бот отвечает без справки)."""
    base = (config.EMBEDDER_URL or "").strip().rstrip("/")
    if not base:
        return None
    url = f"{base}/embed"
    headers = {"content-type": "application/json"}
    if config.EMBEDDER_TOKEN:
        headers["authorization"] = f"Bearer {config.EMBEDDER_TOKEN}"
    payload = {"inputs": [_QUERY_PREFIX + text], "normalize": True}
    try:
        async with aiohttp.ClientSession(timeout=_EMBED_TIMEOUT) as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                if resp.status != 200:
                    logger.warning("TEI embed HTTP %s: %s", resp.status, (await resp.text())[:200])
                    return None
                data = await resp.json()
    except Exception as e:  # таймаут, сеть, не-JSON — RAG молча отключается
        logger.warning("TEI embed недоступен: %s", e)
        return None
    # TEI /embed → [[float, …]] (список эмбеддингов по числу inputs)
    if isinstance(data, list) and data and isinstance(data[0], list):
        return data[0]
    logger.warning("TEI embed неожиданный ответ: %s", str(data)[:200])
    return None


async def retrieve_context(text: str, tenant_id, persona: str | None = None) -> str:
    """Готовый блок справки для подмешивания в запрос агента (или "" — если RAG не дал
    результата). tenant_id=None → платформенная/School-справка. Фильтр по отделу: общая
    справка тенанта (role_tag пуст) + чанки персоны/отдела."""
    vec = await embed_query(text)
    if not vec:
        return ""
    try:
        chunks = await db.kb_search(vec, tenant_id, persona)
    except Exception as e:  # сбой БД/отсутствие таблицы (DDL не применён) → без справки
        logger.warning("kb_search не удался: %s", e)
        return ""
    if not chunks:
        return ""
    body = "\n\n".join(f"• {c.strip()}" for c in chunks)
    return (
        "📚 Справочные факты из базы знаний (опирайся на них, не придумывай сверх них; "
        "если ответа в фактах нет — так и скажи):\n\n" + body
    )


def augment(user_text: str, context: str) -> str:
    """Склейка справки и вопроса в одно сообщение агенту. Пусто → исходный текст."""
    if not context:
        return user_text
    return f"{context}\n\n———\nВопрос клиента: {user_text}"
