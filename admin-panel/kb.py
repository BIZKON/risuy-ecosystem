"""RF-RAG для панели: извлечение текста из файла + чанкинг + эмбеддинг через TEI.

Зеркалит bot-telegram/kb.py (там — эмбеддинг ЗАПРОСА с префиксом `query:`; здесь —
ДОКУМЕНТЫ с префиксом `passage:`). e5 ТРЕБУЕТ эти префиксы. Сеть — stdlib urllib в треде
(как timeweb_ai.py), без новых сетевых зависимостей. PDF — через pypdf (ленивый импорт,
чтобы панель работала для txt/md/csv даже если pypdf ещё не доустановлен).
"""
from __future__ import annotations

import asyncio
import io
import json
import urllib.error
import urllib.request

import config


class KBError(Exception):
    """Сбой обработки файла базы знаний (извлечение текста / эмбеддер недоступен / формат)."""


_PASSAGE = "passage: "


def chunk_text(text: str) -> list[str]:
    """Бьёт текст на чанки ~KB_CHUNK_TARGET символов по границам абзацев, с перекрытием.
    Сохраняет абзацы целыми, пока влезают; длинный абзац режется по словам."""
    target, overlap = config.KB_CHUNK_TARGET, config.KB_CHUNK_OVERLAP
    paras = [p.strip() for p in (text or "").replace("\r\n", "\n").split("\n\n") if p.strip()]
    chunks: list[str] = []
    buf = ""
    for p in paras:
        if len(p) > target:
            if buf:
                chunks.append(buf); buf = ""
            cur = ""
            for w in p.split():
                if len(cur) + len(w) + 1 > target:
                    chunks.append(cur); cur = cur[-overlap:] + " " + w
                else:
                    cur = f"{cur} {w}".strip()
            if cur:
                buf = cur
            continue
        if len(buf) + len(p) + 2 > target:
            chunks.append(buf)
            buf = (buf[-overlap:] + "\n\n" + p) if buf else p
        else:
            buf = f"{buf}\n\n{p}".strip()
    if buf:
        chunks.append(buf)
    return [c.strip() for c in chunks if c.strip()]


def extract_text(filename: str, data: bytes) -> str:
    """Текст из загруженного файла. txt/md/csv — декодируем (UTF-8, фолбэк CP1251);
    pdf — извлекаем текст через pypdf (только текст; картинки/схемы игнорируются)."""
    name = (filename or "").lower()
    if name.endswith(".pdf"):
        try:
            import pypdf  # ленивый импорт
        except ImportError as e:
            raise KBError("PDF не поддержан: библиотека pypdf не установлена (обновите панель)") from e
        try:
            reader = pypdf.PdfReader(io.BytesIO(data))
            return "\n\n".join((page.extract_text() or "") for page in reader.pages)
        except Exception as e:  # битый/защищённый PDF
            raise KBError(f"Не удалось прочитать PDF: {e}") from e
    for enc in ("utf-8", "cp1251"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    raise KBError("Файл не в кодировке UTF-8/CP1251 — сохраните как текст в UTF-8")


def _embed_sync(texts: list[str]) -> list[list[float]]:
    if not config.EMBEDDER_URL:
        raise KBError("Эмбеддер не настроен: задайте EMBEDDER_URL в окружении панели")
    url = config.EMBEDDER_URL.rstrip("/") + "/embed"
    headers = {"content-type": "application/json", "accept": "application/json"}
    if config.EMBEDDER_TOKEN:
        headers["authorization"] = f"Bearer {config.EMBEDDER_TOKEN}"
    out: list[list[float]] = []
    for i in range(0, len(texts), 32):                       # батчами по 32 пассажа
        batch = [_PASSAGE + t for t in texts[i:i + 32]]
        body = json.dumps({"inputs": batch, "normalize": True}).encode()
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                data = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode()[:200]
            except Exception:
                pass
            raise KBError(f"Эмбеддер HTTP {e.code}: {detail}") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise KBError(f"Эмбеддер недоступен: {e}") from e
        except (ValueError, json.JSONDecodeError) as e:
            raise KBError("Эмбеддер вернул невалидный ответ") from e
        if not isinstance(data, list) or len(data) != len(batch):
            raise KBError("Эмбеддер вернул неожиданное число векторов")
        out.extend(data)
    return out


async def embed_passages(texts: list[str]) -> list[list[float]]:
    """Эмбеддинги документов (passage-префикс) через TEI. Исполняется в треде (urllib —
    блокирующий). На любой сбой — KBError (вызывающий покажет ошибку в разделе)."""
    return await asyncio.to_thread(_embed_sync, texts)
