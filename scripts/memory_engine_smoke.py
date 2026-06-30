#!/usr/bin/env python3
"""Pure-смоук СП-2-память: _dialog_text форматирует роли; maybe_summarize срабатывает по
ДЕЛЬТА-порогу (накоплено >= MEMORY_SUMMARIZE_EVERY новых ходов с последней сводки), устойчиво
к дрейфу чётности счётчика. Моки ai/kb/db — без сети/БД.
  PYTHONPATH=bot-telegram:. ./.venv-smoke/bin/python scripts/memory_engine_smoke.py
"""
import asyncio
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bot-telegram"))
# stub-env: import db → import config; config._req падает без обязательных переменных.
os.environ.setdefault("DATABASE_URL", "postgresql://x/y")
os.environ.setdefault("BOT_TOKEN", "smoke")
os.environ.setdefault("CHANNEL_ID", "-100123")
os.environ.setdefault("CHANNEL_URL", "https://t.me/smoke")
os.environ.setdefault("GUIDE_URL", "https://example.com/guide")

# Фейковый ai-модуль ДО импорта memory (maybe_summarize делает `import ai` лениво).
_fake_ai = types.ModuleType("ai")
async def _fake_summarize(dialog, cfg):  # noqa: E306
    return "Клиент хочет запись на маникюр, бюджет 2000."
_fake_ai.summarize_dialog = _fake_summarize
sys.modules["ai"] = _fake_ai

import config  # noqa: E402
import db  # noqa: E402
import kb  # noqa: E402
import memory  # noqa: E402

FAILS = []
def check(n, c):  # noqa: E302
    print(f"  {'OK ' if c else 'FAIL'} {n}")
    if not c:
        FAILS.append(n)

INSERTS = []
STATE = {"last": 0}  # мок last_up_to
async def _fake_embed_passage(text):  # noqa: E306
    return [0.01] * 768
async def _fake_memory_insert(tenant_id, agent_id, content, embedding, **kw):  # noqa: E306
    INSERTS.append({"tenant": tenant_id, "agent": agent_id, "content": content, "meta": kw.get("metadata")})
async def _fake_last_up_to(tenant_id, agent_id, lead_key):  # noqa: E306
    return STATE["last"]
kb.embed_passage = _fake_embed_passage
db.memory_insert = _fake_memory_insert
db.memory_last_up_to = _fake_last_up_to

CFG = {"team_agent_id": "agent-1", "backend": "gateway"}
HIST = [{"role": "user", "content": "Привет"}, {"role": "assistant", "content": "Здравствуйте!"}]


async def _summ(msg_count, cfg=CFG):
    INSERTS.clear()
    await memory.maybe_summarize(external_id=1, tenant_id="T", cfg=cfg, history=HIST,
                                 msg_count=msg_count, lead_key="L1")


async def main():
    every = config.MEMORY_SUMMARIZE_EVERY  # дефолт 10

    # 1. _dialog_text — роли и формат
    check("_dialog_text форматирует роли",
          memory._dialog_text(HIST) == "Клиент: Привет\nАгент: Здравствуйте!")
    check("_dialog_text пропускает пустые", memory._dialog_text([{"role": "user", "content": "  "}]) == "")

    # 2. накоплено >= every (last=0, msg_count=every) → запись
    STATE["last"] = 0
    await _summ(every)
    check(f"delta {every}-0 >= {every} → 1 запись", len(INSERTS) == 1)
    check("запись: сводка + lead + up_to в metadata",
          bool(INSERTS) and INSERTS[0]["content"].startswith("Клиент хочет")
          and INSERTS[0]["meta"].get("lead") == "L1" and INSERTS[0]["meta"].get("up_to") == every)

    # 3. меньше every новых с последней сводки (last=every, msg_count=every+5) → нет записи
    STATE["last"] = every
    await _summ(every + 5)
    check("delta < every → нет записи", len(INSERTS) == 0)

    # 3b. снова накопилось every (last=every, msg_count=2*every) → запись (устойчиво к нечётности)
    STATE["last"] = every
    await _summ(2 * every + 1)  # нечётное msg_count — modulo бы промахнулся, дельта срабатывает
    check("delta снова >= every (нечётный msg_count) → запись", len(INSERTS) == 1)

    # 4. нет team_agent_id → нет записи (легаси-путь)
    STATE["last"] = 0
    await _summ(every, cfg={"backend": "gateway"})
    check("без team_agent_id → нет записи (легаси-путь память пропускает)", len(INSERTS) == 0)

    print("\n" + ("ВСЕ ОК" if not FAILS else "ПРОВАЛЫ: " + ", ".join(FAILS)))
    sys.exit(1 if FAILS else 0)


asyncio.run(main())
