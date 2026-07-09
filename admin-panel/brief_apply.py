"""Применение черновика оркестратора к настройкам тенанта — за HumanGate.

Вызывает ТЕ ЖЕ db-сеттеры, что и ручные формы (единая валидация). Каждая секция —
независимо; ошибка секции не рушит остальные. Перед вызовами — set_active_tenant(tid).

Персона и триггеры/анонсы в v1 НЕ авто-применяются — показываются в дифе как рекомендация:
- персона: нет однозначного сеттера «сделать активной» без риска выдумки → донастройка
  руками в разделе «ИИ-агенты»;
- триггеры: brief-«анонсы» проактивны, а tenant_triggers моделирует реактивные
  стоп-слова/интенты с ОБЯЗАТЕЛЬНЫМ чатом уведомлений (в брифе его нет) → настройка
  руками в разделе «Триггеры».
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

import config
import db
import security
from shared.leadmagnet import _is_email, _is_inn


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
            # 152-ФЗ: settings.funnel НИКОГДА не содержит funnel_enabled (оркестратор его
            # не кладёт) — валидатор set_funnel_config при выключенной воронке пропускает
            # проверку реквизитов (ранний return []), и operator_inn/operator_email ушли бы
            # в tenant_settings без проверки формата, а оттуда — в публичный /legal/{slug}.
            # Точечно проверяем те же реквизиты, что и ручная форма, ДО записи.
            requisite_errs: list[str] = []
            inn_val = fields.get("operator_inn")
            if inn_val and not _is_inn(str(inn_val)):
                requisite_errs.append("ИНН оператора невалиден")
            email_val = fields.get("operator_email")
            if email_val and not _is_email(str(email_val)):
                requisite_errs.append("email оператора невалиден")

            if requisite_errs:
                errors.append("funnel: " + "; ".join(requisite_errs))
            else:
                errs = await db.set_funnel_config(
                    tenant_id, fields, actor=actor, ip=ip, user_agent=user_agent)
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
                # Ссылка из LLM/оркестратора недоверенная — та же схема-валидация, что у
                # ручной формы product_save (security.validate_target_url): отсекает
                # не-http/https, javascript:/data:, protocol-relative //, control-символы, длину.
                link_val = security.validate_target_url(
                    str(prod.get("link") or "").strip() or None, schemes=config.LINK_URL_SCHEMES)
                if not link_val:
                    errors.append(
                        f"products: продукт «{name_val}» — нужна корректная ссылка, добавьте вручную в каталоге")
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

    # Триггеры/анонсы НЕ авто-применяются (см. докстринг модуля): brief-«анонсы»
    # проактивны, а tenant_triggers требует реактивный тип + чат уведомлений, которого
    # в брифе нет. Показываются в дифе (brief_detail.html) как рекомендация.

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
