"""Единый источник истины для бриф-опроса тенанта.

Читают: бот (bot-telegram) — рендер формы; оркестратор (admin-panel) — интерпретация
ответов. Меняем вопросы ТОЛЬКО здесь. Номер версии кладётся в ответы при сабмите.
"""
from __future__ import annotations

BRIEF_VERSION = 1

# Тип вопроса: text | textarea | choice | multichoice | repeatable
# show_if: показывать вопрос, только если answers[q] входит в in
# maps_to: подсказка оркестратору, на какую «ручку» настройки влияет ответ
SECTIONS: list[dict] = [
    {
        "key": "business",
        "title": "О бизнесе",
        "questions": [
            {"key": "company_name", "type": "text", "required": True, "max": 200,
             "label": "Название вашего бизнеса", "maps_to": "funnel.company_name"},
            {"key": "b2b_or_b2c", "type": "choice", "required": True,
             "options": ["B2B", "B2C", "Оба"],
             "label": "Вы продаёте бизнесам, людям или и тем и другим?"},
            {"key": "niche", "type": "text", "required": True, "max": 200,
             "label": "Ниша/отрасль в двух словах"},
            {"key": "positioning", "type": "textarea", "required": False, "max": 1000,
             "label": "Чем вы отличаетесь от конкурентов? Одним абзацем."},
        ],
    },
    {
        "key": "products",
        "title": "Продукты и оферы",
        "questions": [
            {"key": "products_list", "type": "textarea", "required": True, "max": 4000,
             "label": "Перечислите продукты/услуги: название — цена — что даёт. По одному в строке.",
             "maps_to": "products"},
            {"key": "best_seller", "type": "textarea", "required": False, "max": 1000,
             "label": "Что покупают чаще всего и что приносит больше всего прибыли — это одно и то же?"},
            {"key": "lead_magnet", "type": "textarea", "required": False, "max": 1000,
             "label": "Есть ли бесплатный материал/пробник для первого касания? Опишите.",
             "maps_to": "funnel.leadmagnet"},
        ],
    },
    {
        "key": "audience",
        "title": "Клиенты",
        "branch_on": "b2b_or_b2c",
        "questions": [
            {"key": "audience_portrait", "type": "textarea", "required": True, "max": 2000,
             "label": "Портрет вашего клиента (сегмент, не список контактов): кто это, какая ситуация."},
            {"key": "trigger_moment", "type": "textarea", "required": True, "max": 1000,
             "label": "В какой момент клиент понимает, что вы ему нужны? Опишите триггер, а не портрет."},
            {"key": "b2b_decision", "type": "textarea", "required": False, "max": 1000,
             "show_if": {"q": "b2b_or_b2c", "in": ["B2B", "Оба"]},
             "label": "Кто принимает решение о покупке и сколько длится цикл сделки?"},
            {"key": "b2c_objections", "type": "textarea", "required": False, "max": 1000,
             "show_if": {"q": "b2b_or_b2c", "in": ["B2C", "Оба"]},
             "label": "Топ-3 возражения, которые вы слышите чаще всего от людей."},
        ],
    },
    {
        "key": "voice",
        "title": "Тон и стиль общения",
        "questions": [
            {"key": "tone", "type": "choice", "required": True,
             "options": ["Дружелюбный на «ты»", "Уважительный на «вы»", "Экспертный/деловой", "Живой/с юмором"],
             "label": "Как ИИ-сотрудник должен общаться с клиентами?",
             "maps_to": "persona.behavior"},
            {"key": "price_objection_example", "type": "textarea", "required": False, "max": 1000,
             "label": "Как ваш лучший продавец отвечает на «дорого»? Дайте пример фразой.",
             "maps_to": "persona.behavior"},
            {"key": "forbidden", "type": "textarea", "required": False, "max": 1000,
             "label": "Чего ИИ-сотрудник НЕ должен делать/говорить? (стоп-темы, обещания)"},
        ],
    },
    {
        "key": "channels",
        "title": "Каналы и анонсы",
        "questions": [
            {"key": "channels_used", "type": "multichoice", "required": True,
             "options": ["Telegram", "VK", "MAX"],
             "label": "В каких каналах работает ИИ-сотрудник?",
             "maps_to": "channels"},
            {"key": "announcements", "type": "textarea", "required": False, "max": 2000,
             "label": "Что важного происходит регулярно, о чём стоит напоминать подписчикам?",
             "maps_to": "triggers"},
            {"key": "escalation_wanted", "type": "choice", "required": False,
             "options": ["Да", "Нет"],
             "label": "Передавать горячие/сложные обращения живому менеджеру?"},
        ],
    },
    {
        "key": "legal",
        "title": "Реквизиты оператора (152-ФЗ)",
        "questions": [
            {"key": "operator_name", "type": "text", "required": True, "max": 300,
             "label": "Юридическое название (ИП/ООО) — оператор персональных данных",
             "maps_to": "funnel.operator_name"},
            {"key": "operator_inn", "type": "text", "required": True, "max": 20,
             "label": "ИНН оператора", "maps_to": "funnel.operator_inn"},
            {"key": "operator_email", "type": "text", "required": True, "max": 200,
             "label": "Контактный email оператора", "maps_to": "funnel.operator_email"},
        ],
    },
]


def question_index() -> dict[str, dict]:
    """Плоский индекс question_key -> вопрос (+ поле _section с ключом секции)."""
    idx: dict[str, dict] = {}
    for sec in SECTIONS:
        for q in sec["questions"]:
            item = dict(q)
            item["_section"] = sec["key"]
            idx[q["key"]] = item
    return idx


def _is_visible(q: dict, answers: dict) -> bool:
    cond = q.get("show_if")
    if not cond:
        return True
    return str(answers.get(cond["q"], "")) in cond["in"]


def visible_questions(answers: dict) -> list[dict]:
    """Вопросы, видимые при текущих ответах (с учётом ветвления show_if)."""
    idx = question_index()
    return [q for q in idx.values() if _is_visible(q, answers)]


def validate_answers(answers: dict) -> list[str]:
    """Проверка ответов по схеме. Возвращает список ошибок (пусто = валидно).

    Ловит: пропущенные required (видимые), неизвестные варианты choice/multichoice,
    превышение max по длине текста.
    """
    errs: list[str] = []
    idx = question_index()
    for key, q in idx.items():
        visible = _is_visible(q, answers)
        raw = answers.get(key)

        # Обработка multichoice: нормализовать в список
        if q["type"] == "multichoice":
            if raw is None:
                choices = []
            elif isinstance(raw, list):
                choices = raw
            else:
                # Если передан одиночный элемент, обернуть в список
                choices = [raw] if raw else []

            # Проверка: required и видимый → список не должен быть пустым
            if q.get("required") and visible and not choices:
                errs.append(f"{key}: обязательный вопрос не заполнен")

            # Проверка: каждый выбранный элемент должен быть в options
            if choices and q.get("options"):
                for choice in choices:
                    choice_str = str(choice).strip() if choice is not None else ""
                    if choice_str and choice_str not in q["options"]:
                        errs.append(f"{key}: недопустимый вариант «{choice_str}»")
        else:
            # Для остальных типов: text, textarea, choice
            val = "" if raw is None else str(raw).strip()

            if q.get("required") and visible and not val:
                errs.append(f"{key}: обязательный вопрос не заполнен")

            if val and q["type"] == "choice" and q.get("options") and val not in q["options"]:
                errs.append(f"{key}: недопустимый вариант «{val}»")

            if val and q.get("max") and len(val) > int(q["max"]):
                errs.append(f"{key}: превышена длина ({len(val)} > {q['max']})")

    return errs
