#!/usr/bin/env python3
"""Pure-смоук онбординга: derive_steps считает прогресс/done из сигналов (без БД).
  PYTHONPATH=admin-panel ./.venv-smoke/bin/python scripts/onboarding_state_smoke.py
"""
import os
import secrets
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "admin-panel"))
# stub-env для импорта admin db→config (derive_steps к БД не ходит).
os.environ.setdefault("DATABASE_URL", "postgresql://x/y")
os.environ.setdefault("SESSION_SECRET", secrets.token_urlsafe(48))
os.environ.setdefault("ADMIN_USERNAME", "smoke")
os.environ.setdefault("ADMIN_PASSWORD_HASH",
                      "$argon2id$v=19$m=65536,t=3,p=4$c21va2VzbW9rZQ$c21va2VzbW9rZXNtb2tl")

import onboarding  # noqa: E402

FAILS = []
def check(n, c):  # noqa: E302
    print(f"  {'OK ' if c else 'FAIL'} {n}")
    if not c:
        FAILS.append(n)

empty = onboarding.derive_steps({})
check("пусто → 0/5, 0%, не complete", empty["done_count"] == 0 and empty["total"] == 5
      and empty["pct"] == 0 and empty["complete"] is False)
check("5 шагов с ключами bot/team/kb/funnel/aha",
      [s["key"] for s in empty["steps"]] == ["bot", "team", "kb", "funnel", "aha"])
check("каждый шаг имеет label/href/cta/done",
      all({"label", "href", "cta", "done"} <= set(s) for s in empty["steps"]))

full = onboarding.derive_steps({"bot": True, "team": True, "kb": True, "funnel": True, "aha": True})
check("все сигналы → 5/5, 100%, complete", full["done_count"] == 5 and full["pct"] == 100
      and full["complete"] is True)

part = onboarding.derive_steps({"bot": True, "team": True})
check("частично (2/5) → 40%", part["done_count"] == 2 and part["pct"] == 40 and part["complete"] is False)
check("done проставлен только выполненным",
      [s["done"] for s in part["steps"]] == [True, True, False, False, False])

print("\n" + ("ВСЕ ОК" if not FAILS else "ПРОВАЛЫ: " + ", ".join(FAILS)))
sys.exit(1 if FAILS else 0)
