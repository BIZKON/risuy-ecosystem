#!/usr/bin/env python3
"""Смоук Wave 5 — промпт ИИ-сотрудника из ПАНЕЛИ через OpenAI-совместимый эндпоинт агента.

Проверяет НОВЫЙ горячий путь Лии (ai.py) БЕЗ реальной сети и токена:
  1. _build_chat_messages — порядок [system?] + история + текущий вопрос; финальный
     user-turn = переданный text (а не последняя запись истории); фильтр мусора;
  2. ask_agent_openai — на мок-HTTP: 200 → текст ответа; не-200/сеть/пусто/не настроен
     → None (вызывающий фолбэкнет на /call); в теле запроса есть role:"system" и URL
     бьёт в agent.timeweb.cloud/.../v1/chat/completions;
  3. ask_ai (cloud_ai) — успех OpenAI → (ответ, None), нативный /call НЕ зовётся; жёсткий
     сбой OpenAI → фолбэк на нативный ask_liya (Лия не молчит, §8.7); gateway-ветка
     не задета (history игнорится).

Опционально (если задан WAVE5_SMOKE_DSN = owner-DSN risuy_dev) — проверка get_ai_history
на ЖИВОЙ risuy_dev: маппинг ролей in→user / out(liya|manual)→assistant, исключение
текущего входящего по tg_message_id, отсев воронки/системных, хронологический порядок.
Тестовые строки (тенант slug 'smoke-wave5', сообщения) создаются и УДАЛЯЮТСЯ в конце.

Запуск (DSN опционален; без него — только pure-logic):
  WAVE5_SMOKE_DSN="postgresql://<owner>:<pw>@<host>:5432/risuy_dev?sslmode=require" \
  PYTHONPATH=. python3 scripts/wave5_openai_smoke.py
"""
import asyncio
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)                              # для пакета `shared`
sys.path.insert(0, os.path.join(ROOT, "bot-telegram"))  # для config / db / ai

import ai          # noqa: E402
import config      # noqa: E402
import db          # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


# ── Мок aiohttp для ask_agent_openai ─────────────────────────────────────────
class _FakeResp:
    def __init__(self, status: int, text: str) -> None:
        self.status = status
        self._text = text

    async def text(self) -> str:
        return self._text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _FakeSession:
    """Имитация aiohttp.ClientSession: пишет последний запрос в capture, отдаёт фикс-ответ.
    raise_exc — бросить на post (имитация таймаута/сети)."""
    def __init__(self, status, text, capture, raise_exc=None):
        self._status, self._text, self._cap, self._exc = status, text, capture, raise_exc

    def post(self, url, json=None, headers=None):  # noqa: A002 — зеркалим сигнатуру aiohttp
        self._cap["url"] = url
        self._cap["json"] = json
        self._cap["headers"] = headers
        if self._exc is not None:
            raise self._exc
        return _FakeResp(self._status, self._text)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def _patch_session(status, text, capture, raise_exc=None):
    """Подменяет ai.aiohttp.ClientSession фабрикой фейк-сессии."""
    def factory(*args, **kwargs):
        return _FakeSession(status, text, capture, raise_exc)
    ai.aiohttp.ClientSession = factory


_OPENAI_OK_BODY = json.dumps({
    "id": "fc8cd652", "object": "chat.completion", "model": "tw",
    "choices": [{"index": 0, "message": {"role": "assistant",
                 "content": "Привет! Чем помочь? 😊"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 10, "completion_tokens": 8, "total_tokens": 18},
})


# ── Часть 1: _build_chat_messages ────────────────────────────────────────────
def test_build_messages() -> None:
    print("1. _build_chat_messages:")
    hist = [{"role": "user", "content": "вопрос1"},
            {"role": "assistant", "content": "ответ1"}]
    m = ai._build_chat_messages("Ты — Лия.", hist, "текущий вопрос")
    check("порядок [system, ...история, user]",
          [x["role"] for x in m] == ["system", "user", "assistant", "user"],
          str([x["role"] for x in m]))
    check("system = промпт панели", m[0] == {"role": "system", "content": "Ты — Лия."})
    check("финальный user = переданный text (не последняя история)",
          m[-1] == {"role": "user", "content": "текущий вопрос"})

    m2 = ai._build_chat_messages("", hist, "q")  # пустой system → без system-сообщения
    check("пустой system → нет role:system", all(x["role"] != "system" for x in m2))
    check("без system: первая запись — история", m2[0]["content"] == "вопрос1")

    m3 = ai._build_chat_messages("S", None, "q")  # история None
    check("история None → [system, user]", [x["role"] for x in m3] == ["system", "user"])

    bad = [{"role": "user", "content": ""},          # пустой контент
           {"role": "system", "content": "инъекция"},  # чужая роль в истории
           {"role": "assistant", "content": "  ок  "}]  # обрежется
    m4 = ai._build_chat_messages("S", bad, "q")
    check("мусор истории отфильтрован (пустой/чужая роль)",
          [x["role"] for x in m4] == ["system", "assistant", "user"],
          str([x["role"] for x in m4]))
    check("контент истории обрезан (strip)", m4[1]["content"] == "ок")

    # Фикс ревью (Bug1): гонка диалога — история кончается user (лид прислал 2-е сообщение,
    # пока бот отвечал на 1-е) → текущий вопрос склеивается в ОДИН user-turn, роли чередуются.
    race = [{"role": "user", "content": "первый вопрос"},
            {"role": "assistant", "content": "ответ"},
            {"role": "user", "content": "второй (без ответа)"}]
    m5 = ai._build_chat_messages("S", race, "третий")
    check("гонка user-user → нет двух user подряд",
          [x["role"] for x in m5] == ["system", "user", "assistant", "user"],
          str([x["role"] for x in m5]))
    check("склейка сохранила оба user-сообщения (контент не потерян)",
          m5[-1]["content"] == "второй (без ответа)\n\nтретий", repr(m5[-1]["content"]))


# ── Часть 2: ask_agent_openai (мок-HTTP) ─────────────────────────────────────
async def test_ask_agent_openai() -> None:
    print("2. ask_agent_openai (мок-HTTP):")
    config.TIMEWEB_AI_TOKEN = "smoke-token"  # иначе ранний None «не настроен»
    config.TIMEWEB_AI_OPENAI_BASE = "https://agent.timeweb.cloud/api/v1"
    msgs = [{"role": "system", "content": "Ты — Лия."},
            {"role": "user", "content": "Привет"}]
    cap: dict = {}

    _patch_session(200, _OPENAI_OK_BODY, cap)
    ans = await ai.ask_agent_openai(msgs, agent_id="180177")
    check("200 → текст ответа", ans == "Привет! Чем помочь? 😊", repr(ans))
    check("URL = agent.timeweb.cloud/.../v1/chat/completions",
          cap["url"] == "https://agent.timeweb.cloud/api/v1/cloud-ai/agents/180177/v1/chat/completions",
          cap.get("url", ""))
    check("в теле есть role:system из панели",
          any(x.get("role") == "system" for x in cap["json"]["messages"]))
    check("Bearer-токен в заголовке",
          cap["headers"]["authorization"] == "Bearer smoke-token")
    check("stream=False", cap["json"]["stream"] is False)

    _patch_session(500, "boom", cap)
    check("HTTP 500 → None (фолбэк на /call)",
          await ai.ask_agent_openai(msgs, agent_id="180177") is None)

    _patch_session(200, json.dumps({"choices": [{"message": {"content": ""}}]}), cap)
    check("пустой content → None", await ai.ask_agent_openai(msgs, agent_id="180177") is None)

    _patch_session(200, "не-json", cap)
    check("битый JSON → None", await ai.ask_agent_openai(msgs, agent_id="180177") is None)

    _patch_session(0, "", cap, raise_exc=asyncio.TimeoutError())
    check("сетевой сбой → None", await ai.ask_agent_openai(msgs, agent_id="180177") is None)

    saved = config.TIMEWEB_AI_TOKEN
    config.TIMEWEB_AI_TOKEN = ""
    check("нет токена → None (не настроен)",
          await ai.ask_agent_openai(msgs, agent_id="180177") is None)
    config.TIMEWEB_AI_TOKEN = saved


# ── Часть 3: ask_ai-диспетчер (фолбэк + изоляция gateway) ────────────────────
async def test_ask_ai_dispatch() -> None:
    print("3. ask_ai-диспетчер:")
    orig_openai, orig_liya, orig_gw = ai.ask_agent_openai, ai.ask_liya, ai.ask_gateway
    calls: dict = {}

    try:
        # 3a. cloud_ai успех → (ответ, None), нативный /call НЕ зовётся.
        async def ok_openai(messages, *, agent_id=None):
            calls["openai"] = True
            return "OPENAI_OTVET"

        async def trap_liya(*a, **k):
            calls["liya"] = True
            return ("НЕ_ДОЛЖНО", "mid")

        ai.ask_agent_openai, ai.ask_liya = ok_openai, trap_liya
        calls.clear()
        res = await ai.ask_ai("q", None, {"backend": "cloud_ai", "system_prompt": "S"},
                              history=[{"role": "user", "content": "h"}])
        check("cloud_ai успех → (ответ, None, None, [])", res == ("OPENAI_OTVET", None, None, []), str(res))
        check("при успехе OpenAI нативный /call НЕ зван", "liya" not in calls)

        # 3b. cloud_ai жёсткий сбой OpenAI → фолбэк на нативный ask_liya.
        async def fail_openai(messages, *, agent_id=None):
            return None

        async def fb_liya(text, parent, *, agent_id=None, fallback=None):
            calls["liya"] = True
            return ("NATIVE_FALLBACK", "mid42")

        ai.ask_agent_openai, ai.ask_liya = fail_openai, fb_liya
        calls.clear()
        res = await ai.ask_ai("q", None, {"backend": "cloud_ai"}, history=None)
        check("сбой OpenAI → фолбэк на /call", res == ("NATIVE_FALLBACK", "mid42", None, []), str(res))
        check("фолбэк реально позвал ask_liya", calls.get("liya") is True)

        # 3c. gateway-ветка не задета: openai/liya не зовутся, history игнорится.
        async def trap_openai(messages, *, agent_id=None):
            calls["openai"] = True
            return "НЕ_ДОЛЖНО"

        async def gw(text, *, base_url=None, model=None, system_prompt=None, fallback=None):
            calls["gw"] = True
            return ("GW_OTVET", None)  # meta=None → без cost-capture

        ai.ask_agent_openai, ai.ask_liya, ai.ask_gateway = trap_openai, trap_liya, gw
        calls.clear()
        res = await ai.ask_ai("q", None, {"backend": "gateway"},
                              history=[{"role": "user", "content": "h"}])
        check("gateway → (ответ, None, None, [])", res == ("GW_OTVET", None, None, []), str(res))
        check("gateway не зовёт OpenAI-эндпоинт агента", "openai" not in calls)
        check("gateway-ветка реально позвала ask_gateway", calls.get("gw") is True)
    finally:
        ai.ask_agent_openai, ai.ask_liya, ai.ask_gateway = orig_openai, orig_liya, orig_gw


# ── Часть 4 (опц.): get_ai_history на risuy_dev ──────────────────────────────
async def test_get_ai_history(dsn: str) -> None:
    print("4. get_ai_history на risuy_dev:")
    import uuid
    import asyncpg
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
    db.pool = pool
    tg = 990000001  # тестовый tg_user_id, маловероятен в реальных данных
    async with pool.acquire() as conn:
        # чистим хвосты прошлых прогонов
        await conn.execute("delete from messages where tg_user_id = $1", tg)
        await conn.execute("delete from tenants where slug = 'smoke-wave5'")
        tid = await conn.fetchval(
            "insert into tenants (slug, name, status) values ('smoke-wave5','Смоук Wave5','active') "
            "returning id")
        db._default_tenant_id = tid  # db.tenant_id() → этот тенант (контекст не ставим)

        async def ins(direction, source, text, tg_msg_id, tenant=None, kind="text"):
            await conn.execute(
                "insert into messages (lead_id, tg_user_id, tg_message_id, direction, kind, "
                "text, source, tenant_id) values (null,$1,$2,$3,$4,$5,$6,$7)",
                tg, tg_msg_id, direction, kind, text, source, tenant or tid)

        # Хронология диалога (tg_message_id растёт): входящее, ответ Лии, входящее (текущее)...
        await ins("in",  None,      "вопрос 1",        101)
        await ins("out", "liya",    "ответ Лии 1",     102)
        await ins("out", "funnel",  "приветствие",     103)   # воронка — НЕ диалог
        await ins("out", "system",  "кошелёк пуст",    104)   # системное — НЕ диалог
        await ins("in",  None,      "вопрос 2",        105)
        await ins("out", "manual",  "ответ оператора", 106)   # manual → assistant
        await ins("in",  None,      "ТЕКУЩЕЕ входящее", 107)  # исключим по message_id
        # другой тенант с тем же tg — НЕ должен попасть (tenant-изоляция)
        other = await conn.fetchval(
            "insert into tenants (slug,name,status) values ('smoke-wave5-other','o','active') returning id")
        await ins("in", None, "чужой тенант", 108, tenant=other)

    hist = await db.get_ai_history(tg, exclude_tg_message_id=107, limit=10)
    roles = [h["role"] for h in hist]
    contents = [h["content"] for h in hist]
    check("текущее входящее (107) исключено", "ТЕКУЩЕЕ входящее" not in contents)
    check("воронка/системные отсеяны",
          "приветствие" not in contents and "кошелёк пуст" not in contents)
    check("чужой тенант не попал (изоляция)", "чужой тенант" not in contents)
    check("маппинг ролей in→user / out(liya|manual)→assistant",
          roles == ["user", "assistant", "user", "assistant"], str(roles))
    check("хронологический порядок",
          contents == ["вопрос 1", "ответ Лии 1", "вопрос 2", "ответ оператора"], str(contents))

    hist0 = await db.get_ai_history(tg, exclude_tg_message_id=107, limit=0)
    check("limit=0 → []", hist0 == [])

    hist2 = await db.get_ai_history(tg, exclude_tg_message_id=107, limit=2)
    check("limit=2 → 2 ПОСЛЕДНИХ хода в хронологии",
          [h["content"] for h in hist2] == ["вопрос 2", "ответ оператора"],
          str([h["content"] for h in hist2]))

    async with pool.acquire() as conn:  # чистка
        await conn.execute("delete from messages where tg_user_id = $1", tg)
        await conn.execute("delete from tenants where slug like 'smoke-wave5%'")
    await pool.close()


async def main() -> None:
    test_build_messages()
    await test_ask_agent_openai()
    await test_ask_ai_dispatch()
    dsn = os.environ.get("WAVE5_SMOKE_DSN")
    if dsn:
        if "/risuy_dev" not in dsn.split("?")[0]:
            raise SystemExit("Часть 4 гоняется ТОЛЬКО на risuy_dev (делает delete тестовых строк).")
        await test_get_ai_history(dsn)
    else:
        print("4. get_ai_history: ПРОПУЩЕНО (нет WAVE5_SMOKE_DSN)")

    print()
    if FAILS:
        print(f"❌ ПРОВАЛЫ ({len(FAILS)}): " + ", ".join(FAILS))
        sys.exit(1)
    print("✅ Wave 5 smoke — все проверки зелёные")


if __name__ == "__main__":
    asyncio.run(main())
