#!/usr/bin/env python3
"""Unit-смоук Task 0.1 (аудит 2026-07-01, находка ①): argon2 не блокирует event-loop.
verify_password/hash_password стали async (offload в поток) + семафор _ARGON2_SEM(3). Без БД.
  PYTHONPATH=. ./.venv-smoke/bin/python scripts/security_argon2_smoke.py"""
import asyncio
import inspect
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "admin-panel"))

os.environ.setdefault("DATABASE_URL", "postgresql://x/y")
os.environ.setdefault("SESSION_SECRET", "smoke-secret-only-for-import-aaaaaaaa")
os.environ.setdefault("ADMIN_USERNAME", "smoke")
os.environ.setdefault("ADMIN_PASSWORD_HASH", "$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHQ$aGFzaHN0dWI")

import auth  # noqa: E402
import config  # noqa: E402

FAILS = []


def check(name, cond):
    print(f"  {'OK ' if cond else 'FAIL'} {name}")
    if not cond:
        FAILS.append(name)


async def _heartbeat(stop: asyncio.Event, counter: list):
    """Тикает, пока не стоп. Если argon2 блокирует loop — тиков во время работы почти нет."""
    while not stop.is_set():
        counter[0] += 1
        await asyncio.sleep(0.001)


async def main():
    check("verify_password стала корутиной", inspect.iscoroutinefunction(auth.verify_password))
    check("hash_password стала корутиной", inspect.iscoroutinefunction(auth.hash_password))
    check("семафор argon2 ограничивает параллелизм 3", auth._ARGON2_SEM._value == 3)

    # Round-trip: реальный argon2 hash → verify по нему (заодно даёт валидный хеш для теста loop).
    h = await auth.hash_password("known-pw")
    check("hash_password вернул argon2id PHC-хеш", isinstance(h, str) and h.startswith("$argon2id$"))
    config.ADMIN_PASSWORD_HASH = h
    check("verify_password(верный) → True", (await auth.verify_password("known-pw")) is True)
    check("verify_password(неверный) → False", (await auth.verify_password("wrong-pw")) is False)

    # ГЛАВНОЕ: под нагрузкой argon2 event-loop продолжает крутиться (offload в поток).
    stop = asyncio.Event()
    counter = [0]
    hb = asyncio.create_task(_heartbeat(stop, counter))
    await asyncio.sleep(0)  # дать heartbeat стартовать
    t0 = time.monotonic()
    # 6 параллельных verify × 2 argon2 каждый = 12 операций, семафор(3) → несколько волн.
    await asyncio.gather(*[auth.verify_password("load-test") for _ in range(6)])
    dur = time.monotonic() - t0
    stop.set()
    await hb
    # Если бы loop блокировался, за ~сотни мс argon2-работы heartbeat почти не тикал бы.
    check(f"event-loop НЕ блокируется argon2 (heartbeat={counter[0]} тиков за {dur:.2f}с)",
          counter[0] > 50)
    check("argon2 реально исполнялся (не мгновенно)", dur > 0.05)

    print("\nВСЕ ОК" if not FAILS else "\nПРОВАЛЫ: " + ", ".join(FAILS))
    sys.exit(1 if FAILS else 0)


asyncio.run(main())
