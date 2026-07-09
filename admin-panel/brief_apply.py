"""Применение черновика оркестратора к настройкам тенанта — за HumanGate.

Вызывает ТЕ ЖЕ db-сеттеры, что и ручные формы (единая валидация). Каждая секция —
независимо; ошибка секции не рушит остальные. Перед вызовами — set_active_tenant(tid).

Персона в v1 НЕ авто-применяется (нет однозначного сеттера «сделать активной» без риска
выдумки) — proposal.settings.persona показывается в дифе как рекомендация, донастройка
руками в разделе «ИИ-агенты».
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

import config
import db


def _parse_price(raw) -> tuple[Decimal | None, bool]:
    """Зеркало admin-panel/app.py:_parse_price — та же валидация цены, что у ручной формы
    product_save. raw может быть числом/строкой из JSON черновика. Пусто/None → (None, True)
    — цена опциональна (бесплатный продукт). Отрицательную, слишком большую (numeric(12,2):
    целая часть ≤ 10 цифр) и непарсящуюся строку отвергаем (ok=False)."""
    if raw is None:
        return None, True
    s = str(raw).strip()
    if not s:
        return None, True
    s = s.replace(" ", "").replace(" ", "").replace(",", ".")
    try:
        val = Decimal(s)
    except (InvalidOperation, ValueError):
        return None, False
    if val < 0:
        return None, False
    val = val.quantize(Decimal("0.01"))
    if val >= Decimal("10000000000"):
        return None, False
    return val, True


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
        n = 0
        for prod in proposal.get("products") or []:
            name_val = str(prod.get("name") or "")[: config.PRODUCT_NAME_MAX_LEN]
            try:
                # Инвариант «файл ИЛИ ссылка»: у brief-продуктов файла нет (file_meta=None),
                # значит валиден только вариант с непустой link. Оркестратор ссылку не
                # выдумывает — продукт без link помечается оператору для ручного добавления.
                link_val = str(prod.get("link") or "").strip() or None
                if not link_val:
                    errors.append(
                        f"products: продукт «{name_val}» — нужна ссылка, добавьте вручную в каталоге")
                    continue

                price_val, price_ok = _parse_price(prod.get("price"))
                if not price_ok:
                    errors.append(f"products: продукт «{name_val}» — цена указана неверно")
                    continue

                caption_val = str(prod.get("caption") or "").strip()[: config.PRODUCT_CAPTION_MAX_LEN] or None

                await db.create_product_with_audit(
                    name=name_val, kind=str(prod.get("kind") or "main"),
                    price=price_val, currency=str(prod.get("currency") or "RUB"),
                    caption=caption_val, link=link_val,
                    file_meta=None, status="active", tenant_id=tenant_id,
                    actor=actor, ip=ip, user_agent=user_agent)
                n += 1
            except Exception as e:  # noqa: BLE001
                errors.append(f"products: продукт «{name_val}» — {e}")
        if n:
            done.append("products")

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
            if n:
                done.append("triggers")
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
            if n:
                done.append("channels")
        except Exception as e:  # noqa: BLE001
            errors.append(f"channels: {e}")

    return {"sections": done, "errors": errors}
