#!/usr/bin/env python3
"""Смоук РЕНДЕРА партнёрских шаблонов на реальных данных (risuy_dev):
  PARTNER_TPL_SMOKE_DSN="...risuy_dev..." PYTHONPATH=admin-panel:. \
    /Users/konstantin/Downloads/risuy-ecosystem/.venv-smoke/bin/python \
    scripts/partner_templates_smoke.py

🔴 Зачем он есть. Все прежние смоуки проверяли данные и решения, но ни один не РИСОВАЛ
страницу. Из-за этого прошло мимо: `get_partner` перечислял колонки поимённо и отстал от
схемы, карточка показывала `partner.rate_percent`, и заведение партнёра падало 500 прямо
у владельца — на первом же живом действии.

Здесь контекст собирается ТЕМИ ЖЕ функциями db, что и в маршрутах, а затем шаблон
рендерится по-настоящему. Любое поле, которого шаблон ждёт, а запрос не отдаёт, валит
смоук здесь, а не у человека.
"""
import asyncio
import os
import sys
from decimal import Decimal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "admin-panel"))
os.environ.setdefault("DATABASE_URL", "postgresql://x/y")
os.environ.setdefault("SESSION_SECRET", "smoke-secret-padding-0123456789abcdef")
os.environ.setdefault("ADMIN_USERNAME", "smoke")
os.environ.setdefault("ADMIN_PASSWORD_HASH", "$argon2id$v=19$m=65536,t=3,p=4$c21va2U$c21va2U")

import asyncpg  # noqa: E402
from jinja2 import Environment, FileSystemLoader  # noqa: E402

import config  # noqa: E402
import db  # noqa: E402

DSN = os.environ.get("PARTNER_TPL_SMOKE_DSN")
if not DSN or "/risuy_dev" not in DSN.split("?")[0]:
    raise SystemExit("Задайте PARTNER_TPL_SMOKE_DSN на risuy_dev")

FAILS = []
PSELLER = "ШАБЛОН Продавец"
PMENTOR = "ШАБЛОН Наставник"
TNAME = "ШАБЛОН Клиент"
TPL_DIR = os.path.join(ROOT, "admin-panel", "templates")


def check(name, cond, detail=""):
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


class StubSession:
    """Минимальная сессия для base.html: ровно те поля, которые он читает."""

    def __init__(self, role="admin", is_platform=True):
        self.role = role
        self.actor = "smoke@example.test"
        self.csrf_token = "smoke-csrf"
        self.active_tenant_id = None
        self.active_tenant_name = None
        self._is_platform = is_platform

    @property
    def is_platform(self):
        return self._is_platform


def env():
    e = Environment(loader=FileSystemLoader(TPL_DIR))
    # Глобали, которые app.py кладёт в окружение шаблонов.
    e.globals["NAV_TITLES"] = {}
    e.globals["service_site_url"] = "https://info.example.test"
    return e


async def _cleanup(c):
    await c.execute("""delete from partner_accruals where partner_id in
                       (select id from partners where name in ($1,$2))""", PSELLER, PMENTOR)
    await c.execute("""delete from partner_payouts where partner_id in
                       (select id from partners where name in ($1,$2))""", PSELLER, PMENTOR)
    await c.execute("""delete from implementations where tenant_id in
                       (select id from tenants where name = $1)""", TNAME)
    await c.execute("update tenants set partner_id = null where name = $1", TNAME)
    await c.execute("update partners set parent_id = null where name in ($1,$2)", PSELLER, PMENTOR)
    await c.execute("delete from tenants where name = $1", TNAME)
    await c.execute("delete from partners where name in ($1,$2)", PSELLER, PMENTOR)


async def main():
    db.pool = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    e = env()
    try:
        async with db.pool.acquire() as c:
            await _cleanup(c)
            mentor_id = await c.fetchval(
                "insert into partners (name, ref_code) values ($1,$2) returning id",
                PMENTOR, "tplmnt01")
            seller_id = await c.fetchval(
                "insert into partners (name, ref_code, parent_id, rate_percent) "
                "values ($1,$2,$3,15) returning id", PSELLER, "tplsel01", mentor_id)
            tenant_id = await c.fetchval(
                "insert into tenants (name, slug, status, partner_id) "
                "values ($1,$2,'active',$3) returning id", TNAME, "tpl-client", seller_id)
            impl = await db.create_implementation(
                tenant_id, "ШАБЛОН Внедрение", Decimal("60000"), note=None, actor="smoke")
            await db.mark_implementation_paid_by_payment(
                "pay-tpl-smoke", implementation_id=impl, expected_amount="60000.00")
            await db.create_partner_payout(seller_id, Decimal("1000"), method="СБП",
                                           note=None, actor="smoke")

            owner = StubSession()
            partner_sess = StubSession(role="partner", is_platform=False)

            print("1. Карточка партнёра у владельца (та, что падала при заведении):")
            partner = await db.get_partner(seller_id)
            ctx = {
                "partner": partner,
                "tenants": await db.list_partner_tenants(seller_id),
                "accruals": await db.partner_accruals_for(seller_id),
                "payouts": await db.partner_payouts_for(seller_id),
                "totals": (await db.partner_totals([seller_id]))[str(seller_id)],
                "all_partners": await db.list_partners(),
                "base_url": "https://bot.example.test",
                "panel_base": "https://panel.example.test",
                "invite": None, "invite_sent": False, "saved": "created", "err": None,
                "csrf_token": "x", "session": owner, "active": "partners",
            }
            try:
                html = e.get_template("partner_detail.html").render(**ctx)
                check("карточка рендерится", "15" in html, "ставка не попала в разметку")
            except Exception as ex:  # noqa: BLE001
                check("карточка рендерится", False, f"{type(ex).__name__}: {ex}")

            print("2. Список партнёров:")
            partners = await db.list_partners()
            try:
                e.get_template("partners.html").render(
                    partners=partners, base_url="https://bot.example.test",
                    totals=await db.partner_totals([p["id"] for p in partners]),
                    saved=None, err=None, csrf_token="x", session=owner, active="partners")
                check("список рендерится", True)
            except Exception as ex:  # noqa: BLE001
                check("список рендерится", False, f"{type(ex).__name__}: {ex}")

            print("3. Кабинет партнёра и его вкладки:")
            data = await db.partner_cabinet_data(seller_id)
            try:
                e.get_template("partner_cabinet.html").render(
                    partner=partner, base_url="https://bot.example.test",
                    session=partner_sess, csrf_token="x", active="partner", **data)
                check("кабинет рендерится", True)
            except Exception as ex:  # noqa: BLE001
                check("кабинет рендерится", False, f"{type(ex).__name__}: {ex}")

            team, earned = await db.partner_team_data(mentor_id)
            try:
                e.get_template("partner_team.html").render(
                    partner=await db.get_partner(mentor_id), team=team, earned=earned,
                    session=partner_sess, csrf_token="x", active="partner_team")
                check("«Мои партнёры» рендерится", True)
            except Exception as ex:  # noqa: BLE001
                check("«Мои партнёры» рендерится", False, f"{type(ex).__name__}: {ex}")

            try:
                html = e.get_template("partner_guide.html").render(
                    partner=partner, price=config.IMPLEMENTATION_PRICE_RUB,
                    rate=Decimal(partner["rate_percent"]),
                    mentor_rate=config.MENTOR_RATE_PERCENT,
                    mentor_months=config.MENTOR_BONUS_MONTHS,
                    min_plan=Decimal((config.SERVICE_PLANS.get("econom") or {}).get("price") or 0),
                    session=partner_sess, csrf_token="x", active="partner_guide")
                check("обучение рендерится и считает по ЕГО ставке", "9000 ₽" in html,
                      "15% от 60000 должно дать 9000")
            except Exception as ex:  # noqa: BLE001
                check("обучение рендерится", False, f"{type(ex).__name__}: {ex}")

            print("4. Внедрения:")
            try:
                e.get_template("implementations.html").render(
                    items=await db.list_implementations(), tenants=await db.list_tenants_min(),
                    default_price=config.IMPLEMENTATION_PRICE_RUB,
                    partner_rate=config.PARTNER_RATE_PERCENT, yookassa_enabled=True,
                    saved=None, err=None, csrf_token="x", session=owner, active="implementations")
                check("внедрения рендерятся", True)
            except Exception as ex:  # noqa: BLE001
                check("внедрения рендерятся", False, f"{type(ex).__name__}: {ex}")

            await _cleanup(c)
    finally:
        await db.pool.close()

    print()
    if FAILS:
        print(f"ПРОВАЛЕНО: {len(FAILS)} — {FAILS}")
        sys.exit(1)
    print("ВСЁ ЗЕЛЁНОЕ")


asyncio.run(main())
