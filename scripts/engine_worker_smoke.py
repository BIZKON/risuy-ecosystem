"""Смоук S2 (DoD 5+6): контракт event_envelope v1 + каркас BaseWorker.

Envelope — чистые проверки без инфры; base_worker — против живого Redis
(SMOKE_REDIS_URL, БД №1 compose-стека). Транспортные env-ключи выставляются
ДО импорта engine-модулей: константы engine/common/config.py читаются на импорте.
Самоочистка: DEL smoke:worker:raw.
"""
import asyncio
import datetime as dt
import logging
import os
import time

# Фикстура транспорта — строго ДО импорта engine.common (см. докстринг).
os.environ["INGEST_STREAM"] = "smoke:worker:raw"
os.environ["THROTTLE_MIN_S"] = "0.1"
os.environ["THROTTLE_MAX_S"] = "0.2"

import redis.asyncio as redis  # noqa: E402 — импорт после env-фикстуры (осознанно)

from engine.common import base_worker, config, envelope  # noqa: E402 — то же

REDIS_URL = os.environ.get("SMOKE_REDIS_URL")
if not REDIS_URL:
    raise SystemExit("Задайте SMOKE_REDIS_URL (redis compose-стека, БД №1).")


def ожидать_ошибку(fn, reason: str) -> None:
    """Хелпер: fn обязана кинуть EnvelopeError с данным reason (→ dlq_reason)."""
    try:
        fn()
    except envelope.EnvelopeError as exc:
        assert exc.reason == reason, f"reason={exc.reason!r}, ожидался {reason!r}"
    else:
        raise AssertionError(f"ожидался EnvelopeError({reason})")


def check_envelope() -> None:
    """DoD 5: build→parse round-trip, make_external_id по 4 каналам, правила §3 спеки."""
    # ── round-trip всех 9 полей v1; parse получает bytes — как вернёт Redis ──
    posted = dt.datetime(2026, 7, 18, 12, 0, tzinfo=dt.timezone.utc)
    event = envelope.build(
        "telegram",
        envelope.make_external_id("telegram", 999500001, 7),
        "ищу подрядчика на ремонт",
        chat_ref="https://t.me/example",
        author_ref="tg:100500",
        posted_at=posted,
        lang="ru",
        metadata={"ключ": "значение"},
    )
    assert event["v"] == envelope.VERSION, "build() не проставила версию v"
    row = envelope.parse({k.encode(): v.encode() for k, v in event.items()})
    assert row["source_kind"] == "telegram"
    assert row["external_id"] == "999500001:7"
    assert row["body"] == "ищу подрядчика на ремонт"
    assert row["chat_ref"] == "https://t.me/example"
    assert row["author_ref"] == "tg:100500"
    assert row["posted_at"] == posted, f"posted_at round-trip сломан: {row['posted_at']!r}"
    assert row["lang"] == "ru"
    assert row["metadata"] == {"ключ": "значение"}

    # ── make_external_id: 4 канала (спека §3.1) ──
    assert envelope.make_external_id("telegram", 123, 45) == "123:45"
    assert envelope.make_external_id("vk", -1, 2) == "-1:2"
    assert envelope.make_external_id("vk", -1, 2, 3) == "-1:2:3"
    assert envelope.make_external_id("tenders", "0173200001426000123") == "0173200001426000123"
    assert envelope.make_external_id("boards", "youdo", 1) == "youdo:1"

    # ── ядовитое = permanent (fail-closed) ──
    ожидать_ошибку(lambda: envelope.make_external_id("telegram", 123, ""), "invalid_envelope")
    ожидать_ошибку(lambda: envelope.make_external_id("telegram"), "invalid_envelope")
    ожидать_ошибку(lambda: envelope.make_external_id("rss", 1), "invalid_envelope")
    ожидать_ошибку(
        lambda: envelope.parse(
            {"v": "2", "source_kind": "telegram", "external_id": "1:2", "body": "x"}
        ),
        "unsupported_version",
    )
    ожидать_ошибку(
        lambda: envelope.parse({"v": "1", "source_kind": "telegram", "body": "x"}),
        "invalid_envelope",
    )
    ожидать_ошибку(
        lambda: envelope.parse(
            {"v": "1", "source_kind": "rss", "external_id": "1", "body": "x"}
        ),
        "invalid_envelope",
    )
    # build: naive posted_at запрещён на границе продьюсера
    ожидать_ошибку(
        lambda: envelope.build("telegram", "1:2", "x", posted_at=dt.datetime(2026, 1, 1)),
        "invalid_envelope",
    )

    # ── опциональные деградируют мягко: запись живёт (сырьё ценнее строгости) ──
    row = envelope.parse(
        {
            "v": "1",
            "source_kind": "vk",
            "external_id": "-1:2",
            "body": "",
            "posted_at": "не-дата",
            "metadata": "не json",
            "неизвестное": "поле",
        }
    )
    assert row["posted_at"] is None, "кривой posted_at обязан отбрасываться (не DLQ)"
    assert row["metadata"] == {}, "невалидный metadata-JSON обязан заменяться на {}"
    assert "неизвестное" not in row, "неизвестное поле обязано игнорироваться"
    assert row["body"] == "", "пустой body — валиден (ключ присутствует)"
    # metadata валидный JSON, но не объект → тоже {}
    row = envelope.parse(
        {"v": "1", "source_kind": "vk", "external_id": "-1:3", "body": "x", "metadata": "[1, 2]"}
    )
    assert row["metadata"] == {}, "metadata-не-объект обязан заменяться на {}"
    print("  envelope v1: OK")


class ТестВоркер(base_worker.BaseWorker):
    """3 итерации: две пустые (замер throttle), третья эмитит ядовитое+валидное и стопит."""

    def __init__(self, redis_url: str) -> None:
        super().__init__(redis_url)
        self.метки: list[float] = []

    async def collect_once(self) -> list[dict]:
        self.метки.append(time.monotonic())
        if len(self.метки) < 3:
            return []
        self.stop.set()  # graceful из итерации — run() обязан доработать её и выйти сам
        ядовитое = {"v": envelope.VERSION, "source_kind": "telegram", "body": "без external_id"}
        валидное = envelope.build(
            "telegram", envelope.make_external_id("telegram", 999500002, 1), "валидное событие"
        )
        return [ядовитое, валидное]


async def check_worker() -> None:
    """DoD 6: throttle в границах, graceful по stop, emit fail-closed, floodwait_backoff."""
    r = redis.from_url(REDIS_URL)
    try:
        await r.delete(config.INGEST_STREAM)
        w = ТестВоркер(REDIS_URL)
        # graceful: run() завершается сам после stop.set() из 3-й итерации
        await asyncio.wait_for(w.run(), timeout=10)
        assert len(w.метки) == 3, f"итераций {len(w.метки)}, ожидалось 3"
        # throttle ≥ THROTTLE_MIN_S между итерациями (допуск 5 мс на таймер планировщика)
        for a, b in zip(w.метки, w.метки[1:]):
            assert b - a >= 0.095, f"throttle-интервал {b - a:.3f} с < THROTTLE_MIN_S=0.1 с"
        # emit fail-closed: из двух событий 3-й итерации в стриме только валидное
        n = await r.xlen(config.INGEST_STREAM)
        assert n == 1, f"в стриме {n} записей, ожидалась 1 (ядовитое обязано отвергаться)"
        # floodwait_backoff ждёт не меньше retry_after источника
        t0 = time.monotonic()
        await w.floodwait_backoff(0.3)
        assert time.monotonic() - t0 >= 0.3, "floodwait_backoff вернулся раньше retry_after"
        print("  base_worker: OK")
    finally:
        await r.delete(config.INGEST_STREAM)  # самоочистка
        await r.aclose()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    check_envelope()
    asyncio.run(check_worker())
    print("engine_worker_smoke: OK (envelope v1 + throttle/graceful/emit fail-closed)")


main()
