#!/usr/bin/env python3
"""Smoke ЧИСТОЙ логики тенант-воронки (без aiogram/сети/БД): start_text, ветвление шагов,
план выдачи. db.init() НЕ зовём → подключения нет; config удовлетворяем заглушками env.
Запуск:  ./.venv-smoke/bin/python scripts/funnel_flow_smoke.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# funnel импортит db → config fail-fast: даём заглушки (подключения нет, db.init не зовём).
os.environ.setdefault("BOT_TOKEN", "x")
os.environ.setdefault("DATABASE_URL", "postgresql://x/x")
os.environ.setdefault("CHANNEL_ID", "-100")
os.environ.setdefault("CHANNEL_URL", "https://t.me/x")
os.environ.setdefault("GUIDE_URL", "https://x")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "bot-telegram"))

import funnel  # noqa: E402


def main() -> None:
    fails: list[str] = []

    # start_text: приветствие из cfg + блок согласия
    t = funnel.start_text({"welcome_text": "Привет!", "consent_text": "Согласие тут"})
    if "Привет!" not in t or "Согласие тут" not in t:
        fails.append(f"start_text не собрал welcome+consent: {t!r}")
    if funnel.start_text({"welcome_text": "", "consent_text": ""}) != funnel.DEFAULT_WELCOME:
        fails.append("дефолт-приветствие при пустом welcome не подставилось")

    # ветвление после согласия (приоритет phone > gate > deliver)
    if funnel.next_after_consent({"phone_step": True, "gate": {"enabled": True}}) != "phone":
        fails.append("phone должен иметь приоритет после согласия")
    if funnel.next_after_consent({"phone_step": False, "gate": {"enabled": True}}) != "gate":
        fails.append("gate после согласия")
    if funnel.next_after_consent({"phone_step": False, "gate": {"enabled": False}}) != "deliver":
        fails.append("deliver после согласия")

    # после телефона
    if funnel.next_after_phone({"gate": {"enabled": True}}) != "gate":
        fails.append("phone→gate")
    if funnel.next_after_phone({"gate": {"enabled": False}}) != "deliver":
        fails.append("phone→deliver")

    # deliver_plan: ссылка
    p = funnel.deliver_plan({"leadmagnet": {"kind": "link", "url": "https://x/g.pdf", "caption": ""},
                             "video_note_file_id": ""})
    if not (p["configured"] and p["kind"] == "link" and p["url"] == "https://x/g.pdf"):
        fails.append(f"deliver_plan link: {p}")
    if p["caption"] != funnel.DEFAULT_CAPTION:
        fails.append("дефолт-подпись не подставилась")
    if p["has_video"]:
        fails.append("has_video=True без видео")

    # deliver_plan: файл + видео + своя подпись
    pf = funnel.deliver_plan({"leadmagnet": {"kind": "file", "file_id": "BQAC", "caption": "Лови"},
                              "video_note_file_id": "DQAC"})
    if not (pf["configured"] and pf["kind"] == "file" and pf["has_video"] and pf["caption"] == "Лови"):
        fails.append(f"deliver_plan file: {pf}")

    # deliver_plan: файл загружен как продукт (product_id) → configured
    pp = funnel.deliver_plan({"leadmagnet": {"kind": "file", "product_id": "42"}})
    if not (pp["configured"] and pp["product_id"] == "42"):
        fails.append(f"file с product_id должен быть configured: {pp}")

    # deliver_plan: незаполненный лид-магнит → configured=False
    if funnel.deliver_plan({"leadmagnet": {"kind": "link", "url": ""}})["configured"]:
        fails.append("link без url не должен быть configured")
    if funnel.deliver_plan({"leadmagnet": {"kind": "file", "file_id": "", "product_id": ""}})["configured"]:
        fails.append("file без file_id и product_id не должен быть configured")
    if funnel.deliver_plan({})["configured"]:
        fails.append("пустой cfg не должен быть configured")

    if fails:
        print("\n".join("❌ " + f for f in fails))
        raise SystemExit(1)
    print("🟢 funnel_flow_smoke зелёный")


if __name__ == "__main__":
    main()
