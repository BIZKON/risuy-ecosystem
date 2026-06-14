#!/usr/bin/env python3
"""Юнит-смоук конвертера markdown→Telegram-HTML (bot-telegram/richfmt.py). Без БД/сети/aiogram.

Проверяет: корректную разметку, БЕЗОПАСНОЕ экранирование (анти-инъекция тегов), валидность
вывода для Telegram (только разрешённые теги, всё сбалансировано), и что to_html НЕ бросает.

Запуск: PYTHONPATH=bot-telegram python3 scripts/richfmt_smoke.py
"""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot-telegram"))
import richfmt  # noqa: E402

FAILS = []
# Теги, которые понимает Telegram parse_mode=HTML (всё прочее '<' в выводе = баг/инъекция).
ALLOWED = {"b", "i", "u", "s", "code", "pre", "a", "blockquote", "tg-spoiler"}
_TAG = re.compile(r"</?([a-z0-9-]+)(?:\s[^>]*)?>")


def check(name, cond, detail=""):
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


def only_allowed_tags(htmlstr: str) -> bool:
    """В выводе каждый '<' должен открывать ТОЛЬКО разрешённый тег (литералы экранированы в &lt;)."""
    for tag in _TAG.findall(htmlstr):
        if tag not in ALLOWED:
            return False
    # После удаления разрешённых тегов не должно остаться сырых '<' или '>'.
    stripped = _TAG.sub("", htmlstr)
    return "<" not in stripped and ">" not in stripped


def balanced(htmlstr: str) -> bool:
    """Грубая проверка баланса парных тегов (Telegram отвергает несбалансированные)."""
    stack = []
    for m in re.finditer(r"</?([a-z0-9-]+)(?:\s[^>]*)?>", htmlstr):
        full, tag = m.group(0), m.group(1)
        if full.startswith("</"):
            if not stack or stack.pop() != tag:
                return False
        else:
            stack.append(tag)
    return not stack


def main():
    print("1. Базовая разметка:")
    check("**жирный** → <b>", richfmt.to_html("это **жирный** текст") == "это <b>жирный</b> текст")
    check("*курсив* → <i>", "<i>курсив</i>" in richfmt.to_html("вот *курсив* тут"))
    check("~~зачёрк~~ → <s>", "<s>зач</s>" in richfmt.to_html("~~зач~~"))
    check("||спойлер|| → tg-spoiler", "<tg-spoiler>сюрприз</tg-spoiler>" in richfmt.to_html("||сюрприз||"))
    check("`код` → <code>", richfmt.to_html("вызови `func()`") == "вызови <code>func()</code>")
    check("# Заголовок → <b>", richfmt.to_html("# Привет") == "<b>Привет</b>")
    check("- список → •", "• молоко" in richfmt.to_html("- молоко"))
    check("> цитата → blockquote", "<blockquote>цитата</blockquote>" in richfmt.to_html("> цитата"))
    check("[текст](url) → <a>",
          richfmt.to_html("[сайт](https://x10.ru)") == '<a href="https://x10.ru">сайт</a>')

    print("2. Безопасность (анти-инъекция / экранирование):")
    inj = richfmt.to_html("<script>alert(1)</script> & <b>boom</b>")
    check("сырой HTML экранирован (нет <script>/<b> из ввода)",
          "<script>" not in inj and "alert" in inj and "&lt;b&gt;boom" in inj, inj)
    check("амперсанд → &amp;", "&amp;" in richfmt.to_html("Procter & Gamble"))
    check("плохая схема ссылки не даёт href",
          "href" not in richfmt.to_html("[x](javascript:alert(1))"),
          richfmt.to_html("[x](javascript:alert(1))"))
    check("код НЕ форматируется внутри (markdown защищён)",
          richfmt.to_html("`**не жирный**`") == "<code>**не жирный**</code>")
    check("< внутри inline-кода экранирован",
          richfmt.to_html("`a < b`") == "<code>a &lt; b</code>")

    print("3. Валидность вывода для Telegram (только разрешённые теги, баланс):")
    samples = [
        "# Итоги\n\nПривет, **мир**! Вот *список*:\n- раз\n- два\n\n> цитата\n\n`code` и [ссылка](https://t.me)",
        "```python\nprint('hi')\n```\nи `inline`",
        "Смешанный **жир _и курс_** с ||спойлером|| и https://прямая.ссылка",
        "Сломанный **без пары и `код без пары",
        "Эмодзи 🔥 и <теги> & сущности > всякие",
        "Цитата строкой:\n> первая\n> вторая\nконец",
    ]
    for i, s in enumerate(samples):
        out = richfmt.to_html(s)
        check(f"sample#{i}: только разрешённые теги", only_allowed_tags(out), out[:120])
        check(f"sample#{i}: теги сбалансированы", balanced(out), out[:120])

    print("4. Робастность (никогда не бросает):")
    for s in ["", "*" * 50, "[" * 30, "```" * 10, "||" * 20, "\x00\x00", "_" * 40, None]:
        try:
            richfmt.to_html(s if s is not None else "")
            ok = True
        except Exception as e:  # noqa: BLE001
            ok = False
            print("    raised:", repr(e))
        check(f"to_html({(s or '')!r:.20}) не бросает", ok)

    print()
    if FAILS:
        print(f"❌ ПРОВАЛЫ ({len(FAILS)}): " + ", ".join(FAILS))
        sys.exit(1)
    print("✅ richfmt smoke — все проверки зелёные")


if __name__ == "__main__":
    main()
