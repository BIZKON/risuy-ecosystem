#!/usr/bin/env python3
"""Смоук доктора тенанта (Э2) на risuy_dev: намеренно сломанный тенант обязан давать ожидаемые FAIL.

Проверяет не «скрипт не падает», а что каждый оракул срабатывает ровно на своей поломке:
  1. здоровый кабинет — шаги 2 и 7 зелёные;
  2. снят is_default → шаг 2 FAIL (бот не выберет агента);
  3. kb_enabled=false при живых чанках → шаг 7 FAIL (загруженная база молчит);
  4. чанк с role_tag, которого нет среди агентов → шаг 6a FAIL (его не увидит никто);
  5. фолбэк = дефолт из shared/ai_defaults → шаг 9 FAIL (чужой бренд и почта);
  6. статус тенанта не active → шаг 1 FAIL (мультиплекс не поднимет бота);
  7. доктор НИЧЕГО не пишет: admin_audit до и после равен;
  8. содержимое чанков в отчёт не попадает (маркер в тексте не должен всплыть).

🟥 ТОЛЬКО risuy_dev: гард по имени базы. Создаёт временный тенант smoke-doctor-<hex> и удаляет его
   каскадом в finally — даже при падении проверок.

Запуск: DOCTOR_SMOKE_DSN="postgresql://gen_user@host:5432/risuy_dev?sslmode=require" PGPASSWORD=… \\
        PYTHONPATH=. <venv>/bin/python scripts/tenant_doctor_smoke.py
"""
from __future__ import annotations

import asyncio
import os
import secrets
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import asyncpg  # noqa: E402

import tenant_doctor as doc  # noqa: E402

DSN = os.environ.get("DOCTOR_SMOKE_DSN") or os.environ.get("DOCTOR_DSN") or ""
if not DSN:
    raise SystemExit("Нужен DOCTOR_SMOKE_DSN на risuy_dev.")
if doc.dbname_of(DSN) != "risuy_dev":
    raise SystemExit(f"ОТКАЗ: смоук только на risuy_dev, получен {doc.dbname_of(DSN)!r}.")

SLUG = "smoke-doctor-" + secrets.token_hex(4)
MARKER = "МАРКЕР-СОДЕРЖИМОГО-ЧАНКА-НЕ-ДОЛЖЕН-ПОПАСТЬ-В-ОТЧЁТ"
VEC = "[" + ",".join(["0.01"] * 768) + "]"
EMBED_OK = (doc.OK, "", 768)

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


def step(rep: doc.Report, prefix: str) -> tuple[str, str]:
    """Статус и деталь шага отчёта по префиксу его имени."""
    for status, name, detail, _fix in rep.rows:
        if name.startswith(prefix):
            return status, detail
    return "НЕТ", ""


async def run_doctor(slug: str) -> doc.Report:
    """Прогон доктора ОТДЕЛЬНЫМ read-only соединением — как в бою."""
    c = await asyncpg.connect(DSN)
    try:
        await c.execute("set default_transaction_read_only = on")
        assert await c.fetchval("show transaction_read_only") == "on"
        return await doc.check_tenant(c, slug, EMBED_OK)
    finally:
        await c.close()


async def main() -> int:
    w = await asyncpg.connect(DSN)
    tid = None
    try:
        tid = await w.fetchval(
            "insert into tenants (slug, name, status) values ($1, $2, 'active') returning id",
            SLUG, "Смоук доктора")
        await w.execute(
            "insert into team_agents (tenant_id, slug, name, system_prompt, backend, agent_id, "
            "  fallback_text, is_default, enabled, kb_enabled, position) "
            "values ($1,'default','Смоук-агент','инструкции','gateway','', 'свой фолбэк', "
            "  true, true, true, 1)", tid)
        await w.execute(
            "insert into tenant_settings (tenant_id, key, value) values ($1,'ai_model',$2)",
            tid, "deepseek/deepseek-v4-flash")
        doc_id = await w.fetchval(
            "insert into kb_documents (title, content, tenant_id) values ('смоук','x',$1) returning id",
            tid)
        await w.execute(
            "insert into kb_chunks (document_id, chunk_index, content, embedding, metadata, tenant_id) "
            "values ($1, 0, $2, $3::vector, '{}'::jsonb, $4)", doc_id, MARKER, VEC, tid)

        print(f"Смоук доктора · база=risuy_dev · тенант={SLUG}")

        audit_before = await w.fetchval("select count(*) from admin_audit")

        # 1. здоровый кабинет
        rep = await run_doctor(SLUG)
        s2, _ = step(rep, "2.")
        s7, _ = step(rep, "7.")
        check("здоровый: шаг 2 (агент выбран) зелёный", s2 == doc.OK, s2)
        check("здоровый: шаг 7 (тумблер БЗ) зелёный", s7 == doc.OK, s7)
        report_text = "\n".join(f"{a} {b} {c_}" for a, b, c_, _ in rep.rows)
        check("8. содержимое чанка НЕ попало в отчёт", MARKER not in report_text)

        # 2. снят is_default
        await w.execute("update team_agents set is_default = false where tenant_id = $1", tid)
        s2, d2 = step(await run_doctor(SLUG), "2.")
        check("снят is_default → шаг 2 FAIL", s2 == doc.FAIL, f"{s2}: {d2}")
        await w.execute("update team_agents set is_default = true where tenant_id = $1", tid)

        # 3. выключен kb_enabled при живых чанках
        await w.execute("update team_agents set kb_enabled = false where tenant_id = $1", tid)
        s7, d7 = step(await run_doctor(SLUG), "7.")
        check("kb_enabled=false при чанках → шаг 7 FAIL", s7 == doc.FAIL, f"{s7}: {d7}")
        await w.execute("update team_agents set kb_enabled = true where tenant_id = $1", tid)

        # 4. чанк с чужим отделом
        await w.execute(
            "update kb_chunks set metadata = '{\"role_tag\":\"призрак\"}'::jsonb where tenant_id = $1", tid)
        s6a, d6a = step(await run_doctor(SLUG), "6a.")
        check("role_tag без агента → шаг 6a FAIL", s6a == doc.FAIL, f"{s6a}: {d6a}")
        await w.execute("update kb_chunks set metadata = '{}'::jsonb where tenant_id = $1", tid)

        # 5. фолбэк = чужой дефолт
        await w.execute("update team_agents set fallback_text = $2 where tenant_id = $1",
                        tid, doc.AI_DEFAULT_FALLBACK)
        s9, d9 = step(await run_doctor(SLUG), "9.")
        check("фолбэк = дефолт с чужим брендом → шаг 9 FAIL", s9 == doc.FAIL, f"{s9}: {d9}")
        await w.execute("update team_agents set fallback_text = 'свой' where tenant_id = $1", tid)

        # 6. тенант не active
        await w.execute("update tenants set status = 'suspended' where id = $1", tid)
        s1, d1 = step(await run_doctor(SLUG), "1.")
        check("статус suspended → шаг 1 FAIL", s1 == doc.FAIL, f"{s1}: {d1}")
        await w.execute("update tenants set status = 'active' where id = $1", tid)

        # 7. доктор не пишет
        audit_after = await w.fetchval("select count(*) from admin_audit")
        check("7. доктор не пишет в аудит", audit_before == audit_after,
              f"{audit_before} → {audit_after}")

        # 8. несуществующий slug — не падаем, а честно сообщаем
        s1n, _ = step(await run_doctor("нет-такого-кабинета"), "1.")
        check("несуществующий кабинет → FAIL без исключения", s1n == doc.FAIL)
    finally:
        if tid is not None:
            await w.execute("delete from tenants where id = $1", tid)
        await w.close()

    print()
    if FAILS:
        print(f"❌ tenant_doctor_smoke: провалено {len(FAILS)} — " + "; ".join(FAILS))
        return 1
    print("✅ tenant_doctor_smoke: все проверки зелёные")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
