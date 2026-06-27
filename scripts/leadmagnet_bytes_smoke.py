#!/usr/bin/env python3
"""Smoke: байты лид-магнита переживают заливку (для VK/MAX-выдачи). risuy_dev.

Проверяет:
1. set_product_file_id для kind='lead_magnet' НЕ обнуляет колонку file (байты).
2. get_funnel_product возвращает file_bytes (bytes) и file_name в dict.
3. Обычный продукт (kind='digital') — байты ОБНУЛЯЮТСЯ (регрессия одноразовости).

Запуск:
  FUNNEL_SMOKE_DSN="postgresql://gen_user:<pw>@HOST:5432/risuy_dev?sslmode=require" \
  PYTHONPATH=bot-telegram BOT_TOKEN=x DATABASE_URL="$FUNNEL_SMOKE_DSN" \
  CHANNEL_ID=-100 CHANNEL_URL=https://t.me/x GUIDE_URL=https://x \
  ./.venv-smoke/bin/python scripts/leadmagnet_bytes_smoke.py
"""
import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import db  # noqa: E402

DSN = os.environ.get("FUNNEL_SMOKE_DSN") or os.environ.get("DATABASE_URL")
if not DSN or "risuy_dev" not in DSN:
    raise SystemExit("Задайте DSN на risuy_dev (защита от прода).")

SLUG = "smoke-lm-bytes"
FAKE_BYTES = b"%PDF-1.4 test"


async def main() -> None:
    await db.init()
    fails: list[str] = []

    async with db.pool.acquire() as c:

        async def drop() -> None:
            await c.execute(
                "delete from products where tenant_id in (select id from tenants where slug=$1)",
                SLUG,
            )
            await c.execute("delete from tenants where slug=$1", SLUG)

        await drop()
        tid = await c.fetchval(
            "insert into tenants (slug,name,status) values ($1,'SMOKE lm','active') returning id",
            SLUG,
        )

        # ── Кейс 1: lead_magnet — байты должны выжить после set_product_file_id ──
        pid_lm = await c.fetchval(
            "insert into products (tenant_id,name,kind,status,file,file_name,file_mime) "
            "values ($1,'ЛМ тест','lead_magnet','active',$2,'guide.pdf','application/pdf') "
            "returning id",
            tid,
            FAKE_BYTES,
        )

        # ── Кейс 2: обычный продукт — байты должны обнулиться ──
        pid_dig = await c.fetchval(
            "insert into products (tenant_id,name,kind,status,file,file_name,file_mime) "
            "values ($1,'Цифровой','digital','active',$2,'file.zip','application/zip') "
            "returning id",
            tid,
            FAKE_BYTES,
        )

        try:
            tok = db.current_tenant_id.set(tid)
            try:
                await db.set_product_file_id(pid_lm, "TG_FILE_ID_LM")
                await db.set_product_file_id(pid_dig, "TG_FILE_ID_DIG")
                prod = await db.get_funnel_product(pid_lm)
            finally:
                db.current_tenant_id.reset(tok)

            # Проверка 1: байты lead_magnet остались в БД
            left_lm = await c.fetchval("select file from products where id=$1", pid_lm)
            if left_lm is None:
                fails.append(
                    "байты lead_magnet обнулены после заливки — VK/MAX выдать файл не смогут"
                )

            # Проверка 2: get_funnel_product отдаёт file_bytes
            if not prod or not prod.get("file_bytes"):
                fails.append(
                    f"get_funnel_product не отдал file_bytes: prod={prod!r}"
                )

            # Проверка 3: file_name присутствует в ответе
            if not prod or not prod.get("file_name"):
                fails.append(
                    f"get_funnel_product не отдал file_name: prod={prod!r}"
                )

            # Проверка 4: обычный продукт — байты обнулены (одноразовость заливки)
            left_dig = await c.fetchval("select file from products where id=$1", pid_dig)
            if left_dig is not None:
                fails.append(
                    "байты обычного продукта (digital) НЕ обнулены — нарушена одноразовость заливки"
                )

            # Проверка 5: file_tg_id проставлен у lead_magnet
            tg_id = await c.fetchval("select file_tg_id from products where id=$1", pid_lm)
            if tg_id != "TG_FILE_ID_LM":
                fails.append(f"file_tg_id не проставлен корректно: {tg_id!r}")

        finally:
            await drop()

    if fails:
        for f in fails:
            print("❌ " + f)
        raise SystemExit(1)

    print("🟢 leadmagnet_bytes_smoke зелёный")


if __name__ == "__main__":
    asyncio.run(main())
