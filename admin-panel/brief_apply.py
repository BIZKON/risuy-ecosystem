"""Применение черновика оркестратора к настройкам тенанта — за HumanGate.

Вызывает ТЕ ЖЕ db-сеттеры, что и ручные формы (единая валидация). Каждая секция —
независимо; ошибка секции не рушит остальные. Перед вызовами — set_active_tenant(tid).

Персона в v1 НЕ авто-применяется (нет однозначного сеттера «сделать активной» без риска
выдумки) — proposal.settings.persona показывается в дифе как рекомендация, донастройка
руками в разделе «ИИ-агенты».
"""
from __future__ import annotations

import db


async def apply_proposal(tenant_id, proposal: dict, sections: list[str], *,
                         actor: str, ip: str | None, user_agent: str | None) -> dict:
    """Применяет выбранные секции proposal. Возвращает {sections:[применённые], errors:[...]}."""
    db.set_active_tenant(tenant_id)
    settings = proposal.get("settings") or {}
    done: list[str] = []
    errors: list[str] = []

    if "funnel" in sections:
        try:
            fields = {k: v for k, v in (settings.get("funnel") or {}).items() if v not in (None, "")}
            errs = await db.set_funnel_config(tenant_id, fields, actor=actor, ip=ip, user_agent=user_agent)
            if errs:
                errors.append("funnel: " + "; ".join(errs))
            else:
                done.append("funnel")
        except Exception as e:  # noqa: BLE001
            errors.append(f"funnel: {e}")

    if "products" in sections:
        try:
            n = 0
            for prod in proposal.get("products") or []:
                await db.create_product_with_audit(
                    name=str(prod.get("name") or "")[:200], kind=str(prod.get("kind") or "main"),
                    price=prod.get("price"), currency=str(prod.get("currency") or "RUB"),
                    caption=(prod.get("caption") or None), link=(prod.get("link") or None),
                    file_meta=None, status="active", tenant_id=tenant_id,
                    actor=actor, ip=ip, user_agent=user_agent)
                n += 1
            done.append("products") if n else None
        except Exception as e:  # noqa: BLE001
            errors.append(f"products: {e}")

    if "triggers" in sections:
        try:
            n = 0
            for tr in settings.get("triggers") or []:
                kind = str(tr.get("kind") or "")
                val = str(tr.get("value") or "")
                if kind == "stopword" and val:
                    await db.create_tenant_trigger(
                        tenant_id, type_="stopword", action="notify", stopwords=[val],
                        intent_desc="", msg_count=None, notify_chat_id="", notify_topic_id=None,
                        reply_text="", actor=actor, ip=ip, user_agent=user_agent)
                    n += 1
            done.append("triggers") if n else None
        except Exception as e:  # noqa: BLE001
            errors.append(f"triggers: {e}")

    if "channels" in sections:
        try:
            n = 0
            for source, slug in (settings.get("channels") or {}).items():
                if slug:
                    await db.set_channel_agent(tenant_id, source, str(slug),
                                               actor=actor, ip=ip, user_agent=user_agent)
                    n += 1
            done.append("channels") if n else None
        except Exception as e:  # noqa: BLE001
            errors.append(f"channels: {e}")

    return {"sections": done, "errors": errors}
