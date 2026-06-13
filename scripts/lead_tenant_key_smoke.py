#!/usr/bin/env python3
"""Смоук харденинга №1 — лид уникален в пределах ТЕНАНТА (составной ключ).

Гоняет РЕАЛЬНЫЕ функции bot-telegram/db.py против risuy_dev (на нём уже применены обе
миграции: composite unique + drop global). Проверяет, что один и тот же tg_user_id у
ДВУХ тенантов — это ДВА разных лида, а чтения/апдейты не путаются между тенантами.

  1. upsert_start под ctx A и ctx B с одним tg_user_id → 2 лида (по одному на тенанта);
  2. чтения (source/persona/имя/телефон/bot_paused/id) под ctx A видят лид A, под B — B;
  3. апдейты (имя/согласие/подписка) пишут в лид СВОЕГО тенанта;
  4. first-touch идемпотентность: повторный upsert не плодит лид и не перетирает source;
  5. прогрев (get_due_followups) под ctx A НЕ возвращает лид тенанта B (анти-кросс-рассылка).

Тестовые тенанты smoke-lead-* и их лиды удаляются в конце. На прод не запускать.

Запуск: LEADKEY_SMOKE_DSN="postgresql://<owner>@<host>:5432/risuy_dev?sslmode=require" \
        PYTHONPATH=. python3 scripts/lead_tenant_key_smoke.py
"""
import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)                              # пакет shared (на случай)
sys.path.insert(0, os.path.join(ROOT, "bot-telegram"))  # config / db бота
os.environ.setdefault("BOT_TOKEN", "123:smoke")
os.environ.setdefault("DATABASE_URL", "postgresql://x/y")
os.environ.setdefault("CHANNEL_ID", "-100123")
os.environ.setdefault("CHANNEL_URL", "https://t.me/x")
os.environ.setdefault("GUIDE_URL", "https://x")

import asyncpg  # noqa: E402
import db        # noqa: E402  (bot-telegram/db.py)

DSN = os.environ.get("LEADKEY_SMOKE_DSN")
if not DSN or "/risuy_dev" not in DSN.split("?")[0]:
    raise SystemExit("Задайте LEADKEY_SMOKE_DSN на risuy_dev (создаёт/удаляет тестовых тенантов и лидов).")

TG = 990222333  # тестовый tg_user_id, общий для обоих тенантов
FAILS: list[str] = []


def check(name: str, cond: bool, detail="") -> None:
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail != "" else ""))
    if not cond:
        FAILS.append(name)


class ctx:
    """Контекст тенанта: db.tenant_id() = этот tid на время блока (как middleware бота)."""
    def __init__(self, tid):
        self.tid = tid

    def __enter__(self):
        self._tok = db.current_tenant_id.set(self.tid)

    def __exit__(self, *a):
        db.current_tenant_id.reset(self._tok)


async def _cleanup(c):
    await c.execute("delete from leads where tg_user_id = $1", TG)
    await c.execute("delete from tenants where slug like 'smoke-lead-%'")


async def main() -> None:
    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    db.pool = pool
    async with pool.acquire() as c:
        await _cleanup(c)
        ta = await c.fetchval(
            "insert into tenants(slug,name,status) values('smoke-lead-a','A','active') returning id")
        tb = await c.fetchval(
            "insert into tenants(slug,name,status) values('smoke-lead-b','B','active') returning id")

    # 1. upsert под двумя тенантами с одним tg_user_id
    with ctx(ta):
        await db.upsert_start(TG, "telegram_a")
        await db.set_name(TG, "Имя A")
        await db.set_consent(TG, True)
    with ctx(tb):
        await db.upsert_start(TG, "telegram_b")
        await db.set_name(TG, "Имя B")

    async with pool.acquire() as c:
        n = await c.fetchval("select count(*) from leads where tg_user_id = $1", TG)
        src_a = await c.fetchval("select source from leads where tg_user_id=$1 and tenant_id=$2", TG, ta)
        src_b = await c.fetchval("select source from leads where tg_user_id=$1 and tenant_id=$2", TG, tb)
        nm_a = await c.fetchval("select name from leads where tg_user_id=$1 and tenant_id=$2", TG, ta)
        nm_b = await c.fetchval("select name from leads where tg_user_id=$1 and tenant_id=$2", TG, tb)
        cons_a = await c.fetchval("select consent from leads where tg_user_id=$1 and tenant_id=$2", TG, ta)
        cons_b = await c.fetchval("select consent from leads where tg_user_id=$1 and tenant_id=$2", TG, tb)
    print("1. Изоляция одного tg_user_id у двух тенантов:")
    check("один tg_user_id → ДВА лида (по тенанту)", n == 2, f"факт {n}")
    check("source изолирован (A=telegram_a, B=telegram_b)", src_a == "telegram_a" and src_b == "telegram_b",
          f"{src_a!r}/{src_b!r}")
    check("имя изолировано (апдейт ушёл в свой тенант)", nm_a == "Имя A" and nm_b == "Имя B",
          f"{nm_a!r}/{nm_b!r}")
    check("согласие только у A (set_consent под ctx A)", bool(cons_a) and not cons_b,
          f"A={cons_a} B={cons_b}")

    # 2. Чтения через функции бота видят лид своего тенанта
    print("2. Чтения функциями бота — каждый тенант свой лид:")
    with ctx(ta):
        check("get_lead_source(A) == telegram_a", await db.get_lead_source(TG) == "telegram_a")
        lid_a = await db.resolve_lead_id(TG)
        purch_a = await db.get_lead_for_purchase(TG)
    with ctx(tb):
        check("get_lead_source(B) == telegram_b", await db.get_lead_source(TG) == "telegram_b")
        lid_b = await db.resolve_lead_id(TG)
        purch_b = await db.get_lead_for_purchase(TG)
    check("resolve_lead_id даёт РАЗНЫЕ id по тенантам", lid_a is not None and lid_a != lid_b,
          f"{lid_a} vs {lid_b}")
    check("get_lead_for_purchase: имя по тенанту", purch_a["name"] == "Имя A" and purch_b["name"] == "Имя B",
          f"{purch_a['name']!r}/{purch_b['name']!r}")

    # 3. Идемпотентность first-touch: повтор upsert не плодит и не перетирает source
    print("3. First-touch идемпотентность:")
    with ctx(ta):
        await db.upsert_start(TG, "telegram_OTHER")
    async with pool.acquire() as c:
        n2 = await c.fetchval("select count(*) from leads where tg_user_id = $1", TG)
        src_a2 = await c.fetchval("select source from leads where tg_user_id=$1 and tenant_id=$2", TG, ta)
    check("повторный upsert НЕ создал лишний лид (всё ещё 2)", n2 == 2, f"факт {n2}")
    check("source НЕ перетёрт (first-touch telegram_a)", src_a2 == "telegram_a", repr(src_a2))

    # 4. Прогрев (get_due_followups) под ctx A не цепляет лид тенанта B
    print("4. Прогрев изолирован (анти-кросс-рассылка):")
    col = next(iter(db._FOLLOWUP_COLS))
    async with pool.acquire() as c:
        # делаем лид B «созревшим» для касания col: выдан гайд давно, касание не слано
        await c.execute(
            f"update leads set guide_sent_at = now() - interval '1 day', {col} = null, "
            "unsubscribed_at = null, bot_paused = false "
            "where tg_user_id = $1 and tenant_id = $2", TG, tb)
    with ctx(ta):
        due_a = await db.get_due_followups(col, 60)
    with ctx(tb):
        due_b = await db.get_due_followups(col, 60)
    check("прогрев тенанта A НЕ видит лид B", TG not in due_a, f"due_a={due_a}")
    check("прогрев тенанта B видит свой лид B", TG in due_b)

    async with pool.acquire() as c:  # чистка
        await _cleanup(c)
    await pool.close()

    print()
    if FAILS:
        print(f"❌ ПРОВАЛЫ ({len(FAILS)}): " + ", ".join(FAILS))
        sys.exit(1)
    print("✅ Lead-tenant-key smoke — все проверки зелёные")


if __name__ == "__main__":
    asyncio.run(main())
