#!/usr/bin/env python3
"""СП-1: бэкфилл команды — из легаси одной-персоны тенанта (tenant_settings.ai_system_prompt/
ai_backend/ai_agent_id) создать дефолтного агента команды в team_agents (slug='default').
Idempotent: создаёт ТОЛЬКО отсутствующих (тенант без агентов); существующих НЕ трогает
(не затирает правки тенанта в /my-team).

🟥 По умолчанию ТОЛЬКО risuy_dev. Прод — лишь при BACKFILL_ALLOW_PROD=yes.
ЗАПУСК:
  BACKFILL_DSN="postgresql://gen_user:<pw>@81.31.246.136:5432/risuy_dev?sslmode=require" \
      ./.venv-smoke/bin/python scripts/backfill_team_agents.py
"""
import asyncio
import os

import asyncpg

DSN = os.environ.get("BACKFILL_DSN")
if not DSN:
    raise SystemExit("Задайте BACKFILL_DSN.")
DBNAME = DSN.split("?")[0].rstrip("/").split("/")[-1]
if DBNAME == "risuy" and os.environ.get("BACKFILL_ALLOW_PROD") != "yes":
    raise SystemExit("ОТКАЗ: боевой risuy. Для прода явно: BACKFILL_ALLOW_PROD=yes.")


async def main() -> None:
    print(f"backfill_team_agents · база={DBNAME}")
    c = await asyncpg.connect(DSN)
    created = 0
    try:
        async with c.transaction():
            rows = await c.fetch(
                "select t.id as tid, s.value as prompt, "
                "  (select value from tenant_settings where tenant_id=t.id and key='ai_backend') as backend, "
                "  (select value from tenant_settings where tenant_id=t.id and key='ai_agent_id') as agent_id "
                "from tenants t join tenant_settings s "
                "  on s.tenant_id=t.id and s.key='ai_system_prompt' "
                "where coalesce(s.value,'') <> '' "
                "  and not exists (select 1 from team_agents a where a.tenant_id=t.id)")
            for r in rows:
                await c.execute(
                    """
                    insert into team_agents
                        (tenant_id, slug, name, system_prompt, backend, agent_id, is_default, position)
                    values ($1, 'default', 'ИИ-сотрудник', $2, $3, $4, true, 0)
                    on conflict (tenant_id, slug) do nothing
                    """,
                    r["tid"], r["prompt"] or "", (r["backend"] or None), (r["agent_id"] or ""))
                created += 1
        print(f"✅ бэкфилл: обработано тенантов {created}")
    finally:
        await c.close()


if __name__ == "__main__":
    asyncio.run(main())
