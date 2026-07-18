"""Engine-пул asyncpg под ролью engine_rw. БЕЗ ботового фолбэка _default_tenant_id:
tenant_id ВСЕГДА явный (см. Global Constraints плана S0M, [critic-fix I3])."""
from __future__ import annotations

import asyncpg


async def make_pool(dsn: str) -> asyncpg.Pool:
    # Роль (engine_rw) берётся из DSN. Без bypassrls → RLS применяется.
    return await asyncpg.create_pool(dsn, min_size=1, max_size=4)


async def set_tenant(conn: asyncpg.Connection, tenant_id: str) -> None:
    # Явный тенант-скоуп на соединении. Пустой tenant_id = ошибка, НЕ молчаливый дефолт.
    if not tenant_id:
        raise ValueError("set_tenant: tenant_id обязателен (фолбэк на дефолт-тенанта запрещён)")
    await conn.execute("select set_config('app.tenant_id', $1, false)", str(tenant_id))
