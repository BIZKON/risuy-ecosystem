#!/usr/bin/env python3
"""Смоук: бейдж «непрочитанное» гаснет при ОТКРЫТИИ диалога (аудит lead_view/thread_view),
а не только при ответе оператора. Гоняет РЕАЛЬНЫЕ функции панели list_dialogs /
count_unanswered_dialogs на risuy_dev (заодно валидирует SQL правки).

Регрессия бага (2026-06-25): входящее залипало в «Диалогах»/«Демо-мониторе» после прочтения;
веб-лиды демо (ответить нельзя — композер скрыт) висели с бейджем навсегда.

Проверяемые переходы для одного посева:
  1) входящее               → unread = 1   (есть непрочитанное)
  2) аудит lead_view (открыли)→ unread = 0  ← ФИКС: просмотр снимает бейдж
  3) НОВОЕ входящее позже     → unread = 1   (снова зажглось — корректно)
  4) исходящее (ответ)        → unread = 0   (легаси-поведение сохранено)
И что count_unanswered_dialogs() даёт тот же дельта-переход (+1 / 0).

Запуск (owner-DSN risuy_dev; config панели удовлетворяем заглушками):
  DATABASE_URL="postgresql://gen_user:<pw>@HOST:5432/risuy_dev?sslmode=require" \
  SESSION_SECRET="$(python3 -c 'import secrets;print(secrets.token_urlsafe(48))')" \
  ADMIN_USERNAME=smoke \
  ADMIN_PASSWORD_HASH='$argon2id$v=19$m=65536,t=3,p=4$c21va2VzbW9rZQ$c21va2VzbW9rZXNtb2tl' \
  PYTHONPATH=admin-panel ./.venv-smoke/bin/python scripts/dialogs_unread_seen_smoke.py
"""
import asyncio
import os
from datetime import datetime, timedelta, timezone

DSN = os.environ.get("DATABASE_URL", "")
if "risuy_dev" not in DSN:
    raise SystemExit("DATABASE_URL должен указывать на risuy_dev (защита от прода).")

import db  # admin-panel/db.py (PYTHONPATH=admin-panel)

NAME = "SMOKE-unread-seen"
ACTOR = "smoke-unread"


async def main() -> None:
    await db.init()
    failures: list[str] = []

    async def unread_of(c, lead_id) -> int:
        rows = await db.list_dialogs({"q_name": NAME}, limit=50, offset=0)
        for r in rows:
            if r["id"] == lead_id:
                return int(r["unread"])
        raise AssertionError("посеянный лид не найден в list_dialogs (по имени)")

    async with db.pool.acquire() as c:
        tid = (await c.fetchval("select id from tenants where slug='demo-sandbox'")
               or await c.fetchval("select id from tenants order by created_at limit 1"))
        assert tid is not None, "нет ни одного тенанта в risuy_dev"

        mcols = {r["column_name"] for r in await c.fetch(
            "select column_name from information_schema.columns where table_name='messages'")}
        lcols = {r["column_name"] for r in await c.fetch(
            "select column_name from information_schema.columns where table_name='leads'")}

        # ── посев лида (адаптивно к живой схеме: tenant_id обязателен после мульти-тенант миграции)
        lcol, lval = ["messenger", "source", "name"], ["web", "other", NAME]
        if "tenant_id" in lcols:
            lcol.insert(0, "tenant_id"); lval.insert(0, tid)
        ph = ",".join(f"${i+1}" for i in range(len(lval)))
        lead_id = await c.fetchval(
            f"insert into leads ({','.join(lcol)}) values ({ph}) returning id", *lval)

        async def add_msg(direction: str, when: datetime, source: str | None = None) -> None:
            cols, vals = ["lead_id", "direction", "kind", "text", "created_at"], \
                         [lead_id, direction, "text", f"{direction}-msg", when]
            if "tenant_id" in mcols:
                cols.append("tenant_id"); vals.append(tid)
            if "messenger" in mcols:
                cols.append("messenger"); vals.append("web")
            if "tg_user_id" in mcols:
                cols.append("tg_user_id"); vals.append(0)
            if source is not None and "source" in mcols:
                cols.append("source"); vals.append(source)
            p = ",".join(f"${i+1}" for i in range(len(vals)))
            await c.execute(f"insert into messages ({','.join(cols)}) values ({p})", *vals)

        async def add_view(when: datetime) -> None:
            await c.execute(
                "insert into admin_audit (actor, action, lead_id, at) values ($1,'lead_view',$2,$3)",
                ACTOR, lead_id, when)

        async def cleanup() -> None:
            await c.execute("delete from admin_audit where lead_id=$1", lead_id)
            await c.execute("delete from messages where lead_id=$1", lead_id)
            await c.execute("delete from leads where id=$1", lead_id)

        try:
            t0 = datetime.now(timezone.utc) - timedelta(minutes=5)

            def check(label: str, got: int, want: int) -> None:
                if got == want:
                    print(f"✅ {label}: unread={got}")
                else:
                    failures.append(f"{label}: ожидалось {want}, получено {got}")
                    print(f"❌ {label}: ожидалось {want}, получено {got}")

            base = await db.count_unanswered_dialogs()

            # 1) входящее → непрочитано
            await add_msg("in", t0)
            check("1) входящее", await unread_of(c, lead_id), 1)
            if await db.count_unanswered_dialogs() != base + 1:
                failures.append("count_unanswered не вырос на 1 после входящего")
            else:
                print("✅ count_unanswered: +1 после входящего")

            # 2) открыли диалог (аудит lead_view) → бейдж снят  ← ФИКС
            await add_view(t0 + timedelta(seconds=30))
            check("2) после открытия (lead_view)", await unread_of(c, lead_id), 0)
            if await db.count_unanswered_dialogs() != base:
                failures.append("count_unanswered не вернулся к base после открытия")
            else:
                print("✅ count_unanswered: вернулся к base после открытия")

            # 3) новое входящее ПОЗЖЕ просмотра → снова непрочитано
            await add_msg("in", t0 + timedelta(seconds=60))
            check("3) новое входящее после просмотра", await unread_of(c, lead_id), 1)

            # 4) ответ оператора (исходящее) → снято (легаси сохранено)
            await add_msg("out", t0 + timedelta(seconds=90), source="manual")
            check("4) после ответа (исходящее)", await unread_of(c, lead_id), 0)
        finally:
            await cleanup()

    if failures:
        print("\n".join("‼️ " + f for f in failures))
        raise SystemExit(1)
    print("\n🟢 ВСЕ ПРОВЕРКИ ЗЕЛЁНЫЕ — просмотр снимает «непрочитанное», ответ тоже; новое входящее зажигает заново.")


if __name__ == "__main__":
    asyncio.run(main())
