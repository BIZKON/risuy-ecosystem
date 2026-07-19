#!/usr/bin/env python3
"""seed-CLI: создать/обновить engine.search_profiles (движковый минимум S3, tenant-scoped).

Вызывает engine.collectors.registry.create_profile под ролью engine_rw (owner) с ЯВНЫМ
tenant_id + set_tenant-backstop. Панельный tenant-UI приедет в S7M поверх той же функции.
Гард прода: пишем только в эфемерный risuy_dev; боевая БД — лишь при ACCOUNT_ADMIN_ALLOW_PROD=yes.

ЗАПУСК (env ENGINE_ADMIN_DSN|ENGINE_DSN — engine-owner DSN):
  ENGINE_ADMIN_DSN="postgresql://engine_rw:...@host:5432/risuy_dev" \
      python scripts/engine_profile_add.py --tenant-id <uuid> --name "стройка МСК" \
      --keyword ремонт --keyword подрядчик --industry строительство \
      --geo '{"lat":55.75,"lon":37.62}' --min-intent-score 0.6 --min-urgency 0.4
"""
import argparse
import asyncio
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)  # для engine.collectors.registry / engine.common.db

from engine.common import db  # noqa: E402 — после sys.path.insert (как в engine_account_add.py)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Seed/ротация поискового профиля engine.search_profiles.")
    p.add_argument("--tenant-id", required=True, help="uuid тенанта (public.tenants.id)")
    p.add_argument("--name", required=True, help="имя профиля (ключ ротации с tenant_id)")
    p.add_argument("--keyword", action="append", default=None,
                   help="ключевое слово намерения; можно повторять (--keyword a --keyword b)")
    p.add_argument("--industry", default=None, help="отрасль (опц.)")
    p.add_argument("--geo", default=None,
                   help="JSON гео-фильтра, напр. '{\"lat\":55.75,\"lon\":37.62}'")
    p.add_argument("--min-intent-score", type=float, default=None, help="порог intent-score (опц.)")
    p.add_argument("--min-urgency", type=float, default=None, help="порог urgency (опц.)")
    p.add_argument("--disabled", action="store_true", help="создать выключенным (enabled=false)")
    p.add_argument("--dsn", default=None,
                   help="engine-owner DSN; иначе ENGINE_ADMIN_DSN/ENGINE_DSN из env")
    return p.parse_args()


async def _run(args: argparse.Namespace, dsn: str) -> None:
    from engine.collectors import registry
    geo = json.loads(args.geo) if args.geo else None  # валидируем JSON до подключения к БД
    pool = await db.make_pool(dsn)
    try:
        profile_id = await registry.create_profile(
            pool, args.tenant_id, args.name,
            intent_keywords=args.keyword or [], industry=args.industry, geo=geo,
            min_intent_score=args.min_intent_score, min_urgency=args.min_urgency,
            enabled=not args.disabled,
        )
    finally:
        await pool.close()
    print(f"profiles: OK id={profile_id} tenant={args.tenant_id} name={args.name} "
          f"keywords={len(args.keyword or [])} enabled={not args.disabled}")


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
