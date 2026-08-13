#!/usr/bin/env python3
"""Ингест справочного контента в базу знаний RF-RAG (pgvector).

Чанкинг → эмбеддинг (наш TEI, intfloat/multilingual-e5-base, passage-префикс) → запись
в kb_documents/kb_chunks. БЕЗ OpenAI, всё в РФ-инфре. Запускает ВЛАДЕЛЕЦ (owner-DSN —
ингест пишет векторы; панель под panel_rw сделает то же позже через UI).

Пример:
    python3 scripts/kb_ingest.py \\
        --dsn "postgresql://gen_user:PASS@81.31.246.136:5432/risuy?sslmode=require" \\
        --embedder http://<vm-ip>:8080 \\
        --title "Тарифы и услуги" \\
        --file docs/kb/tarify.md docs/kb/uslugi.md
        # --role lia        # слаг персоны; пусто = общая справка для ВСЕХ ролей

Идемпотентно по (tenant_id, title, role): повторный запуск с тем же тенантом, заголовком и ролью
УДАЛЯЕТ прежний документ (каскадом — его чанки) и грузит заново. Форматы: .md/.txt/.csv (любой
UTF-8 текст). Эмбеддер должен быть поднят (см. docs/rag-embedder-vm.md).

🔴 БЕЗОПАСНОСТЬ (Э3). Три предохранителя, которых раньше не было:
  • DELETE фильтруется по tenant_id, и тенант резолвится ДО удаления. Прежняя редакция сносила
    документ с таким же заголовком у ЛЮБОГО тенанта — загрузка «Прайса» одному клиенту стирала
    «Прайс» всем остальным.
  • --tenant-slug обязателен. Дефолт 'lesov-school' означал, что забытый флаг молча грузит справку
    клиента в кабинет Школы.
  • Dry-run по умолчанию: без --apply скрипт только считает и показывает, что сделает. Для записи
    нужен ещё и --confirm-name с именем тенанта — защита от «не тот slug под рукой».
Штатная загрузка через панель пишет аудит (admin-panel/db.py), скрипт раньше не писал — теперь
пишет тоже: actor='kb_ingest.py', action='kb_ingest', tenant_id в detail.

Зависимости — те же, что у бота: asyncpg, aiohttp (см. bot-telegram/requirements.txt).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import aiohttp
import asyncpg

_PASSAGE_PREFIX = "passage: "   # обязательный префикс e5 для документа (запрос — "query: ")
_CHUNK_TARGET = 700             # символов в чанке (≈ абзац-два справки)
_CHUNK_OVERLAP = 100           # перекрытие соседних чанков — чтобы не рвать смысл на стыке
_EMBED_BATCH = 32              # сколько пассажей слать в TEI за один запрос


def chunk_text(text: str) -> list[str]:
    """Бьёт текст на чанки ~_CHUNK_TARGET символов по границам абзацев, с перекрытием.
    Сохраняет абзацы целыми, пока влезают; длинный абзац режется по словам."""
    paras = [p.strip() for p in text.replace("\r\n", "\n").split("\n\n") if p.strip()]
    chunks: list[str] = []
    buf = ""
    for p in paras:
        if len(p) > _CHUNK_TARGET:                 # очень длинный абзац — режем по словам
            if buf:
                chunks.append(buf); buf = ""
            words, cur = p.split(), ""
            for w in words:
                if len(cur) + len(w) + 1 > _CHUNK_TARGET:
                    chunks.append(cur); cur = cur[-_CHUNK_OVERLAP:] + " " + w
                else:
                    cur = f"{cur} {w}".strip()
            if cur:
                buf = cur
            continue
        if len(buf) + len(p) + 2 > _CHUNK_TARGET:
            chunks.append(buf)
            buf = (buf[-_CHUNK_OVERLAP:] + "\n\n" + p) if buf else p
        else:
            buf = f"{buf}\n\n{p}".strip()
    if buf:
        chunks.append(buf)
    return [c.strip() for c in chunks if c.strip()]


async def embed_passages(session: aiohttp.ClientSession, base: str, token: str,
                         passages: list[str]) -> list[list[float]]:
    """Эмбеддинги документов через TEI (passage-префикс, батчами). Падает с ошибкой,
    если эмбеддер недоступен — ингест без векторов смысла не имеет."""
    url = base.rstrip("/") + "/embed"
    headers = {"content-type": "application/json"}
    if token:
        headers["authorization"] = f"Bearer {token}"
    out: list[list[float]] = []
    for i in range(0, len(passages), _EMBED_BATCH):
        batch = [_PASSAGE_PREFIX + p for p in passages[i:i + _EMBED_BATCH]]
        async with session.post(url, json={"inputs": batch, "normalize": True},
                                headers=headers) as resp:
            if resp.status != 200:
                raise SystemExit(f"TEI embed HTTP {resp.status}: {(await resp.text())[:300]}")
            data = await resp.json()
        if not isinstance(data, list) or len(data) != len(batch):
            raise SystemExit(f"TEI вернул неожиданный ответ: {str(data)[:200]}")
        out.extend(data)
    return out


def _vec_literal(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


async def main() -> None:
    ap = argparse.ArgumentParser(description="Ингест справки в базу знаний RF-RAG (pgvector)")
    ap.add_argument("--dsn", required=True, help="owner-DSN Postgres (postgresql://…?sslmode=require)")
    ap.add_argument("--embedder", required=True, help="URL TEI, напр. http://<vm-ip>:8080")
    ap.add_argument("--token", default="", help="опц. Bearer для TEI (если за reverse-proxy)")
    ap.add_argument("--title", required=True, help="заголовок документа (ключ идемпотентности)")
    ap.add_argument("--role", default="", help="слаг персоны; пусто = общая справка для всех")
    # Wave 3: kb_documents/kb_chunks tenant-scoped (tenant_id NOT NULL, DEFAULT снят 3d).
    # Э3: дефолта НЕТ намеренно — забытый флаг раньше грузил справку клиента в кабинет Школы.
    ap.add_argument("--tenant-slug", required=True, help="тенант справки (обязательно)")
    ap.add_argument("--confirm-name", default="",
                    help="имя тенанта из БД — подтверждение при --apply (защита от чужого slug)")
    ap.add_argument("--apply", action="store_true",
                    help="реально записать; без него — только показать, что будет сделано")
    ap.add_argument("--file", nargs="+", required=True, help="один или несколько текстовых файлов")
    args = ap.parse_args()

    texts: list[str] = []
    for fp in args.file:
        path = Path(fp)
        if not path.is_file():
            raise SystemExit(f"Файл не найден: {fp}")
        texts.append(path.read_text(encoding="utf-8"))
    content = "\n\n".join(texts)
    chunks = chunk_text(content)
    if not chunks:
        raise SystemExit("После чанкинга пусто — нечего грузить.")
    role = args.role.strip()
    source = ", ".join(Path(f).name for f in args.file)
    print(f"Файлов: {len(args.file)} · чанков: {len(chunks)} · роль: {role or '(общая)'}")

    conn = await asyncpg.connect(args.dsn)
    try:
        # ── Э3: тенант резолвится ДО любых удалений. Раньше delete шёл первым и бил по всем. ──
        trow = await conn.fetchrow(
            "select id, name from tenants where slug = $1", args.tenant_slug)
        if trow is None:
            raise SystemExit(f"Тенант '{args.tenant_slug}' не найден в tenants")
        tid, tname = trow["id"], trow["name"]

        # Что именно снесёт идемпотентность — считаем ДО, чтобы показать в отчёте.
        doomed = await conn.fetchrow(
            "select count(*) docs, coalesce(sum(n), 0) chunks from ("
            "  select d.id, (select count(*) from kb_chunks k where k.document_id = d.id) n"
            "    from kb_documents d"
            "   where d.tenant_id = $1 and d.title = $2 and coalesce(d.role_tag,'') = $3) s",
            tid, args.title, role)

        print(f"\nтенант: {tname} ({args.tenant_slug})")
        print(f"удалю:  документов {doomed['docs']}, чанков {doomed['chunks']}")
        print(f"залью:  документов 1, чанков {len(chunks)}")

        if not args.apply:
            print("\nDRY-RUN: ничего не записано. Для записи добавь --apply "
                  f"и --confirm-name «{tname}».")
            return
        if (args.confirm_name or "").strip().casefold() != (tname or "").strip().casefold():
            raise SystemExit(
                f"ОТКАЗ: --confirm-name не совпал с именем тенанта в БД.\n"
                f"  ожидалось: {tname!r}\n  получено:  {args.confirm_name!r}")

        async with aiohttp.ClientSession() as session:
            print("\nЭмбеддинг через TEI…")
            vectors = await embed_passages(session, args.embedder, args.token, chunks)
        dim = len(vectors[0]) if vectors else 0
        print(f"Готово эмбеддингов: {len(vectors)} (размерность {dim})")
        if dim != 768:
            print(f"⚠️  Размерность {dim} ≠ 768 (схема ждёт vector(768) для e5-base). Проверь модель TEI.",
                  file=sys.stderr)

        async with conn.transaction():
            # Идемпотентность СВОЕГО тенанта: (tenant_id, title, role). Каскад чистит чанки.
            await conn.execute(
                "delete from kb_documents where tenant_id = $3 and title = $1 "
                "and coalesce(role_tag,'') = $2",
                args.title, role, tid,
            )
            doc_id = await conn.fetchval(
                """insert into kb_documents (title, source, role_tag, content, created_by, tenant_id)
                   values ($1, $2, nullif($3,''), $4, 'script', $5) returning id""",
                args.title, source, role, content, tid,
            )
            meta = {"role_tag": role, "title": args.title, "source": source}
            await conn.executemany(
                """insert into kb_chunks (document_id, chunk_index, content, embedding, metadata, tenant_id)
                   values ($1, $2, $3, $4::vector, $5::jsonb,
                           (select tenant_id from kb_documents where id = $1))""",
                [
                    (doc_id, i, ch, _vec_literal(vec),
                     json.dumps({**meta, "chunk_index": i}, ensure_ascii=False))
                    for i, (ch, vec) in enumerate(zip(chunks, vectors))
                ],
            )
            # Э3: штатная загрузка через панель пишет аудит — скрипт теперь тоже.
            # Текстов документа в detail нет: содержимое базы знаний в аудит не выносим.
            await conn.execute(
                "insert into admin_audit (actor, action, detail) values ($1, $2, $3::jsonb)",
                "kb_ingest.py", "kb_ingest",
                json.dumps({"tenant_id": str(tid), "tenant_slug": args.tenant_slug,
                            "title": args.title, "role_tag": role, "source": source,
                            "document_id": str(doc_id), "chunks": len(chunks),
                            "deleted_docs": int(doomed["docs"]),
                            "deleted_chunks": int(doomed["chunks"])}, ensure_ascii=False),
            )
        print(f"✅ Загружено: документ {doc_id}, чанков {len(chunks)}.")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
