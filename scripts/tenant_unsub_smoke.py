#!/usr/bin/env python3
"""Смоук дыры D: отписка у ТЕНАНТ-ботов (152-ФЗ/38-ФЗ) + фабрика тенант-роутера.

Рассылка тенант-бота всегда вешает inline-кнопку «Отписаться» (messaging.broadcast_markup),
но у тенант-роутера долго не было ни её обработчика, ни /stop, ни слова «СТОП» — лид не мог
отказаться, а «СТОП» текстом уходил в платный вызов Лии. Смоук пинит фикс на четырёх слоях:

  ЧАСТЬ A (AST, без aiogram) — структура bot-telegram/multiplex.py и messaging.py:
    роутер строится ФАБРИКОЙ (модульного синглтона нет — иначе второй тенант-бот не поднимется),
    ни одна команда не спрятана под catch-all `F.text`, литерал кнопки == литералу фильтра,
    cb.answer() первым действием, в отписке нет гейтов (paused/erase/provenance/upsert_start),
    оживлённый /shop гейтится паузой оператора и отзывом согласия ДО записи ПДн, а в VK/MAX
    ветка «СТОП» стоит ВЫШЕ upsert_start (паритет с TG: на отказ ПДн не заводим).
  ЧАСТЬ R (рантайм, нужен aiogram) — build_tenant_router() отдаёт НОВЫЙ Router на каждый
    Dispatcher: два тенант-бота в одном процессе поднимаются без RuntimeError «Router is already
    attached». Плюс: слово-отписка реализована ФИЛЬТРОМ (ранний return в теле съел бы весь текст
    и Лия замолчала бы у всех тенантов).
  ЧАСТЬ C (тексты) — подтверждение отписки тенант-нейтрально: без «Школы» и чужой почты,
    с безусловным упоминанием /revoke.
  ЧАСТЬ B (данные, risuy_dev) — set_unsubscribed возвращает lead_id|None, идемпотентен, снимает
    лида с аудитории рассылки и НЕ пишет consent_events (отписка ≠ отзыв согласия).

Части A/C/R работают без БД. Часть B требует DSN на risuy_dev и на прод НЕ запускается.

Запуск (A+C+R, venv с aiogram):
  PYTHONPATH=. ./.venv-smoke/bin/python scripts/tenant_unsub_smoke.py
Запуск с частью B:
  TENANT_UNSUB_SMOKE_DSN="postgresql://<owner>@<host>:5432/risuy_dev?sslmode=require" \
  PYTHONPATH=. ./.venv-smoke/bin/python scripts/tenant_unsub_smoke.py
"""
import ast
import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "bot-telegram"))
os.environ.setdefault("BOT_TOKEN", "0:smoke")
os.environ.setdefault("DATABASE_URL", "postgresql://x/y")
os.environ.setdefault("CHANNEL_ID", "-1001234567890")
os.environ.setdefault("CHANNEL_URL", "https://t.me/smoke")
os.environ.setdefault("GUIDE_URL", "https://example.org/guide")

import texts  # noqa: E402  (bot-telegram/texts.py — без внешних зависимостей)

MULTIPLEX = os.path.join(ROOT, "bot-telegram", "multiplex.py")
MESSAGING = os.path.join(ROOT, "bot-telegram", "messaging.py")

DSN = os.environ.get("TENANT_UNSUB_SMOKE_DSN")
if DSN and "/risuy_dev" not in DSN.split("?")[0]:
    raise SystemExit("TENANT_UNSUB_SMOKE_DSN только на risuy_dev (защита от прода).")

SLUG = "smoke-unsub"
TG = 990004441          # лид, который отпишется
TG_CTRL = 990004442     # контрольный лид (остаётся в аудитории)
TG_ALIEN = 990004449    # id, которого нет в тенанте

FAILS: list[str] = []


def check(name, cond, detail=""):
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail != "" else ""))
    if not cond:
        FAILS.append(name)


def _src(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def _funcs(tree) -> dict:
    return {n.name: n for n in ast.walk(tree)
            if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))}


def _called_names(node) -> set[str]:
    """Имена всех вызванных функций/методов внутри узла (как в channel_bool_branching_smoke)."""
    names: set[str] = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Attribute):
                names.add(f.attr)
            elif isinstance(f, ast.Name):
                names.add(f.id)
    return names


def _registrations(func) -> list[dict]:
    """Регистрации хендлеров внутри фабрики: [{'observer','handler','filters'}] В ПОРЯДКЕ кода.

    Ждём плоскую последовательность `<router>.<observer>.register(<handler>, <фильтры…>)`.
    """
    out: list[dict] = []
    for stmt in func.body:
        if not isinstance(stmt, ast.Expr) or not isinstance(stmt.value, ast.Call):
            continue
        call = stmt.value
        f = call.func
        if not (isinstance(f, ast.Attribute) and f.attr == "register"
                and isinstance(f.value, ast.Attribute)):
            continue
        if not call.args or not isinstance(call.args[0], ast.Name):
            continue
        out.append({
            "observer": f.value.attr,
            "handler": call.args[0].id,
            # ast.unparse печатает строки в одинарных кавычках — нормализуем, чтобы сравнивать
            # фильтры с литералами кода независимо от стиля кавычек.
            "filters": [ast.unparse(a).replace("'", '"') for a in call.args[1:]],
        })
    return out


def _first_await_call(func) -> str:
    """Имя функции/метода в ПЕРВОМ await тела (для «cb.answer() первым действием»)."""
    for n in ast.walk(func):
        if isinstance(n, ast.Await) and isinstance(n.value, ast.Call):
            f = n.value.func
            return f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "?")
    return ""


def _call_line(func, name: str) -> int | None:
    """Строка ПЕРВОГО вызова name внутри функции (порядок гейтов пиним по строкам)."""
    lines = [n.lineno for n in ast.walk(func) if isinstance(n, ast.Call)
             and ((isinstance(n.func, ast.Attribute) and n.func.attr == name)
                  or (isinstance(n.func, ast.Name) and n.func.id == name))]
    return min(lines) if lines else None


def _string_kwarg(func, name: str) -> str | None:
    """Значение строкового именованного аргумента (callback_data='unsub') внутри функции."""
    for n in ast.walk(func):
        if isinstance(n, ast.Call):
            for kw in n.keywords:
                if kw.arg == name and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                    return kw.value.value
    return None


# ── ЧАСТЬ A: структура тенант-роутера ────────────────────────────────────────────
def part_a() -> None:
    print("A. Структура тенант-роутера (AST):")
    src = _src(MULTIPLEX)
    tree = ast.parse(src)
    funcs = _funcs(tree)

    # A1. Модульного синглтона нет: Router(), созданный на уровне модуля, включается в ВТОРОЙ
    # Dispatcher и aiogram 3 бросает RuntimeError «Router is already attached» → второй
    # тенант-бот не поднимается вовсе (проверено запуском на aiogram 3.30).
    singleton = [t.id for n in tree.body if isinstance(n, ast.Assign)
                 for t in n.targets
                 if isinstance(t, ast.Name) and isinstance(n.value, ast.Call)
                 and isinstance(n.value.func, ast.Name) and n.value.func.id == "Router"]
    check("нет модульного Router() (роутер строится фабрикой)", not singleton, repr(singleton))

    # A2/A3. Фабрика есть, создаёт Router внутри себя, и её зовёт _run_tenant.
    factory = funcs.get("build_tenant_router")
    check("есть build_tenant_router()", factory is not None)
    if factory is None:
        return
    check("фабрика создаёт Router() внутри себя", "Router" in _called_names(factory))
    run_tenant = funcs.get("_run_tenant")
    check("_run_tenant зовёт build_tenant_router()",
          run_tenant is not None and "build_tenant_router" in _called_names(run_tenant))

    regs = _registrations(factory)
    msg = [r for r in regs if r["observer"] == "message"]
    cbq = [r for r in regs if r["observer"] == "callback_query"]
    check("в фабрике есть регистрации message/callback_query", bool(msg) and bool(cbq),
          f"message={len(msg)}, callback_query={len(cbq)}")
    if not msg or not cbq:
        return

    names = [r["handler"] for r in msg]
    print(f"     порядок message-хендлеров: {names}")
    idx_text = next((i for i, r in enumerate(msg) if r["handler"] == "t_text"), -1)
    check("t_text зарегистрирован", idx_text >= 0)
    check("t_text — ПОСЛЕДНЯЯ message-регистрация (catch-all внизу)",
          idx_text == len(msg) - 1, f"idx={idx_text} из {len(msg)}")

    # A4. Ни одна команда не спрятана под catch-all: хендлер ниже `F.text` недостижим —
    # t_text возвращает None на '/'-текст, а aiogram 3 останавливает пропагацию (так /shop и умер).
    for i, r in enumerate(msg):
        for flt in r["filters"]:
            if flt.startswith("Command("):
                check(f"{flt} выше catch-all F.text", idx_text < 0 or i < idx_text,
                      f"idx={i}, idx_text={idx_text}")
    have_cmds = {flt for r in msg for flt in r["filters"] if flt.startswith("Command(")}
    for cmd in ('Command("start"', 'Command("revoke"', 'Command("stop"', 'Command("shop"'):
        check(f"есть регистрация {cmd}…)", any(f.startswith(cmd) for f in have_cmds), repr(sorted(have_cmds)))

    # A5. Слово-отписка — ФИЛЬТРОМ, не ранним return в теле: иначе хендлер съел бы ВЕСЬ текст
    # (aiogram не пропускает апдейт дальше после return) и Лия замолчала бы у всех тенантов.
    word_regs = [(i, r) for i, r in enumerate(msg) if any("_is_unsub" in f for f in r["filters"])]
    check("слово-отписка зарегистрирована фильтром с _is_unsub", bool(word_regs),
          repr([r["filters"] for r in msg]))
    for i, r in word_regs:
        check("слово-отписка выше catch-all F.text", idx_text < 0 or i < idx_text, f"idx={i}")
    bare_text = [(i, r["handler"]) for i, r in enumerate(msg)
                 if i != idx_text and r["filters"] == ["F.text"]]
    check("выше t_text нет второго голого F.text (он бы перехватил весь текст)",
          not bare_text, repr(bare_text))

    # A6. Литерал кнопки == литералу фильтра: не два независимых пина, а равенство — иначе смена
    # callback_data в рассылке снова молча убьёт хендлер, а смоук останется зелёным.
    mtree = ast.parse(_src(MESSAGING))
    btn = _string_kwarg(_funcs(mtree)["broadcast_markup"], "callback_data")
    got = [f for r in cbq for f in r["filters"] if f.startswith("F.data ==")]
    expected = f'F.data == "{btn}"' if btn else None
    check("callback-фильтр отписки == callback_data кнопки рассылки",
          expected is not None and expected in got, f"кнопка={btn!r}, фильтры={got}")

    # A7. cb.answer() первым действием (иначе часы на кнопке и «query is too old»).
    unsub_cb = [r["handler"] for r in cbq if any("_is_unsub" in f or (btn and btn in f) for f in r["filters"])]
    for h in unsub_cb:
        fn = funcs.get(h)
        check(f"{h}: первый await — cb.answer()", fn is not None and _first_await_call(fn) == "answer",
              _first_await_call(fn) if fn else "нет функции")

    # A8. В отписке нет гейтов: аутбаунд-лид ОБЯЗАН иметь возможность отказаться (fail-closed-гейт
    # превратил бы отказ в ловушку). upsert_start не зовём — не заводим ПДн на того, кто просит
    # его не трогать.
    forbidden = {"is_bot_paused", "is_erase_requested", "lead_provenance", "upsert_start"}
    unsub_handlers = set(unsub_cb) | {r["handler"] for _, r in word_regs} | {
        r["handler"] for r in msg if any(f.startswith('Command("stop"') for f in r["filters"])}
    check("хендлеры отписки найдены", bool(unsub_handlers), repr(sorted(unsub_handlers)))
    for h in sorted(unsub_handlers):
        fn = funcs.get(h)
        bad = forbidden & _called_names(fn) if fn is not None else {"НЕТ ФУНКЦИИ"}
        check(f"{h}: без гейтов {sorted(forbidden)}", not bad, repr(sorted(bad)))
        check(f"{h}: зовёт set_unsubscribed", fn is not None and "set_unsubscribed" in _called_names(fn))

    # A9. Оживлённый /shop — продающая поверхность, а не волеизъявление субъекта: послабления
    # отписки (A8) на неё НЕ распространяются. Гейты обязаны стоять ДО upsert_start, иначе
    # витрина с платёжной ссылкой открывается на паузе оператора и отозвавшему согласие, а
    # запись ПДн обновляется раньше проверок. До фикса порядка команда была мёртвой — дыру
    # создало именно оживление, поэтому пин ставим здесь же.
    shop = funcs.get("t_shop")
    check("есть t_shop", shop is not None)
    if shop is not None:
        l_pause, l_erase = _call_line(shop, "is_bot_paused"), _call_line(shop, "is_erase_requested")
        l_upsert = _call_line(shop, "upsert_start")
        check("t_shop: гейт паузы оператора", l_pause is not None)
        check("t_shop: гейт отзыва согласия (152-ФЗ)", l_erase is not None)
        check("t_shop: оба гейта ВЫШЕ upsert_start",
              l_upsert is None or (l_pause is not None and l_erase is not None
                                   and l_pause < l_upsert and l_erase < l_upsert),
              f"paused={l_pause}, erase={l_erase}, upsert={l_upsert}")

    # A10. Паритет отписки VK/MAX с TG: ветка «СТОП» стоит ВЫШЕ upsert_start (в MAX — и выше
    # note_max_chat_id, он тоже пишет ПДн-адрес). Иначе на постороннего, написавшего «стоп» в
    # сообщество, заводится запись ПДн по факту отказа — ровно то, чего избегает t_stop.
    for fname, extra in (("_vk_respond", ()), ("_max_respond", ("note_max_chat_id",))):
        fn = funcs.get(fname)
        check(f"есть {fname}", fn is not None)
        if fn is None:
            continue
        unsub_ifs = [n.lineno for n in ast.walk(fn)
                     if isinstance(n, ast.If) and "_is_unsub" in _called_names(n.test)]
        l_unsub = min(unsub_ifs) if unsub_ifs else None
        check(f"{fname}: есть ветка _is_unsub", l_unsub is not None)
        for wname in ("upsert_start", *extra):
            l_w = _call_line(fn, wname)
            check(f"{fname}: {wname} НИЖЕ ветки отписки",
                  l_w is None or (l_unsub is not None and l_unsub < l_w),
                  f"unsub={l_unsub}, {wname}={l_w}")


# ── ЧАСТЬ C: тексты тенант-нейтральны ────────────────────────────────────────────
def part_c() -> None:
    print("C. Тексты подтверждения отписки:")
    fn = getattr(texts, "unsubscribed_ok", None)
    check("есть texts.unsubscribed_ok(operator_email)", callable(fn))
    if not callable(fn):
        return
    plain = fn(None)
    with_mail = fn("operator@example.org")
    for label, body in (("без почты", plain), ("с почтой", with_mail)):
        check(f"{label}: нет 'lesovschool'", "lesovschool" not in body)
        check(f"{label}: нет упоминания Школы", "Школ" not in body)
        check(f"{label}: /revoke упомянут БЕЗУСЛОВНО", "/revoke" in body)
    check("без почты: чужой почты не появилось", "@" not in plain, plain)
    check("с почтой: почта тенанта подставлена", "operator@example.org" in with_mail)
    # VK/MAX больше не отвечают школьным текстом (там была почта Школы у лида чужого тенанта).
    src = _src(MULTIPLEX)
    check("в multiplex.py нет texts.UNSUBSCRIBED_OK", "texts.UNSUBSCRIBED_OK" not in src)


# ── ЧАСТЬ R: фабрика роутера в рантайме (нужен aiogram) ──────────────────────────
async def part_r() -> None:
    print("R. Фабрика роутера (два Dispatcher'а в одном процессе):")
    try:
        from aiogram import Dispatcher
    except ImportError:
        print("  SKIP — в этом venv нет aiogram; прогнать в venv с aiogram 3.x")
        return
    import multiplex

    factory = getattr(multiplex, "build_tenant_router", None)
    check("multiplex.build_tenant_router импортируется", callable(factory))
    if not callable(factory):
        return
    r1, r2 = factory(), factory()
    check("фабрика отдаёт РАЗНЫЕ объекты Router", r1 is not r2)
    dp1, dp2 = Dispatcher(), Dispatcher()
    try:
        dp1.include_router(r1)
        dp2.include_router(r2)
        ok, err = True, ""
    except RuntimeError as e:  # «Router is already attached»
        ok, err = False, str(e)
    check("два тенант-бота поднимаются в одном процессе", ok, err)

    # Контроль осмысленности теста: ОДИН роутер в двух Dispatcher'ах обязан падать — иначе
    # проверка выше зелёная по любой причине.
    dp3 = Dispatcher()
    try:
        dp3.include_router(r1)
        control = False
    except RuntimeError:
        control = True
    check("контроль: повторный include ОДНОГО роутера всё ещё RuntimeError", control)

    order = [h.callback.__name__ for h in r1.message.handlers]
    print(f"     живой порядок message-хендлеров: {order}")
    check("t_text — последний в живом порядке", bool(order) and order[-1] == "t_text", repr(order))

    # Слово-отписка ловится ФИЛЬТРОМ: «стоп» → True, обычный текст → False (иначе Лия молчит).
    class _FakeMsg:
        def __init__(self, text):
            self.text = text

    # aiogram кладёт magic-фильтр в FilterObject.magic (в .callback остаётся его .resolve).
    magic = None
    for h in r1.message.handlers:
        for flt in h.filters or ():
            m = getattr(flt, "magic", None)
            if m is not None and m.resolve(_FakeMsg("СТОП")) is True:   # F.text отдал бы строку
                magic = m
                break
        if magic is not None:
            break
    check("есть фильтр, ловящий «СТОП»", magic is not None)
    if magic is not None:
        check("фильтр НЕ ловит обычный текст (Лия жива)", magic.resolve(_FakeMsg("привет")) is False)
        check("фильтр не падает на сообщении без текста", magic.resolve(_FakeMsg(None)) is False)

    # Кто РЕАЛЬНО поймает апдейт: прогоняем фильтры aiogram по порядку (сам хендлер не зовём —
    # в нём БД). Bot нужен фильтру Command и создаётся ОФФЛАЙН: конструктор в сеть не ходит,
    # запросов не делаем. Это прямой оракул «мёртвых» команд: до фикса /shop доставался t_text.
    import datetime

    from aiogram import Bot
    from aiogram.types import Chat, Message, User

    bot = Bot(token="123456:AAHfake_token_for_smoke_only_00000000")
    try:
        def _msg(text: str) -> Message:
            return Message(message_id=1, date=datetime.datetime.now(datetime.timezone.utc),
                           chat=Chat(id=42, type="private"),
                           from_user=User(id=42, is_bot=False, first_name="Т"), text=text)

        async def _winner(text: str) -> str | None:
            for h in r1.message.handlers:
                ok, _data = await h.check(_msg(text), bot=bot)
                if ok:
                    return h.callback.__name__
            return None

        for text, expected in (("/start", "t_start"), ("/revoke", "t_revoke"), ("/stop", "t_stop"),
                               ("/shop", "t_shop"), ("СТОП", "t_stop"), ("/стоп", "t_stop"),
                               ("привет", "t_text")):
            got = await _winner(text)
            check(f"«{text}» → {expected}", got == expected, str(got))
    finally:
        await bot.session.close()


# ── ЧАСТЬ B: данные (risuy_dev) ─────────────────────────────────────────────────
async def part_b() -> None:
    print("B. Данные (risuy_dev):")
    if not DSN:
        print("  SKIP — задайте TENANT_UNSUB_SMOKE_DSN на risuy_dev, чтобы прогнать часть B")
        return
    import asyncpg
    import db  # noqa: F401 (bot-telegram/db.py)

    db.pool = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    tid = None
    try:
        async with db.pool.acquire() as c:
            async def drop() -> None:
                await c.execute(
                    "delete from broadcast_recipients where broadcast_id in "
                    "(select id from broadcasts where tenant_id in (select id from tenants where slug=$1))", SLUG)
                await c.execute("delete from broadcasts where tenant_id in (select id from tenants where slug=$1)", SLUG)
                await c.execute("delete from consent_events where tenant_id in (select id from tenants where slug=$1)", SLUG)
                await c.execute("delete from leads where tenant_id in (select id from tenants where slug=$1)", SLUG)
                await c.execute("delete from tenants where slug=$1", SLUG)

            await drop()
            tid = await c.fetchval(
                "insert into tenants (slug,name,status) values ($1,'SMOKE unsub','active') returning id", SLUG)
            tok = db.current_tenant_id.set(tid)
            try:
                # Лиды-инбаунд с согласием: ровно те, кому МОЖНО слать (provenance='inbound_optin').
                await db.upsert_start(TG, "other")
                await db.upsert_start(TG_CTRL, "other")
                async with db.pool.acquire() as c2:
                    await c2.execute(
                        "update leads set consent = true where tenant_id = $1 and tg_user_id = any($2::bigint[])",
                        tid, [TG, TG_CTRL])
                lead_id = await db.get_lead_id(TG)
                ctrl_id = await db.get_lead_id(TG_CTRL)
                events_before = await c.fetchval(
                    "select count(*) from consent_events where tenant_id = $1", tid)

                # 1. set_unsubscribed возвращает lead_id (без него вызывающий не отличит отписку от промаха).
                got = await db.set_unsubscribed(TG)
                check("set_unsubscribed вернул lead_id", got is not None and str(got) == str(lead_id), str(got))
                first = await c.fetchval(
                    "select unsubscribed_at from leads where tenant_id=$1 and tg_user_id=$2", tid, TG)
                check("unsubscribed_at выставлен", first is not None)

                # 2. Идемпотентность (coalesce): повторная отписка не перетирает первый момент.
                again = await db.set_unsubscribed(TG)
                second = await c.fetchval(
                    "select unsubscribed_at from leads where tenant_id=$1 and tg_user_id=$2", tid, TG)
                check("повтор вернул тот же lead_id", again is not None and str(again) == str(lead_id))
                check("повтор НЕ сдвинул unsubscribed_at", first == second, f"{first} → {second}")

                # 3. Чужой/несуществующий id → None (нечего отписывать).
                check("чужой tg_user_id → None", await db.set_unsubscribed(TG_ALIEN) is None)

                # 4. Аудитория рассылки: отписанного нет, контрольный есть.
                check("recipient_recheck отписанного = False",
                      await db.recipient_recheck(lead_id, "tg") is False)
                check("recipient_recheck контрольного = True",
                      await db.recipient_recheck(ctrl_id, "tg") is True)
                bid = await c.fetchval(
                    "insert into broadcasts (title, messenger, kind, body_template, status, "
                    "created_by, tenant_id) values ('unsub','tg','text','тест','draft','smoke',$1) returning id", tid)
                n = await db.materialize_recipients(bid)
                rows = [str(r["lead_id"]) for r in await c.fetch(
                    "select lead_id from broadcast_recipients where broadcast_id = $1", bid)]
                check("отписанного нет в получателях", str(lead_id) not in rows, f"n={n}, {rows}")
                check("контрольный в получателях есть", str(ctrl_id) in rows, f"n={n}, {rows}")

                # 5. Отписка ≠ отзыв согласия: consent_events не растёт (иначе юрсмысл подменён).
                events_after = await c.fetchval(
                    "select count(*) from consent_events where tenant_id = $1", tid)
                check("consent_events не вырос от отписки", events_after == events_before,
                      f"{events_before} → {events_after}")

                # 6. Возврат: согласие снимает unsubscribed_at (лид снова в аудитории).
                await db.set_consent(TG, True, consent_text="ТЕСТ", channel="tg")
                back = await c.fetchval(
                    "select unsubscribed_at from leads where tenant_id=$1 and tg_user_id=$2", tid, TG)
                check("set_consent(True) снял unsubscribed_at", back is None, str(back))
            finally:
                db.current_tenant_id.reset(tok)
                await drop()
    finally:
        await db.pool.close()


async def main() -> None:
    part_a()
    part_c()
    await part_r()
    await part_b()
    print()
    if FAILS:
        print(f"❌ ПРОВАЛЫ ({len(FAILS)}): " + ", ".join(FAILS))
        sys.exit(1)
    print("🟢 tenant_unsub_smoke зелёный")


if __name__ == "__main__":
    asyncio.run(main())
