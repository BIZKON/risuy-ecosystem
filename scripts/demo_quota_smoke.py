#!/usr/bin/env python3
"""Смоук суточных счётчиков демо-чата (дыра C) на risuy_dev: db.demo_chat_quota_take,
db.demo_chat_tokens_add/_used и поле daily_limit в db.get_demo_chat_cfg.

Счётчики глобальные (не тенант-скоуплены), лежат в app_settings под суточными ключами
UTC — как dadata_quota__<дата> в панели. Проверяем:
  1. имя ключа ровно 'demo_chat_quota__<YYYY-MM-DD>' (UTC) — по нему владелец смотрит расход;
  2. при limit=3 четыре вызова дают (True,1) (True,2) (True,3) (False,4);
  3. ГОНКА: 20 параллельных take(5) на чистом ключе → ровно 5 True, 15 False, номера {1..20},
     значение ключа в БД 20. Read-modify-write этот тест проваливает — потому счётчик берётся
     ОДНИМ атомарным insert … on conflict … returning;
  4. ровно ОДИН вызов вернул cur == limit+1 — на нём висит разовый суточный алерт владельцу;
  5. limit=0 (kill-switch) → первый же вызов False;
  6. счётчик растёт и ПОСЛЕ исчерпания — семантика «попытки» (как у dadata), не баг;
  7. токен-бюджет: tokens_add(1234) на чистом ключе даёт 1234, а НЕ 1 (механическая копия
     dadata записала бы литерал '1' и первый запрос суток занизил бы расход); отрицательная
     дельта (корректировка брони фактом) уменьшает счётчик, но не загоняет его в минус;
  8. get_demo_chat_cfg().daily_limit: app_settings['demo_chat_daily_limit'] ПОВЕРХ env,
     мусор/отсутствие ключа → дефолт из config, значение ВСЕГДА int (None дал бы
     TypeError в гейте → мёртвая витрина). Проверяется на СВОЁМ throwaway-тенанте
     (канон scripts/consent_revoke_smoke.py) — раньше здесь стоял SKIP, засчитанный как OK,
     и на dev без demo-sandbox единственная проверка override'а превращалась в зелёную
     галочку при заведомо сломанном коде.

⚠️ Смоук ПИШЕТ и УДАЛЯЕТ суточные ключи app_settings и заводит throwaway-тенанта — на проде
не запускать никогда (съел бы дневной потолок живой витрины). Гард: DSN обязан указывать на
risuy_dev. Значения ключей сохраняются до теста и восстанавливаются в finally, тенант
удаляется там же.

Запуск:
  DEMO_QUOTA_SMOKE_DSN="postgresql://<user>:<pw>@<host>:5432/risuy_dev?sslmode=require" \
  PYTHONPATH=bot-telegram BOT_TOKEN=x DATABASE_URL="$DEMO_QUOTA_SMOKE_DSN" CHANNEL_ID=-100 \
  CHANNEL_URL=https://t.me/x GUIDE_URL=https://x \
  ./.venv-smoke/bin/python scripts/demo_quota_smoke.py
"""
import asyncio
import os
import sys
from datetime import datetime, timezone

DSN = os.environ.get("DEMO_QUOTA_SMOKE_DSN") or os.environ.get("DATABASE_URL")
if not DSN or "/risuy_dev" not in DSN.split("?")[0]:
    raise SystemExit("Задайте DEMO_QUOTA_SMOKE_DSN на risuy_dev (защита от прода: "
                     "смоук перезаписывает суточные счётчики демо).")
os.environ["DATABASE_URL"] = DSN

import config  # noqa: E402  (bot-telegram/config.py)
import db      # noqa: E402  (bot-telegram/db.py)

QUOTA_KEY = "demo_chat_quota__" + datetime.now(timezone.utc).date().isoformat()
TOKENS_KEY = "demo_chat_tokens__" + datetime.now(timezone.utc).date().isoformat()
LIMIT_KEY = "demo_chat_daily_limit"
# Свой тенант вместо dev-фикстуры: блок 8 обязан проверять механизм, а не наличие
# demo-sandbox на конкретной базе. get_demo_chat_cfg принимает слаг параметром.
SMOKE_SLUG = "smoke-demo-cap"

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


async def _get(c, key):
    return await c.fetchval("select value from app_settings where key = $1", key)


async def _drop_tenant(c) -> None:
    """Снос throwaway-тенанта. tenant_settings уходит каскадом (on delete cascade)."""
    await c.execute("delete from tenants where slug = $1", SMOKE_SLUG)


async def _restore(c, key, value) -> None:
    """Возврат ключа в состояние ДО смоука (иначе смоук сам съест дневной потолок)."""
    if value is None:
        await c.execute("delete from app_settings where key = $1", key)
    else:
        await c.execute(
            "insert into app_settings (key, value) values ($1, $2) "
            "on conflict (key) do update set value = excluded.value", key, value)


async def main() -> None:
    await db.init()
    saved: dict[str, str | None] = {}
    try:
        async with db.pool.acquire() as c:
            for k in (QUOTA_KEY, TOKENS_KEY, LIMIT_KEY):
                saved[k] = await _get(c, k)

            # 1+2. имя ключа и последовательность при limit=3
            print("1-2. ключ суток и последовательность take(3)")
            await c.execute("delete from app_settings where key = $1", QUOTA_KEY)
            seq = [await db.demo_chat_quota_take(3) for _ in range(4)]
            check("ключ суток создан под ожидаемым именем",
                  await _get(c, QUOTA_KEY) is not None, QUOTA_KEY)
            check("последовательность (True,1) (True,2) (True,3) (False,4)",
                  seq == [(True, 1), (True, 2), (True, 3), (False, 4)], str(seq))

            # 6. счётчик растёт и после исчерпания («попытки», как у dadata)
            print("6. счётчик растёт после исчерпания")
            after = await db.demo_chat_quota_take(3)
            check("пятый вызов → (False, 5)", after == (False, 5), str(after))

            # 3+4. гонка на чистом ключе
            print("3-4. гонка: 20 параллельных take(5)")
            await c.execute("delete from app_settings where key = $1", QUOTA_KEY)
            res = await asyncio.gather(*[db.demo_chat_quota_take(5) for _ in range(20)])
            trues = [r for r in res if r[0]]
            nums = {r[1] for r in res}
            check("ровно 5 True и 15 False", len(trues) == 5 and len(res) - len(trues) == 15,
                  f"True={len(trues)}")
            check("номера уникальны и покрывают {1..20} (нет read-modify-write)",
                  nums == set(range(1, 21)), str(sorted(nums)))
            check("значение ключа в БД == 20", (await _get(c, QUOTA_KEY)) == "20",
                  str(await _get(c, QUOTA_KEY)))
            edge = [r for r in res if r[1] == 6]  # limit+1 → на нём висит разовый алерт
            check("ровно ОДИН вызов вернул cur == limit+1 (один алерт в сутки)",
                  len(edge) == 1 and edge[0][0] is False, str(edge))

            # 5. kill-switch
            print("5. limit=0 — kill-switch")
            await c.execute("delete from app_settings where key = $1", QUOTA_KEY)
            zero = await db.demo_chat_quota_take(0)
            check("первый же вызов False", zero == (False, 1), str(zero))

            # 7. токен-бюджет
            print("7. токен-бюджет")
            await c.execute("delete from app_settings where key = $1", TOKENS_KEY)
            first = await db.demo_chat_tokens_add(1234)
            second = await db.demo_chat_tokens_add(1234)
            check("первый add(1234) → 1234, а не 1 (INSERT-ветка пишет $2, не литерал)",
                  first == 1234, str(first))
            check("повтор → 2468", second == 2468, str(second))
            check("tokens_used читает то же значение",
                  (await db.demo_chat_tokens_used()) == 2468,
                  str(await db.demo_chat_tokens_used()))
            # Корректировка брони фактом: дельта отрицательная, счётчик не уходит в минус.
            neg = await db.demo_chat_tokens_add(30 - 2468)
            check("отрицательная дельта уменьшает счётчик до факта", neg == 30, str(neg))
            zero = await db.demo_chat_tokens_add(-10 ** 9)
            check("счётчик не уходит в минус — greatest(0, …)", zero == 0, str(zero))
            await c.execute("delete from app_settings where key = $1", TOKENS_KEY)
            check("без ключа расход == 0 (fail-open, витрина не закрывается)",
                  (await db.demo_chat_tokens_used()) == 0)

            # 8. daily_limit в конфиге демо — на СВОЁМ тенанте
            # Пропуск здесь недопустим: это единственное место во всей волне, где проверяется
            # решение владельца «менять потолок апсертом app_settings без деплоя». Раньше при
            # отсутствии demo-sandbox печаталось OK, и блок проходил при заведомо сломанном
            # коде (переименованный get_app_setting, daily_limit=None, потерянный max(int,0)).
            print("8. daily_limit в get_demo_chat_cfg (throwaway-тенант)")
            await _drop_tenant(c)
            tid = await c.fetchval(
                "insert into tenants (slug,name,status) values ($1,'SMOKE demo-cap','active') "
                "returning id", SMOKE_SLUG)
            await c.execute(
                "insert into tenant_settings (tenant_id,key,value) values "
                "($1,'ai_enabled','1'),($1,'ai_model','smoke/model'),"
                "($1,'ai_system_prompt','системный промпт смоука')", tid)
            await _restore(c, LIMIT_KEY, "7")
            cfg = await db.get_demo_chat_cfg(SMOKE_SLUG)
            check("конфиг демо читается для нашего тенанта", cfg is not None, str(cfg))
            check("app_settings['demo_chat_daily_limit'] перекрывает env",
                  cfg is not None and cfg["daily_limit"] == 7,
                  str(cfg and cfg["daily_limit"]))
            await _restore(c, LIMIT_KEY, "мусор")
            cfg = await db.get_demo_chat_cfg(SMOKE_SLUG)
            check("мусор в настройке → дефолт из config, тип int",
                  cfg is not None and cfg["daily_limit"] == config.DEMO_CHAT_DAILY_LIMIT
                  and isinstance(cfg["daily_limit"], int), str(cfg and cfg["daily_limit"]))
            await _restore(c, LIMIT_KEY, "-5")
            cfg = await db.get_demo_chat_cfg(SMOKE_SLUG)
            check("отрицательный потолок клампится в 0 (kill-switch), а не уходит в минус",
                  cfg is not None and cfg["daily_limit"] == 0, str(cfg and cfg["daily_limit"]))
            await c.execute("delete from app_settings where key = $1", LIMIT_KEY)
            cfg = await db.get_demo_chat_cfg(SMOKE_SLUG)
            check("нет ключа → дефолт из config",
                  cfg is not None and cfg["daily_limit"] == config.DEMO_CHAT_DAILY_LIMIT,
                  str(cfg and cfg["daily_limit"]))
    finally:
        if db.pool is not None:
            async with db.pool.acquire() as c:
                for k, v in saved.items():
                    await _restore(c, k, v)
                await _drop_tenant(c)
            await db.close()

    print()
    if FAILS:
        print(f"❌ ПРОВАЛЫ ({len(FAILS)}): " + ", ".join(FAILS))
        sys.exit(1)
    print("🟢 demo_quota_smoke зелёный")


if __name__ == "__main__":
    asyncio.run(main())
