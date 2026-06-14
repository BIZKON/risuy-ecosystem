"""markdown-подобный текст → Telegram HTML (parse_mode='HTML').

Делает ответы Лии и ботов «красивыми» БЕЗ правки промпта: LLM естественно пишет markdown
(**жирный**, списки, `код`, > цитаты, [ссылки](url)) — этот модуль конвертирует его в
HTML-теги, которые Telegram поддерживает: b/i/u/s/code/pre/a/blockquote/tg-spoiler.

⚠️ БЕЗОПАСНОСТЬ — главный инвариант: to_html() НИКОГДА не бросает и ВСЕГДА экранирует литералы
(& < >), чтобы текст пользователя/LLM не мог инжектить теги. При любом сбое → escape(plain).
Окончательная страховка §8.7 — в messaging.py: если Telegram отвергнет HTML (битая разметка),
отправка повторяется plain-текстом. Поэтому даже несовершенная конвертация безопасна.

Чего HTML Telegram НЕ умеет (деградируем в читаемый текст): заголовки → <b>, маркеры списков
→ «• », таблицы/чек-листы/сноски/формулы — это уже Bot API 10.1 sendRichMessage (Track 2 —
пока НЕ подключён: aiogram 3.7 без типов метода + рендер только на новых клиентах). Здесь —
надёжный слой «красивого текста» для ВСЕХ клиентов сегодня.
"""
from __future__ import annotations

import html
import re

# Разрешённые схемы ссылок (иначе ссылка рендерится как обычный текст — анти-инъекция).
_URL_OK = re.compile(r"^(https?://|tg://|mailto:)", re.IGNORECASE)
_PLACEHOLDER = "\x00{}\x00"  # маркер защищённого кода (NUL не встречается в тексте Telegram)


def _esc(s: str) -> str:
    """Экранирование текста: & < > (кавычки в тексте не трогаем)."""
    return html.escape(s, quote=False)


def _esc_attr(s: str) -> str:
    """Экранирование значения href (вкл. кавычки)."""
    return html.escape(s, quote=True)


def _convert(text: str) -> str:
    codes: list[str] = []  # защищённые фрагменты кода (восстановим в конце уже как HTML)

    def _stash(html_fragment: str) -> str:
        codes.append(html_fragment)
        return _PLACEHOLDER.format(len(codes) - 1)

    # 1) Защищаем КОД до экранирования/инлайна (внутри него markdown не трогаем).
    def _fence(m: re.Match) -> str:
        lang = (m.group(1) or "").strip()
        body = m.group(2)
        inner = _esc(body)
        if lang and re.fullmatch(r"[\w+-]+", lang):
            return _stash(f'<pre><code class="language-{_esc_attr(lang)}">{inner}</code></pre>')
        return _stash(f"<pre>{inner}</pre>")

    text = re.sub(r"```([^\n`]*)\n?(.*?)```", _fence, text, flags=re.DOTALL)
    text = re.sub(r"`([^`\n]+)`", lambda m: _stash(f"<code>{_esc(m.group(1))}</code>"), text)

    # 2) Экранируем ВСЁ остальное (теперь любой '<' пользователя безопасен).
    text = _esc(text)

    # 3) Блочная разметка построчно (заголовки / цитаты / маркеры списков).
    out_lines: list[str] = []
    quote_buf: list[str] = []

    def _flush_quote() -> None:
        if quote_buf:
            out_lines.append("<blockquote>" + "\n".join(quote_buf) + "</blockquote>")
            quote_buf.clear()

    for line in text.split("\n"):
        mq = re.match(r"^\s{0,3}&gt;\s?(.*)$", line)  # '>' стал '&gt;' после экранирования
        if mq is not None:
            quote_buf.append(mq.group(1))
            continue
        _flush_quote()
        mh = re.match(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$", line)
        if mh is not None:
            out_lines.append(f"<b>{mh.group(2)}</b>")
            continue
        ml = re.match(r"^(\s*)[-*+]\s+(.*)$", line)
        if ml is not None:
            out_lines.append(f"{ml.group(1)}• {ml.group(2)}")
            continue
        if re.match(r"^\s{0,3}([-*_])\1{2,}\s*$", line):  # --- *** ___ → разделитель
            out_lines.append("──────────")
            continue
        out_lines.append(line)
    _flush_quote()
    text = "\n".join(out_lines)

    # 4) Инлайн-разметка (порядок важен: сперва парные двойные, потом одиночные).
    # Ссылки [текст](url): скобки экранирование не трогает; невалидная схема → текст как есть.
    def _link(m: re.Match) -> str:
        label, url = m.group(1), m.group(2)
        if _URL_OK.match(url):
            return f'<a href="{_esc_attr(url)}">{label}</a>'
        return f"{label} ({url})"

    text = re.sub(r"\[([^\]\n]+)\]\(([^)\s]+)\)", _link, text)
    text = re.sub(r"\*\*(\S.*?\S|\S)\*\*", r"<b>\1</b>", text)        # **bold**
    text = re.sub(r"__(\S.*?\S|\S)__", r"<b>\1</b>", text)            # __bold__
    text = re.sub(r"~~(\S.*?\S|\S)~~", r"<s>\1</s>", text)            # ~~strike~~
    text = re.sub(r"\|\|(\S.*?\S|\S)\|\|", r"<tg-spoiler>\1</tg-spoiler>", text)  # ||spoiler||
    text = re.sub(r"(?<![\w*])\*(\S.*?\S|\S)\*(?![\w*])", r"<i>\1</i>", text)     # *italic*
    # _italic_ только на границах слова (чтобы не ломать snake_case / file_name).
    text = re.sub(r"(?<![\w_])_(\S.*?\S|\S)_(?![\w_])", r"<i>\1</i>", text)

    # 5) Возвращаем защищённый код.
    def _unstash(m: re.Match) -> str:
        return codes[int(m.group(1))]

    text = re.sub(r"\x00(\d+)\x00", _unstash, text)
    return text


def to_html(text: str) -> str:
    """markdown-текст → Telegram HTML. НИКОГДА не бросает: при сбое → экранированный plain."""
    if not text:
        return ""
    try:
        return _convert(text)
    except Exception:  # noqa: BLE001 — конвертер не должен ронять отправку (фолбэк-на-plain ниже)
        return _esc(text)
