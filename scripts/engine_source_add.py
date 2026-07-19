#!/usr/bin/env python3
"""seed-CLI: создать/обновить engine.sources (движковый минимум S3, tenant-scoped).

Вызывает engine.collectors.registry.create_source под ролью engine_rw (owner) с ЯВНЫМ
tenant_id + set_tenant-backstop. Панельный tenant-UI приедет в S7M поверх той же функции.
Гард прода: пишем только в эфемерный risuy_dev; боевая БД — лишь при ACCOUNT_ADMIN_ALLOW_PROD=yes.

ЗАПУСК (env ENGINE_ADMIN_DSN|ENGINE_DSN — engine-owner DSN):
  ENGINE_ADMIN_DSN="postgresql://engine_rw:...@host:5432/risuy_dev" \
      python scripts/engine_source_add.py --tenant-id <uuid> --source-kind telegram \
      --kind channel --external-ref https://t.me/somechannel [--disabled]
"""
import argparse
import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)  # для engine.collectors.registry / engine.common.db

from engine.common import db  # noqa: E402 — после sys.path.insert (как в engine_account_add.py)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Seed/ротация источника engine.sources.")
    p.add_argument("--tenant-id", required=True, help="uuid тенанта (public.tenants.id)")
    p.add_argument("--source-kind", required=True, help="telegram|vk|boards|tenders")
    p.add_argument("--kind", default=None, help="chat|channel|group|board|tender_region")
    p.add_argument("--external-ref", required=True,
                   help="публичная ссылка/ref источника (ключ дедупа с source_kind)")
    p.add_argument("--disabled", action="store_true", help="создать выключенным (enabled=false)")
    p.add_argument("--dsn", default=None,
                   help="engine-owner DSN; иначе ENGINE_ADMIN_DSN/ENGINE_DSN из env")
    return p.parse_args()


async def _run(args: argparse.Namespace, dsn: str) -> None:
    from engine.collectors import registry
    pool = await db.make_pool(dsn)
    try:
        source_id = await registry.create_source(
            pool, args.tenant_id, args.source_kind, args.kind,
            args.external_ref, enabled=not args.disabled,
        )
    finally:
        await pool.close()
    print(f"sources: OK id={source_id} tenant={args.tenant_id} "
          f"kind={args.source_kind}/{args.kind} enabled={not args.disabled}")


def main() -> None:
    args = _parse_args()
    dsn = args.dsn or os.environ.get("ENGINE_ADMIN_DSN") or os.environ.get("ENGINE_DSN")
    if not dsn:
        raise SystemExit("Нужен --dsn или ENGINE_ADMIN_DSN/ENGINE_DSN в env (engine-owner DSN).")
    if "/risuy_dev" not in dsn.split("?")[0] and os.environ.get("ACCOUNT_ADMIN_ALLOW_PROD") != "yes":
        raise SystemExit("ОТКАЗ: DSN не risuy_dev. Для боевой БД явно: ACCOUNT_ADMIN_ALLOW_PROD=yes.")
    asyncio.run(_run(args, dsn))


if __name__ == "__main__":
    main()
