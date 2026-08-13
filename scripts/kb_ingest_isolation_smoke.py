#!/usr/bin/env python3
"""Смоук Э3: kb_ingest.py не сносит документы чужого тенанта и не пишет без явного разрешения.

Проверяет ровно тот дефект, ради которого правился скрипт: раньше идемпотентный DELETE шёл
БЕЗ фильтра по тенанту и ДО резолва тенанта, поэтому загрузка «Прайса» одному клиенту стирала
«Прайс» у всех остальных. Плюс новые предохранители: dry-run по умолчанию и --confirm-name.

Гоняет НАСТОЯЩИЙ scripts/kb_ingest.py подпроцессом — так проверяются и аргументы, и гейты,
а не только SQL.

  1. dry-run (без --apply) — ничего не изменил, ни документа, ни аудита;
  2. --apply с неверным --confirm-name — отказ, ничего не изменил;
  3. --apply с верным именем — документ тенанта A заменён;
  4. документ тенанта B с ТЕМ ЖЕ заголовком — цел (главный оракул);
  5. запись в admin_audit появилась (actor='kb_ingest.py');
  6. --tenant-slug обязателен: запуск без него падает.

🟥 ТОЛЬКО risuy_dev (гард по имени базы). Тестовые тенанты удаляются каскадом в finally.

Запуск:
  KB_SMOKE_DSN="postgresql://gen_user@host:5432/risuy_dev?sslmode=require" PGPASSWORD=… \\
  EMBEDDER_URL="http://…" [EMBEDDER_TOKEN=…] PYTHONPATH=. <venv>/bin/python \\
      scripts/kb_ingest_isolation_smoke.py
"""
from __future__ import annotations

import asyncio
import os
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path

import asyncpg

ROOT = Path(__file__).resolve().parent.parent
DSN = os.environ.get("KB_SMOKE_DSN") or os.environ.get("TEAM_DSN") or ""
EMBEDDER = (os.environ.get("EMBEDDER_URL") or "").strip()
TOKEN = os.environ.get("EMBEDDER_TOKEN", "")

if not DSN:
    raise SystemExit("Нужен KB_SMOKE_DSN на risuy_dev.")
if "/risuy_dev" not in DSN.split("?")[0]:
    raise SystemExit("ОТКАЗ: смоук только на risuy_dev.")
if not EMBEDDER:
    raise SystemExit("Нужен EMBEDDER_URL — шаг 3 грузит по-настоящему.")

TAG = secrets.token_hex(4)
SLUG_A, SLUG_B = f"smoke-kb-a-{TAG}", f"smoke-kb-b-{TAG}"
NAME_A, NAME_B = f"Тенант А {TAG}", f"Тенант Б {TAG}"
TITLE = "Прайс"          # ОДИН И ТОТ ЖЕ заголовок у обоих — в этом весь смысл проверки
VEC = "[" + ",".join(["0.01"] * 768) + "]"

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


def run_ingest(*extra: str, file_path: str) -> subprocess.CompletedProcess:
    cmd = [sys.executable, str(ROOT / "scripts" / "kb_ingest.py"),
           "--dsn", DSN, "--embedder", EMBEDDER, "--title", TITLE,
           "--file", file_path, *extra]
    if TOKEN:
        cmd += ["--token", TOKEN]
    env = {**os.environ, "PGPASSWORD": os.environ.get("PGPASSWORD", "")}
    return subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=str(ROOT), timeout=180)


async def seed_doc(c: asyncpg.Connection, tid, marker: str) -> None:
    doc = await c.fetchval(
        "insert into kb_documents (title, content, tenant_id) values ($1,$2,$3) returning id",
        TITLE, marker, tid)
    await c.execute(
        "insert into kb_chunks (document_id, chunk_index, content, embedding, metadata, tenant_id) "
        "values ($1,0,$2,$3::vector,'{}'::jsonb,$4)", doc, marker, VEC, tid)


async def state(c: asyncpg.Connection, tid) -> tuple[int, str]:
    row = await c.fetchrow(
        "select count(*) n, coalesce(max(content),'') content from kb_documents "
        "where tenant_id = $1 and title = $2", tid, TITLE)
    return int(row["n"]), row["content"]


async def main() -> int:
    c = await asyncpg.connect(DSN)
    tmp = Path(tempfile.mkdtemp()) / "novyy-price.md"
    tmp.write_text("НОВЫЙ ПРАЙС\n\nСтроки нового прайса для проверки загрузки.", encoding="utf-8")
    ta = tb = None
    try:
        ta = await c.fetchval(
            "insert into tenants(slug,name,status) values($1,$2,'active') returning id", SLUG_A, NAME_A)
        tb = await c.fetchval(
            "insert into tenants(slug,name,status) values($1,$2,'active') returning id", SLUG_B, NAME_B)
        await seed_doc(c, ta, "СТАРЫЙ-ПРАЙС-А")
        await seed_doc(c, tb, "ПРАЙС-Б-ТРОГАТЬ-НЕЛЬЗЯ")
        audit_before = await c.fetchval("select count(*) from admin_audit where actor = 'kb_ingest.py'")

        print(f"Смоук kb_ingest · база=risuy_dev · тенанты {SLUG_A} / {SLUG_B}")

        # 1. dry-run
        p = run_ingest("--tenant-slug", SLUG_A, file_path=str(tmp))
        n_a, content_a = await state(c, ta)
        check("1. dry-run: код возврата 0", p.returncode == 0, p.stderr.strip()[:200])
        check("1. dry-run: документ А не тронут", content_a == "СТАРЫЙ-ПРАЙС-А", content_a[:40])
        check("1. dry-run: сказано, что будет сделано", "DRY-RUN" in p.stdout)

        # 2. --apply с чужим именем
        p = run_ingest("--tenant-slug", SLUG_A, "--apply", "--confirm-name", "Совсем не тот",
                       file_path=str(tmp))
        n_a, content_a = await state(c, ta)
        # Оракул именно на гейт, а не на «упало хоть как-то»: падение по другой причине
        # (нет зависимости, опечатка в DSN) не должно засчитываться как сработавшая защита.
        check("2. неверный --confirm-name: отказ именно гейтом",
              p.returncode != 0 and "confirm-name" in (p.stdout + p.stderr),
              f"rc={p.returncode}: {(p.stderr or p.stdout).strip()[-120:]}")
        check("2. неверный --confirm-name: документ А не тронут", content_a == "СТАРЫЙ-ПРАЙС-А")

        # 3. настоящая загрузка
        p = run_ingest("--tenant-slug", SLUG_A, "--apply", "--confirm-name", NAME_A,
                       file_path=str(tmp))
        n_a, content_a = await state(c, ta)
        check("3. --apply: код возврата 0", p.returncode == 0,
              (p.stderr.strip() or p.stdout.strip())[-200:])
        check("3. документ А заменён на новый", content_a.startswith("НОВЫЙ ПРАЙС"), content_a[:40])
        check("3. документ А ровно один", n_a == 1, str(n_a))

        # 4. ГЛАВНЫЙ ОРАКУЛ: чужой тенант с тем же заголовком цел
        n_b, content_b = await state(c, tb)
        check("4. документ Б с тем же заголовком ЦЕЛ", n_b == 1 and content_b == "ПРАЙС-Б-ТРОГАТЬ-НЕЛЬЗЯ",
              f"n={n_b}, {content_b[:40]}")
        chunks_b = await c.fetchval("select count(*) from kb_chunks where tenant_id = $1", tb)
        check("4. чанки Б целы", chunks_b == 1, str(chunks_b))

        # 5. аудит
        audit_after = await c.fetchval("select count(*) from admin_audit where actor = 'kb_ingest.py'")
        check("5. аудит записан", audit_after == audit_before + 1, f"{audit_before} → {audit_after}")
        det = await c.fetchval(
            "select detail from admin_audit where actor = 'kb_ingest.py' order by id desc limit 1")
        check("5. в аудите указан тенант", det is not None and str(ta) in str(det))

        # 6. --tenant-slug обязателен
        p = run_ingest("--apply", "--confirm-name", NAME_A, file_path=str(tmp))
        check("6. без --tenant-slug падает именно на argparse",
              p.returncode != 0 and "tenant-slug" in (p.stdout + p.stderr),
              (p.stderr or p.stdout).strip()[-120:])
    finally:
        for t in (ta, tb):
            if t is not None:
                await c.execute("delete from kb_documents where tenant_id = $1", t)
                await c.execute("delete from tenants where id = $1", t)
        await c.execute("delete from admin_audit where actor = 'kb_ingest.py' and detail->>'tenant_slug' = any($1::text[])",
                        [SLUG_A, SLUG_B])
        await c.close()

    print()
    if FAILS:
        print(f"❌ kb_ingest_isolation_smoke: провалено {len(FAILS)} — " + "; ".join(FAILS))
        return 1
    print("✅ kb_ingest_isolation_smoke: все проверки зелёные")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
