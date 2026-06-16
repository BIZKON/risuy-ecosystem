#!/usr/bin/env python3
"""Golden-smoke паритета ЯДРА audience-WHERE рассылки (аудит #2): панель ↔ бот.

Ядро «кому можно слать» ДОЛЖНО побайтово совпадать у бота (bot-telegram/db.py::_audience_where) и
панели (admin-panel/db.py::_broadcast_audience_where), иначе предпросмотр/recipient_count/cap-гейт
разъедутся с реальной доставкой. Бот и панель — РАЗНЫЕ процессы (импорт невозможен), поэтому здесь
пиним КАНОН литералом и проверяем ПАНЕЛЬ против него; бот-сторона пинится тем же литералом в
c3_channel_outbound_smoke.py (оба == канон ⇒ совпадают между собой).

unsubscribed_at is null — В ЯДРЕ ВСЕГДА (решение владельца): бот режет отписанных всегда, тумблер
exclude_unsubscribed больше не влияет на WHERE.

Запуск: PYTHONPATH=admin-panel ./.venv-smoke/bin/python scripts/audience_parity_smoke.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "admin-panel"))
os.environ.setdefault("DATABASE_URL", "postgresql://x/y")
os.environ.setdefault("SESSION_SECRET", "x" * 40)
os.environ.setdefault("ADMIN_USERNAME", "admin")
# config требует argon2-PHC формат (валидация). Dummy-хеш нужного вида (смоук БД не трогает).
os.environ.setdefault("ADMIN_PASSWORD_HASH",
                      "$argon2id$v=19$m=65536,t=3,p=4$c2FsdHNhbHRzYWx0$aGFzaGhhc2hoYXNoaGFzaGhhc2g")

import db  # noqa: E402  (admin-panel/db.py)

FAILS: list[str] = []

# КАНОН ядра по каналу (порядок клауз == bot-telegram/db.py::_audience_where). Адрес-колонка:
# tg→tg_user_id, vk→vk_user_id, max→max_chat_id. Эти же литералы пинит c3-смоук на стороне бота.
CANON = {
    "tg": ("messenger = 'tg' and tg_user_id is not null and consent = true "
           "and unsubscribed_at is null and erase_requested_at is null and bot_paused = false"),
    "vk": ("messenger = 'vk' and vk_user_id is not null and consent = true "
           "and unsubscribed_at is null and erase_requested_at is null and bot_paused = false"),
    "max": ("messenger = 'max' and max_chat_id is not null and consent = true "
            "and unsubscribed_at is null and erase_requested_at is null and bot_paused = false"),
}


def check(name, cond, detail=""):
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail != "" else ""))
    if not cond:
        FAILS.append(name)


def main() -> None:
    print("1. Панель _broadcast_audience_where: ядро == канон (без операторских сужений):")
    for m, canon in CANON.items():
        where, params, _ = db._broadcast_audience_where({"messenger": m})
        check(f"{m}: ядро побайтово == канон", where == canon, where)
        check(f"{m}: без параметров (пустая аудитория)", params == [])

    print("2. Тумблер exclude_unsubscribed больше НЕ влияет на WHERE (unsubscribed в ядре всегда):")
    w_default, _, _ = db._broadcast_audience_where({"messenger": "tg"})
    w_off, _, _ = db._broadcast_audience_where({"messenger": "tg", "exclude_unsubscribed": False})
    check("снятый тумблер → тот же WHERE", w_default == w_off and "unsubscribed_at is null" in w_off)

    print("3. Операторские сужения добавляются ПОВЕРХ ядра ($-плейсхолдеры):")
    where, params, _ = db._broadcast_audience_where({"messenger": "tg", "source": "vk"})
    check("source-сужение добавлено", where.startswith(CANON["tg"]) and "source = $1" in where and params == ["vk"])

    print()
    if FAILS:
        print(f"❌ ПРОВАЛЫ ({len(FAILS)}): " + ", ".join(FAILS)); sys.exit(1)
    print("✅ audience_parity smoke — ядро панели совпадает с каноном (== bot _audience_where)")


if __name__ == "__main__":
    main()
