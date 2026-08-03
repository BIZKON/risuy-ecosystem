#!/usr/bin/env python3
"""Этап 0, дыра B: бэкфилл реестра `tenant_agents` по уже созданным агентам персон.

ЗАЧЕМ. Снапшот-воркер метрирует только агентов из реестра (bot-telegram/metering_worker.py:94).
Агенты персон создавались панелью и в реестр не попадали — их расход не списывался никому.
Числовой id уже лежит в базе (`app_settings['ai_persona_agent_nid__<slug>']`, кладёт
db.save_persona_agent), поэтому обращение к Timeweb API и токен ИИ здесь НЕ нужны.

Владелец агентов персон — ДЕФОЛТНЫЙ тенант (Школа): ключи `ai_persona_agent__*` глобальные и
читает их только School-путь бота. Тот же выбор зашит в admin-panel/app.py::_persona_agent_tenant.

🔴 ПОРЯДОК ВАЖЕН: сперва цена модели, потом бэкфилл. Пока в `model_prices` нет строки
provider='timeweb-cloud-ai' для слага модели агентов, воркер на каждом тике будет писать
ошибку и слать ops-алерт, а дельта копиться (metering_worker.py:194-208). Когда цена
появится — накопленный объём спишется одним ударом. Скрипт проверяет это сам и без
--i-know-no-price отказывается применять.

🟥 По умолчанию ТОЛЬКО risuy_dev и ТОЛЬКО dry-run.
ЗАПУСК:
  BACKFILL_DSN="postgresql://gen_user:<pw>@81.31.246.136:5432/risuy_dev?sslmode=require" \
      ./.venv/bin/python scripts/backfill_tenant_agents.py            # показать план
  … scripts/backfill_tenant_agents.py --apply                          # записать
Прод: дополнительно BACKFILL_ALLOW_PROD=yes и отдельное «да» владельца.
"""
import asyncio
import json
import os
import sys

import asyncpg

DSN = os.environ.get("BACKFILL_DSN")
if not DSN:
    raise SystemExit("Задайте BACKFILL_DSN.")
DBNAME = DSN.split("?")[0].rstrip("/").split("/")[-1]
if DBNAME == "risuy" and os.environ.get("BACKFILL_ALLOW_PROD") != "yes":
    raise SystemExit("ОТКАЗ: боевой risuy. Для прода явно: BACKFILL_ALLOW_PROD=yes.")

APPLY = "--apply" in sys.argv
IGNORE_PRICE = "--i-know-no-price" in sys.argv
TENANT_SLUG = os.environ.get("DEFAULT_TENANT_SLUG", "lesov-school")
ACTOR = "backfill_tenant_agents.py"


async def main() -> None:
    print(f"backfill_tenant_agents · база={DBNAME} · режим={'ЗАПИСЬ' if APPLY else 'dry-run'} "
          f"· тенант-владелец={TENANT_SLUG}")
    c = await asyncpg.connect(DSN)
    try:
        tid = await c.fetchval("select id from tenants where slug = $1", TENANT_SLUG)
        if not tid:
            raise SystemExit(f"ОТКАЗ: тенант «{TENANT_SLUG}» не найден — некому приписать агентов.")

        rows = await c.fetch(
            "select key, value from app_settings "
            "where key like 'ai_persona_agent_nid__%' and coalesce(value,'') <> '' order by key")
        access = {r["key"].split("ai_persona_agent__", 1)[1]: (r["value"] or "").strip()
                  for r in await c.fetch(
                      "select key, value from app_settings where key like 'ai_persona_agent__%'")}

        plan: list[tuple[str, int, str]] = []   # (слаг персоны, числовой id, access_id)
        skipped: list[str] = []
        for r in rows:
            slug = r["key"].split("ai_persona_agent_nid__", 1)[1]
            try:
                nid = int((r["value"] or "").strip())
            except ValueError:
                skipped.append(f"{slug}: нечисловой id {r['value']!r}")
                continue
            owner = await c.fetchval("select tenant_id from tenant_agents where agent_id = $1", nid)
            if owner == tid:
                skipped.append(f"{slug}: агент {nid} уже в реестре за этим тенантом")
                continue
            if owner is not None:
                skipped.append(f"{slug}: агент {nid} ЗАКРЕПЛЁН ЗА ДРУГИМ тенантом — пропуск")
                continue
            plan.append((slug, nid, access.get(slug, "")))

        print(f"\nК регистрации: {len(plan)}")
        for slug, nid, acc in plan:
            print(f"  + персона {slug}: агент {nid} (access_id {acc or '—'})")
        if skipped:
            print(f"\nПропущено: {len(skipped)}")
            for s in skipped:
                print(f"  · {s}")

        if not plan:
            print("\nНечего делать.")
            return

        # Гейт цены: без строки model_prices регистрация включит поток ops-алертов
        # и накопление дельты вместо учёта.
        prices = await c.fetchval(
            "select count(*) from model_prices where provider = 'timeweb-cloud-ai'")
        if not prices and not IGNORE_PRICE:
            raise SystemExit(
                "\n🔴 ОТКАЗ: в model_prices нет ни одной строки provider='timeweb-cloud-ai'. "
                "Регистрация без цены = ежечасный ops-алерт и копящаяся дельта. Впишите цену "
                "модели агентов (данные из ЛК Timeweb) либо осознанно повторите "
                "с --i-know-no-price.")
        if prices:
            models = await c.fetch(
                "select distinct model from model_prices where provider = 'timeweb-cloud-ai'")
            print("\nЦены есть для моделей: " + ", ".join(m["model"] for m in models))
            print("⚠️  Сверьте, что модель агентов персон (PERSONA_AGENT_MODEL_ID) — в этом списке: "
                  "воркер ищет цену по слагу public_name модели.")

        if not APPLY:
            print("\ndry-run: ничего не записано. Повторите с --apply.")
            return

        written = 0
        async with c.transaction():
            for slug, nid, acc in plan:
                res = await c.execute(
                    "insert into tenant_agents (agent_id, tenant_id, access_id, note) "
                    "values ($1,$2,$3,$4) on conflict (agent_id) do nothing",
                    nid, tid, acc or None, f"персона {slug} (бэкфилл)")
                if res.endswith(" 0"):
                    print(f"  · {slug}: агент {nid} уже появился в реестре — пропуск")
                    continue
                await c.execute(
                    "insert into admin_audit (actor, action, ip, user_agent, detail) "
                    "values ($1,$2,null,null,$3::jsonb)",
                    ACTOR, "tenant_agent_register",
                    json.dumps({"tenant_id": str(tid), "agent_id": nid, "persona": slug,
                                "source": "backfill"}, ensure_ascii=False))
                written += 1
        print(f"\nЗаписано: {written}")
    finally:
        await c.close()


if __name__ == "__main__":
    asyncio.run(main())
