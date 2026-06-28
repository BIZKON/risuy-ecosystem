#!/usr/bin/env python3
"""Чистый смоук shared/anon: псевдоним, formula-guard, валидация persona, сборка строк anon/map.
Запуск: PYTHONPATH=. ./.venv-smoke/bin/python scripts/pii_anon_smoke.py  (БД не нужна)"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from shared import anon  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


UID1 = "11111111-1111-4111-8111-111111111111"
UID2 = "22222222-2222-4222-8222-222222222222"


def main() -> None:
    # --- subject_code: детерминизм, префикс, длина, различимость ---
    c1 = anon.subject_code(UID1)
    check("subject_code детерминирован", c1 == anon.subject_code(UID1), c1)
    check("subject_code префикс", c1.startswith("СУБЪЕКТ-"))
    check("subject_code длина 16 hex", len(c1) == len("СУБЪЕКТ-") + 16, str(len(c1)))
    check("subject_code различимость", anon.subject_code(UID2) != c1)

    # --- csv_safe: formula-guard ---
    for raw, exp in [("=cmd", "'=cmd"), ("+1", "'+1"), ("-1", "'-1"), ("@x", "'@x"),
                     ("\tx", "'\tx"), ("\rx", "'\rx"), ("ok", "ok"), ("", ""), (None, "")]:
        check(f"csv_safe({raw!r})", anon.csv_safe(raw) == exp, repr(anon.csv_safe(raw)))

    # --- valid_persona ---
    allowed = {"liya", "mark"}
    check("valid_persona known", anon.valid_persona("liya", allowed) == "liya")
    check("valid_persona unknown→пусто", anon.valid_persona("evil", allowed) == "")
    check("valid_persona None→пусто", anon.valid_persona(None, allowed) == "")

    # --- anon_row: нет прямых идентификаторов, длина = заголовку, has_notes/persona ---
    rec = {
        "id": UID1, "messenger": "tg", "source": "vk", "consent": True, "subscribed": False,
        "status": "new", "created_at": None, "updated_at": None, "guide_sent_at": None,
        "follow_up_1_at": None, "follow_up_2_at": None, "follow_up_3_at": None,
        "unsubscribed_at": None, "erase_requested_at": None, "ai_persona": "evil",
        "bot_paused": False, "escalated_at": None, "has_notes": True,
    }
    row = anon.anon_row(rec, allowed)
    check("anon_row длина = ANON_HEADER", len(row) == len(anon.ANON_HEADER), str(len(row)))
    check("anon_row[0] = subject_code", row[0] == c1)
    check("anon_row has_notes=да", row[anon.ANON_HEADER.index("has_notes")] == "да")
    check("anon_row невалидный persona→пусто", row[anon.ANON_HEADER.index("ai_persona")] == "")
    # структурная гарантия: anon_row не читает ключи прямых идентификаторов (иначе KeyError выше)
    check("anon_row не требует name/phone", "name" not in rec and "phone" not in rec)

    # --- map_row: обычный лид vs отзыв ---
    mrec = {"id": UID1, "name": "Иван", "phone": "+79001112233", "tg_user_id": 42,
            "vk_user_id": None, "max_user_id": None, "max_chat_id": None,
            "web_session_id": None, "notes": "живёт на Тверской", "erase_requested_at": None}
    mrow = anon.map_row(mrec)
    check("map_row длина = MAP_HEADER", len(mrow) == len(anon.MAP_HEADER), str(len(mrow)))
    check("map_row[0] = subject_code (стабилен)", mrow[0] == c1)
    check("map_row name присутствует", mrow[anon.MAP_HEADER.index("name")] == "Иван")
    check("map_row phone присутствует", mrow[anon.MAP_HEADER.index("phone")] == "+79001112233")

    import datetime
    erec = dict(mrec, erase_requested_at=datetime.datetime(2026, 6, 1))
    erow = anon.map_row(erec)
    check("отзыв: name обнулён", erow[anon.MAP_HEADER.index("name")] == "")
    check("отзыв: phone обнулён", erow[anon.MAP_HEADER.index("phone")] == "")
    check("отзыв: notes обнулён", erow[anon.MAP_HEADER.index("notes")] == "")
    check("отзыв: tg_user_id обнулён", erow[anon.MAP_HEADER.index("tg_user_id")] == "")
    check("отзыв: флаг проставлен",
          erow[anon.MAP_HEADER.index("erase_status")] == "отзыв — обезличивание в процессе")
    check("отзыв: subject_code сохранён", erow[0] == c1)

    if FAILS:
        print("\n".join("❌ " + f for f in FAILS))
        raise SystemExit(1)
    print("🟢 pii_anon_smoke зелёный")


if __name__ == "__main__":
    main()
