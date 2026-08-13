#!/usr/bin/env python3
"""Доктор тенанта (Э2 программы доводки): read-only снимок цепочки «агент → БЗ → LLM → расход →
память» для одного тенанта или для всех сразу.

ЧТО ЭТО. Один прогон превращает «у клиента что-то не отвечает» в список точных отказов по каждому
кабинету. Скрипт объединяет «доктора» и readiness-проверку — двух скриптов не будет.

🟩 НИЧЕГО НЕ ПИШЕТ. Первой командой соединения выставляется `default_transaction_read_only = on`,
факт read-only перепроверяется запросом до первой проверки. Ни одной мутации, ни строки аудита, ни
записи в usage_ledger: живой вызов эмбеддера (проверка 5) идёт HTTP-запросом мимо метеринга, то есть
расход тенанту не начисляется.

🟩 БЕЗ ПДн В ВЫВОДЕ. Не печатаются имена, телефоны и тексты сообщений лидов, а также содержимое
чанков базы знаний. Где нужен субъект — только `shared.anon.subject_code`. Обоснование: вывод
скрипта читает ассистент, а перечень получателей ПДн закрыт (service-site/privacy.html:91,94).

ВСЕ SQL-проверки выполняются СВОИМ соединением доктора. Умышленно не зовём `bot-telegram/db.kb_search`
и прочие функции бота: они ходят своим пулом мимо read-only пояса (bot-telegram/db.py:2194). Чистый
`_pick_team_agent` импортируется — он без БД, и импорт гарантирует, что доктор выбирает агента ровно
теми же слоями, что и бот.

ГАРД БАЗЫ (канон scripts/reconcile_yookassa.py): имя БД должно быть известным; боевой `risuy` —
только при DOCTOR_ALLOW_PROD=1. Гард оставлен несмотря на read-only: он же ловит опечатку в DSN.

ЗАПУСК:
  DOCTOR_ALLOW_PROD=1 DOCTOR_DSN="postgresql://gen_user@host:5432/risuy?sslmode=require" \\
    PGPASSWORD=… EMBEDDER_URL="http://…" PYTHONPATH=. python3 scripts/tenant_doctor.py --all
  (или --tenant-slug demo-sandbox для одного кабинета)

Код возврата: 1, если хотя бы одна проверка FAIL; 0 — иначе. WARN на код возврата не влияет.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)                              # shared.*
sys.path.insert(0, os.path.join(ROOT, "bot-telegram"))  # чистый резолвер команды
# Стабы окружения: bot-telegram/config.py валидирует env на импорте, а доктору нужен из него
# только чистый _pick_team_agent. Никакие из этих значений в работе не участвуют.
os.environ.setdefault("BOT_TOKEN", "x")
os.environ.setdefault("DATABASE_URL", "postgresql://x/y")
os.environ.setdefault("CHANNEL_ID", "-1001234567890")
os.environ.setdefault("CHANNEL_URL", "https://t.me/x")
os.environ.setdefault("GUIDE_URL", "https://example.com/guide")

import asyncpg  # noqa: E402

import db as botdb  # noqa: E402  (bot-telegram/db.py — берём только _pick_team_agent и _AI_BACKENDS)
from shared.ai_defaults import AI_DEFAULT_FALLBACK  # noqa: E402

KNOWN_DBS = ("risuy", "risuy_dev")
PROD_DB = "risuy"
EMBED_DIMS = 768
PROBE_TEXT = "query: проверка живости эмбеддера"  # нейтральная строка, не ПДн

# Дефолт-тенант (Школа) живёт ИНАЧЕ и проверяется отдельной веткой: его бот поднимается из env
# главной таской (bot.py), мультиплекс его пропускает (bot-telegram/multiplex.py:558), а конфиг ИИ
# читается из ГЛОБАЛЬНЫХ app_settings, не из tenant_settings (bot-telegram/db.py:1464). Проверять
# его правилами тенант-бота — значит получить ложный FAIL на самом живом кабинете.
DEFAULT_SLUG = os.environ.get("DEFAULT_TENANT_SLUG", "lesov-school")

# Ключи легаси-конфига ИИ в tenant_settings — зеркало bot-telegram/db.py::get_tenant_ai_overrides.
LEGACY_KEYS = ["ai_enabled", "ai_backend", "ai_agent_id", "ai_model",
               "ai_gateway_base_url", "ai_system_prompt", "ai_fallback_text"]

TEAM_COLS = ("id, slug, name, role_preset, system_prompt, backend, agent_id, fallback_text, "
             "escalation_chat_id, escalation_topic_id, is_default, is_orchestrator, "
             "memory_enabled, kb_enabled, enabled")

OK, WARN, FAIL = "OK", "WARN", "FAIL"


class Report:
    """Накопитель результатов одного тенанта. Хранит (статус, шаг, деталь, что чинить)."""

    def __init__(self, slug: str):
        self.slug = slug
        self.rows: list[tuple[str, str, str, str]] = []

    def add(self, status: str, step: str, detail: str = "", fix: str = "") -> None:
        self.rows.append((status, step, detail, fix))

    @property
    def failed(self) -> bool:
        return any(s == FAIL for s, *_ in self.rows)

    def print(self) -> None:
        print(f"\n── тенант {self.slug} " + "─" * max(0, 60 - len(self.slug)))
        for status, step, detail, _fix in self.rows:
            mark = {OK: "OK  ", WARN: "WARN", FAIL: "FAIL"}[status]
            print(f"  {mark} {step}" + (f" — {detail}" if detail else ""))

    def fixes(self) -> list[str]:
        return [f"[{self.slug}] {step}: {fix}"
                for status, step, _d, fix in self.rows if status != OK and fix]


def dbname_of(dsn: str) -> str:
    return dsn.split("?")[0].rstrip("/").split("/")[-1]


def embed_probe(base: str, token: str) -> tuple[bool, str, int]:
    """Живой POST {EMBEDDER_URL}/embed. Возвращает (успех, деталь, длина вектора).
    Синхронный urllib намеренно: доктору не нужен aiohttp, а лишняя зависимость — лишний риск."""
    url = base.rstrip("/") + "/embed"
    payload = json.dumps({"inputs": [PROBE_TEXT], "normalize": True}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST")
    req.add_header("content-type", "application/json")
    if token:
        req.add_header("authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status != 200:
                return False, f"HTTP {resp.status}", 0
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}", 0
    except Exception as e:  # noqa: BLE001 — сеть/таймаут/не-JSON: для доктора это один исход
        return False, f"{type(e).__name__}: {e}", 0
    if isinstance(data, list) and data and isinstance(data[0], list):
        return True, "", len(data[0])
    return False, "неожиданный формат ответа", 0


async def check_school(c: asyncpg.Connection, r: Report, tid, embed_state) -> Report:
    """Ветка дефолт-тенанта (Школа). Отличия от тенант-бота, из-за которых нужна своя ветка:
      • конфиг ИИ — в ГЛОБАЛЬНЫХ app_settings, а не в tenant_settings (bot-telegram/db.py:1464);
      • поверх глобального слоя работают канал и персона (ai_persona → ai_persona_agent__*);
      • при пустом ai_agent_id эффективный агент берётся из env бота (config.AGENT_ID) — из базы
        он не виден в принципе, поэтому доктор честно пишет WARN, а не выдумывает FAIL;
      • команда team_agents на этом пути не участвует.
    """
    g = {x["key"]: (x["value"] or "") for x in await c.fetch(
        "select key, value from app_settings where key like 'ai_%' or key = 'kb_enabled'")}
    backend = (g.get("ai_backend") or "").strip() or "cloud_ai"
    persona = (g.get("ai_persona") or "").strip()
    global_agent = (g.get("ai_agent_id") or "").strip()
    persona_nid = (g.get(f"ai_persona_agent_nid__{persona}") or "").strip() if persona else ""
    persona_uuid = (g.get(f"ai_persona_agent__{persona}") or "").strip() if persona else ""

    r.add(OK, "2. схема кабинета", "School-путь: глобальные app_settings, команда не участвует")

    if persona_uuid or persona_nid:
        eff_src = f"персона {persona}"
        eff_id = persona_nid or persona_uuid
    elif global_agent:
        eff_src, eff_id = "глобальный ai_agent_id", global_agent
    else:
        eff_src, eff_id = "env бота (AGENT_ID)", ""

    if eff_id:
        r.add(OK, "3. агент LLM привязан", f"backend={backend}, источник — {eff_src}")
    else:
        r.add(WARN, "3. агент LLM привязан",
              f"backend={backend}, агент из env бота — из базы не виден",
              "проверить AGENT_ID в env приложения 201859, если бот молчит")

    # 4. расход: смотрим реестр tenant_agents этого тенанта. Агент из env сверить с базой нельзя.
    reg = [str(x["agent_id"]) for x in await c.fetch(
        "select agent_id from tenant_agents where tenant_id = $1 order by agent_id", tid)]
    if not reg:
        r.add(FAIL, "4. расход тарифицируется", "в tenant_agents нет ни одного агента",
              "расход копится и спишется одним ударом")
    elif eff_id and eff_id in reg:
        r.add(OK, "4. расход тарифицируется", f"агент {eff_id} в реестре")
    elif eff_id:
        r.add(FAIL, "4. расход тарифицируется", f"агента {eff_id} нет в реестре (есть: {', '.join(reg)})",
              "зарегистрировать агента, иначе метеринг его не видит")
    else:
        r.add(WARN, "4. расход тарифицируется", f"в реестре {', '.join(reg)}; агент из env не сверяем",
              "убедиться, что AGENT_ID из env есть в этом списке")

    e_status, e_detail, e_dims = embed_state
    r.add(OK if e_status == OK else e_status, "5. эмбеддер жив",
          f"вектор {e_dims}" if e_status == OK else e_detail,
          "" if e_status == OK else "без него база знаний не работает")

    kb = await c.fetchrow(
        "select count(*) total, count(*) filter (where embedding is null) no_vec "
        "from kb_chunks where tenant_id = $1", tid)
    if kb["total"] == 0:
        r.add(WARN, "6. база знаний", "чанков нет", "загрузить документы в /knowledge")
    elif kb["no_vec"]:
        r.add(FAIL, "6. база знаний", f"{kb['no_vec']} из {kb['total']} чанков без вектора",
              "переиндексировать")
    else:
        r.add(OK, "6. база знаний", f"{kb['total']} чанков, все с векторами")

    kb_on = bool((g.get("kb_enabled") or "").strip())
    if kb_on and kb["total"] == 0:
        r.add(WARN, "7. тумблер базы знаний", "включён глобально, но чанков нет")
    elif not kb_on and kb["total"] > 0:
        r.add(FAIL, "7. тумблер базы знаний", f"выключен при {kb['total']} чанках",
              "включить kb_enabled в /agents — загруженная база молчит")
    else:
        r.add(OK, "7. тумблер базы знаний", "включён" if kb_on else "выключен")

    fb = (g.get("ai_fallback_text") or "").strip()
    if fb == AI_DEFAULT_FALLBACK.strip():
        r.add(WARN, "9. текст при сбое LLM", "совпадает с дефолтом",
              "для Школы это её собственный текст — не ошибка, но лучше задать явно")
    elif not fb:
        r.add(WARN, "9. текст при сбое LLM", "пуст — уйдёт хардкод ai.py",
              "записать текст в /agents (нужно для Э4 программы доводки)")
    else:
        r.add(OK, "9. текст при сбое LLM", "свой")

    await _check_wallet(c, r, tid)
    return r


async def _check_wallet(c: asyncpg.Connection, r: Report, tid) -> None:
    """Проверка 10 — общая для обеих веток: пул жив, средства есть, паузы ИИ нет."""
    w = await c.fetchrow(
        "select included_microrub, included_period_end, topup_microrub, "
        "       included_period_end > now() as pool_alive from credit_wallets where tenant_id = $1", tid)
    blocked = await c.fetchval(
        "select 1 from tenant_settings where tenant_id = $1 and key = 'ai_wallet_blocked'", tid)
    if w is None:
        r.add(WARN, "10. кошелёк", "кошелька нет — тариф не оплачивался",
              "списания пойдут в минус без остановки")
    else:
        pool = int(w["included_microrub"] or 0) if w["pool_alive"] else 0
        avail = pool + int(w["topup_microrub"] or 0)
        if not w["pool_alive"] and int(w["included_microrub"] or 0):
            r.add(WARN, "10. кошелёк", "пул периода сгорел", "продлить подписку")
        if avail > 0:
            r.add(OK, "10. средства", f"{avail / 1_000_000:.2f} ₽ доступно")
        else:
            r.add(FAIL, "10. средства", f"{avail / 1_000_000:.2f} ₽",
                  "пополнить: расход уходит в минус (allow_negative)")
    if blocked:
        r.add(FAIL, "10a. пауза ИИ", "стоит ai_wallet_blocked",
              "снимается успешным платежом — клиент сейчас без ответов")


async def check_tenant(c: asyncpg.Connection, slug: str, embed_state: tuple[str, str, int]) -> Report:
    """Все проверки одного тенанта. embed_state — общий на прогон результат проверки 5."""
    r = Report(slug)
    t = await c.fetchrow("select id, status, plan_id from tenants where slug = $1", slug)
    if t is None:
        r.add(FAIL, "1. тенант существует", "нет такого slug", "проверить имя кабинета")
        return r
    tid = t["id"]

    # 1. статус тенанта: мультиплекс поднимает бота только для active (bot-telegram/multiplex.py:557)
    if t["status"] == "active":
        r.add(OK, "1. статус тенанта", "active")
    else:
        r.add(FAIL, "1. статус тенанта", t["status"],
              "бот не поднимется; active даёт только оплата подписки")

    if slug == DEFAULT_SLUG:
        return await check_school(c, r, tid, embed_state)

    # ── конфиг: команда агентов + легаси tenant_settings ──
    team = [dict(x) for x in await c.fetch(
        f"select {TEAM_COLS} from team_agents where tenant_id = $1 and enabled", tid)]
    rows = await c.fetch(
        "select key, value from tenant_settings where tenant_id = $1 and key = any($2::text[])",
        tid, LEGACY_KEYS)
    kv = {x["key"]: (x["value"] or "") for x in rows}
    legacy_backend = (kv.get("ai_backend") or "").strip()
    if legacy_backend not in botdb._AI_BACKENDS:
        legacy_backend = "cloud_ai"
    legacy = {
        "backend": legacy_backend,
        "agent_id": (kv.get("ai_agent_id") or "").strip(),
        "model": (kv.get("ai_model") or "").strip(),
        "fallback": kv.get("ai_fallback_text") or "",
        "system_prompt": kv.get("ai_system_prompt") or "",
    }

    # 2. выбор агента теми же слоями, что у бота (без диалога и канала → остаётся is_default)
    picked = botdb._pick_team_agent(team, lead_agent_slug=None, channel_slug=None) if team else None
    if picked is not None:
        r.add(OK, "2. агент выбран", f"team_agents/{picked['slug']}")
    elif team:
        r.add(FAIL, "2. агент выбран", f"строк {len(team)}, но ни одна не is_default",
              "в /my-team отметить агента дефолтным")
    else:
        r.add(WARN, "2. агент выбран", "команды нет — работает легаси-конфиг",
              "не ошибка, но кабинет живёт на старой схеме")

    # 3. эффективный конфиг — зеркало resolve_team_agent_cfg (bot-telegram/db.py:1780-1797)
    if picked is not None:
        eff_backend = (picked["backend"] or "").strip()
        if eff_backend not in botdb._AI_BACKENDS:
            eff_backend = "cloud_ai"
        eff = {
            "backend": eff_backend,
            "agent_id": (picked["agent_id"] or "").strip() or legacy["agent_id"],
            "model": legacy["model"],
            "fallback": picked["fallback_text"] or legacy["fallback"],
            # Э3: промпт коалесцируется с легаси — зеркало bot-telegram/db.py после правки.
            "system_prompt": picked["system_prompt"] or legacy["system_prompt"],
            "kb_enabled": bool(picked["kb_enabled"]),
            "memory_enabled": bool(picked["memory_enabled"]),
            "agent_slug": picked["slug"],
        }
    else:
        eff = {**legacy, "kb_enabled": False, "memory_enabled": False, "agent_slug": ""}

    if eff["backend"] == "cloud_ai" and not eff["agent_id"]:
        r.add(FAIL, "3. агент LLM привязан", "backend=cloud_ai, agent_id пуст",
              "бот примет сообщение и промолчит; нужен провижининг агента")
    elif eff["backend"] == "gateway" and not eff["model"]:
        r.add(FAIL, "3. агент LLM привязан", "backend=gateway, ai_model пуст",
              "задать tenant_settings.ai_model")
    else:
        r.add(OK, "3. агент LLM привязан",
              f"backend={eff['backend']}" + (f", model={eff['model']}" if eff["model"] else ""))

    if not eff["system_prompt"]:
        r.add(WARN, "3a. инструкции агента", "промпт пуст",
              "агент ответит без роли; /my-agent или /my-team")

    # 4. расход тарифицируется: cloud_ai → агент в реестре; gateway → цена модели в прайсе
    if eff["backend"] == "cloud_ai":
        if not eff["agent_id"]:
            r.add(FAIL, "4. расход тарифицируется", "agent_id пуст — метеринг слеп",
                  "см. шаг 3")
        else:
            owner = await c.fetchval(
                "select tenant_id from tenant_agents where agent_id = $1", int(eff["agent_id"])
                if eff["agent_id"].isdigit() else -1)
            if owner is None:
                r.add(FAIL, "4. расход тарифицируется", f"агента {eff['agent_id']} нет в tenant_agents",
                      "расход копится и спишется одним ударом; зарегистрировать агента")
            elif str(owner) != str(tid):
                r.add(FAIL, "4. расход тарифицируется", "агент числится за ДРУГИМ тенантом",
                      "расход уедет чужому кабинету — разобрать вручную")
            else:
                r.add(OK, "4. расход тарифицируется", f"агент {eff['agent_id']} в реестре")
    else:
        price = await c.fetchrow(
            "select price_in_microrub_per_1k pin, price_out_microrub_per_1k pout "
            "from model_prices where provider = 'timeweb-ai-gateway' and model = $1 "
            "order by effective_from desc limit 1", eff["model"])
        if price is None:
            r.add(FAIL, "4. расход тарифицируется", f"нет цены модели {eff['model'] or '—'}",
                  "вписать строку model_prices (provider=timeweb-ai-gateway)")
        else:
            r.add(OK, "4. расход тарифицируется",
                  f"{price['pin'] / 1000:.1f}/{price['pout'] / 1000:.1f} ₽ за млн")

    # 5. эмбеддер — общий на прогон (проверка сделана один раз до цикла)
    e_status, e_detail, e_dims = embed_state
    if e_status == OK:
        r.add(OK, "5. эмбеддер жив", f"вектор {e_dims}")
    else:
        r.add(e_status, "5. эмбеддер жив", e_detail,
              "без него база знаний и долгая память не работают")

    # 6. база знаний тенанта. Содержимое чанков НЕ печатаем — только счётчики.
    kb = await c.fetchrow(
        "select count(*) total, count(*) filter (where embedding is null) no_vec, "
        "       count(distinct coalesce(metadata->>'role_tag','')) tags "
        "from kb_chunks where tenant_id = $1", tid)
    tags = [x["tag"] for x in await c.fetch(
        "select distinct coalesce(metadata->>'role_tag','') tag from kb_chunks where tenant_id = $1",
        tid)]
    if kb["total"] == 0:
        r.add(WARN, "6. база знаний", "чанков нет", "загрузить документы в /knowledge")
    elif kb["no_vec"]:
        r.add(FAIL, "6. база знаний", f"{kb['no_vec']} из {kb['total']} чанков без вектора",
              "переиндексировать: без вектора чанк невидим для поиска")
    else:
        r.add(OK, "6. база знаний", f"{kb['total']} чанков, все с векторами")

    # Отделы: чанк с role_tag, которого нет среди slug'ов команды, не увидит НИКТО.
    known = {""} | {x["slug"] for x in team}
    orphan = sorted(set(tags) - known)
    if orphan:
        r.add(FAIL, "6a. отделы чанков", f"role_tag без агента: {', '.join(orphan)}",
              "эти чанки не увидит ни один агент — переназначить отдел или завести агента")

    # 7. тумблер базы знаний против фактического наличия чанков
    if picked is not None:
        if eff["kb_enabled"] and kb["total"] == 0:
            r.add(WARN, "7. тумблер базы знаний", "включён, но чанков нет",
                  "загрузить документы либо выключить, чтобы не ждать справку")
        elif not eff["kb_enabled"] and kb["total"] > 0:
            r.add(FAIL, "7. тумблер базы знаний", f"выключен при {kb['total']} чанках",
                  "включить kb_enabled в /my-team — иначе загруженная база молчит")
        else:
            r.add(OK, "7. тумблер базы знаний", "включён" if eff["kb_enabled"] else "выключен")

    # 8. долгая память при мёртвом эмбеддере — денежный цикл впустую
    if eff["memory_enabled"] and e_status != OK:
        r.add(FAIL, "8. долгая память", "включена при недоступном эмбеддере",
              "выключить память или поднять эмбеддер")
    elif eff["memory_enabled"]:
        r.add(OK, "8. долгая память", "включена, эмбеддер жив")

    # 9. фолбэк не должен быть чужим брендом из дефолта
    if (eff["fallback"] or "").strip() == AI_DEFAULT_FALLBACK.strip():
        r.add(FAIL, "9. текст при сбое LLM", "дефолт с чужим брендом и почтой",
              "задать свой текст в /my-agent — клиент увидит почту Школы Лесова")
    elif not (eff["fallback"] or "").strip():
        r.add(WARN, "9. текст при сбое LLM", "пуст — уйдёт общий дефолт",
              "задать свой текст в /my-agent")
    else:
        r.add(OK, "9. текст при сбое LLM", "свой")

    # 10. кошелёк: пул жив, средства есть, паузы нет
    await _check_wallet(c, r, tid)
    return r


async def main() -> int:
    ap = argparse.ArgumentParser(description="Read-only доктор тенанта (Э2)")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--tenant-slug", help="slug одного кабинета")
    g.add_argument("--all", action="store_true", help="все живые тенанты")
    args = ap.parse_args()

    dsn = os.environ.get("DOCTOR_DSN") or os.environ.get("DATABASE_URL") or ""
    if not dsn or dsn == "postgresql://x/y":
        return _die("Нужен DOCTOR_DSN (или DATABASE_URL) на боевую/dev базу.")
    dbname = dbname_of(dsn)
    if dbname not in KNOWN_DBS:
        return _die(f"ОТКАЗ: незнакомая база {dbname!r}. Ожидались: {', '.join(KNOWN_DBS)}.")
    if dbname == PROD_DB and os.environ.get("DOCTOR_ALLOW_PROD") != "1":
        return _die("ОТКАЗ: боевая база risuy. Для прода явно: DOCTOR_ALLOW_PROD=1.")

    embedder = (os.environ.get("EMBEDDER_URL") or "").strip()
    c = await asyncpg.connect(dsn)
    try:
        await c.execute("set default_transaction_read_only = on")
        ro = await c.fetchval("show transaction_read_only")
        if ro != "on":
            return _die("ОТКАЗ: не удалось включить read-only на соединении.")
        role = await c.fetchval("select current_user")

        print("=" * 72)
        print(f"Доктор тенанта · база={dbname} · роль={role} · read-only={ro}")
        print("Проверки 1–4, 6–10 — SQL своим соединением доктора (read-only).")
        print("Проверка 5 — живой HTTP POST к эмбеддеру, мимо БД и мимо метеринга.")
        print("Записей не делается ни одной. ПДн в вывод не попадают.")
        print("=" * 72)

        if embedder:
            ok, detail, dims = await asyncio.to_thread(
                embed_probe, embedder, os.environ.get("EMBEDDER_TOKEN", ""))
            if ok and dims == EMBED_DIMS:
                embed_state = (OK, "", dims)
            elif ok:
                embed_state = (FAIL, f"вектор {dims}, ожидалось {EMBED_DIMS}", dims)
            else:
                embed_state = (FAIL, detail, 0)
        else:
            embed_state = (WARN, "EMBEDDER_URL не задан — проверка пропущена", 0)

        if args.all:
            slugs = [x["slug"] for x in await c.fetch(
                "select slug from tenants order by created_at")]
        else:
            slugs = [args.tenant_slug]

        reports = [await check_tenant(c, s, embed_state) for s in slugs]
    finally:
        await c.close()

    for rep in reports:
        rep.print()

    fixes = [f for rep in reports for f in rep.fixes()]
    bad = [rep.slug for rep in reports if rep.failed]
    print("\n" + "=" * 72)
    if fixes:
        print("ЧТО ЧИНИТЬ:")
        for f in fixes:
            print("  • " + f)
        print("\nРазделы кабинета: /my-team (агенты и отделы) · /my-agent (инструкции, фолбэк,"
              " эскалация) · /knowledge (база знаний)")
    else:
        print("Замечаний нет.")
    print("=" * 72)
    if bad:
        print(f"❌ FAIL у тенантов: {', '.join(bad)}")
        return 1
    print("✅ Блокирующих отказов нет")
    return 0


def _die(msg: str) -> int:
    print(msg, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
