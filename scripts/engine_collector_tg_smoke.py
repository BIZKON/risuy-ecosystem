#!/usr/bin/env python3
"""Смоук S3 DoD 5: collector-telegram (фейк, БЕЗ сети/MTProto).

Проверяет:
  (1) _canon_chat_id: marked -100xxxxxxxxxx → внутренний id БЕЗ -100 (regression §13);
  (2) _to_envelope (ЧИСТАЯ): external_id chat_id:message_id без -100; chat_ref публичный при
      @username / fallback source_external_ref без; author_ref tg:<id> (опущен без sender);
      posted_at UTC; lang='ru';
  (3) идемпотентность через engine.ingest_consumer.handle_event: повтор того же external_id →
      INSERT 0 0 (engine.raw_messages не растёт);
  (4) floodwait-seam: _on_floodwait(acc, N) → пауза ≥ N (floodwait_backoff) + accounts.status
      ='floodwait', floodwait_until в будущем (аккаунт выходит из выборки claim);
  (5) graceful: фейк-коллектор дренирует фикстуру, эмитит в изолированный engine:raw-стрим и
      выходит по stop (fake — один проход, без сети).

Гард DSN: только эфемерный risuy_dev (роль engine_rw, owner engine-таблиц). Изоляция Redis:
своя БД + уникальный на прогон стрим. Самоочистка (accounts/sources/raw_messages/стрим/фикстура).
ENV: ENGINE_DSN (engine_rw), REDIS_URL, VAULT_MASTER_KEY (hex-64, для тест-аккаунта).
"""
import asyncio
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)  # shared.vault + engine.*

# ── env-фикстура ДО импорта engine.* (config читает окружение на импорте) ──────
DSN = os.environ.get("ENGINE_DSN") or os.environ.get("ENGINE_COLLECTOR_TG_SMOKE_DSN")
if not DSN or "/risuy_dev" not in DSN.split("?")[0]:
    raise SystemExit("Задайте ENGINE_DSN на эфемерном risuy_dev (роль engine_rw).")
os.environ["ENGINE_DSN"] = DSN
os.environ["REDIS_URL"] = os.environ.get("SMOKE_REDIS_URL", "redis://redis:6379/1")
RUN = uuid.uuid4().hex[:8]
os.environ["INGEST_STREAM"] = f"engine:raw:tgsmoke:{RUN}"
os.environ["INGEST_GROUP"] = f"tgsmoke-{RUN}"
os.environ["INGEST_CONSUMER"] = "tgsmoke-1"
FIXTURE = f"/tmp/engine_tg_fake_{RUN}.json"
os.environ["FAKE_TELEGRAM"] = FIXTURE

import asyncpg  # noqa: E402
import redis.asyncio as redis  # noqa: E402

from engine.collectors import registry, telegram  # noqa: E402
from engine.common import config, streams  # noqa: E402
from engine.ingest_consumer import main as ingest  # noqa: E402

REDIS_URL = os.environ["REDIS_URL"]
TENANT_A = "11111111-1111-1111-1111-111111111111"
SOURCE_REF = f"https://t.me/x10_smoke_{RUN}"
CH = "telegram"
LABEL = f"smoke-tg-{RUN}"
# Уникальные каноничные chat_id на прогон → external_id не пересекается с чужими строками.
UNIQ = int(RUN, 16) % (10**11)           # идемпотентность
UNIQG = (UNIQ ^ 0xABCDEF) % (10**11)     # graceful-фикстура


def test_pure() -> None:
    """(1) canon + (2) _to_envelope — чистые, без сети/БД."""
    assert telegram._canon_chat_id(-1001234567890) == 1234567890, "(1) canon: -100 не снят"
    assert telegram._canon_chat_id(777000) == 777000, "(1) canon исказил положительный id"
    assert telegram._canon_chat_id(-4512345) == 4512345, "(1) canon исказил базовую группу"
    # regression §13: ЧИСЛОВАЯ инверсия get_peer_id, НЕ строковый префикс '-100'.
    # Канал с internal ≥ 10**10 — marked '-102…'/'-110…' НЕ начинается на '-100' (префикс промахивался).
    assert telegram._canon_chat_id(-(10**12 + 20_000_000_000)) == 20_000_000_000, \
        "(1) canon: канал internal≥10**10 (marked -102…) сломан строковым префиксом"
    assert telegram._canon_chat_id(-(10**12 + 100_000_000_000)) == 100_000_000_000, \
        "(1) canon: канал internal=10**11 (marked -110…) сломан строковым префиксом"
    # Базовая группа с id на «100» (marked -1005) — префикс '-100' ложно принимал её за канал.
    assert telegram._canon_chat_id(-1005) == 1005, \
        "(1) canon: базовая группа id на «100» ложно распознана как канал"

    pub = telegram.CollectedMessage(
        chat_id=-1001234567890, message_id=42, raw_text="ищу подрядчика на ремонт, срочно",
        date="2026-07-18T09:30:00+00:00", sender_id=100500,
        chat_username="x10_channel", source_external_ref=SOURCE_REF,
    )
    ev = telegram._to_envelope(pub)
    assert ev["external_id"] == "1234567890:42", f"(2) external_id={ev['external_id']} (ждали 1234567890:42)"
    assert ev["source_kind"] == "telegram", "(2) source_kind"
    assert ev["chat_ref"] == "https://t.me/x10_channel", "(2) публичный chat_ref по username"
    assert ev["author_ref"] == "tg:100500", "(2) author_ref"
    assert ev["lang"] == "ru", "(2) lang"
    assert ev["posted_at"] == "2026-07-18T09:30:00+00:00", f"(2) posted_at={ev['posted_at']} (не UTC-iso)"
    assert ev["body"] == "ищу подрядчика на ремонт, срочно", "(2) body"

    priv = telegram.CollectedMessage(
        chat_id=-1009999999999, message_id=7, raw_text="приватный без username",
        date=datetime(2026, 7, 18, 10, 0, tzinfo=timezone.utc), sender_id=None,
        chat_username=None, source_external_ref=SOURCE_REF,
    )
    evp = telegram._to_envelope(priv)
    assert evp["chat_ref"] == SOURCE_REF, "(2) fallback chat_ref для приватного (source_external_ref)"
    assert "author_ref" not in evp, "(2) без sender_id author_ref не должен писаться"
    assert evp["external_id"] == "9999999999:7", f"(2) приватный external_id={evp['external_id']}"


async def _insert_account(pool, vault) -> str:
    """Активный telegram-аккаунт (vault-конверт, как CLI) для floodwait-теста."""
    acc_id = str(uuid.uuid4())
    ct, nonce, ver = vault.encrypt("SESSION-TG-" + uuid.uuid4().hex, aad=f"{acc_id}:session")
    async with pool.acquire() as c:
        await c.execute(
            "insert into engine.accounts (id, channel, label, ciphertext, nonce, key_version, status) "
            "values ($1,$2,$3,$4,$5,$6,'active')",
            acc_id, CH, LABEL, ct, nonce, ver,
        )
    return acc_id


async def test_floodwait(pool, vault) -> None:
    """(4) _on_floodwait: пауза floodwait_backoff ≥ N + accounts.status='floodwait'."""
    acc_id = await _insert_account(pool, vault)
    collector = telegram.TelegramCollector(REDIS_URL, DSN)
    seconds = 2.0
    t0 = time.monotonic()
    await collector._on_floodwait(acc_id, seconds)
    elapsed = time.monotonic() - t0
    assert elapsed >= seconds, f"(4) floodwait_backoff не выдержал паузу ({elapsed:.2f} < {seconds})"
    async with pool.acquire() as c:
        row = await c.fetchrow(
            "select status, floodwait_until, last_error from engine.accounts where id=$1", acc_id
        )
    assert row["status"] == "floodwait", f"(4) статус={row['status']} (ждали floodwait)"
    assert row["floodwait_until"] is not None and row["floodwait_until"] > datetime.now(timezone.utc), \
        "(4) floodwait_until не в будущем"
    assert row["last_error"] == "floodwait", "(4) last_error должен быть 'floodwait' (БЕЗ session)"


async def _count_raw(pool, external_id: str) -> int:
    async with pool.acquire() as c:
        return await c.fetchval(
            "select count(*) from engine.raw_messages where source_kind='telegram' and external_id=$1",
            external_id,
        )


async def test_idempotency(pool, r) -> None:
    """(3) один external_id дважды через ingest.handle_event → вторая вставка INSERT 0 0."""
    item = telegram.CollectedMessage(
        chat_id=-(10**12 + UNIQ), message_id=1, raw_text="дубль-тест", date="2026-07-18T09:30:00+00:00",
        sender_id=1, chat_username=None, source_external_ref=SOURCE_REF,
    )
    event = telegram._to_envelope(item)
    ext = event["external_id"]
    assert ext == f"{UNIQ}:1", f"(3) неожиданный external_id {ext}"

    await streams.ensure_group(r)

    async def _one_pass() -> None:
        await streams.emit(r, event)
        batch = await streams.read_batch(r)
        assert batch, "(3) read_batch пуст — событие не доехало до стрима"
        for message_id, fields in batch:
            await ingest.handle_event(pool, r, message_id, fields)

    await _one_pass()
    assert await _count_raw(pool, ext) == 1, "(3) первое событие не записано в raw_messages"
    await _one_pass()
    assert await _count_raw(pool, ext) == 1, "(3) дубль записан повторно — идемпотентность сломана"


async def test_graceful(pool, r) -> None:
    """(5) фейк-коллектор: фикстура → изолированный стрим, выход по stop (один проход)."""
    await registry.create_source(pool, TENANT_A, "telegram", "channel", SOURCE_REF, True)
    fixture = [
        {
            "chat_id": -(10**12 + UNIQG), "message_id": 10, "raw_text": "фикстура-1, ищу поставщика",
            "date": "2026-07-18T09:00:00+00:00", "sender_id": 5, "chat_username": "x10_channel",
            "source_external_ref": SOURCE_REF,
        },
        {
            "chat_id": -(10**12 + UNIQG), "message_id": 11, "raw_text": "фикстура-2, нужен подрядчик",
            "date": "2026-07-18T09:01:00+00:00", "sender_id": 6, "chat_username": "x10_channel",
            "source_external_ref": SOURCE_REF,
        },
    ]
    with open(FIXTURE, "w", encoding="utf-8") as fh:
        json.dump(fixture, fh, ensure_ascii=False)

    before = await r.xlen(config.INGEST_STREAM)
    collector = telegram.TelegramCollector(REDIS_URL, DSN)
    # stop выставляется самим коллектором (fake один проход); потолок — на случай регресса seam.
    await asyncio.wait_for(collector.run(), timeout=25)
    after = await r.xlen(config.INGEST_STREAM)
    assert after - before >= 2, f"(5) стрим вырос на {after - before} (ждали ≥2 события фикстуры)"

    # last_polled_at теперь пишется СРАЗУ в collect_once (пульс, [critic-fix I3]) — не ждёт
    # второго витка. Курсор в fake-один-проход остаётся в _deferred (нет второго collect_once) —
    # это ОК для TG (push, cursor=None; на рестарте максимум дубль). Проверяем и пульс, и эмит.
    last_polled = await pool.fetchval(
        "select last_polled_at from engine.sources where source_kind='telegram' and external_ref=$1",
        SOURCE_REF)
    assert last_polled is not None, "(5) last_polled_at не проставлен в текущем collect_once (пульс I3)"


async def _cleanup(pool, r) -> None:
    async with pool.acquire() as c:
        await c.execute("delete from engine.accounts where channel=$1 and label like $2", CH, f"smoke-tg-{RUN}%")
        await c.execute(
            "delete from engine.raw_messages where source_kind='telegram' "
            "and (external_id like $1 or external_id like $2)",
            f"{UNIQ}:%", f"{UNIQG}:%",
        )
        await c.execute("delete from engine.sources where source_kind='telegram' and external_ref=$1", SOURCE_REF)
    try:
        await r.delete(config.INGEST_STREAM)
        await r.delete(config.INGEST_DLQ_STREAM)
    except Exception:  # noqa: BLE001 — очистка стрима best-effort
        pass
    try:
        os.remove(FIXTURE)
    except FileNotFoundError:
        pass


async def amain() -> None:
    from shared import vault
    if not vault.enabled():
        raise SystemExit("VAULT_MASTER_KEY не задан/невалиден в env.")

    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    r = redis.from_url(REDIS_URL)
    try:
        await _cleanup(pool, r)
        test_pure()
        await test_floodwait(pool, vault)
        await test_idempotency(pool, r)
        await test_graceful(pool, r)
    finally:
        try:
            await _cleanup(pool, r)
        finally:
            await r.aclose()
            await pool.close()
    print("engine_collector_tg_smoke: OK (canon-100 + _to_envelope(pub/priv) + "
          "идемпотентность(ingest) + floodwait-seam + graceful-fake)")


asyncio.run(amain())
