#!/usr/bin/env python3
"""Смоук Фазы 1 security-remediation (аудит 2026-07-01, находка ②, частично): структурные
PII-паттерны (СНИЛС + паспорт по контексту) в shared/pii.py + удаление agent_memory лида при
erase (ПДн at-rest, код-часть Task 1.2). NER свободных ФИО/адреса — зона Masker (вне этого кода).
Юнит-часть без БД; DB-часть — только при TEAM_DSN (risuy_dev):
  TEAM_DSN="postgresql://gen_user:<pw>@<host>:5432/risuy_dev?sslmode=require" \
  PYTHONPATH=bot-telegram:. ./.venv-smoke/bin/python scripts/security_phase1_smoke.py"""
import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bot-telegram"))
sys.path.insert(0, ROOT)  # пакет shared/
os.environ.setdefault("DATABASE_URL", os.environ.get("TEAM_DSN", "postgresql://x/y"))
os.environ.setdefault("BOT_TOKEN", "smoke")
os.environ.setdefault("CHANNEL_ID", "-100123")
os.environ.setdefault("CHANNEL_URL", "https://t.me/smoke")
os.environ.setdefault("GUIDE_URL", "https://example.com/guide")

from shared import pii  # noqa: E402

FAILS: list[str] = []


def check(name, cond):
    print(f"  {'OK ' if cond else 'FAIL'} {name}")
    if not cond:
        FAILS.append(name)


def unit_pii():
    print("— структурные PII-паттерны (Task 1.1, Фаза 1)")
    # СНИЛС — ТОЛЬКО по контексту «СНИЛС» (как ИНН/паспорт)
    masked, mp = pii.redact_text("Мой СНИЛС 112-233-445 95, оформите")
    check("СНИЛС (с ключевым словом) замаскирован", "112-233-445 95" not in masked and "[SNILS_1]" in masked)
    check("слово «СНИЛС» сохранено как контекст", "СНИЛС" in masked)
    check("СНИЛС восстановлен при unmask", pii.unmask_text(masked, mp) == "Мой СНИЛС 112-233-445 95, оформите")
    # Косвенный падеж СНИЛС («по СНИЛСу») — тоже контекст
    masked, _ = pii.redact_text("по СНИЛСу 112-233-445 95 проверьте")
    check("СНИЛС в косвенном падеже замаскирован", "112-233-445 95" not in masked and "[SNILS_1]" in masked)
    # Негатив: формат 3-3-3-2 БЕЗ слова «СНИЛС» (номер договора/трека) — НЕ трогаем (не портим для ИИ)
    masked, _ = pii.redact_text("Договор 123-456-789 12 подписан")
    check("формат СНИЛС БЕЗ слова «СНИЛС» НЕ замаскирован (номер договора)",
          "123-456-789 12" in masked and "[SNILS" not in masked)
    # Паспорт — по контексту «паспорт» в ЛЮБОМ падеже (косвенные — самый частый случай в речи)
    for phrase in ("паспорт 4509 123456 выдан", "данные паспорта: 4509 123456",
                   "по паспорту 4509 123456 оформлен", "в паспорте 4509 123456"):
        masked, mp = pii.redact_text(phrase)
        check(f"паспорт замаскирован: «{phrase[:22]}…»", "4509 123456" not in masked and "[PASSPORT_1]" in masked)
        check(f"паспорт восстановлен: «{phrase[:22]}…»", pii.unmask_text(masked, mp) == phrase)
    # Голый 10-значный прогон БЕЗ слова «паспорт» — НЕ трогаем (низкий false-positive, как ИНН)
    masked, _ = pii.redact_text("Заказ 4509123456 готов")
    check("голый 10-знак БЕЗ «паспорт» НЕ замаскирован (не портим номер заказа)",
          "4509123456" in masked and "[PASSPORT" not in masked)
    # Консистентность: один СНИЛС → один плейсхолдер (оба вхождения с ключевым словом)
    masked, _ = pii.redact_text("СНИЛС 112-233-445 95, повторно СНИЛС 112-233-445 95")
    check("один СНИЛС → один плейсхолдер (консистентность)", masked.count("[SNILS_1]") == 2)
    # Регрессия: телефон/email/ИНН по-прежнему маскируются
    masked, _ = pii.redact_text("тел +79111234567, mail a@b.ru, ИНН 7707083893")
    check("регрессия: телефон замаскирован", "+79111234567" not in masked and "[PHONE_1]" in masked)
    check("регрессия: email замаскирован", "a@b.ru" not in masked and "[EMAIL_1]" in masked)
    check("регрессия: ИНН замаскирован", "7707083893" not in masked and "[INN_1]" in masked)
    # Орфан-очистка охватывает новые типы
    check("orphan-очистка срезает [SNILS_N]/[PASSPORT_N]",
          pii.unmask_text("остаток [SNILS_9] [PASSPORT_3]", pii.Mapping()) == "остаток  ")


async def db_part():
    dsn = os.environ.get("TEAM_DSN") or ""
    if not dsn:
        print("— DB-часть: SKIP (TEAM_DSN не задан)")
        return
    assert "/risuy_dev" in dsn.split("?")[0], "только risuy_dev"
    print("— erase_lead удаляет agent_memory лида (Task 1.2 код, risuy_dev)")
    import asyncpg
    import db as bdb
    veclit = "[" + ",".join("0.01" for _ in range(768)) + "]"
    bdb.pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    async with bdb.pool.acquire() as c:
        t = await c.fetchval("insert into tenants(slug,name,status) values('p1-erase','P1','active') returning id")
        a = await c.fetchval("insert into team_agents(tenant_id,slug,name) values($1,'sales','S') returning id", t)
        tg = 990100001
        lead = await c.fetchval(
            "insert into leads(tenant_id,tg_user_id,name,phone,consent) values($1,$2,'Иван','+79990001122',true) returning id",
            t, tg)
        # память ЭТОГО лида (metadata.lead = str(tg)) + КОНТРОЛЬ (другой лид, не должен удалиться)
        await c.execute("insert into agent_memory(tenant_id,agent_id,kind,content,embedding,metadata) "
                        "values($1,$2,'summary','сводка лида',$3::vector,$4::jsonb)",
                        t, a, veclit, '{"lead": "990100001"}')
        await c.execute("insert into agent_memory(tenant_id,agent_id,kind,content,embedding,metadata) "
                        "values($1,$2,'summary','сводка ДРУГОГО',$3::vector,$4::jsonb)",
                        t, a, veclit, '{"lead": "990100999"}')
    try:
        await bdb.erase_lead(str(lead), actor="smoke")
        async with bdb.pool.acquire() as c:
            mine = await c.fetchval("select count(*) from agent_memory where tenant_id=$1 and metadata->>'lead'='990100001'", t)
            other = await c.fetchval("select count(*) from agent_memory where tenant_id=$1 and metadata->>'lead'='990100999'", t)
            nm = await c.fetchval("select name from leads where id=$1", lead)
        check("память лида удалена при erase", mine == 0)
        check("память ДРУГОГО лида цела (точечность)", other == 1)
        check("лид обезличен (name=null)", nm is None)
    finally:
        async with bdb.pool.acquire() as c:
            # FK-порядок: admin_audit (erase_lead создал 'lead_erased') → leads → tenants
            # (leads_tenant_id_fkey без cascade). agent_memory/team_agents/messages — каскадом тенанта.
            await c.execute("delete from admin_audit where lead_id in (select id from leads where tenant_id=$1)", t)
            await c.execute("delete from leads where tenant_id=$1", t)
            await c.execute("delete from tenants where id=$1", t)
        await bdb.pool.close()
        bdb.pool = None


async def main():
    unit_pii()
    await db_part()
    print("\nВСЕ ОК" if not FAILS else "\nПРОВАЛЫ: " + ", ".join(FAILS))
    sys.exit(1 if FAILS else 0)


asyncio.run(main())
