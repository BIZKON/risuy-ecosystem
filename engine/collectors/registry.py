"""CRUD engine.sources/engine.search_profiles — движковый минимум S3 (tenant-scoped).

Пул — под ролью engine_rw (Вариант A: owner tenant-scoped таблиц → RLS обходится
owner'ом). Изоляция держится на ЯВНОМ tenant_id в каждом запросе + `set_tenant`-backstop
(engine.common.db.set_tenant) в той же tx на том же соединении:
  - явный tenant_id в WHERE/INSERT — реальная граница под owner'ом (RLS не срабатывает);
  - set_tenant — backstop и дисциплина: если роль сменится на не-owner ИЛИ включат
    FORCE ROW LEVEL SECURITY, WITH CHECK/USING поймают форж; пустой tenant_id падает
    (fail-closed, [critic-fix I3] — никаких тенант-фолбэков).

Панельный tenant-UI (admin-panel) сядет ПОВЕРХ этих функций в S7M без переписывания —
поэтому сигнатуры стабильны и не тянут ботовый/панельный код (engine изолирован).
jsonb-поля кодируются json.dumps(ensure_ascii=False) + ::jsonb — канон engine.ingest_consumer.
"""
from __future__ import annotations

import json

from engine.common import db


async def create_source(pool, tenant_id, source_kind, kind, external_ref, enabled=True):
    """Создать/обновить источник (идемпотентно по unique (tenant_id, source_kind, external_ref)).

    on conflict → обновляем kind/enabled (ротация конфигурации источника). Возвращает id.
    """
    async with pool.acquire() as conn:
        await db.set_tenant(conn, tenant_id)
        return await conn.fetchval(
            """
            insert into engine.sources (tenant_id, source_kind, kind, external_ref, enabled)
            values ($1, $2, $3, $4, $5)
            on conflict (tenant_id, source_kind, external_ref) do update
              set kind = excluded.kind, enabled = excluded.enabled, updated_at = now()
            returning id
            """,
            tenant_id, source_kind, kind, external_ref, enabled,
        )


async def list_sources(pool, tenant_id):
    """Источники тенанта. Явный фильтр tenant_id обязателен: owner обходит RLS, поэтому
    без него вернулись бы строки всех тенантов (set_tenant тут — только backstop)."""
    async with pool.acquire() as conn:
        await db.set_tenant(conn, tenant_id)
        return await conn.fetch(
            """
            select id, tenant_id, source_kind, kind, external_ref, title,
                   enabled, last_polled_at, cursor
            from engine.sources
            where tenant_id = $1
            order by created_at
            """,
            tenant_id,
        )


async def set_source_enabled(pool, tenant_id, source_id, enabled):
    """Тумблер enabled источника. Явный tenant_id в WHERE — под owner'ом не даст задеть
    чужую строку. Возвращает id при попадании, иначе None (нет строки под этим тенантом)."""
    async with pool.acquire() as conn:
        await db.set_tenant(conn, tenant_id)
        return await conn.fetchval(
            "update engine.sources set enabled = $3, updated_at = now() "
            "where id = $1 and tenant_id = $2 returning id",
            source_id, tenant_id, enabled,
        )


async def create_profile(pool, tenant_id, name, intent_keywords=None, industry=None,
                         geo=None, min_intent_score=None, min_urgency=None, enabled=True):
    """Создать/обновить поисковый профиль (идемпотентно по unique (tenant_id, name)).

    intent_keywords (list) и geo (dict|None) → jsonb; min_*-пороги (numeric) → передаём
    как текст с ::numeric-кастом (asyncpg numeric ждёт Decimal — текстовый каст надёжнее
    и принимает int/float/str/None единообразно). Возвращает id.
    """
    async with pool.acquire() as conn:
        await db.set_tenant(conn, tenant_id)
        return await conn.fetchval(
            """
            insert into engine.search_profiles
              (tenant_id, name, intent_keywords, industry, geo,
               min_intent_score, min_urgency, enabled)
            values ($1, $2, $3::jsonb, $4, $5::jsonb, $6::numeric, $7::numeric, $8)
            on conflict (tenant_id, name) do update
              set intent_keywords = excluded.intent_keywords, industry = excluded.industry,
                  geo = excluded.geo, min_intent_score = excluded.min_intent_score,
                  min_urgency = excluded.min_urgency, enabled = excluded.enabled,
                  updated_at = now()
            returning id
            """,
            tenant_id, name,
            json.dumps(intent_keywords or [], ensure_ascii=False),
            industry,
            json.dumps(geo, ensure_ascii=False) if geo is not None else None,
            None if min_intent_score is None else str(min_intent_score),
            None if min_urgency is None else str(min_urgency),
            enabled,
        )


async def list_profiles(pool, tenant_id):
    """Поисковые профили тенанта (явный фильтр tenant_id — см. list_sources)."""
    async with pool.acquire() as conn:
        await db.set_tenant(conn, tenant_id)
        return await conn.fetch(
            """
            select id, tenant_id, name, intent_keywords, industry, geo,
                   min_intent_score, min_urgency, enabled
            from engine.search_profiles
            where tenant_id = $1
            order by created_at
            """,
            tenant_id,
        )
