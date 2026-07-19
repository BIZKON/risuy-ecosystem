#!/usr/bin/env python3
"""Смоук S3 DoD 4: движковый CRUD engine.sources/search_profiles (RLS-канон, seed-CLI-путь).

Две роли:
  - engine_rw (owner, обходит RLS) — CRUD через engine.collectors.registry с ЯВНЫМ tenant_id
    (реальная граница) + set_tenant-backstop;
  - panel_rw (не-owner, RLS применяется) — РЕГРЕСС изоляции: ctx A видит источник A, НЕ видит B;
    форж чужого tenant_id при insert отклоняется WITH CHECK (не ломать engine_tenant_isolation_smoke).

Проверяет:
  (1) create под явным tenant_id A/B → list видит только свой тенант (явный фильтр под owner'ом);
  (2) идемпотентность source: повтор create того же (tenant,source_kind,external_ref) → тот же id,
      обновлённые поля (on conflict do update);
  (3) тумблер set_source_enabled;
  (4) кросс-тенант под owner'ом: тумблер источника B из-под тенанта A не срабатывает (None);
  (5) профили: create A/B, list-изоляция; round-trip intent_keywords(list)/geo(dict)/numeric-порогов;
  (5b) идемпотентность профиля (on conflict (tenant_id,name));
  (6) fail-closed: пустой tenant_id → ValueError (set_tenant, [critic-fix I3] — фолбэк запрещён);
  (7) РЕГРЕСС RLS под panel_rw: USING (ctx A не видит B) + WITH CHECK (форж отклонён).

Гард DSN: только эфемерный risuy_dev. Сид-тенанты 1111…/2222… (roles_bootstrap). Самоочистка по MARK.
ENV: ENGINE_REGISTRY_SMOKE_DSN (engine_rw), PANEL_RW_SMOKE_DSN (panel_rw).
"""
import asyncio
import json
import os
import sys
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)  # для engine.collectors.registry / engine.common.db

import asyncpg  # noqa: E402 — после sys.path.insert (как в engine_accounts_smoke.py)

ENGINE_DSN = os.environ.get("ENGINE_REGISTRY_SMOKE_DSN")
PANEL_DSN = os.environ.get("PANEL_RW_SMOKE_DSN")
if not ENGINE_DSN or "/risuy_dev" not in ENGINE_DSN.split("?")[0]:
    raise SystemExit("Задайте ENGINE_REGISTRY_SMOKE_DSN на эфемерном risuy_dev (роль engine_rw).")
if not PANEL_DSN or "/risuy_dev" not in PANEL_DSN.split("?")[0]:
    raise SystemExit("Задайте PANEL_RW_SMOKE_DSN на эфемерном risuy_dev (роль panel_rw).")

TA = "11111111-1111-1111-1111-111111111111"
TB = "22222222-2222-2222-2222-222222222222"

MARK = f"regsmoke-{uuid.uuid4().hex[:8]}"  # уникален на прогон → изоляция + самоочистка по LIKE
EXT_A = f"https://t.me/{MARK}-a"
EXT_B = f"https://t.me/{MARK}-b"
PROF_A = f"{MARK}-профиль-A"
PROF_B = f"{MARK}-профиль-B"


async def _rejected(conn, sql: str, *args) -> bool:
    # Вставка в сейвпоинте/tx: отказ WITH CHECK откатывает только её (эталон
    # engine_tenant_isolation_smoke._rejected).
    try:
        async with conn.transaction():
            await conn.execute(sql, *args)
        return False
    except asyncpg.PostgresError:
        return True


async def _cleanup(pool) -> None:
    async with pool.acquire() as c:  # engine_rw owner обходит RLS → чистит строки обоих тенантов
        await c.execute("delete from engine.sources where external_ref like $1", f"%{MARK}%")
        await c.execute("delete from engine.search_profiles where name like $1", f"%{MARK}%")


async def main() -> None:
    from engine.collectors import registry

    pool = await asyncpg.create_pool(ENGINE_DSN, min_size=1, max_size=4)
    panel = await asyncpg.create_pool(PANEL_DSN, min_size=1, max_size=2)
    try:
        await _cleanup(pool)

        # (1) create под явным tenant_id A и B; list видит только свой тенант.
        sid_a = await registry.create_source(pool, TA, "telegram", "channel", EXT_A, enabled=True)
        sid_b = await registry.create_source(pool, TB, "telegram", "channel", EXT_B, enabled=True)
        assert sid_a and sid_b and sid_a != sid_b, "(1) create_source вернул пустой/одинаковый id"
        rows_a = await registry.list_sources(pool, TA)
        refs_a = {r["external_ref"] for r in rows_a}
        assert EXT_A in refs_a, "(1) A не видит свой источник"
        assert EXT_B not in refs_a, "(1) A видит источник B (явный фильтр tenant_id сломан)"
        assert all(str(r["tenant_id"]) == TA for r in rows_a), "(1) в выдаче A есть чужие тенанты"

        # (2) идемпотентность source: повтор create → тот же id, обновлённый enabled.
        sid_a2 = await registry.create_source(pool, TA, "telegram", "channel", EXT_A, enabled=False)
        assert sid_a2 == sid_a, "(2) повторный create сменил id (не идемпотентно)"
        row_a = next(r for r in await registry.list_sources(pool, TA) if r["external_ref"] == EXT_A)
        assert row_a["enabled"] is False, "(2) on conflict do update не обновил enabled"

        # (3) тумблер enabled.
        toggled = await registry.set_source_enabled(pool, TA, sid_a, True)
        assert toggled == sid_a, "(3) set_source_enabled не вернул id"
        row_a = next(r for r in await registry.list_sources(pool, TA) if r["id"] == sid_a)
        assert row_a["enabled"] is True, "(3) enabled не переключился"

        # (4) кросс-тенант под owner'ом: тумблер источника B из-под A → None (WHERE tenant_id=A).
        cross = await registry.set_source_enabled(pool, TA, sid_b, False)
        assert cross is None, "(4) A смог переключить источник B (граница tenant_id пробита)"
        row_b = next(r for r in await registry.list_sources(pool, TB) if r["id"] == sid_b)
        assert row_b["enabled"] is True, "(4) источник B изменён из-под тенанта A"

        # (5) профили: create A/B, list-изоляция; round-trip keywords/geo/numeric.
        kw = ["ремонт", "подрядчик"]
        geo = {"lat": 55.75, "lon": 37.62}
        pid_a = await registry.create_profile(
            pool, TA, PROF_A, intent_keywords=kw, industry="строительство", geo=geo,
            min_intent_score=0.6, min_urgency=0.4)
        pid_b = await registry.create_profile(pool, TB, PROF_B, intent_keywords=["иное"])
        assert pid_a and pid_b and pid_a != pid_b, "(5) create_profile вернул пустой/одинаковый id"
        profs_a = await registry.list_profiles(pool, TA)
        names_a = {p["name"] for p in profs_a}
        assert PROF_A in names_a, "(5) A не видит свой профиль"
        assert PROF_B not in names_a, "(5) A видит профиль B (изоляция нарушена)"
        pa = next(p for p in profs_a if p["name"] == PROF_A)
        assert json.loads(pa["intent_keywords"]) == kw, "(5) intent_keywords round-trip сломан"
        assert json.loads(pa["geo"]) == geo, "(5) geo round-trip сломан"
        assert float(pa["min_intent_score"]) == 0.6 and float(pa["min_urgency"]) == 0.4, \
            "(5) numeric-пороги round-trip сломан"

        # (5b) идемпотентность профиля: повтор create → тот же id.
        pid_a2 = await registry.create_profile(pool, TA, PROF_A, intent_keywords=["ремонт"])
        assert pid_a2 == pid_a, "(5b) повторный create профиля сменил id (не идемпотентно)"

        # (6) fail-closed: пустой tenant_id → ValueError (никаких тенант-фолбэков).
        try:
            await registry.create_source(pool, "", "telegram", "channel", f"{MARK}-empty")
            raise AssertionError("(6) пустой tenant_id не упал (фолбэк на дефолт запрещён)")
        except ValueError:
            pass

        # (7) РЕГРЕСС RLS под panel_rw (не-owner → RLS применяется).
        async with panel.acquire() as pc:
            await pc.execute("select set_config('app.tenant_id',$1,false)", TA)
            seen_a = await pc.fetchval(
                "select count(*) from engine.sources where external_ref=$1", EXT_A)
            assert seen_a == 1, f"(7) panel_rw ctx A не видит источник A (видит {seen_a})"
            seen_b = await pc.fetchval(
                "select count(*) from engine.sources where external_ref=$1", EXT_B)
            assert seen_b == 0, f"(7) panel_rw ctx A видит источник B — RLS USING пробита ({seen_b})"
            forged = await _rejected(
                pc,
                "insert into engine.sources (tenant_id, source_kind, external_ref) "
                "values ($1,'telegram',$2)",
                TB, f"{MARK}-forge")
            assert forged, "(7) форж чужого tenant_id (TB под ctx A) не отклонён WITH CHECK"

        await _cleanup(pool)
    finally:
        try:
            await _cleanup(pool)
        finally:
            await pool.close()
            await panel.close()
    print("engine_registry_smoke: OK (CRUD sources/profiles + tenant-изоляция + идемпотентность + "
          "fail-closed + RLS-регресс panel_rw)")


asyncio.run(main())
