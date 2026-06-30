#!/usr/bin/env python3
"""DB-смоук онбординга: set_onboarding_flag/get_onboarding_flags round-trip + allowlist-отказ
произвольного ключа, на risuy_dev. Throwaway-тенант, чистка каскадом.
  TEAM_DSN="postgresql://gen_user:<pw>@<host>:5432/risuy_dev?sslmode=require" \
  DATABASE_URL="$TEAM_DSN" SESSION_SECRET=x ADMIN_USERNAME=smoke \
  ADMIN_PASSWORD_HASH='$argon2id$v=19$m=65536,t=3,p=4$c21va2VzbW9rZQ$c21va2VzbW9rZXNtb2tl' \
  PYTHONPATH=admin-panel ./.venv-smoke/bin/python scripts/onboarding_flags_smoke.py
"""
import asyncio
import os
import secrets
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "admin-panel"))

DSN = os.environ.get("TEAM_DSN") or os.environ.get("DATABASE_URL", "")
assert DSN and "/risuy_dev" in DSN.split("?")[0], "только risuy_dev (owner-DSN от владельца)"
os.environ.setdefault("DATABASE_URL", DSN)
os.environ.setdefault("SESSION_SECRET", secrets.token_urlsafe(48))
os.environ.setdefault("ADMIN_USERNAME", "smoke")
os.environ.setdefault("ADMIN_PASSWORD_HASH",
                      "$argon2id$v=19$m=65536,t=3,p=4$c21va2VzbW9rZQ$c21va2VzbW9rZXNtb2tl")

import db  # noqa: E402

FAILS = []
def check(n, c):  # noqa: E302
    print(f"  {'OK ' if c else 'FAIL'} {n}")
    if not c:
        FAILS.append(n)


async def main():
    await db.init()
    async with db.pool.acquire() as c:
        tid = await c.fetchval(
            "insert into tenants(slug,name,status) values('onb-smoke','ONB','active') returning id")
    try:
        ok1 = await db.set_onboarding_flag(tid, "welcome_seen", "1", actor="smoke", ip=None, user_agent=None)
        ok2 = await db.set_onboarding_flag(tid, "onboarding_niche", "салон", actor="smoke", ip=None, user_agent=None)
        ok3 = await db.set_onboarding_flag(tid, "help_dismissed__dialogs", "1", actor="smoke", ip=None, user_agent=None)
        bad = await db.set_onboarding_flag(tid, "funnel_enabled", "1", actor="smoke", ip=None, user_agent=None)
        flags = await db.get_onboarding_flags(tid)
        check("валидные флаги записаны (True)", ok1 and ok2 and ok3)
        check("произвольный ключ ОТКЛОНЁН (allowlist)", bad is False)
        check("round-trip welcome_seen=1", flags.get("welcome_seen") == "1")
        check("round-trip niche=салон", flags.get("onboarding_niche") == "салон")
        check("round-trip help_dismissed__dialogs=1", flags.get("help_dismissed__dialogs") == "1")
        check("отклонённый ключ НЕ записан", "funnel_enabled" not in flags)
        check("get для None тенанта → {}", await db.get_onboarding_flags(None) == {})
    finally:
        async with db.pool.acquire() as c:
            await c.execute("delete from tenants where slug = 'onb-smoke'")  # cascade чистит tenant_settings
    print("\n" + ("ВСЕ ОК" if not FAILS else "ПРОВАЛЫ: " + ", ".join(FAILS)))
    sys.exit(1 if FAILS else 0)


asyncio.run(main())
