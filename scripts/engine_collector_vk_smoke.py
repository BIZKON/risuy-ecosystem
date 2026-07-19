#!/usr/bin/env python3
"""Смоук S3 DoD 6: collector-vk (фейк, БЕЗ сети VK).

Проверяет:
  (1) ЧИСТЫЕ маппинги: _post_to_envelope → external_id {owner_id}:{post_id} (знак '-'
      сообщества сохранён), chat_ref/author_ref/posted_at(UTC); _comment_to_envelope →
      {owner_id}:{post_id}:{comment_id} (три части); _unix_to_utc; _wall_ref;
  (2) newsfeed.search: инжектированный VKClient отдаёт items+next_from → poll_source мапит
      посты, а sources.cursor двигается на next_from ПОСЛЕ collect_once (контракт курсора);
  (3) wall.get: посты стены мапятся через тот же _post_to_envelope;
  (4) идемпотентность через engine.ingest_consumer.handle_event: повтор external_id →
      INSERT 0 0 (engine.raw_messages не растёт);
  (5) лимит VK error 6 → _on_vk_flood → floodwait_backoff (пауза ≥ _VK_FLOOD_BACKOFF_S);
  (6) фейк-фикстура FAKE_VK → _build_fake_client (реплей + __error__ → VKError).

Гард DSN: только эфемерный risuy_dev (роль engine_rw, owner engine-таблиц). Изоляция Redis:
своя БД + уникальный на прогон стрим. Самоочистка (sources/profiles/raw_messages/стрим/фикстура).
ENV: ENGINE_DSN (engine_rw), REDIS_URL (переопределяется на изолированную БД).
"""
import asyncio
import json
import os
import sys
import time
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)  # engine.* + shared.*

# ── env-фикстура ДО импорта engine.* (config читает окружение на импорте) ──────
DSN = os.environ.get("ENGINE_DSN") or os.environ.get("ENGINE_COLLECTOR_VK_SMOKE_DSN")
if not DSN or "/risuy_dev" not in DSN.split("?")[0]:
    raise SystemExit("Задайте ENGINE_DSN на эфемерном risuy_dev (роль engine_rw).")
os.environ["ENGINE_DSN"] = DSN
os.environ["REDIS_URL"] = os.environ.get("SMOKE_REDIS_URL", "redis://redis:6379/1")
RUN = uuid.uuid4().hex[:8]
os.environ["INGEST_STREAM"] = f"engine:raw:vksmoke:{RUN}"
os.environ["INGEST_GROUP"] = f"vksmoke-{RUN}"
os.environ["INGEST_CONSUMER"] = "vksmoke-1"
FIXTURE = f"/tmp/engine_vk_fake_{RUN}.json"
os.environ["FAKE_VK"] = FIXTURE

import asyncpg  # noqa: E402
import redis.asyncio as redis  # noqa: E402

from engine.collectors import registry, vk  # noqa: E402
from engine.common import streams  # noqa: E402
from engine.ingest_consumer import main as ingest  # noqa: E402

REDIS_URL = os.environ["REDIS_URL"]
TENANT_A = "11111111-1111-1111-1111-111111111111"
# Идентичности источников/профилей (уникальны на прогон → изоляция от чужих строк).
CH_REF = f"vk_ch_{RUN}"                                   # newsfeed-источник (kind channel)
WALL_REF = str(-(7_000_000 + int(RUN, 16) % 100_000))    # wall-источник (owner_id сообщества)
FLOOD_REF = f"vk_flood_{RUN}"                             # источник для теста лимита
PROFILE_CUR = f"vk-smoke-cur-{RUN}"                       # профиль newsfeed-теста
PROFILE_FLOOD = f"vk-smoke-flood-{RUN}"                   # профиль flood-теста
ALL_REFS = [CH_REF, WALL_REF, FLOOD_REF]
ALL_PROFILES = [PROFILE_CUR, PROFILE_FLOOD]
NEXT_FROM = f"next/{RUN}"
# external_id идемпотентности: owner фикс, post_id уникален на прогон.
DUP_OWNER = -777
DUP_POST = int(RUN, 16) % (10**9) + 1
DUP_EXT = f"{DUP_OWNER}:{DUP_POST}"


async def _drop_source(pool, ref: str) -> None:
    """Удалить источник по external_ref (изоляция collector-тестов: collect_once читает
    ВСЕ enabled-vk-источники платформенно — незачищенный источник добавил бы чужой опрос)."""
    async with pool.acquire() as c:
        await c.execute(
            "delete from engine.sources where source_kind='vk' and external_ref=$1", ref)


async def _drop_profile(pool, name: str) -> None:
    async with pool.acquire() as c:
        await c.execute(
            "delete from engine.search_profiles where tenant_id=$1 and name=$2", TENANT_A, name)


def test_pure() -> None:
    """(1) чистые функции — без сети/БД."""
    # _unix_to_utc
    assert vk._unix_to_utc(None) is None, "(1) _unix_to_utc(None)"
    moment = vk._unix_to_utc(1_721_300_000)
    assert moment.tzinfo is not None and moment.utcoffset().total_seconds() == 0, "(1) posted_at не UTC"

    # _wall_ref
    assert vk._wall_ref("-321") == {"owner_id": -321}, "(1) _wall_ref числовой owner_id"
    assert vk._wall_ref("https://vk.com/durov") == {"domain": "durov"}, "(1) _wall_ref domain из URL"
    assert vk._wall_ref("club123") == {"domain": "club123"}, "(1) _wall_ref короткое имя"

    # _post_to_envelope: знак сообщества сохранён, три поля-ссылки
    post = {"id": 456, "owner_id": -123, "from_id": 789, "date": 1_721_300_000,
            "text": "ищу поставщика оборудования, срочно"}
    ep = vk._post_to_envelope(post)
    assert ep["external_id"] == "-123:456", f"(1) пост external_id={ep['external_id']} (ждали -123:456)"
    assert ep["source_kind"] == "vk", "(1) source_kind"
    assert ep["chat_ref"] == "https://vk.com/wall-123_456", "(1) chat_ref поста"
    assert ep["author_ref"] == "vk:789", "(1) author_ref поста"
    assert ep["lang"] == "ru", "(1) lang"
    assert ep["posted_at"] == "2024-07-18T10:53:20+00:00", f"(1) posted_at={ep['posted_at']}"
    assert ep["body"] == "ищу поставщика оборудования, срочно", "(1) body"

    # пост без from_id → author_ref опускается
    ep2 = vk._post_to_envelope({"id": 1, "owner_id": -5, "date": 1_721_300_000, "text": "x"})
    assert "author_ref" not in ep2, "(1) без from_id author_ref не пишется"

    # _comment_to_envelope: три части external_id (owner:post:comment)
    comment = {"id": 789, "from_id": 42, "date": 1_721_300_100, "text": "а сколько стоит?"}
    ec = vk._comment_to_envelope(comment, -123, 456)
    assert ec["external_id"] == "-123:456:789", f"(1) коммент external_id={ec['external_id']}"
    assert ec["chat_ref"] == "https://vk.com/wall-123_456?reply=789", "(1) chat_ref коммента"
    assert ec["author_ref"] == "vk:42", "(1) author_ref коммента"


async def test_newsfeed_cursor(pool) -> None:
    """(2) newsfeed.search: маппинг постов + курсор next_from двигается ПОСЛЕ collect_once."""
    async def _api(method, params):
        if method == "newsfeed.search":
            return {"items": [{"id": 456, "owner_id": -123, "from_id": 789,
                               "date": 1_721_300_000, "text": "ищу поставщика"}],
                    "next_from": NEXT_FROM}
        return {}

    collector = vk.VKCollector(REDIS_URL, DSN)
    collector._client = vk.VKClient("tok", api=_api)  # инжект — _ensure_client становится no-op
    try:
        await registry.create_source(pool, TENANT_A, "vk", "channel", CH_REF, True)
        await registry.create_profile(pool, TENANT_A, PROFILE_CUR,
                                      intent_keywords=["поставщик", "оборудование"])
        # pass1: опрос → событие смаплено, курсор намечен в _deferred (ещё не в БД)
        events = await collector.collect_once()
        mine = [e for e in events if e["external_id"] == "-123:456"]
        assert mine, "(2) newsfeed событие не смаплено в collect_once"
        assert mine[0]["chat_ref"] == "https://vk.com/wall-123_456", "(2) chat_ref события"
        assert mine[0]["author_ref"] == "vk:789", "(2) author_ref события"
        cur0 = await pool.fetchval(
            "select cursor from engine.sources where source_kind='vk' and external_ref=$1", CH_REF)
        assert cur0 is None, "(2) курсор персистнут ДО подтверждения доставки (нарушение контракта)"
        # pass2: _persist_deferred в начале collect_once записывает курсор pass1
        await collector.collect_once()
        cur1 = await pool.fetchval(
            "select cursor from engine.sources where source_kind='vk' and external_ref=$1", CH_REF)
        assert cur1 == NEXT_FROM, f"(2) курсор не сдвинулся на next_from (в БД {cur1!r})"
    finally:
        await _drop_source(pool, CH_REF)
        await _drop_profile(pool, PROFILE_CUR)
        if collector._pg is not None:
            await collector._pg.close()


async def test_wall(pool) -> None:
    """(3) wall.get: посты стены мапятся через _post_to_envelope."""
    async def _api(method, params):
        if method == "wall.get":
            return {"items": [{"id": 654, "owner_id": -321, "from_id": 111,
                               "date": 1_721_300_000, "text": "нужен подрядчик на монтаж"}]}
        return {}

    collector = vk.VKCollector(REDIS_URL, DSN)
    collector._client = vk.VKClient("tok", api=_api)
    try:
        await registry.create_source(pool, TENANT_A, "vk", "wall", WALL_REF, True)
        events = await collector.collect_once()
        mine = [e for e in events if e["external_id"] == "-321:654"]
        assert mine, "(3) wall.get пост не смаплен"
        assert mine[0]["chat_ref"] == "https://vk.com/wall-321_654", "(3) chat_ref wall-поста"
    finally:
        await _drop_source(pool, WALL_REF)
        if collector._pg is not None:
            await collector._pg.close()


async def _count_raw(pool, external_id: str) -> int:
    async with pool.acquire() as c:
        return await c.fetchval(
            "select count(*) from engine.raw_messages where source_kind='vk' and external_id=$1",
            external_id,
        )


async def test_idempotency(pool, r) -> None:
    """(4) один external_id дважды через ingest.handle_event → вторая вставка INSERT 0 0."""
    event = vk._post_to_envelope(
        {"id": DUP_POST, "owner_id": DUP_OWNER, "from_id": 5,
         "date": 1_721_300_000, "text": "дубль-тест vk"})
    assert event["external_id"] == DUP_EXT, f"(4) неожиданный external_id {event['external_id']}"

    await streams.ensure_group(r)

    async def _one_pass() -> None:
        await streams.emit(r, event)
        batch = await streams.read_batch(r)
        assert batch, "(4) read_batch пуст — событие не доехало до стрима"
        for message_id, fields in batch:
            await ingest.handle_event(pool, r, message_id, fields)

    await _one_pass()
    assert await _count_raw(pool, DUP_EXT) == 1, "(4) первое событие не записано в raw_messages"
    await _one_pass()
    assert await _count_raw(pool, DUP_EXT) == 1, "(4) дубль записан повторно — идемпотентность сломана"


async def test_flood(pool) -> None:
    """(5) VK error 6 → poll_source ловит → _on_vk_flood → floodwait_backoff (пауза ≥ backoff)."""
    async def _api(method, params):
        raise vk.VKError(6, "Too many requests per second")

    collector = vk.VKCollector(REDIS_URL, DSN)
    collector._client = vk.VKClient("tok", api=_api)
    try:
        await registry.create_source(pool, TENANT_A, "vk", "channel", FLOOD_REF, True)
        await registry.create_profile(pool, TENANT_A, PROFILE_FLOOD, intent_keywords=["лид"])
        # профиль даёт ключевые слова → q непустой → вызов VK состоится → инжект бросит error 6
        t0 = time.monotonic()
        await collector.collect_once()
        elapsed = time.monotonic() - t0
        assert elapsed >= vk._VK_FLOOD_BACKOFF_S, \
            f"(5) floodwait_backoff не выдержал паузу ({elapsed:.2f} < {vk._VK_FLOOD_BACKOFF_S})"
        # сервисный ключ без строки accounts → _account_id None → пометки нет, но и не падаем
        assert collector._account_id is None, "(5) в фейке account_id не должен появляться"
    finally:
        await _drop_source(pool, FLOOD_REF)
        await _drop_profile(pool, PROFILE_FLOOD)
        if collector._pg is not None:
            await collector._pg.close()


async def test_fixture_fake() -> None:
    """(6) FAKE_VK-фикстура → _build_fake_client: реплей ответа + __error__ → VKError."""
    fixture = {
        "newsfeed.search": {"items": [{"id": 9, "owner_id": -9, "date": 1_721_300_000, "text": "y"}],
                            "next_from": "nf"},
        "err.method": {"__error__": {"code": 6, "msg": "flood"}},
    }
    with open(FIXTURE, "w", encoding="utf-8") as fh:
        json.dump(fixture, fh, ensure_ascii=False)

    collector = vk.VKCollector(REDIS_URL, DSN)
    try:
        await collector._ensure_client()  # фейк-режим (FAKE_VK) → строит реплей-клиент
        assert collector._client is not None, "(6) фикстура FAKE_VK не подняла клиент"
        resp = await collector._client.call("newsfeed.search", {"q": "x"})
        assert isinstance(resp, dict) and resp.get("items"), "(6) реплей фикстуры не вернул items"
        raised = 0
        try:
            await collector._client.call("err.method", {})
        except vk.VKError as exc:
            raised = exc.code
        assert raised == 6, "(6) фикстура __error__ должна бросать VKError(code=6)"
    finally:
        if collector._pg is not None:
            await collector._pg.close()


async def _cleanup(pool, r) -> None:
    async with pool.acquire() as c:
        await c.execute(
            "delete from engine.raw_messages where source_kind='vk' and external_id=$1", DUP_EXT)
        await c.execute(
            "delete from engine.sources where source_kind='vk' and external_ref = any($1::text[])",
            ALL_REFS,
        )
        await c.execute(
            "delete from engine.search_profiles where tenant_id=$1 and name = any($2::text[])",
            TENANT_A, ALL_PROFILES,
        )
    try:
        await r.delete(os.environ["INGEST_STREAM"])
        await r.delete("engine:raw:dlq")
    except Exception:  # noqa: BLE001 — очистка стрима best-effort
        pass
    try:
        os.remove(FIXTURE)
    except FileNotFoundError:
        pass


async def amain() -> None:
    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    r = redis.from_url(REDIS_URL)
    try:
        await _cleanup(pool, r)
        test_pure()
        await test_newsfeed_cursor(pool)
        await test_wall(pool)
        await test_idempotency(pool, r)
        await test_flood(pool)
        await test_fixture_fake()
    finally:
        try:
            await _cleanup(pool, r)
        finally:
            await r.aclose()
            await pool.close()
    print("engine_collector_vk_smoke: OK (_post/_comment/_wall_ref + newsfeed-курсор(next_from) + "
          "wall + идемпотентность(ingest) + error6-floodwait + fixture-fake)")


asyncio.run(amain())
