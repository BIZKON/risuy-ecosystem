#!/usr/bin/env python3
"""Смоук фикса A1: бот чтит ГЛОБАЛЬНУЮ активную персону как дефолт (bot-telegram/db.get_ai_overrides).

Проверяет приоритет: диалог > канал > активная персона > глобальный ai_system_prompt.
Пишет/удаляет временные ключи app_settings на risuy_dev (app_settings — глобальная, без RLS),
сохраняет и восстанавливает реальные ai_persona/ai_system_prompt. На прод НЕ запускать.

Запуск: PERSONA_SMOKE_DSN="postgresql://<owner>@<host>:5432/risuy_dev?sslmode=require" \
        PYTHONPATH=. ./.venv-smoke/bin/python scripts/persona_fallback_smoke.py
"""
import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "bot-telegram"))
# Env-заглушки для импорта bot-telegram/config.py (смоук использует свой пул, не config.DATABASE_URL).
os.environ.setdefault("BOT_TOKEN", "0:smoke")
os.environ.setdefault("DATABASE_URL", "postgresql://x/y")
os.environ.setdefault("CHANNEL_ID", "-1001234567890")
os.environ.setdefault("CHANNEL_URL", "https://t.me/smoke")
os.environ.setdefault("GUIDE_URL", "https://example.org/guide")

import asyncpg  # noqa: E402
import db        # noqa: E402  (bot-telegram/db.py)

DSN = os.environ.get("PERSONA_SMOKE_DSN")
if not DSN or "/risuy_dev" not in DSN.split("?")[0]:
    raise SystemExit("Задайте PERSONA_SMOKE_DSN на risuy_dev.")

FAILS: list[str] = []
A, DLG, CH = "zzsmokeactive", "zzsmokedialog", "zzsmokechan"
TEST_KEYS = [
    "ai_persona", "ai_system_prompt",
    f"ai_persona_prompt__{A}", f"ai_persona_prompt__{DLG}", f"ai_system_prompt__{CH}",
]


def check(name: str, cond: bool, detail="") -> None:
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail != "" else ""))
    if not cond:
        FAILS.append(name)


async def _set(c, key, value):
    await c.execute(
        "insert into app_settings (key, value) values ($1,$2) "
        "on conflict (key) do update set value = excluded.value", key, value)


async def main() -> None:
    db.pool = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    saved: dict[str, str | None] = {}
    try:
        async with db.pool.acquire() as c:
            for k in ("ai_persona", "ai_system_prompt"):
                saved[k] = await c.fetchval("select value from app_settings where key=$1", k)
            # сцена: активная персона A с роль-промптом, глобальный ai_system_prompt ПУСТ
            await _set(c, "ai_persona", A)
            await _set(c, "ai_system_prompt", "")
            await _set(c, f"ai_persona_prompt__{A}", "ROLE-ACTIVE")
            await _set(c, f"ai_persona_prompt__{DLG}", "ROLE-DIALOG")
            await _set(c, f"ai_system_prompt__{CH}", "CH-PROMPT")

        print("1. активная персона = дефолт, когда нет диалога/канала:")
        o = await db.get_ai_overrides()
        check("system_prompt = ROLE-ACTIVE (фолбэк на активную персону)", o["system_prompt"] == "ROLE-ACTIVE", repr(o["system_prompt"]))

        print("2. персона диалога перекрывает активную:")
        o = await db.get_ai_overrides(persona=DLG)
        check("system_prompt = ROLE-DIALOG", o["system_prompt"] == "ROLE-DIALOG", repr(o["system_prompt"]))

        print("3. канал перекрывает активную персону:")
        o = await db.get_ai_overrides(source=CH)
        check("system_prompt = CH-PROMPT", o["system_prompt"] == "CH-PROMPT", repr(o["system_prompt"]))

        print("4. заданный глобальный ai_system_prompt отключает фолбэк на активную персону:")
        async with db.pool.acquire() as c:
            await _set(c, "ai_system_prompt", "GLOBAL-SP")
        o = await db.get_ai_overrides()
        check("system_prompt = GLOBAL-SP (активная персона НЕ перекрывает явный глобал)", o["system_prompt"] == "GLOBAL-SP", repr(o["system_prompt"]))
        async with db.pool.acquire() as c:
            await _set(c, "ai_system_prompt", "")

        print("5. нет активной персоны → пусто (как раньше):")
        async with db.pool.acquire() as c:
            await _set(c, "ai_persona", "")
        o = await db.get_ai_overrides()
        check("system_prompt пуст (нет активной персоны, нет глобала)", o["system_prompt"] == "", repr(o["system_prompt"]))

    finally:
        async with db.pool.acquire() as c:
            for k in TEST_KEYS:
                if k in saved:  # реальные ключи — восстановить прежнее значение
                    if saved[k] is None:
                        await c.execute("delete from app_settings where key=$1", k)
                    else:
                        await _set(c, k, saved[k])
                else:           # тестовые ключи — удалить
                    await c.execute("delete from app_settings where key=$1", k)
        await db.pool.close()

    print()
    if FAILS:
        print(f"❌ ПРОВАЛЫ ({len(FAILS)}): " + ", ".join(FAILS)); sys.exit(1)
    print("✅ persona fallback smoke — все проверки зелёные")


if __name__ == "__main__":
    asyncio.run(main())
