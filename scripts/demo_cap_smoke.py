#!/usr/bin/env python3
"""Smoke: суточный потолок публичного демо-чата (дыра C). Без сети и БД.

Что проверяем:
  • чистое тело отказа bot._demo_cap_payload() — форма ответа, которую видит виджет сайта;
  • расщепление rate-бакетов демо и брифа (флуд демо-чата не должен закрывать форму брифа
    платящего клиента — оба эндпоинта раньше делили один словарь _chat_rl_hits), причём
    пинится не только хелпер, но и КАЖДЫЙ call-site — AST-инвариантом по bot.py;
  • сам гейт: прямой вызов bot._demo_chat с фейковым request и подменённым слоем db —
    потолок по ответам, потолок по токенам, сбой БД, разовость алертов владельцу и то,
    что при исчерпании потолка платный ai.ask_gateway НЕ вызывается вовсе;
  • источник потолка: во всех кейсах daily_limit фейка ОТЛИЧАЕТСЯ от config-стаба, иначе
    подмена cfg['daily_limit'] на config.DEMO_CHAT_DAILY_LIMIT (смерть решения «менять
    потолок апсертом app_settings без деплоя») прошла бы мимо смоука;
  • бронь токенов: оценка запроса записывается ДО платного вызова и корректируется фактом
    после — без этого суточный бюджет срабатывал бы с задержкой на время полёта запросов;
  • семантика нуля: DEMO_CHAT_DAILY_TOKENS = 0 ВЫКЛЮЧАЕТ бюджет, а не закрывает витрину.

Запуск (сеть/БД не нужны, config и aiogram застабены):
  /path/to/.venv/bin/python scripts/demo_cap_smoke.py

⚠️ В stderr по ходу прогона ОЖИДАЕМЫ строки ERROR/WARNING с трейсбеками: кейсы 6 и 8
намеренно роняют слой БД и шлюз, а бот обязан их логировать. Вердикт смоука — только
последняя строка (🟢) и коды OK/FAIL.
"""
import ast
import asyncio
import json
import os
import sys
import types

# ── заглушки отсутствующих зависимостей (aiogram + внутренние модули бота) ──
# Делаем МИНИМАЛЬНЫЙ stub: bot.py импортирует только имена, нам не нужна логика.


def _stub_module(name: str, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m


# aiogram
_aiogram = _stub_module("aiogram")
_aiogram.Bot = object
_aiogram.Dispatcher = object
_stub_module("aiogram.client", session=None)
_stub_module("aiogram.client.session")
_sub = _stub_module("aiogram.client.session.aiohttp")
_sub.AiohttpSession = object
_stub_module("aiogram.types").BotCommand = object

# Внутренние модули бота (у них нет зависимостей от aiogram в top-level,
# но при импорте bot.py они импортируются — достаточно пустых стабов).
for _mod in ("ai", "config", "db", "escalation", "metering_worker",
             "multiplex", "nurture", "retention", "richfmt", "triggers",
             "worker", "handlers", "messaging"):
    if _mod not in sys.modules:
        _stub_module(_mod)

# handlers.router нужен как атрибут
sys.modules["handlers"].router = object()
# messaging.LoggingMiddleware
sys.modules["messaging"].LoggingMiddleware = object

# Константы потолка живут в config — стабу их надо задать явно (боевые дефолты в config.py).
sys.modules["config"].DEMO_CHAT_DAILY_LIMIT = 100
sys.modules["config"].DEMO_CHAT_DAILY_TOKENS = 1_000_000

# Чистые функции, которые дёргает _demo_chat после ответа шлюза (в проде — реальные модули).
sys.modules["ai"]._with_immunity = lambda s: s
sys.modules["escalation"].parse_escalation = lambda answer: (answer, None)
sys.modules["triggers"].parse_trigger_markers = lambda answer: (answer, None)
sys.modules["richfmt"].to_plain = lambda answer: answer

# ── теперь импортируем bot ───────────────────────────────────────────────────
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bot-telegram"))

import bot  # noqa: E402

fails: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        fails.append(name)


# ── фейки запроса и слоя БД ──────────────────────────────────────────────────
class _FakeRequest:
    """Минимальная замена aiohttp.web.Request для прямого (не через HTTP) вызова _demo_chat.

    Атрибут remote обязателен: _client_ip читает его, когда нет X-Forwarded-For (канон
    фейка — scripts/dadata_metering_smoke.py)."""

    def __init__(self, body: dict, *, method: str = "POST", remote: str = "203.0.113.7"):
        self.method = method
        self.headers: dict = {}   # у dict есть .get с дефолтом — этого хватает _client_ip
        self.remote = remote
        self._body = body

    async def json(self):
        return self._body


class _FakeDB:
    """Подменяет ботовый слой db: гейт обязан работать поверх ЛЮБЫХ значений счётчиков.

    Пишет журнал вызовов — по нему проверяем «слот не взят», «алерт не отправлен» и
    «расход токенов дописан»."""

    def __init__(self, *, daily_limit: int = 100, tokens_used: int = 0,
                 take: tuple = (True, 1), take_raises: bool = False,
                 owner_chat: str | None = "777", tid=None):
        self.daily_limit = daily_limit
        self.tokens_used = tokens_used
        self.take = take
        self.take_raises = take_raises
        self.owner_chat = owner_chat
        self.tid = tid
        self.take_calls: list[int] = []
        self.tokens_added: list[int] = []
        self.notify: list[tuple] = []

    async def get_demo_chat_cfg(self):
        return {"tid": self.tid, "system_prompt": "промпт", "model": "модель",
                "fallback": "", "daily_limit": self.daily_limit}

    async def demo_chat_tokens_used(self):
        return self.tokens_used

    async def demo_chat_quota_take(self, limit):
        self.take_calls.append(limit)
        if self.take_raises:
            raise RuntimeError("смоук: БД недоступна")
        return self.take

    async def demo_chat_tokens_add(self, tokens):
        self.tokens_added.append(tokens)
        return tokens

    async def get_owner_chat_id(self):
        return self.owner_chat

    async def enqueue_platform_notify(self, chat_id, text):
        self.notify.append((chat_id, text))
        return 1


ask_calls: list = []   # журнал обращений к платному шлюзу
ask_seen: list = []    # снимок db.tokens_added В МОМЕНТ платного вызова (пин порядка брони)


def _reset(fake: "_FakeDB", ask):
    """Общий сброс перед кейсом: счётчики rate-limit и память разовых алертов, фейки db/ai.

    _chat_rl_hits чистим обязательно: без этого кейсы упрутся в per-IP лимит 20/мин и
    смоук проверит рейт-лимитер вместо потолка."""
    bot._chat_rl_hits.clear()
    bot._demo_alert_sent.clear()
    ask_calls.clear()
    ask_seen.clear()
    bot.db = fake
    bot.ai.ask_gateway = ask


async def _boom(*_a, **_kw):
    """Платный вызов, которого при исчерпанном потолке быть не должно.

    Факт вызова фиксируем СПИСКОМ, а не только исключением: _demo_chat ловит Exception
    (а AssertionError — его наследник), поэтому «тест упал бы» здесь не работает — молча
    получили бы 200 с фолбэком и ложно-зелёный оракул."""
    ask_calls.append("вызван")
    raise AssertionError("платный ai.ask_gateway вызван при исчерпанном потолке")


def _body(text: str = "сколько стоит внедрение?") -> dict:
    return {"consent": True, "messages": [{"role": "user", "content": text}]}


# ── 1. чистое тело отказа ────────────────────────────────────────────────────
print("1. тело отказа _demo_cap_payload()")
resp = bot._demo_cap_payload()
payload = json.loads(resp.body)
check("статус 429", resp.status == 429, str(resp.status))
check("есть непустой reply (виджет читает data.reply раньше статуса)",
      bool((payload.get("reply") or "").strip()), repr(payload.get("reply"))[:60])
check("error == 'daily_cap' (а не 'rate_limited' — иначе фронт скажет «подождите минуту»)",
      payload.get("error") == "daily_cap", repr(payload.get("error")))
check("ответ прошёл через _cors",
      resp.headers.get("Access-Control-Allow-Origin") == bot._DEMO_CHAT_ORIGIN,
      repr(resp.headers.get("Access-Control-Allow-Origin")))
_low = (payload.get("reply") or "").lower()
check("текст без канцелярита («лимит»/«квота»/«ошибк»)",
      not any(w in _low for w in ("лимит", "квота", "ошибк")), _low[:60])

# ── 2. расщепление бакетов демо и брифа (C8) ─────────────────────────────────
print("2. rate-бакеты демо и брифа разведены")
bot._chat_rl_hits.clear()
demo_allowed = sum(1 for _ in range(25) if bot._rl_allow_chat("198.51.100.9", "demo"))
check("per-IP демо: из 25 попыток прошло ровно _CHAT_RL_MAX",
      demo_allowed == bot._CHAT_RL_MAX, f"прошло {demo_allowed}, лимит {bot._CHAT_RL_MAX}")
check("флуд демо не закрыл приём брифа с того же IP",
      bot._rl_allow_chat("198.51.100.9", "brief") is True)
# общий бюджет эндпоинта тоже per-scope: выбираем его в демо разными IP
bot._chat_rl_hits.clear()
for i in range(bot._CHAT_RL_GLOBAL_MAX):
    bot._rl_allow_chat(f"192.0.2.{i % 250}.{i}", "demo")
check("общий бюджет демо исчерпан", bot._rl_allow_chat("203.0.113.1", "demo") is False)
check("общий бюджет брифа при этом свободен",
      bot._rl_allow_chat("203.0.113.1", "brief") is True)

# ── 3. AST-инвариант: scope пинится на КАЖДОМ call-site, а не только в хелпере ─
# Без этого блока возврат в _brief_submit вызова _rl_allow_chat(ip) без scope (дефолт
# 'demo') прошёл бы молча — то есть вернулся бы ровно тот регресс, ради которого волна
# делалась. Канон AST-части — scripts/channel_bool_branching_smoke.py.
print("3. AST: scope рейт-лимитера закреплён на call-site")
_TREE = ast.parse(open(os.path.join(ROOT, "bot-telegram", "bot.py"), encoding="utf-8").read())
_FUNCS = {n.name: n for n in ast.walk(_TREE)
          if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))}


def _rl_scopes(func_name: str) -> list | None:
    """Литералы 2-го позиционного аргумента всех вызовов _rl_allow_chat внутри функции.
    None — самой функции в bot.py нет (переименовали → смоук обязан упасть, а не смолчать)."""
    func = _FUNCS.get(func_name)
    if func is None:
        return None
    out: list = []
    for n in ast.walk(func):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                and n.func.id == "_rl_allow_chat"):
            a = n.args[1] if len(n.args) > 1 else None
            out.append(a.value if isinstance(a, ast.Constant) else None)
    return out


_demo_scopes, _brief_scopes = _rl_scopes("_demo_chat"), _rl_scopes("_brief_submit")
check("_demo_chat зовёт _rl_allow_chat со scope 'demo'", _demo_scopes == ["demo"],
      str(_demo_scopes))
check("_brief_submit зовёт _rl_allow_chat со scope 'brief'", _brief_scopes == ["brief"],
      str(_brief_scopes))
_all_rl = [n for n in ast.walk(_TREE) if isinstance(n, ast.Call)
           and isinstance(n.func, ast.Name) and n.func.id == "_rl_allow_chat"]
check("во всём bot.py ровно два call-site (новый эндпоинт не делит бакет молча)",
      len(_all_rl) == 2, f"найдено {len(_all_rl)}")

# ── 4. интеграционный оракул: потолок закрывает платный вызов (C10) ──────────
print("4. гейт _demo_chat: потолок ответов исчерпан")
fake = _FakeDB(daily_limit=0, take=(False, 1))
_reset(fake, _boom)
r = asyncio.run(bot._demo_chat(_FakeRequest(_body())))
p = json.loads(r.body)
check("статус 429", r.status == 429, str(r.status))
check("в теле есть reply", bool((p.get("reply") or "").strip()))
check("error == 'daily_cap'", p.get("error") == "daily_cap", repr(p.get("error")))
check("потолок взят из cfg['daily_limit'] (0), а не из env-константы", fake.take_calls == [0],
      str(fake.take_calls))
check("платный ask_gateway НЕ вызван", ask_calls == [], str(ask_calls))
check("при daily_limit=0 алерт владельцу НЕ отправлен (иначе ложный сигнал каждый день)",
      fake.notify == [], str(fake.notify))

# ── 5. потолок по токенам срабатывает ДО взятия слота ────────────────────────
print("5. гейт _demo_chat: суточный бюджет токенов исчерпан")
fake = _FakeDB(daily_limit=42, tokens_used=1_000_000)
_reset(fake, _boom)
r = asyncio.run(bot._demo_chat(_FakeRequest(_body())))
p = json.loads(r.body)
check("статус 429 и daily_cap", r.status == 429 and p.get("error") == "daily_cap",
      f"{r.status} {p.get('error')}")
check("слот суточного счётчика НЕ взят (не жжём потолок ответов зря)",
      fake.take_calls == [], str(fake.take_calls))
check("платный ask_gateway НЕ вызван", ask_calls == [], str(ask_calls))
check("бронь токенов не ставилась (до платного вызова дело не дошло)",
      fake.tokens_added == [], str(fake.tokens_added))
check("владельцу ушёл ровно один алерт про бюджет токенов", len(fake.notify) == 1,
      str(len(fake.notify)))
check("в тексте алерта есть израсходованное число (владелец видит масштаб)",
      bool(fake.notify) and "1000000" in fake.notify[0][1],
      fake.notify[0][1][:80] if fake.notify else "")
# Повтор БЕЗ сброса _demo_alert_sent — алерт разовый, а не на каждый запрос.
asyncio.run(bot._demo_chat(_FakeRequest(_body())))
check("второй запрос подряд второй алерт НЕ шлёт", len(fake.notify) == 1, str(len(fake.notify)))
# Отматываем отметку на 2 часа назад: при суточном интервале (86400) алерта по-прежнему нет,
# при часовом (дефолт _DEMO_ALERT_INTERVAL) он бы ушёл. Так пинится именно нестандартный
# интервал: уронят его до часа — владелец получит 24 сигнала в сутки, и оракул покраснеет.
bot._demo_alert_sent["tokens"] -= 7200.0
asyncio.run(bot._demo_chat(_FakeRequest(_body())))
check("через 2 часа алерт всё ещё не повторился (интервал суточный, а не часовой)",
      len(fake.notify) == 1, str(len(fake.notify)))

# ── 6. алерт владельцу — ровно на переходе через потолок ─────────────────────
print("6. алерт владельцу на первом переборе")
fake = _FakeDB(daily_limit=42, take=(False, 43))
_reset(fake, _boom)
asyncio.run(bot._demo_chat(_FakeRequest(_body())))
check("платный ask_gateway НЕ вызван", ask_calls == [], str(ask_calls))
check("слот взят с потолком из cfg (42), а не из env-константы", fake.take_calls == [42],
      str(fake.take_calls))
check("на cur == limit+1 алерт отправлен ровно один", len(fake.notify) == 1, str(fake.notify))
check("алерт ушёл на owner_chat_id числом",
      bool(fake.notify) and fake.notify[0][0] == 777, str(fake.notify[:1]))
check("в тексте алерта стоит потолок ИЗ cfg (42)",
      bool(fake.notify) and "42" in fake.notify[0][1],
      fake.notify[0][1][:80] if fake.notify else "")
check("рецепт подъёма — АПСЕРТ (update по несуществующему ключу тронул бы 0 строк)",
      bool(fake.notify) and "insert into app_settings" in fake.notify[0][1],
      fake.notify[0][1][-120:] if fake.notify else "")

fake = _FakeDB(daily_limit=42, take=(False, 150))
_reset(fake, _boom)
asyncio.run(bot._demo_chat(_FakeRequest(_body())))
check("на последующих отказах суток алерт НЕ повторяется", fake.notify == [], str(fake.notify))

# ── 7. сбой БД ≠ потолок: витрина закрыта, но диагноз другой ─────────────────
print("7. сбой БД при взятии слота")
fake = _FakeDB(daily_limit=42, take_raises=True)
_reset(fake, _boom)
r = asyncio.run(bot._demo_chat(_FakeRequest(_body())))
p = json.loads(r.body)
check("посетителю тот же вежливый отказ", r.status == 429 and p.get("error") == "daily_cap",
      f"{r.status} {p.get('error')}")
check("платный ask_gateway НЕ вызван (fail-closed по факту сбоя счётчика)",
      ask_calls == [], str(ask_calls))
check("владельцу ушёл отдельный алерт про СБОЙ", len(fake.notify) == 1, str(fake.notify))
check("в тексте алерта сказано, что это сбой, а не потолок",
      bool(fake.notify) and "сбо" in fake.notify[0][1].lower(),
      fake.notify[0][1][:70] if fake.notify else "")
# повтор в том же часу не должен слать второй алерт (рейт-лимит по образцу _price_warned)
asyncio.run(bot._demo_chat(_FakeRequest(_body())))
check("повторный сбой в том же часу второй алерт НЕ шлёт", len(fake.notify) == 1,
      str(len(fake.notify)))

# ── 8. регресс: в пределах потолка демо работает, бронь → факт (C7) ─────────
print("8. в пределах потолка демо отвечает, бронь ставится ДО вызова и сверяется фактом")

_PROMPT = "промпт"           # то же, что отдаёт _FakeDB.get_demo_chat_cfg
_QUESTION = "сколько стоит внедрение?"


async def _ask_ok(*_a, **_kw):
    # Снимок учёта В МОМЕНТ платного вызова: бронь обязана быть записана ДО него. Перенос
    # брони под ask_gateway вернул бы read-before/write-after — этот оракул его ловит.
    ask_seen.append(list(bot.db.tokens_added))
    return "Ответ Лии", {"model": "m", "request_id": "r",
                         "usage": {"prompt_tokens": 10, "completion_tokens": 20}}

fake = _FakeDB(daily_limit=42, take=(True, 7))
_reset(fake, _ask_ok)
r = asyncio.run(bot._demo_chat(_FakeRequest(_body(_QUESTION))))
p = json.loads(r.body)
_reserve = bot._demo_reserve_tokens(_PROMPT, [], _QUESTION)
check("статус 200 и ответ Лии дошёл", r.status == 200 and p.get("reply") == "Ответ Лии",
      f"{r.status} {p.get('reply')!r}")
check("слот взят ровно один раз с потолком из cfg (42)", fake.take_calls == [42],
      str(fake.take_calls))
check("бронь записана ДО платного вызова",
      bool(ask_seen) and ask_seen[0] == [_reserve], f"{ask_seen} против брони {_reserve}")
check("бронь посчитана по системному промпту, истории и вопросу",
      fake.tokens_added[:1] == [_reserve], str(fake.tokens_added[:1]))
check("после ответа записана дельта до факта (сумма записей == prompt+completion)",
      len(fake.tokens_added) == 2 and sum(fake.tokens_added) == 30, str(fake.tokens_added))
check("алерта нет — потолок не пройден", fake.notify == [], str(fake.notify))
check("история удорожает бронь (system-промпт и история считаются, а не только вопрос)",
      bot._demo_reserve_tokens(_PROMPT, [{"role": "user", "content": "и" * 2000}], _QUESTION)
      > _reserve)

# ── 9. авария шлюза: мягкий ответ, бронь снята (C6) ─────────────────────────
print("9. авария шлюза: мягкий ответ, без 500, бронь возвращена")


async def _ask_fail(*_a, **_kw):
    raise RuntimeError("смоук: шлюз недоступен")

fake = _FakeDB(daily_limit=42, take=(True, 8))
_reset(fake, _ask_fail)
r = asyncio.run(bot._demo_chat(_FakeRequest(_body())))
p = json.loads(r.body)
check("статус 200 с мягким текстом (не 500 без CORS)",
      r.status == 200 and bool((p.get("reply") or "").strip()), f"{r.status} {p.get('reply')!r}")
check("бронь снята: суммарно записан ноль (поломка шлюза не жжёт бюджет)",
      len(fake.tokens_added) == 2 and sum(fake.tokens_added) == 0, str(fake.tokens_added))

# ── 10. DEMO_CHAT_DAILY_TOKENS = 0 — это ВЫКЛЮЧЕННЫЙ бюджет, а не kill-switch ─
# Kill-switch витрины — только DEMO_CHAT_DAILY_LIMIT = 0 (кейс 4). Ноль в бюджете токенов
# снимает бюджет совсем: витрина отвечает, брони нет, факт по-прежнему пишется.
print("10. нулевой бюджет токенов не закрывает витрину")
sys.modules["config"].DEMO_CHAT_DAILY_TOKENS = 0
try:
    fake = _FakeDB(daily_limit=42, tokens_used=10**9, take=(True, 9))
    _reset(fake, _ask_ok)
    r = asyncio.run(bot._demo_chat(_FakeRequest(_body(_QUESTION))))
    p = json.loads(r.body)
    check("витрина отвечает, хотя расход уже огромный", r.status == 200 and ask_seen != [],
          f"{r.status} {ask_seen}")
    check("брони нет, записан только факт", fake.tokens_added == [30], str(fake.tokens_added))
finally:
    sys.modules["config"].DEMO_CHAT_DAILY_TOKENS = 1_000_000

print()
if fails:
    print("\n".join("❌ " + f for f in fails))
    raise SystemExit(1)
print("🟢 demo_cap_smoke зелёный")
