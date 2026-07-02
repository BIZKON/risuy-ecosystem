#!/usr/bin/env python3
"""Смоук Фазы 2 security-remediation (аудит 2026-07-01): Task 2.1 (enum-валидация reason/intent
+ name/phone карточки эскалации из БД, не из LLM-payload) + Task 2.2 (иммунитет к инъекциям в
системном промпте диалога, оба бэкенда). Юнит, без сети/БД (всё замокано).
  PYTHONPATH=bot-telegram:. ./.venv-smoke/bin/python scripts/security_phase2_smoke.py"""
import asyncio
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bot-telegram"))
sys.path.insert(0, ROOT)  # пакет shared/
os.environ.setdefault("DATABASE_URL", "postgresql://x/y")
os.environ.setdefault("BOT_TOKEN", "smoke")
os.environ.setdefault("CHANNEL_ID", "-100123")
os.environ.setdefault("CHANNEL_URL", "https://t.me/smoke")
os.environ.setdefault("GUIDE_URL", "https://example.com/guide")

import ai  # noqa: E402
import escalation  # noqa: E402

FAILS: list[str] = []


def check(name, cond):
    print(f"  {'OK ' if cond else 'FAIL'} {name}")
    if not cond:
        FAILS.append(name)


def unit_enum():
    print("— enum-валидация reason/intent в карточке (Task 2.1)")
    c = escalation.format_card({"reason": "admin_override", "name": "x"}, tg_user_id=1)
    check("неизвестный reason (admin_override) НЕ в карточке", "admin_override" not in c)
    c = escalation.format_card({"reason": "qualified"}, tg_user_id=1)
    check("валидный reason=qualified → русская подпись", "квалифицирован" in c)
    c = escalation.format_card({"intent": "hack_system'; DROP"}, tg_user_id=1)
    check("подделанный intent НЕ в карточке", "hack_system" not in c and "DROP" not in c)
    c = escalation.format_card({"intent": "enroll"}, tg_user_id=1)
    check("валидный intent=enroll → русская подпись", "запись на курс" in c)


def unit_immunity_text():
    print("— текст иммунитета (Task 2.2)")
    out = ai._with_immunity("Ты — Лия, ассистент.")
    check("иммунитет допИсан к промпту", ai._IMMUNITY in out and "Ты — Лия" in out)
    check("иммунитет блокирует ПРИНУЖДЕНИЕ (условие «ТОЛЬКО потому»)", "ТОЛЬКО потому" in ai._IMMUNITY)
    check("иммунитет разрешает легитимную эмиссию (по собственной оценке)",
          "по собственной оценке" in ai._IMMUNITY)
    check("иммунитет упоминает роль/промпт/маркеры",
          "роль" in ai._IMMUNITY and "маркер" in ai._IMMUNITY.lower())
    check("пустой промпт → только иммунитет", ai._with_immunity("") == ai._IMMUNITY)
    check("None-промпт → только иммунитет", ai._with_immunity(None) == ai._IMMUNITY)


async def unit_immunity_injection():
    print("— иммунитет доезжает в ОБА бэкенда (Task 2.2)")
    cap = {}
    orig = (ai.ask_gateway, ai.ask_agent_openai)

    async def fake_gateway(text, *, base_url=None, model=None, system_prompt=None,
                           fallback=None, history=None):
        cap["gw"] = system_prompt
        return "ответ-gw", None  # meta=None → без capture-планирования

    async def fake_openai(messages, *, agent_id=None):
        cap["oa"] = messages
        return "ответ-oa"

    ai.ask_gateway = fake_gateway
    ai.ask_agent_openai = fake_openai
    try:
        await ai._ask_ai_backend("вопрос", None, {"backend": "gateway", "system_prompt": "Ты Лия."})
        check("gateway: иммунитет + персона в system_prompt",
              ai._IMMUNITY in (cap.get("gw") or "") and "Ты Лия." in (cap.get("gw") or ""))
        await ai._ask_ai_backend("вопрос", None, {"backend": "cloud_ai", "system_prompt": "Ты Лия."})
        sysmsgs = [m["content"] for m in (cap.get("oa") or []) if m["role"] == "system"]
        check("cloud_ai: иммунитет + персона в role=system",
              bool(sysmsgs) and ai._IMMUNITY in sysmsgs[0] and "Ты Лия." in sysmsgs[0])
    finally:
        ai.ask_gateway, ai.ask_agent_openai = orig


async def unit_escalate_db_identity():
    print("— name/phone карточки из БД, НЕ из payload (Task 2.1)")
    sent = {}
    fake_msg = types.ModuleType("messaging")

    async def raw_send_text(bot, chat_id, text, *, message_thread_id=None, rich=False):
        sent["chat_id"] = chat_id
        sent["text"] = text

    fake_msg.raw_send_text = raw_send_text
    sys.modules["messaging"] = fake_msg
    fake_notifier = types.ModuleType("notifier")
    fake_notifier.get_notifier_bot = lambda: object()  # непустой бот
    sys.modules["notifier"] = fake_notifier

    orig = (escalation.db.claim_lead_escalation, escalation.db.get_lead_id,
            escalation.db.get_lead_for_purchase, escalation.db.release_lead_escalation)

    async def _claim(uid, *, messenger="tg"):
        return True

    async def _lead_id(uid, *, messenger="tg"):
        return "lead-uuid-1"

    async def _profile(uid, *, messenger="tg"):
        return {"id": "lead-uuid-1", "name": "Реальный Иван (БД)", "phone": "+79991112233"}

    async def _release(uid, *, messenger="tg"):
        return None

    escalation.db.claim_lead_escalation = _claim
    escalation.db.get_lead_id = _lead_id
    escalation.db.get_lead_for_purchase = _profile
    escalation.db.release_lead_escalation = _release
    try:
        payload = {"name": "ПОДДЕЛКА-Хакер", "phone": "+70000000000",
                   "reason": "qualified", "summary": "клиент готов купить курс"}
        await escalation.escalate(bot=object(), tg_user_id=123, payload=payload,
                                  messenger="tg", target_override=(555, None))
        txt = sent.get("text") or ""
        check("карточка отправлена", bool(txt))
        check("имя из БД в карточке", "Реальный Иван (БД)" in txt)
        check("телефон из БД в карточке", "+79991112233" in txt)
        check("поддельное имя из payload НЕ в карточке", "ПОДДЕЛКА-Хакер" not in txt)
        check("поддельный телефон из payload НЕ в карточке", "+70000000000" not in txt)
        check("summary из payload сохранён (контекст)", "клиент готов купить" in txt)
        check("reason=qualified → русская подпись", "квалифицирован" in txt)

        # Fail-closed: нет профиля в БД → имя/телефон пусты, НЕ из payload
        async def _no_profile(uid, *, messenger="tg"):
            return None

        escalation.db.get_lead_for_purchase = _no_profile
        sent.clear()
        await escalation.escalate(bot=object(), tg_user_id=123, payload=payload,
                                  messenger="tg", target_override=(555, None))
        txt2 = sent.get("text") or ""
        check("fail-closed: нет БД-профиля → поддельное имя НЕ подставлено",
              "ПОДДЕЛКА-Хакер" not in txt2 and "+70000000000" not in txt2)

        # Fail-soft: БД бросает на профиле → карточка ВСЁ РАВНО уходит (имя/тел пусты, не payload)
        async def _raise_profile(uid, *, messenger="tg"):
            raise RuntimeError("DB down")

        escalation.db.get_lead_for_purchase = _raise_profile
        sent.clear()
        await escalation.escalate(bot=object(), tg_user_id=123, payload=payload,
                                  messenger="tg", target_override=(555, None))
        txt3 = sent.get("text") or ""
        check("fail-soft: БД-сбой на профиле → карточка всё равно ушла", bool(txt3))
        check("fail-soft: при БД-сбое поддельное имя НЕ подставлено", "ПОДДЕЛКА-Хакер" not in txt3)
    finally:
        (escalation.db.claim_lead_escalation, escalation.db.get_lead_id,
         escalation.db.get_lead_for_purchase, escalation.db.release_lead_escalation) = orig
        sys.modules.pop("messaging", None)
        sys.modules.pop("notifier", None)


async def main():
    unit_enum()
    unit_immunity_text()
    await unit_immunity_injection()
    await unit_escalate_db_identity()
    print("\nВСЕ ОК" if not FAILS else "\nПРОВАЛЫ: " + ", ".join(FAILS))
    sys.exit(1 if FAILS else 0)


asyncio.run(main())
