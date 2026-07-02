#!/usr/bin/env python3
"""Смоук флага ai_inference_rf (bot-telegram/db.py::get_ai_inference_rf).

ЧАСТЬ A (fail-safe, БЕЗ БД, выполняется ВСЕГДА) — подсовываем db.pool объект,
чей acquire() бросает исключение (как при реальном сбое соединения), и
проверяем, что хелпер не падает, а возвращает False (fail-safe: ошибка чтения
→ трансграничным считаем, ложную декларацию не публикуем).

ЧАСТЬ B (живая БД, risuy_dev) — гард: TEAM_DSN обязателен, иначе SKIP.
Запуск: TEAM_DSN=<owner-DSN risuy_dev> PYTHONPATH=bot-telegram:. ./.venv-smoke/bin/python scripts/ai_inference_rf_db_smoke.py
"""
import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bot-telegram"))

# config.py бота требует эти переменные при импорте (_req) — смоук их не использует
# по-настоящему (часть A без БД, часть B получает свой пул из TEAM_DSN).
os.environ.setdefault("BOT_TOKEN", "0:smoke")
os.environ.setdefault("DATABASE_URL", "postgresql://x/y")
os.environ.setdefault("CHANNEL_ID", "-1001234567890")
os.environ.setdefault("CHANNEL_URL", "https://t.me/smoke")
os.environ.setdefault("GUIDE_URL", "https://example.org/guide")

import db  # bot-telegram/db.py

FAILS = []


def check(name, cond, detail=""):
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


class _RaisingAcquire:
    """Имитация сбойного пула: acquire() бросает, как при обрыве соединения к Postgres."""

    def acquire(self):
        raise RuntimeError("смоук: имитация сбоя соединения с БД")


async def part_a_fail_safe():
    print("ЧАСТЬ A: fail-safe без БД")
    db.pool = _RaisingAcquire()
    try:
        result = await db.get_ai_inference_rf()
    except Exception as e:  # noqa: BLE001 — сама попытка не должна выбрасывать наружу
        check("get_ai_inference_rf() не падает при сбое пула", False, f"выброшено {e!r}")
        return
    check("сбой чтения → False (fail-safe, трансгран по умолчанию)", result is False)


async def part_b_live_db():
    DSN = os.environ.get("TEAM_DSN", "")
    if "risuy_dev" not in DSN:
        print("ЧАСТЬ B: SKIP: нужен TEAM_DSN на risuy_dev (гард)")
        return

    import asyncpg

    print("ЧАСТЬ B: живая БД risuy_dev")
    db.pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
    try:
        await db.pool.execute("delete from app_settings where key='ai_inference_rf'")
        check("дефолт (нет ключа) → False", await db.get_ai_inference_rf() is False)
        await db.pool.execute(
            "insert into app_settings(key,value) values('ai_inference_rf','1') "
            "on conflict(key) do update set value=excluded.value")
        check("'1' → True", await db.get_ai_inference_rf() is True)
        await db.pool.execute("update app_settings set value='0' where key='ai_inference_rf'")
        check("'0' → False", await db.get_ai_inference_rf() is False)
    finally:
        await db.pool.execute("delete from app_settings where key='ai_inference_rf'")
        await db.pool.close()


async def main():
    await part_a_fail_safe()
    await part_b_live_db()
    print(("\nFAIL: " + ", ".join(FAILS)) if FAILS else "\nOK: ai_inference_rf helper")
    sys.exit(1 if FAILS else 0)


asyncio.run(main())
