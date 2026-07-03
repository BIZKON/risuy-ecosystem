#!/usr/bin/env python3
"""Unit-смоук лендинга клуба: _club_landing_html (bot.py) — без сети/БД.
Запуск: PYTHONPATH=bot-telegram:. ./.venv-smoke/bin/python scripts/club_landing_smoke.py"""
import os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, "bot-telegram"))
os.environ.setdefault("BOT_TOKEN", "0:smoke")
os.environ.setdefault("DATABASE_URL", "postgresql://x/y")
os.environ.setdefault("CHANNEL_ID", "-1001234567890")
os.environ.setdefault("CHANNEL_URL", "https://t.me/smoke")
os.environ.setdefault("GUIDE_URL", "https://example.org/guide")
import bot  # noqa: E402  (bot-telegram/bot.py)

FAILS = []
def check(name, cond, detail=""):
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not cond: FAILS.append(name)

def main():
    html = bot._club_landing_html("ООО <Ромашка>", "https://t.me/mybot?start=club",
                                  "https://x.ru/legal/romashka/privacy")
    check("есть кнопка «Вступить»", "Вступить в клуб" in html)
    check("есть deep-link", "https://t.me/mybot?start=club" in html)
    check("есть ссылка на Политику", "/legal/romashka/privacy" in html)
    check("operator_name экранирован (нет сырого <)", "<Ромашка>" not in html and "&lt;Ромашка&gt;" in html)
    check("название клуба в заголовке", "Клуб предпринимателей" in html)
    html2 = bot._club_landing_html("ООО Роза", "https://t.me/mybot?start=club", "")
    check("без policy_url — Политику не рендерим, но лендинг цел", "Политика" not in html2 and "Вступить в клуб" in html2)
    print()
    if FAILS: print(f"❌ ПРОВАЛЫ ({len(FAILS)}): " + ", ".join(FAILS)); sys.exit(1)
    print("✅ club_landing_smoke — все проверки зелёные")

if __name__ == "__main__": main()
