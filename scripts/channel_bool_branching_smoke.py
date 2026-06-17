#!/usr/bin/env python3
"""Смоук инварианта #1 HIGH (аудит B3, circuit-breaker каналов): «драйвер возвращает ЧЕСТНЫЙ bool,
воркер по False делает release_* (НЕ mark_sent)». Регрессия именно этого пути ломала статистику
доставки и ослепляла circuit-breaker. Тест дёшево защищает инвариант на ДВУХ слоях:

  ЧАСТЬ A (поведенческая) — драйверы VK/MAX send*/send_media/send_photo/send_document возвращают
    True ТОЛЬКО при подтверждённом успехе API (VK — нет 'error' в теле; MAX — HTTP 200) и False при
    ошибке-в-теле / HTTP≠200 / исключении/сети. Тестируется БЕЗ настоящего aiohttp: подсовываем
    лёгкий стаб `aiohttp` в sys.modules (драйверы импортят его ЛЕНИВО внутри методов) + фейковую
    сессию со сценарными ответами. Прод-код НЕ меняется, venv-контракт (без aiohttp) сохранён.

  ЧАСТЬ B (AST-инвариант воркера) — worker.py импортит aiogram сверху → в .venv-smoke не импортнуть;
    поэтому проверяем СТРУКТУРНО (ast.parse, без импорта): в _drain_outbox_channels и _send_batch
    есть ветка `if not ok:` , которая зовёт release_outbox/release_recipient и НЕ зовёт mark_*_sent
    (а mark_*_sent присутствует в функции — на успешном пути). Ловит удаление ветки/возврат к
    «всегда mark_sent».

Запуск: PYTHONPATH=. ./.venv-smoke/bin/python scripts/channel_bool_branching_smoke.py
"""
import ast
import asyncio
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "bot-telegram"))

# ── Стаб aiohttp (драйверы импортят его лениво внутри методов) ───────────────────
_aio = types.ModuleType("aiohttp")


class _ClientError(Exception):
    pass


class _FormData:
    def __init__(self, *a, **k):
        pass

    def add_field(self, *a, **k):
        pass


class _ClientTimeout:
    def __init__(self, *a, **k):
        pass


_aio.ClientError = _ClientError
_aio.FormData = _FormData
_aio.ClientTimeout = _ClientTimeout
sys.modules["aiohttp"] = _aio

import max_driver  # noqa: E402
import vk_driver  # noqa: E402

max_driver._MEDIA_NOT_READY_PAUSE = 0  # не спим в ретрае медиа во время теста

FAILS: list[str] = []


def check(name, cond, detail=""):
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail != "" else ""))
    if not cond:
        FAILS.append(name)


# ── Фейк HTTP (async-context-manager response + FIFO-сессия) ─────────────────────
class FakeResp:
    def __init__(self, *, payload=None, status=200, text="", raise_on_enter=None):
        self._payload = payload
        self.status = status
        self._text = text
        self._raise = raise_on_enter

    async def __aenter__(self):
        if self._raise is not None:
            raise self._raise
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self, content_type=None):
        return self._payload

    async def text(self):
        return self._text


class FakeSession:
    """FIFO-очередь ответов: get/post по очереди отдают заранее заданные FakeResp."""

    def __init__(self, responses):
        self._q = list(responses)
        self.calls: list[tuple[str, str]] = []

    def _pop(self, method, url):
        self.calls.append((method, url))
        if not self._q:
            raise AssertionError(f"FakeSession: нет ответа для {method} {url}")
        return self._q.pop(0)

    def get(self, url, **kw):
        return self._pop("GET", url)

    def post(self, url, **kw):
        return self._pop("POST", url)

    async def close(self):
        pass


def _vk(responses):
    b = vk_driver.VKBot("tok", -100, on_message=None)
    b._session = FakeSession(responses)
    return b


def _max(responses):
    b = max_driver.MAXBot("tok", on_message=None)
    b._session = FakeSession(responses)
    return b


async def part_a_vk():
    print("A1. VK send (messages.send):")
    check("успех (response в теле) → True",
          await _vk([FakeResp(payload={"response": 999})]).send(123, "hi") is True)
    check("error-в-теле (HTTP 200) → False",
          await _vk([FakeResp(payload={"error": {"error_code": 9, "error_msg": "flood"}})]).send(123, "hi") is False)
    check("исключение сети → False",
          await _vk([FakeResp(raise_on_enter=_ClientError("net"))]).send(123, "hi") is False)

    print("A2. VK send_photo (upload→save→send):")
    ok_chain = [
        FakeResp(payload={"response": {"upload_url": "http://up"}}),   # getMessagesUploadServer
        FakeResp(payload={"photo": "p", "server": 1, "hash": "h"}),    # _upload
        FakeResp(payload={"response": [{"owner_id": -100, "id": 5}]}), # saveMessagesPhoto
        FakeResp(payload={"response": 1}),                              # messages.send
    ]
    check("успешная цепочка → True",
          await _vk(ok_chain).send_photo(123, b"img", caption="c") is True)
    check("ошибка на первом шаге (error в теле) → False",
          await _vk([FakeResp(payload={"error": {"error_code": 15}})]).send_photo(123, b"img") is False)

    print("A3. VK send_document:")
    ok_doc = [
        FakeResp(payload={"response": {"upload_url": "http://up"}}),   # docs.getMessagesUploadServer
        FakeResp(payload={"file": "f"}),                               # _upload
        FakeResp(payload={"response": {"doc": {"owner_id": -100, "id": 7}}}),  # docs.save
        FakeResp(payload={"response": 1}),                             # messages.send
    ]
    check("успешная цепочка → True",
          await _vk(ok_doc).send_document(123, b"doc", filename="f.pdf") is True)
    check("исключение сети → False",
          await _vk([FakeResp(raise_on_enter=_ClientError("net"))]).send_document(123, b"d", filename="f") is False)


async def part_a_max():
    print("A4. MAX send (POST /messages):")
    check("HTTP 200 → True",
          await _max([FakeResp(status=200, text="ok")]).send(160, "hi") is True)
    check("HTTP 400 → False",
          await _max([FakeResp(status=400, text="bad")]).send(160, "hi") is False)
    check("исключение сети → False",
          await _max([FakeResp(raise_on_enter=_ClientError("net"))]).send(160, "hi") is False)

    print("A5. MAX send_media (uploads→upload→messages):")
    ok_media = [
        FakeResp(payload={"url": "http://up"}),   # POST /uploads
        FakeResp(payload={"token": "t"}),         # POST upload url
        FakeResp(status=200, text="ok"),          # POST /messages
    ]
    check("успех (200) → True",
          await _max(ok_media).send_media(160, media_type="image", content=b"x", caption="c") is True)
    fail_media = [
        FakeResp(payload={"url": "http://up"}),
        FakeResp(payload={"token": "t"}),
        FakeResp(status=400, text="bad"),         # не attachment.not.ready
    ]
    check("HTTP 400 (не not.ready) → False",
          await _max(fail_media).send_media(160, media_type="file", content=b"x") is False)
    retry_media = [
        FakeResp(payload={"url": "http://up"}),
        FakeResp(payload={"token": "t"}),
        FakeResp(status=400, text="attachment.not.ready"),  # 1-я отправка: ещё не готово
        FakeResp(status=200, text="ok"),                     # ретрай: ок
    ]
    check("attachment.not.ready → ретрай → 200 → True",
          await _max(retry_media).send_media(160, media_type="image", content=b"x") is True)
    check("исключение на upload → False",
          await _max([FakeResp(raise_on_enter=_ClientError("net"))]).send_media(160, media_type="file", content=b"x") is False)


# ── ЧАСТЬ B: AST-инвариант воркера ──────────────────────────────────────────────
def _called_names(node) -> set[str]:
    names: set[str] = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Attribute):
                names.add(f.attr)
            elif isinstance(f, ast.Name):
                names.add(f.id)
    return names


def _find_not_ok_if(func):
    """Найти ветку `if not ok:` внутри функции."""
    for n in ast.walk(func):
        if isinstance(n, ast.If):
            t = n.test
            if (isinstance(t, ast.UnaryOp) and isinstance(t.op, ast.Not)
                    and isinstance(t.operand, ast.Name) and t.operand.id == "ok"):
                return n
    return None


def part_b_worker():
    print("B. AST-инвариант воркера (if not ok → release_*, НЕ mark_sent):")
    src = open(os.path.join(ROOT, "bot-telegram", "worker.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    funcs = {n.name: n for n in ast.walk(tree)
             if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))}

    cases = [
        ("_drain_outbox_channels", "release_outbox", "mark_outbox_sent"),
        ("_send_batch", "release_recipient", "mark_recipient_sent"),
    ]
    for fname, release_fn, mark_fn in cases:
        func = funcs.get(fname)
        if func is None:
            check(f"{fname}: функция найдена", False, "НЕ найдена в worker.py")
            continue
        notok = _find_not_ok_if(func)
        check(f"{fname}: есть ветка `if not ok:`", notok is not None)
        if notok is None:
            continue
        branch_calls: set[str] = set()
        for stmt in notok.body:
            branch_calls |= _called_names(stmt)
        check(f"{fname}: ветка неуспеха зовёт {release_fn}()", release_fn in branch_calls,
              repr(sorted(branch_calls)))
        check(f"{fname}: ветка неуспеха НЕ зовёт {mark_fn}() (нет ложного 'sent')",
              mark_fn not in branch_calls)
        check(f"{fname}: {mark_fn}() есть на успешном пути функции", mark_fn in _called_names(func))


async def main():
    await part_a_vk()
    await part_a_max()
    part_b_worker()
    print()
    if FAILS:
        print(f"❌ ПРОВАЛЫ ({len(FAILS)}): " + ", ".join(FAILS))
        sys.exit(1)
    print("✅ channel_bool_branching smoke — bool-контракт драйверов + ветвление воркера зелёные")


if __name__ == "__main__":
    asyncio.run(main())
