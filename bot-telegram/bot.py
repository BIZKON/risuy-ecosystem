"""Точка входа Telegram-бота.

Бот работает на long-polling. Рядом поднимаем крошечный HTTP-сервер на $PORT — его ждёт
Timeweb App Platform (проксирует 80/443 на порт контейнера). На том же сервере живёт
публичный трекинг-редирект /r/<token> (клик по ссылке рассылки → лог + 302 на target_url).

Фоновые таски рядом с polling (все по образцу nurture.run, прогресс в БД → переживают редеплой):
  • nurture.run    — прогрев (не трогаем; +2 фильтра в его SQL).
  • worker.run     — дренаж outbox (точечные ответы оператора) + исполнение рассылок.
  • retention.run  — обезличивание ПДн по отзыву согласия + TTL переписки (152-ФЗ).
"""
import asyncio
import html as _html
import json as _json
import logging
import os
import re
import time
from urllib.parse import urlparse

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.types import BotCommand
from aiohttp import web

import ai
import config
import db
import escalation
import metering_worker
import multiplex
import nurture
import retention
import richfmt
import triggers
import worker
from handlers import router
from messaging import LoggingMiddleware

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("bot")

_BOT_USERNAME = ""  # username бота (из get_me при старте) — для deep-link лендинга клуба

# ── Трекинг-редирект /r/<token> ──────────────────────────────────────────────
# Токен — secrets.token_urlsafe(16) → алфавит [A-Za-z0-9_-]. Валидируем ДО любого SELECT:
# мусор / %00 / гигантская строка → 404 без обращения к БД.
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
_ALLOWED_SCHEMES = {"http", "https"}

# Лёгкий in-memory per-IP rate-limit от флуда клик-логов превью-ботами/сканерами
# (single-instance — этого достаточно; редирект всё равно отдаём, режем только запись лога).
_RL_WINDOW = 10.0          # окно, сек
_RL_MAX = 30               # макс. кликов с одного IP за окно (для записи лога)
_rl_hits: dict[str, list[float]] = {}


def _safe_target(url: str) -> bool:
    """Defence-in-depth (§6.3): пускаем ТОЛЬКО http/https с непустым host и без protocol-relative.

    Дублирует allow-list, который панель применяет на записи. target_url берётся ИЗ БД,
    query клиента игнорируется. javascript:/data:/file: и '//host' отвергаются.
    """
    if not url or url.startswith("//"):
        return False
    try:
        p = urlparse(url)
    except Exception:  # noqa: BLE001
        return False
    return p.scheme in _ALLOWED_SCHEMES and bool(p.netloc)


def _rl_allow_log(ip: str | None) -> bool:
    """True, если клик с этого IP можно залогировать (не превышен лимит окна)."""
    if not ip:
        return True
    now = time.monotonic()
    hits = [t for t in _rl_hits.get(ip, ()) if now - t < _RL_WINDOW]
    if len(hits) >= _RL_MAX:
        _rl_hits[ip] = hits
        return False
    hits.append(now)
    _rl_hits[ip] = hits
    # Гигиена памяти: периодически чистим протухшие ключи (дёшево, без отдельной таски).
    if len(_rl_hits) > 10000:
        for k in list(_rl_hits.keys()):
            if all(now - t >= _RL_WINDOW for t in _rl_hits[k]):
                _rl_hits.pop(k, None)
    return True


def _sec_headers(resp: web.StreamResponse) -> web.StreamResponse:
    """Ручные заголовки на 302/404 (голый aiohttp-сервер их сам не ставит)."""
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


async def _redirect(request: web.Request) -> web.StreamResponse:
    """Публичный GET /r/{token}: лог клика fire-and-forget → 302 на target_url из БД.

    Всё в try/except: при любой ошибке отдаём 404 (или редирект, если уже знаем target).
    В лог пишем только префикс токена, БЕЗ сырого UA/IP в текст. Редирект важнее лога:
    при недоступности пула всё равно отдаём 302.
    """
    token = request.match_info.get("token", "")
    if not _TOKEN_RE.match(token):
        return _sec_headers(web.Response(status=404, text="not found"))

    try:
        row = await db.get_link_token(token)
    except Exception:  # noqa: BLE001 — БД недоступна
        logger.warning("/r: ошибка чтения токена %s…", token[:6], exc_info=True)
        return _sec_headers(web.Response(status=404, text="not found"))

    if row is None:
        return _sec_headers(web.Response(status=404, text="not found"))

    target = row["target_url"]
    # Повторная проверка target ПЕРЕД редиректом (не доверяем «панель проверила», §6.3).
    if not _safe_target(target):
        logger.error("/r: небезопасный target у токена %s… — инцидент", token[:6])
        return _sec_headers(web.Response(status=404, text="not found"))

    # Лог клика — fire-and-forget с коротким таймаутом; per-IP rate-limit от флуда.
    ip = _client_ip(request)
    if _rl_allow_log(ip):
        ua = request.headers.get("User-Agent")
        asyncio.create_task(_log_click_safe(token, row["broadcast_id"], row["lead_id"], ua, ip))

    return _sec_headers(web.HTTPFound(target))


def _client_ip(request: web.Request) -> str | None:
    """Best-effort IP за прокси Timeweb (X-Forwarded-For), advisory — не security-контроль.

    Берём ПОСЛЕДНИЙ (правый) токен XFF: его дописывает НАШ LB и подделать его клиент не
    может; левый токен — клиентский заголовок, тривиально спуфится (случайный «IP» на
    каждый запрос обнулял бы per-IP rate-limit). За одним доверенным LB правый токен =
    реальный peer клиента."""
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        return xff.rsplit(",", 1)[-1].strip()[:64]
    peer = request.remote
    return peer[:64] if peer else None


async def _log_click_safe(token, broadcast_id, lead_id, ua, ip) -> None:
    """Обёртка лога клика: короткий таймаут, никогда не бросает (редирект уже отдан)."""
    try:
        await asyncio.wait_for(
            db.log_link_click(token, broadcast_id, lead_id, ua, ip), timeout=3.0
        )
    except Exception:  # noqa: BLE001
        logger.warning("/r: клик не залогирован (token=%s…)", str(token)[:6])


async def _health(_request: web.Request) -> web.Response:
    return web.Response(text="ok")


# ── Веб-чат демо «Лии» на сайте: POST /api/demo-chat ──────────────────────────
# Зеркало Telegram-демо на info.pro-agent-ai.ru: тот же промпт/модель демо-тенанта через
# ai.ask_gateway (stateless, контекст в messages[]). Публичный, без сессии. Защита от слива
# баланса: CORS только для сайта, per-IP rate-limit, кап длины истории/сообщения. Метеринг НЕ
# списываем (демо, цены модели нет — иначе спам ERROR; ответ важнее учёта).
_DEMO_CHAT_ORIGIN = "https://info.pro-agent-ai.ru"
_CHAT_RL_WINDOW = 60.0       # окно rate-limit, сек
_CHAT_RL_MAX = 20            # макс. сообщений с одного IP за окно
_CHAT_RL_GLOBAL_MAX = 120    # общий потолок эндпоинта за окно: анти-спуф XFF — распределённый
                             # перебор «IP» упирается в общий бюджет (паттерн _GLOBAL_KEY auth панели)
_CHAT_RL_GLOBAL_KEY = "\x00global"  # непечатаемый префикс — не пересечётся с реальным IP
_CHAT_MAX_MESSAGES = 24      # кап длины присланной истории
_CHAT_MAX_LEN = 2000         # кап длины одного сообщения
_chat_rl_hits: dict[str, list[float]] = {}


def _rl_allow_chat(ip: str | None) -> bool:
    """True, если запрос чата в пределах лимитов окна (single-instance, in-memory):
    сначала ОБЩИЙ бюджет эндпоинта (защита платного LLM от распределённого спуфа XFF),
    затем per-IP."""
    now = time.monotonic()
    g = [t for t in _chat_rl_hits.get(_CHAT_RL_GLOBAL_KEY, ()) if now - t < _CHAT_RL_WINDOW]
    if len(g) >= _CHAT_RL_GLOBAL_MAX:
        _chat_rl_hits[_CHAT_RL_GLOBAL_KEY] = g
        return False
    g.append(now)
    _chat_rl_hits[_CHAT_RL_GLOBAL_KEY] = g
    if not ip:
        return True
    hits = [t for t in _chat_rl_hits.get(ip, ()) if now - t < _CHAT_RL_WINDOW]
    if len(hits) >= _CHAT_RL_MAX:
        _chat_rl_hits[ip] = hits
        return False
    hits.append(now)
    _chat_rl_hits[ip] = hits
    if len(_chat_rl_hits) > 10000:
        for k in list(_chat_rl_hits.keys()):
            if k != _CHAT_RL_GLOBAL_KEY and all(now - t >= _CHAT_RL_WINDOW for t in _chat_rl_hits[k]):
                _chat_rl_hits.pop(k, None)
    return True


# Дедуп веб-эскалации: одна карточка горячего лида с IP за окно. Веб-чат stateless (нет лид-записи
# для claim-дедупа, как в TG), а Лия может переэмитить [[ESCALATE]] на следующих ходах того же
# диалога → без этого менеджер получил бы дубли. Окно щедрое: один посетитель = один сигнал.
_ESC_WEB_WINDOW = 1800.0     # 30 мин
_esc_web_sent: dict[str, float] = {}


def _esc_allow_web(ip: str | None) -> bool:
    """True (и фиксирует отметку), если веб-эскалацию с этого IP можно отправить сейчас."""
    if not ip:
        return True  # IP не определён (редко за прокси) — не дедупим, но и не блокируем сигнал
    now = time.monotonic()
    if now - _esc_web_sent.get(ip, 0.0) < _ESC_WEB_WINDOW:
        return False
    _esc_web_sent[ip] = now
    if len(_esc_web_sent) > 10000:  # гигиена памяти (как у rate-limit'ов выше)
        for k in list(_esc_web_sent.keys()):
            if now - _esc_web_sent[k] >= _ESC_WEB_WINDOW:
                _esc_web_sent.pop(k, None)
    return True


_esc_web_tasks: set = set()  # ссылки на фоновые таски веб-эскалации (иначе GC съест до отправки)


async def _escalate_web_bg(tid, esc: dict, ip: str | None) -> None:
    """Фоновая доставка карточки горячего веб-лида — НЕ на пути ответа посетителю (карточка идёт
    в TG через РФ-прокси с безлимитным 429-ретраем; ответ сайту её ждать не должен). Таймаут
    бортирует залипший ретрай. Неуспех доставки → ОТКАТ дедуп-окна: у веб-лида нет claim/release,
    единственный ретрай — следующая эмиссия [[ESCALATE]] того же диалога, и её нельзя глушить."""
    ok = False
    try:
        ok = await asyncio.wait_for(escalation.escalate_web(tid, esc), timeout=12.0)
    except Exception:  # noqa: BLE001 — таймаут/отмена/сеть: фон не валим
        logger.warning("веб-эскалация: фоновая доставка не удалась/таймаут", exc_info=True)
    if not ok and ip:
        _esc_web_sent.pop(ip, None)  # доставки не было → освобождаем окно под ретрай


def _cors(resp: web.StreamResponse) -> web.StreamResponse:
    """CORS только для сайта (другие origin'ы в браузере не пустит) + no-store."""
    resp.headers["Access-Control-Allow-Origin"] = _DEMO_CHAT_ORIGIN
    resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resp.headers["Access-Control-Max-Age"] = "86400"
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _consent_required(body: dict) -> bool:
    """Чистый гард согласия 152-ФЗ для веб-чата. True → нужно согласие (запрос блокируем).
    Тестируется напрямую в web_consent_smoke.py без сети/БД."""
    return not bool(isinstance(body, dict) and body.get("consent"))


async def _demo_chat(request: web.Request) -> web.StreamResponse:
    """POST /api/demo-chat: тело {messages:[{role,content}...]} (последнее — текущий вопрос user).
    Возвращает {reply}. Любая ошибка → мягкий JSON (не 5xx-утечка). OPTIONS → preflight."""
    if request.method == "OPTIONS":
        return _cors(web.Response(status=204))
    if not _rl_allow_chat(_client_ip(request)):
        return _cors(web.json_response({"error": "rate_limited"}, status=429))
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _cors(web.json_response({"error": "bad_json"}, status=400))
    # 152-ФЗ: требуем явное согласие до любого обращения к Лие
    if _consent_required(body):
        return _cors(web.json_response({
            "error": "consent_required",
            "reply": "Чтобы продолжить, отметьте согласие на обработку персональных данных 🙏",
        }, status=403))
    msgs = body.get("messages") if isinstance(body, dict) else None
    if not isinstance(msgs, list) or not msgs:
        return _cors(web.json_response({"error": "no_messages"}, status=400))
    norm: list[dict] = []
    for m in msgs[-_CHAT_MAX_MESSAGES:]:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        content = m.get("content")
        if role in ("user", "assistant") and isinstance(content, str) and content.strip():
            norm.append({"role": role, "content": content.strip()[:_CHAT_MAX_LEN]})
    if not norm or norm[-1]["role"] != "user":
        return _cors(web.json_response({"error": "last_not_user"}, status=400))
    text, history = norm[-1]["content"], norm[:-1]
    try:
        cfg = await db.get_demo_chat_cfg()
    except Exception:  # noqa: BLE001
        cfg = None
    if cfg is None:
        return _cors(web.json_response({"error": "demo_off"}, status=503))
    try:
        answer, _meta = await ai.ask_gateway(
            text, model=cfg["model"], system_prompt=ai._with_immunity(cfg["system_prompt"]),
            fallback=cfg.get("fallback"), history=history,
        )
    except Exception:  # noqa: BLE001 — сеть/шлюз: не роняем, мягкий ответ
        logger.warning("demo-chat: ask_gateway упал", exc_info=True)
        answer = "Извините, не получилось ответить. Попробуйте ещё раз или напишите нам в Telegram."
    # Веб-чат идёт МИМО ai.ask_ai (он зовёт ask_gateway напрямую), а служебные маркеры вырезает
    # именно ask_ai → делаем это ЗДЕСЬ: посетитель сайта НИКОГДА не должен увидеть сырой
    # [[ESCALATE]]/[[TRIGGER:N]]. При маркере эскалации — карточка горячего лида в TG-группу
    # тенанта (per-IP дедуп от переэмита). На фолбэк-тексте маркеров нет → no-op.
    answer, esc = escalation.parse_escalation(answer)
    answer, _trig = triggers.parse_trigger_markers(answer)
    # Веб-виджет показывает {reply} как plain (textContent) и markdown НЕ рендерит → сырые **/`/#
    # смотрятся «сломанно». Срезаем разметку в чистый текст (TG-путь не трогаем — там rich-рендер).
    answer = richfmt.to_plain(answer)
    # Персистенция веб-чата как лида demo-sandbox (раздел «Демо-монитор» в панели): upsert лида по
    # session_id виджета + лог входящего и ответа Лии под тенантом демо. Best-effort — ответ
    # посетителю не должен падать из-за записи; messenger='web' (tg_user_id в messages = NULL).
    sid = body.get("session_id") if isinstance(body, dict) else None
    if isinstance(sid, str) and 8 <= len(sid) <= 80 and cfg.get("tid"):
        _tok = db.current_tenant_id.set(cfg["tid"])
        try:
            await db.upsert_start(sid, "web", messenger="web")
            # Дедуп согласия: пишем granted-событие ОДИН РАЗ на сессию (не на каждое сообщение)
            _snap = await db.get_lead_snapshot(sid, messenger="web")
            if not (_snap and _snap.get("consent")):
                await db.set_consent(sid, True, consent_text=None, channel="web", messenger="web")
            _lid = await db.get_lead_id(sid, messenger="web")
            await db.log_message(lead_id=_lid, tg_user_id=0, messenger="web", direction="in", text=text)
            await db.log_message(lead_id=_lid, tg_user_id=0, messenger="web", direction="out",
                                 text=answer, source="liya")
        except Exception:  # noqa: BLE001
            logger.warning("demo-chat: персистенция веб-чата не удалась", exc_info=True)
        finally:
            db.current_tenant_id.reset(_tok)
    if esc is not None and cfg.get("tid"):
        ip = _client_ip(request)
        # _esc_allow_web помечает окно ОПТИМИСТИЧНО (атомарно → анти-гонка двойной карточки при
        # параллельных запросах одного IP). Доставку шлём ФОНОМ (ответ посетителю не ждёт TG);
        # если доставка не удалась — _escalate_web_bg откатит окно, чтобы лид не потерялся.
        if _esc_allow_web(ip):
            t = asyncio.create_task(_escalate_web_bg(cfg["tid"], esc, ip))
            _esc_web_tasks.add(t)
            t.add_done_callback(_esc_web_tasks.discard)
    return _cors(web.json_response({"reply": answer}))


def _esc_html(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _legal_html(title: str, body: str) -> str:
    """Минимальная HTML-обёртка юр-документа: экранируем, plain-текст с \\n → абзацы."""
    paras = "".join(
        f"<p>{_esc_html(line)}</p>" if line.strip() else "<div style='height:8px'></div>"
        for line in body.split("\n"))
    return (
        "<!doctype html><html lang=\"ru\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{_esc_html(title)}</title>"
        "<style>body{font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;max-width:760px;"
        "margin:24px auto;padding:0 16px;line-height:1.5;color:#1F2937}h1{font-size:20px;margin:0 0 12px}"
        "p{margin:5px 0}</style></head><body>"
        f"<h1>{_esc_html(title)}</h1>{paras}</body></html>"
    )


def _club_landing_html(operator_name: str, deeplink: str, policy_url: str) -> str:
    """Минимальный самодостаточный HTML-лендинг клуба (без внешних ресурсов). Публичный,
    inbound: посетитель приходит сам и жмёт «Вступить» → бот-воронка клуба (согласие 152-ФЗ)."""
    import html as _html
    name = _html.escape(operator_name or "")
    if policy_url:
        policy = (f'<p class="muted">Вступая, вы даёте согласие на обработку данных вашего '
                  f'бизнеса. <a href="{_html.escape(policy_url)}">Политика конфиденциальности</a>.</p>')
    else:
        policy = '<p class="muted">Вступая, вы даёте согласие на обработку данных вашего бизнеса.</p>'
    return (
        '<!doctype html><html lang="ru"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>Клуб предпринимателей — {name}</title>'
        '<style>body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:640px;'
        'margin:0 auto;padding:32px 20px;color:#1F2937;line-height:1.55}'
        '.btn{display:inline-block;background:#E63946;color:#fff;padding:14px 28px;'
        'border-radius:12px;text-decoration:none;font-weight:600;margin:20px 0}'
        '.muted{color:#6b7280;font-size:14px}h1{font-size:24px}</style></head><body>'
        f'<h1>Клуб предпринимателей — {name}</h1>'
        '<p>Сообщество предпринимателей для поиска комплементарных партнёров. Система сама '
        'подбирает, кто может быть вам полезен, а знакомство происходит только по взаимному '
        'согласию обеих сторон.</p>'
        '<p>Вступление бесплатное. Ваши контакты не раскрываются, пока вы сами не согласитесь '
        'на знакомство.</p>'
        f'<a class="btn" href="{_html.escape(deeplink)}">Вступить в клуб</a>'
        f'{policy}</body></html>'
    )


# ── Бриф-лендинг тенанта: GET/POST /brief/{token} ────────────────────────────
# Публичный, без авторизации: ссылку тенант получает лично (в ЛС от менеджера/бота).
# Схема вопросов — shared/brief_schema.py (ЕДИНЫЙ источник истины, читает и панель).
_BRIEF_TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")

_BRIEF_NOTICE = (
    "Вы передаёте бизнес-данные оператору сервиса «ИИ-Агент Про». "
    "Пожалуйста, НЕ вставляйте персональные данные ваших клиентов (ФИО, телефоны, "
    "списки контактов) — опишите портрет аудитории, а не конкретных людей."
)


def _brief_schema_payload() -> dict:
    """Схема в форме, удобной для JS фронта: секции как есть + плоский список вопросов
    (для ветвления show_if, которое ходит по вопросам вне секций)."""
    from shared import brief_schema
    questions = []
    for sec in brief_schema.SECTIONS:
        for q in sec["questions"]:
            questions.append(q)
    return {"sections": brief_schema.SECTIONS, "questions": questions}


def _brief_html(*, title: str, company: str, action: str) -> str:
    """Рендер самодостаточной страницы брифа из шаблона + инъекция схемы."""
    tmpl_path = os.path.join(os.path.dirname(__file__), "templates", "brief.html")
    with open(tmpl_path, encoding="utf-8") as f:
        tmpl = f.read()
    schema_json = _json.dumps(_brief_schema_payload(), ensure_ascii=False)
    return (tmpl
            .replace("{{TITLE}}", _html.escape(title))
            .replace("{{COMPANY}}", _html.escape(company))
            .replace("{{ACTION}}", _html.escape(action))
            .replace("{{NOTICE}}", _html.escape(_BRIEF_NOTICE))
            .replace("{{SCHEMA_JSON}}", schema_json)
            .replace("{{ANSWERS_JSON}}", "{}"))


def _brief_parse(pairs) -> dict:
    """Из пар (name, value) формы → {question_key: value|list}. name = q_<key>.
    multichoice копится в список (несколько чекбоксов с одинаковым name)."""
    from shared import brief_schema
    idx = brief_schema.question_index()
    out: dict = {}
    for name, value in pairs:
        if not name.startswith("q_"):
            continue
        key = name[2:]
        q = idx.get(key)
        if not q:
            continue
        if q["type"] == "multichoice":
            out.setdefault(key, []).append(value)
        else:
            out[key] = value
    return out


async def _brief_landing(request: web.Request) -> web.StreamResponse:
    """Публичная страница брифа: GET /brief/{token}, без авторизации.
    404 — токен неизвестен/истёк; «уже отправлен» — бриф уже в статусе submitted."""
    token = request.match_info.get("token", "")
    if not _BRIEF_TOKEN_RE.match(token):
        return web.Response(status=404, text="Ссылка недействительна")
    try:
        data = await db.get_brief_by_token(token)
    except Exception:  # noqa: BLE001
        logger.warning("brief landing: ошибка чтения токена %s…", token[:6], exc_info=True)
        data = None
    if data is None or data.get("expired"):
        return web.Response(status=404, text="Ссылка недействительна или истекла")
    if data["status"] != "pending":
        return web.Response(text="Бриф уже получен. Спасибо!", content_type="text/html", charset="utf-8")
    html = _brief_html(title="Бриф — ИИ-Агент Про",
                        company=data.get("tenant_name") or "вашей компании",
                        action=f"/brief/{token}")
    resp = web.Response(text=html, content_type="text/html", charset="utf-8")
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


async def _brief_submit(request: web.Request) -> web.StreamResponse:
    """Приём ответов: POST /brief/{token}. Валидация схемой + per-IP rate-limit (флуд/боты)."""
    from shared import brief_schema
    token = request.match_info.get("token", "")
    if not _BRIEF_TOKEN_RE.match(token):
        return web.Response(status=404, text="Ссылка недействительна")
    ip = _client_ip(request)
    if not _rl_allow_chat(ip):
        return web.Response(status=429, text="Слишком много попыток, попробуйте позже")
    post = await request.post()
    answers = _brief_parse(list(post.items()))
    errs = brief_schema.validate_answers(answers)
    if errs:
        # Простая страница с ошибками + возврат назад (правки — заново открыть ссылку).
        return web.Response(
            text="<p>Проверьте обязательные поля:</p><ul>"
                 + "".join(f"<li>{_html.escape(e)}</li>" for e in errs)
                 + '</ul><a href="javascript:history.back()">Назад</a>',
            content_type="text/html", charset="utf-8", status=400)
    answers["version"] = brief_schema.BRIEF_VERSION
    try:
        res = await db.submit_brief(token, answers)
    except Exception:  # noqa: BLE001
        logger.warning("brief submit: ошибка записи токена %s…", token[:6], exc_info=True)
        return web.Response(status=500, text="Ошибка сохранения, попробуйте ещё раз")

    if res == "ok":
        return web.Response(text="<h1>Спасибо!</h1><p>Бриф получен. Мы настроим вашего "
                                  "ИИ-сотрудника и свяжемся с вами.</p>",
                             content_type="text/html", charset="utf-8")
    elif res == "already":
        return web.Response(text="Бриф уже был отправлен ранее.",
                             content_type="text/html", charset="utf-8")
    elif res == "expired":
        return web.Response(status=410,
                             text="Ссылка истекла. Запросите новую у команды.",
                             content_type="text/html", charset="utf-8")
    elif res == "unknown":
        return web.Response(status=404,
                             text="Ссылка недействительна.",
                             content_type="text/html", charset="utf-8")
    return web.Response(status=400,
                         text="Ошибка обработки брифа, попробуйте ещё раз.",
                         content_type="text/html", charset="utf-8")


async def _legal_page(request: web.Request) -> web.StreamResponse:
    """Публичная юр-страница тенанта: GET /legal/{slug}/{doc_type} (privacy|consent), без авторизации.
    Генерит документ из реквизитов оператора (tenant_settings по слагу) — единый источник
    shared/leadmagnet. 404, если тенанта нет или обязательные реквизиты не заполнены."""
    slug = request.match_info.get("slug", "")
    doc_type = request.match_info.get("doc_type", "")
    if doc_type not in ("privacy", "consent"):
        return web.Response(status=404, text="Документ не найден")
    try:
        kv = await db.get_legal_doc_data(slug)
    except Exception:  # noqa: BLE001
        logger.warning("legal-page: чтение реквизитов упало slug=%s", slug, exc_info=True)
        kv = None
    if kv is None:
        return web.Response(status=404, text="Документ не найден или реквизиты оператора не заполнены")
    from shared.leadmagnet import build_consent_text, build_privacy_policy
    phone = bool((kv.get("phone_step_enabled") or "").strip())
    if doc_type == "privacy":
        rf = await db.get_ai_inference_rf()
        title = "Политика обработки персональных данных"
        body = build_privacy_policy(
            kv["operator_name"], kv["operator_inn"], kv["operator_email"],
            operator_ogrn=kv.get("operator_ogrn") or None,
            operator_address=kv.get("operator_address") or None,
            data_purpose=kv.get("data_purpose") or None, phone_step=phone,
            transborder=not rf, club=bool(kv.get("_club")))
    else:
        title = "Согласие на обработку персональных данных"
        body = build_consent_text(
            kv["operator_name"], kv["operator_inn"], kv["operator_email"],
            data_purpose=kv.get("data_purpose") or None, privacy_url=None, phone_step=phone)
    resp = web.Response(text=_legal_html(title, body), content_type="text/html", charset="utf-8")
    resp.headers["Cache-Control"] = "public, max-age=600"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


async def _club_landing(request: web.Request) -> web.StreamResponse:
    """Публичная страница-приглашение в клуб тенанта: GET /club/{slug}, без авторизации.
    404, если тенанта нет, реквизиты оператора не заполнены (клуб не может принять вступление),
    или бот ещё не знает свой username (deep-link не построить)."""
    slug = request.match_info.get("slug", "")
    try:
        kv = await db.get_legal_doc_data(slug)
    except Exception:  # noqa: BLE001
        logger.warning("club-landing: чтение реквизитов упало slug=%s", slug, exc_info=True)
        kv = None
    if kv is None or not _BOT_USERNAME:
        return web.Response(status=404, text="Клуб не найден или не настроен")
    deeplink = f"https://t.me/{_BOT_USERNAME}?start=club"
    base = config.BOT_PUBLIC_BASE_URL
    policy_url = f"{base}/legal/{slug}/privacy" if base else ""
    resp = web.Response(
        text=_club_landing_html(kv["operator_name"], deeplink, policy_url),
        content_type="text/html", charset="utf-8",
    )
    resp.headers["Cache-Control"] = "public, max-age=600"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


def _partner_landing_html(partner_name: str, deeplink: str) -> str:
    """Самодостаточный HTML-лендинг реф-партнёра (без внешних ресурсов). Публичный."""
    import html as _html
    name = _html.escape(partner_name or "")
    return (
        '<!doctype html><html lang="ru"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>Персональный бриф — {name}</title>'
        '<style>body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:640px;'
        'margin:0 auto;padding:32px 20px;color:#1F2937;line-height:1.55}'
        '.btn{display:inline-block;background:#E63946;color:#fff;padding:14px 28px;'
        'border-radius:12px;text-decoration:none;font-weight:600;margin:20px 0}'
        '.muted{color:#6b7280;font-size:14px}h1{font-size:24px}</style></head><body>'
        f'<h1>Персональный бриф от партнёра {name}</h1>'
        '<p>Нажмите кнопку — бот задаст пару вопросов о вашей компании и подготовит '
        'персональный бриф для настройки ИИ-сотрудника.</p>'
        f'<a class="btn" href="{_html.escape(deeplink)}">Получить бриф</a>'
        '<p class="muted">Продолжая, вы соглашаетесь на обработку данных вашего бизнеса.</p>'
        '</body></html>'
    )


async def _partner_landing(request: web.Request) -> web.StreamResponse:
    """Публичная страница реф-партнёра: GET /p/{code}. Неизвестный/disabled/нет username → 404."""
    code = request.match_info.get("code", "")
    try:
        partner = await db.get_partner_by_ref_code(code)
    except Exception:  # noqa: BLE001
        logger.warning("partner-landing: резолв упал code=%s", code, exc_info=True)
        partner = None
    if partner is None or not _BOT_USERNAME:
        return web.Response(status=404, text="Ссылка недействительна")
    deeplink = f"https://t.me/{_BOT_USERNAME}?start=ref_{code}"
    resp = web.Response(text=_partner_landing_html(partner["name"], deeplink),
                        content_type="text/html", charset="utf-8")
    resp.headers["Cache-Control"] = "public, max-age=600"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


async def _start_health() -> web.AppRunner:
    app = web.Application()
    app.router.add_get("/", _health)
    app.router.add_get("/health", _health)
    app.router.add_get("/r/{token}", _redirect)  # публичный трекинг-редирект (§6.2)
    app.router.add_get("/legal/{slug}/{doc_type}", _legal_page)  # публичные юр-страницы тенанта (152-ФЗ)
    app.router.add_get("/club/{slug}", _club_landing)  # публичный лендинг-приглашение в клуб тенанта
    app.router.add_get("/p/{code}", _partner_landing)  # публичный лендинг реф-партнёра
    app.router.add_get("/brief/{token}", _brief_landing)   # публичный бриф-лендинг тенанта
    app.router.add_post("/brief/{token}", _brief_submit)   # приём ответов брифа
    app.router.add_post("/api/demo-chat", _demo_chat)     # веб-чат демо-Лии для сайта
    app.router.add_options("/api/demo-chat", _demo_chat)  # CORS preflight
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", config.PORT)
    await site.start()
    logger.info("HTTP-сервер (health + /r) на порту %s", config.PORT)
    return runner


async def main() -> None:
    await db.init()
    health = await _start_health()

    if config.TELEGRAM_PROXY:
        # Прячем креды прокси в логе — печатаем только host:port.
        logger.info("Telegram через прокси: %s", config.TELEGRAM_PROXY.rsplit("@", 1)[-1])
        bot = Bot(token=config.BOT_TOKEN, session=AiohttpSession(proxy=config.TELEGRAM_PROXY))
    else:
        bot = Bot(token=config.BOT_TOKEN)
    dp = Dispatcher()
    # Лог входящих ДО роутинга — ловит всё вне фильтров состояния. ТОЛЬКО на message
    # (не callback_query — нажатия кнопок не переписка). Ошибки лога изолированы внутри.
    dp.message.outer_middleware(LoggingMiddleware())
    dp.include_router(router)

    nurture_task = asyncio.create_task(nurture.run(bot))
    worker_task = asyncio.create_task(worker.run(bot))
    retention_task = asyncio.create_task(retention.run())
    metering_task = asyncio.create_task(metering_worker.run(bot))  # Wave 3: снапшоты+списания
    # Wave 3: мультиплекс тенант-ботов. Школа НЕ здесь (она = эта главная таска из
    # env); при пустом реестре non-default active-тенантов — строго no-op (§8.7).
    multiplex_task = asyncio.create_task(multiplex.run())
    try:
        # ЕДИНСТВЕННЫЙ set_my_commands со ВСЕМИ командами меню (§5.8): второй вызов сотрёт
        # /start из меню (механизм запуска воронки). /start — запуск; /stop — отписка.
        await bot.set_my_commands([
            BotCommand(command="start", description="Начать заново 🌷"),
            BotCommand(command="stop", description="Отписаться от рассылок"),
        ])
        await bot.delete_webhook(drop_pending_updates=True)
        # Публикуем НЕ-секретный снимок конфигурации в app_settings (bot_username для
        # deep-link'ов панели + статус интеграций). Сбой изолируем — статус-борд не
        # критичен, бот должен подняться в любом случае.
        try:
            me = await bot.get_me()
            global _BOT_USERNAME
            _BOT_USERNAME = (me.username or "").strip()
            await db.publish_runtime_status(
                bot_username=me.username or "",
                gate_channel_url=config.CHANNEL_URL,
                guide_url_env=config.GUIDE_URL,
                proxy_set=bool(config.TELEGRAM_PROXY),
                agent_token_set=bool(config.TIMEWEB_AI_TOKEN),
                gateway_token_set=bool(config.AI_GATEWAY_TOKEN),
                public_base_url=config.BOT_PUBLIC_BASE_URL,
                shop_yookassa_set=config.SHOP_PAYMENTS_CONFIGURED,
            )
            logger.info("Статус рантайма опубликован в app_settings (bot @%s)", me.username)
        except Exception as e:  # noqa: BLE001 — публикация статуса не должна валить старт
            logger.warning("Не удалось опубликовать статус рантайма: %s", e)
        logger.info("Бот запущен на long-polling")
        await dp.start_polling(bot)
    finally:
        nurture_task.cancel()
        worker_task.cancel()
        retention_task.cancel()
        metering_task.cancel()
        multiplex_task.cancel()
        await health.cleanup()
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
