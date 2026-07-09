#!/usr/bin/env python3
"""Смоук оркестратора: форма proposal валидна; фолбэк без LLM работает;
LLM-сбой не крешит; no-fabrication (нет ИНН в ответах → gap, не выдумка).
  PYTHONPATH=admin-panel:. python3 scripts/brief_orchestrator_smoke.py
"""
import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "admin-panel"))
os.environ.setdefault("DATABASE_URL", "postgresql://x/y")
os.environ.setdefault("SESSION_SECRET", "smoke-secret")
os.environ.setdefault("ADMIN_USERNAME", "smoke")
os.environ.setdefault("ADMIN_PASSWORD_HASH", "$argon2id$v=19$m=65536,t=3,p=4$c21va2U$c21va2U")

import brief_orchestrator as orch  # noqa: E402

FAILS: list[str] = []


def check(name, cond, detail=""):
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


def _valid_shape(p: dict) -> bool:
    return (isinstance(p, dict) and isinstance(p.get("settings"), dict)
            and isinstance(p.get("products"), list) and isinstance(p.get("recommendations"), list)
            and isinstance(p.get("gaps"), list))


ANSWERS_NO_INN = {"version": 1, "company_name": "Кофейня «Зерно»", "b2b_or_b2c": "B2C",
                  "niche": "кофейня", "products_list": "Абонемент — 3000 ₽ — 30 чашек",
                  "tone": "Дружелюбный на «ты»", "channels_used": ["Telegram"]}


async def main() -> None:
    print("1. фолбэк без LLM:")
    fb = orch.fallback_proposal(ANSWERS_NO_INN)
    check("форма валидна", _valid_shape(fb))
    check("продукты перенесены", len(fb["products"]) >= 1)
    check("company_name → funnel", fb["settings"].get("funnel", {}).get("company_name") == "Кофейня «Зерно»")

    print("2. no-fabrication: нет ИНН → gap, не выдумка:")
    check("есть gap про ИНН", any("инн" in (g.get("field", "") + g.get("question", "")).lower()
                                  for g in fb["gaps"]))
    check("ИНН НЕ выдуман", not fb["settings"].get("funnel", {}).get("operator_inn"))

    print("3. LLM-сбой → фолбэк, не креш:")
    async def broken_llm(prompt: str) -> str:
        raise RuntimeError("llm down")
    p = await orch.analyze(ANSWERS_NO_INN, llm=broken_llm)
    check("вернул валидный proposal при сбое LLM", _valid_shape(p))

    print("4. LLM-успех (мок валидного JSON):")
    async def ok_llm(prompt: str) -> str:
        return ('{"settings":{"persona":{"name":"Бариста","role":"ИИ-продавец",'
                '"behavior_prompt":"дружелюбно на ты","knowledge":""},"funnel":{},'
                '"triggers":[],"channels":{}},"products":[],"recommendations":'
                '[{"title":"Включить приветствие","why":"первое касание","section":"funnel"}],"gaps":[]}')
    p2 = await orch.analyze(ANSWERS_NO_INN, llm=ok_llm)
    check("распарсил LLM-ответ", _valid_shape(p2) and p2["settings"]["persona"]["name"] == "Бариста")

    print("5. merge продуктов: LLM вернул часть продуктов → непокрытый фолбэк-продукт сохраняется:")
    answers_two_products = dict(ANSWERS_NO_INN)
    answers_two_products["products_list"] = (
        "Абонемент — 3000 ₽ — 30 чашек\nДегустация — 500 ₽ — набор из 5 сортов")
    fb2 = orch.fallback_proposal(answers_two_products)
    check("фолбэк собрал 2 продукта", len(fb2["products"]) == 2, str(fb2["products"]))

    async def partial_llm(prompt: str) -> str:
        return ('{"settings":{"persona":{},"funnel":{},"triggers":[],"channels":{}},'
                '"products":[{"name":"Абонемент","price":3500,"currency":"RUB",'
                '"caption":"обновлено LLM","kind":"main"}],"recommendations":[],"gaps":[]}')
    p3 = await orch.analyze(answers_two_products, llm=partial_llm)
    names = {p.get("name") for p in p3["products"]}
    check("LLM-продукт (Абонемент) присутствует и обновлён", "Абонемент" in names
          and any(p.get("name") == "Абонемент" and p.get("price") == 3500 for p in p3["products"]),
          str(p3["products"]))
    check("непокрытый фолбэк-продукт (Дегустация) сохранён", "Дегустация" in names,
          str(p3["products"]))
    check("итого 2 продукта (не потеряли и не задвоили)", len(p3["products"]) == 2,
          str(p3["products"]))

    print("6. _get нормализует multichoice (список) в строку без python-repr:")
    val = orch._get({"channels_used": ["Telegram", "VK"]}, "channels_used")
    check("список → 'Telegram, VK' (не python-repr)", val == "Telegram, VK", repr(val))
    val_section = orch._get({"section": {"channels_used": ["A", "B"]}}, "channels_used")
    check("список из секции тоже нормализован", val_section == "A, B", repr(val_section))

    print()
    if FAILS:
        print("❌ ПРОВАЛЫ: " + ", ".join(FAILS))
        sys.exit(1)
    print("✅ brief_orchestrator smoke — OK")


if __name__ == "__main__":
    asyncio.run(main())
