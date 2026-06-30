#!/usr/bin/env python3
"""Pure-смоук СП-2-память: _dialog_text форматирует роли; maybe_summarize срабатывает ТОЛЬКО на
кратных MEMORY_SUMMARIZE_EVERY и пишет память (моки ai/kb/db — без сети/БД).
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
async def _fake_embed_passage(text):  # noqa: E306
    return [0.01] * 768
async def _fake_memory_insert(tenant_id, agent_id, content, embedding, **kw):  # noqa: E306
    INSERTS.append({"tenant": tenant_id, "agent": agent_id, "content": content, "meta": kw.get("metadata")})
kb.embed_passage = _fake_embed_passage
db.memory_insert = _fake_memory_insert

CFG = {"team_agent_id": "agent-1", "backend": "gateway"}
HIST = [{"role": "user", "content": "Привет"}, {"role": "assistant", "content": "Здравствуйте!"}]


async def main():
    # 1. _dialog_text — роли и формат
    check("_dialog_text форматирует роли",
          memory._dialog_text(HIST) == "Клиент: Привет\nАгент: Здравствуйте!")
    check("_dialog_text пропускает пустые", memory._dialog_text([{"role": "user", "content": "  "}]) == "")

    every = config.MEMORY_SUMMARIZE_EVERY  # дефолт 10
    # 2. кратное порогу → запись памяти
    INSERTS.clear()
    await memory.maybe_summarize(external_id=1, tenant_id="T", cfg=CFG, history=HIST,
                                 msg_count=every, lead_key="L1")
    check(f"msg_count={every} (кратно) → 1 запись памяти", len(INSERTS) == 1)
    check("запись содержит сводку и lead в metadata",
          bool(INSERTS) and INSERTS[0]["content"].startswith("Клиент хочет")
          and INSERTS[0]["meta"].get("lead") == "L1")

    # 3. НЕ кратное → нет записи
    INSERTS.clear()
    await memory.maybe_summarize(external_id=1, tenant_id="T", cfg=CFG, history=HIST,
                                 msg_count=every + 1, lead_key="L1")
    check(f"msg_count={every + 1} (не кратно) → нет записи", len(INSERTS) == 0)

    # 4. нет team_agent_id → нет записи (легаси-путь)
    INSERTS.clear()
    await memory.maybe_summarize(external_id=1, tenant_id="T", cfg={"backend": "gateway"},
                                 history=HIST, msg_count=every, lead_key="L1")
    check("без team_agent_id → нет записи (легаси-путь память пропускает)", len(INSERTS) == 0)

    print("\n" + ("ВСЕ ОК" if not FAILS else "ПРОВАЛЫ: " + ", ".join(FAILS)))
    sys.exit(1 if FAILS else 0)


asyncio.run(main())
