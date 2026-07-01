"""DaData-провайдер обогащения по ЕГРЮЛ/ЕГРИП (per-lookup). Паттерн mailer.py:
is_configured() + graceful-degrade. Телефоны/email/закрытые категории вырезаются
ДО возврата (комплаенс §8 спеки). find-party — точный поиск по ИНН/ОГРН; suggest —
интерактивный ручной поиск по названию (оферта DaData 4.2.1 запрещает автообработку suggest).
HTTP — stdlib urllib в to_thread (без новых зависимостей; для per-lookup достаточно)."""
from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone

import config

log = logging.getLogger("dadata")

_FIND_URL = "https://suggestions.dadata.ru/suggestions/api/4_1/rs/findById/party"
_SUGGEST_URL = "https://suggestions.dadata.ru/suggestions/api/4_1/rs/suggest/party"
# Аллоулист безопасных реестровых полей для raw. Минимизация ПДн (152-ФЗ): НЕ храним
# phones/emails/fio/founders/managers/management; для ИП — и адрес места жительства.
_RAW_ALLOWED = ("inn", "kpp", "ogrn", "ogrn_date", "opf", "type",
                "okved", "okveds", "state")  # name/address добавляются в raw ТОЛЬКО для ЮЛ


def is_configured() -> bool:
    """DaData настроен для реальных запросов (нужны токен + секрет для find-party)?"""
    return bool(config.DADATA_API_KEY and config.DADATA_SECRET_KEY)


@dataclass
class ProspectCard:
    inn: str
    subject_type: str            # 'legal' | 'individual'
    kpp: str | None = None
    ogrn: str | None = None
    name_short: str | None = None
    name_full: str | None = None
    opf: str | None = None
    okved: str | None = None
    okved_name: str | None = None
    okveds: list | None = None
    address: str | None = None
    region: str | None = None
    city: str | None = None
    status: str | None = None
    registration_date: str | None = None   # ISO 'YYYY-MM-DD' | None
    liquidation_date: str | None = None
    management: dict | None = None
    raw: dict = field(default_factory=dict)  # санитизированный (без контактов)


def _sanitize(data: dict, stype: str) -> dict:
    """raw ТОЛЬКО из безопасных реестровых полей (аллоулист). Вырезаются phones/emails/
    fio/founders/managers/management (ПДн физлиц). Для ИП в raw НЕ кладём ни адрес места
    жительства, ни наименование (=ФИО ИП): ФИО хранится лишь в колонке name_full как
    наименование субъекта, дублировать его в jsonb-raw не нужно (минимизация ПДн)."""
    raw = {k: data[k] for k in _RAW_ALLOWED if k in data}
    if stype == "legal":
        if "name" in data:
            raw["name"] = data["name"]          # наименование ЮЛ — не ПДн
        if isinstance(data.get("address"), dict):
            raw["address"] = data["address"]    # юр.адрес ЮЛ — не ПДн
    return raw


def _epoch_ms_to_date(v) -> str | None:
    if not v:
        return None
    try:
        return datetime.fromtimestamp(int(v) / 1000, tz=timezone.utc).date().isoformat()
    except (ValueError, TypeError, OSError):
        return None


def _main_okved_name(okveds) -> str | None:
    if not okveds:
        return None
    for o in okveds:
        if o.get("main"):
            return o.get("name")
    return okveds[0].get("name")


def _parse_party(data: dict) -> ProspectCard:
    """DaData data-объект → ProspectCard (санитизированный, без телефонов/email)."""
    name = data.get("name") or {}
    opf = data.get("opf") or {}
    addr = data.get("address") if isinstance(data.get("address"), dict) else {}
    addr_data = addr.get("data") or {}
    state = data.get("state") or {}
    # fail-safe: ЮЛ-режим (хранение адреса) ТОЛЬКО при явном type='LEGAL'; всё иное
    # (INDIVIDUAL / неизвестный / отсутствует / иной регистр) → individual (минимум ПДн).
    stype = "legal" if str(data.get("type") or "").upper() == "LEGAL" else "individual"
    return ProspectCard(
        inn=data.get("inn") or "",
        subject_type=stype,
        kpp=data.get("kpp"),
        ogrn=data.get("ogrn"),
        name_short=(name.get("short_with_opf") or name.get("short") or data.get("value")),
        name_full=(name.get("full_with_opf") or name.get("full")),
        opf=opf.get("short"),
        okved=data.get("okved"),
        okved_name=_main_okved_name(data.get("okveds")),
        okveds=data.get("okveds"),
        address=(addr.get("value") if stype == "legal" else None),  # ИП: адрес места жительства НЕ храним
        region=addr_data.get("region"),
        city=addr_data.get("city") or addr_data.get("settlement"),
        status=state.get("status"),
        registration_date=_epoch_ms_to_date(state.get("registration_date")),
        liquidation_date=_epoch_ms_to_date(state.get("liquidation_date")),
        management=(data.get("management") if stype == "legal" else None),
        raw=_sanitize(data, stype),
    )


def _post(url: str, payload: dict) -> dict:
    """Синхронный POST (в to_thread). stdlib — без новых зависимостей."""
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    req.add_header("Authorization", f"Token {config.DADATA_API_KEY}")
    req.add_header("X-Secret", config.DADATA_SECRET_KEY)
    with urllib.request.urlopen(req, timeout=config.DADATA_TIMEOUT_SEC) as resp:
        return json.loads(resp.read().decode("utf-8"))


async def find_party(query: str) -> ProspectCard | None:
    """Точный поиск по ИНН/ОГРН (findById). None — не настроено/не найдено/ошибка."""
    if not is_configured():
        return None
    q = (query or "").strip()
    if not q or len(q) > 300:
        return None
    try:
        resp = await asyncio.to_thread(_post, _FIND_URL, {"query": q})
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as e:
        log.warning("DaData find-party ошибка: %s", e)
        return None
    sugg = (resp or {}).get("suggestions") or []
    return _parse_party(sugg[0].get("data") or {}) if sugg else None


async def suggest_party(query: str, count: int = 7) -> list[dict]:
    """Интерактивный поиск по названию (suggest). Телефоны/email тут и так null."""
    if not is_configured():
        return []
    q = (query or "").strip()
    if not q or len(q) > 300:
        return []
    try:
        resp = await asyncio.to_thread(_post, _SUGGEST_URL, {"query": q, "count": max(1, min(count, 20))})
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as e:
        log.warning("DaData suggest ошибка: %s", e)
        return []
    out = []
    for s in (resp or {}).get("suggestions") or []:
        d = s.get("data") or {}
        addr = d.get("address") if isinstance(d.get("address"), dict) else {}
        out.append({
            "inn": d.get("inn") or "",
            "name": s.get("value") or "",
            "city": (addr.get("data") or {}).get("city") or "",
            "status": (d.get("state") or {}).get("status") or "",
        })
    return out
