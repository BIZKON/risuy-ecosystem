"""Конфиг движка: env-слой транспорта (спека S2 §6).

Все транспортные ключи — с дефолтами (compose их не дублирует); обязательные
(ENGINE_DSN, REDIS_URL) сервисы читают через req() на старте, НЕ на импорте
(смоуки импортируют модули без полного окружения).
Новые ключи добавлять сюда — единая точка (урок x10: «ключ не проброшен в bindings»).
"""
import os
import socket


def req(name: str) -> str:
    """Обязательная переменная окружения; пустая/отсутствующая — громкий отказ на старте."""
    val = os.environ.get(name, "")
    if not val:
        raise RuntimeError(f"Обязательная переменная окружения {name} не задана")
    return val


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, str(default)))


def _env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, str(default)))


# ── Транспорт (S2) ──────────────────────────────────────────────────────────
INGEST_STREAM = os.environ.get("INGEST_STREAM", "engine:raw")
INGEST_GROUP = os.environ.get("INGEST_GROUP", "ingest")
INGEST_CONSUMER = os.environ.get("INGEST_CONSUMER", socket.gethostname())
INGEST_BATCH = _env_int("INGEST_BATCH", 100)
INGEST_BLOCK_MS = _env_int("INGEST_BLOCK_MS", 5000)
INGEST_MAX_DELIVERIES = _env_int("INGEST_MAX_DELIVERIES", 5)
INGEST_RECLAIM_IDLE_MS = _env_int("INGEST_RECLAIM_IDLE_MS", 60_000)
INGEST_DLQ_STREAM = os.environ.get("INGEST_DLQ_STREAM", "engine:raw:dlq")
STREAM_MAXLEN = _env_int("STREAM_MAXLEN", 100_000)
INGEST_BACKOFF_MAX_S = _env_float("INGEST_BACKOFF_MAX_S", 30.0)
INGEST_LAG_LOG_EVERY_S = _env_float("INGEST_LAG_LOG_EVERY_S", 60.0)
THROTTLE_MIN_S = _env_float("THROTTLE_MIN_S", 1.0)
THROTTLE_MAX_S = _env_float("THROTTLE_MAX_S", 2.0)

# ── Коллекторы (S3 §4) ────────────────────────────────────────────────────────
# VAULT_MASTER_KEY читает напрямую shared/vault.py (_master_key) из окружения —
# сюда НЕ выносим и НЕ оборачиваем в req() на импорте: коллектор проверяет наличие
# ключа через vault.enabled() на старте (fail-fast только в боевом, не в фейке).
SOURCES_POLL_INTERVAL_S = _env_int("SOURCES_POLL_INTERVAL_S", 30)
TG_COLLECTOR_NAME = os.environ.get("TG_COLLECTOR_NAME", socket.gethostname())
VK_COLLECTOR_NAME = os.environ.get("VK_COLLECTOR_NAME", socket.gethostname())
VK_API_VERSION = os.environ.get("VK_API_VERSION", "5.199")
VK_API_BASE = os.environ.get("VK_API_BASE", "https://api.vk.com/method")
COLLECTOR_HEALTH_PORT = _env_int("COLLECTOR_HEALTH_PORT", 8091)
# Фейк-режим смоуков: путь к JSON-фикстуре; пусто → боевой клиент (сеть).
FAKE_TELEGRAM = os.environ.get("FAKE_TELEGRAM", "")
FAKE_VK = os.environ.get("FAKE_VK", "")
# MTProto app-креды (одни на платформу): обязательны в боевом (req в main коллектора),
# в фейк-режиме не нужны — поэтому здесь мягкий get, НЕ req() на импорте.
TG_API_ID = os.environ.get("TG_API_ID", "")
TG_API_HASH = os.environ.get("TG_API_HASH", "")
