"""Контракт event_envelope v1 (спека S2 §3) — ЕДИНСТВЕННАЯ точка сборки/разбора событий.

Коллекторы (S3/S13) собирают события ТОЛЬКО через build()/make_external_id();
консьюмер разбирает ТОЛЬКО через parse(). Прямой XADD мимо модуля — нарушение контракта.
Расширение полей = новая версия envelope, не мутация v1.
"""
from __future__ import annotations

import datetime as dt
import json
import logging

logger = logging.getLogger(__name__)

VERSION = "1"
SOURCE_KINDS = ("telegram", "vk", "boards", "tenders")
# Обязательные поля события; body может быть пустой строкой, но ключ обязан присутствовать.
REQUIRED = ("v", "source_kind", "external_id", "body")
OPTIONAL = ("chat_ref", "author_ref", "posted_at", "lang", "metadata")


class EnvelopeError(ValueError):
    """Ядовитое событие (permanent → DLQ). reason попадает в dlq_reason."""

    def __init__(self, message: str, reason: str = "invalid_envelope") -> None:
        super().__init__(message)
        self.reason = reason


def make_external_id(source_kind: str, *parts: object) -> str:
    """Единая точка сборки композитного external_id (спека §3.1, разделитель ':')."""
    if source_kind not in SOURCE_KINDS:
        raise EnvelopeError(f"неизвестный source_kind: {source_kind!r}")
    if not parts:
        raise EnvelopeError("external_id: нужна хотя бы одна часть")
    str_parts = [str(p).strip() for p in parts]
    if any(not p for p in str_parts):
        raise EnvelopeError(f"external_id: пустая часть в {parts!r}")
    return ":".join(str_parts)


def build(
    source_kind: str,
    external_id: str,
    body: str,
    *,
    chat_ref: str | None = None,
    author_ref: str | None = None,
    posted_at: dt.datetime | None = None,
    lang: str | None = None,
    metadata: dict | None = None,
) -> dict[str, str]:
    """Собирает валидное событие v1 — плоский dict str→str под XADD."""
    if source_kind not in SOURCE_KINDS:
        raise EnvelopeError(f"неизвестный source_kind: {source_kind!r}")
    if not (external_id or "").strip():
        raise EnvelopeError("external_id пуст")
    event = {
        "v": VERSION,
        "source_kind": source_kind,
        "external_id": external_id,
        "body": body or "",
    }
    if chat_ref:
        event["chat_ref"] = chat_ref
    if author_ref:
        event["author_ref"] = author_ref
    if posted_at is not None:
        if posted_at.tzinfo is None:
            raise EnvelopeError("posted_at должен быть timezone-aware (UTC)")
        event["posted_at"] = posted_at.astimezone(dt.timezone.utc).isoformat()
    if lang:
        event["lang"] = lang
    if metadata is not None:
        event["metadata"] = json.dumps(metadata, ensure_ascii=False)
    return event


def _decode(fields: dict) -> dict[str, str]:
    """bytes-хэш Redis → str-словарь; ошибка декодирования = ядовитое событие."""
    out: dict[str, str] = {}
    for k, v in fields.items():
        try:
            key = k.decode("utf-8") if isinstance(k, bytes) else str(k)
            val = v.decode("utf-8") if isinstance(v, bytes) else str(v)
        except UnicodeDecodeError as exc:
            raise EnvelopeError(f"поле не декодируется как utf-8: {exc}") from exc
        out[key] = val
    return out


def parse(fields: dict) -> dict:
    """Хэш события → нормализованный dict под колонки raw_messages.

    Кидает EnvelopeError на ядовитом (permanent); опциональные поля деградируют
    мягко (warning + запись без поля) — сырьё ценнее строгости (спека §3).
    Возвращает ключи: source_kind, external_id, body, chat_ref, author_ref,
    posted_at (datetime|None), lang, metadata (dict).
    """
    f = _decode(fields)
    # Отсутствие обязательного ключа v — invalid_envelope (спека §3: «обязательные
    # поля отсутствуют → ядовитое»); unsupported_version — ТОЛЬКО для присутствующей,
    # но незнакомой версии (иначе dlq_reason врал бы про причину).
    if "v" not in f:
        raise EnvelopeError("обязательное поле v отсутствует")
    version = f["v"]
    if version != VERSION:
        raise EnvelopeError(
            f"неподдерживаемая версия envelope: {version!r}", reason="unsupported_version"
        )
    for name in ("source_kind", "external_id"):
        if not f.get(name, "").strip():
            raise EnvelopeError(f"обязательное поле {name} отсутствует/пусто")
    if "body" not in f:
        raise EnvelopeError("обязательное поле body отсутствует")
    if f["source_kind"] not in SOURCE_KINDS:
        raise EnvelopeError(f"source_kind вне allow-list: {f['source_kind']!r}")

    known = set(REQUIRED) | set(OPTIONAL)
    extra = sorted(set(f) - known)
    if extra:
        logger.warning("envelope: неизвестные поля проигнорированы: %s", extra)

    posted_at: dt.datetime | None = None
    if f.get("posted_at"):
        try:
            posted_at = dt.datetime.fromisoformat(f["posted_at"])
            if posted_at.tzinfo is None:
                raise ValueError("naive datetime")
        except ValueError:
            logger.warning(
                "envelope %s: кривой posted_at %r — поле отброшено", f["external_id"], f["posted_at"]
            )
            posted_at = None

    metadata: dict = {}
    if f.get("metadata"):
        try:
            parsed = json.loads(f["metadata"])
            if not isinstance(parsed, dict):
                raise ValueError("metadata не объект")
            metadata = parsed
        except ValueError:
            logger.warning(
                "envelope %s: невалидный metadata-JSON — заменён на {}", f["external_id"]
            )

    return {
        "source_kind": f["source_kind"],
        "external_id": f["external_id"],
        "body": f["body"],
        "chat_ref": f.get("chat_ref") or None,
        "author_ref": f.get("author_ref") or None,
        "posted_at": posted_at,
        "lang": f.get("lang") or None,
        "metadata": metadata,
    }
