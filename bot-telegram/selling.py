"""Слой C: канал-агностичное ЯДРО продаж — чистые хелперы (без aiogram/aiohttp/сети), тестируемы
в смоук-venv. Определение торговой команды из нажатой кнопки/текста + сборка абстрактных кнопок
витрины. Транспорт (TG InlineKeyboard / VK keyboard / MAX inline_keyboard) и create_payment —
в multiplex/драйверах канала."""
import texts

# Слова-триггеры витрины (точное совпадение текста, без ведущего «/»). Подстроку в свободной
# фразе НЕ ловим — «хочу купить курс» не откроет витрину, а вот «купить» / «/shop» откроют.
SHOP_WORDS = {"shop", "магазин", "купить", "товары", "каталог"}


def selling_command(text, payload) -> tuple[str, int | None] | None:
    """Торговая команда из payload нажатой кнопки ИЛИ из текста. ('buy', id) / ('shop', None) / None.
    payload (dict кнопки): {'cmd':'buy','id':N} | {'cmd':'shop'}. Невалидный id → None (не падаем)."""
    if isinstance(payload, dict):
        c = payload.get("cmd")
        if c == "buy":
            try:
                return ("buy", int(payload.get("id")))
            except (TypeError, ValueError):
                return None
        if c == "shop":
            return ("shop", None)
    t = (text or "").strip().lower().lstrip("/")
    if t in SHOP_WORDS:
        return ("shop", None)
    return None


def shop_button_rows(products: list[dict]) -> list[dict]:
    """Абстрактные кнопки витрины: по продукту → {label, payload={'cmd':'buy','id':N}}. Драйвер
    канала рендерит их по-своему. Цену форматируем единым texts.format_price."""
    return [{"label": f"💳 {p['name']} — {texts.format_price(p.get('price'), p.get('currency'))}",
             "payload": {"cmd": "buy", "id": p["id"]}} for p in products]
