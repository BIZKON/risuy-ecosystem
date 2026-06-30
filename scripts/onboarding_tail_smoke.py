#!/usr/bin/env python3
"""Render-смоук «онбординг-хвост»: channels help_card + Task 8 (ролевой сплит инфра-сообщений
+ кнопки поддержки в тупиковых empty-states). Чистый Jinja (без БД/HTTP).

Покрывает:
  • channels.html — help_card «Как подключить канал» (показ/скрытие/dismiss-форма; текст ссылается
    на карточку «Telegram», не на «поле ниже»), ссылка поддержки в тупиках, скрытие на платформе.
  • data_protection.html — тупик «кабинет не привязан»: тенанту кнопка поддержки, платформе → /tenants.
  • keys.html / knowledge.html — vault/embedder off: тенант видит задачное RU + поддержку (БЕЗ
    VAULT_MASTER_KEY / EMBEDDER_URL), платформа видит техжаргон.
  • subscription.html — ЮKassa off: тенант → задачное RU + поддержка (БЕЗ YOOKASSA_*), платформа → жаргон.
  • products.html — тупики «кабинет не привязан»/«хранилище» → кликабельная ссылка поддержки.

undefined=Undefined (дефолт jinja2, как прод Jinja2Templates) — честный детектор undefined-обращений
(ревью-находка: ChainableUndefined маскировал бы будущие незащищённые session.* → 500 в проде).

  PYTHONPATH=. ./.venv-smoke/bin/python scripts/onboarding_tail_smoke.py
"""
import os
import sys

from jinja2 import Environment, FileSystemLoader, Undefined, select_autoescape

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
env = Environment(
    loader=FileSystemLoader(os.path.join(ROOT, "admin-panel", "templates")),
    autoescape=select_autoescape(["html", "xml"]),
    undefined=Undefined,
)
env.globals["asset_version"] = ""
env.globals["service_site_url"] = ""

FAILS = []
def check(n, c):  # noqa: E302
    print(f"  {'OK ' if c else 'FAIL'} {n}")
    if not c:
        FAILS.append(n)


SUPPORT = "https://t.me/profysales"


class Sess:
    """Минимальный аналог auth.Session для рендера (атрибуты, которые читают шаблоны/base.html)."""
    def __init__(self, is_platform=False, tid=1, name="ООО Ромашка"):
        self.is_platform = is_platform
        self.active_tenant_id = tid
        self.active_tenant_name = name
        self.csrf_token = "csrf"
        self.actor = "operator@example.com"
        self.username = "operator@example.com"


def render(tpl, **ctx):
    base = dict(csrf_token="csrf", active="x")
    return env.get_template(tpl).render(**{**base, **ctx})


# ─────────────────────────── channels.html ───────────────────────────
print("channels.html")
TEN = Sess(is_platform=False)
ch_ten = dict(is_platform=False, vault_enabled=True, channel_cards=[], value_max=4000,
              support_url=SUPPORT, notifier_username="ConversionConsultant_bot",
              saved_flash=False, err=None, session=TEN)

html = render("channels.html", has_tenant=True, help_dismissed=False, **ch_ten)
check("help_card «Как подключить канал» показан тенанту", "Как подключить канал" in html)
check("dismiss-форма с ключом channels", 'action="/onboarding/dismiss-help"' in html and "help_dismissed__channels" in html)
check("BotFather-инструкция в теле", "@BotFather" in html)
check("текст ссылается на карточку «Telegram», без «поле … ниже»", "карточку «Telegram»" in html and "поле «Telegram»" not in html)

html = render("channels.html", has_tenant=True, help_dismissed=True, **ch_ten)
check("help_card скрыт при dismiss", "Как подключить канал" not in html)

html = render("channels.html", has_tenant=False, help_dismissed=True, **ch_ten)
check("тупик «кабинет не привязан» → ссылка поддержки", "Напишите в поддержку" in html and SUPPORT in html)

ch_novault = dict(ch_ten); ch_novault["vault_enabled"] = False
html = render("channels.html", has_tenant=True, help_dismissed=True, **ch_novault)
check("тупик «хранилище не настроено» (клиент) → поддержка, без VAULT_MASTER_KEY", "Напишите в поддержку" in html and "VAULT_MASTER_KEY" not in html)

PLAT = Sess(is_platform=True)
html = render("channels.html", is_platform=True, vault_enabled=False, channel_cards=[], value_max=4000,
              notifier_username="x", saved_flash=False,
              attribution={"total_leads": 0, "total_converted": 0, "overall_pct": 0, "rows": []},
              clicks=0, runtime={"gate_channel_url": ""}, bot_username="", deep_links=[],
              channel_staff=[], persona_options=[], saved_persona=False, err=None,
              help_dismissed=True, session=PLAT)
check("платформа — help_card скрыт", "Как подключить канал" not in html)
check("платформа — VAULT_MASTER_KEY оставлен (это владелец)", "VAULT_MASTER_KEY" in html)


# ─────────────────────── data_protection.html ───────────────────────
print("data_protection.html")
html = render("data_protection.html", session=TEN, has_tenant=False, support_url=SUPPORT, lead_count=0, row_cap=5000)
check("тенант, тупик → кнопка «Написать в поддержку»", "Написать в поддержку" in html and SUPPORT in html)
check("тенант, тупик → нет /tenants", "/tenants" not in html)

html = render("data_protection.html", session=PLAT, has_tenant=False, support_url=SUPPORT, lead_count=0, row_cap=5000)
check("платформа, тупик → «Клиент не выбран» + /tenants", "Клиент не выбран" in html and "/tenants" in html)

html = render("data_protection.html", session=TEN, has_tenant=True, support_url=SUPPORT, lead_count=3, row_cap=5000)
check("has_tenant → рабочие карточки (Обезличенная база)", "Обезличенная база" in html)


# ────────────────────────────── keys.html ───────────────────────────
print("keys.html")
KEYS_BASE = dict(secret_keys=[], secrets_meta=[], known_keys=set(), saved_flash=False, deleted_flash=False, err=None, support_url=SUPPORT)
html = render("keys.html", session=Sess(is_platform=False, tid=1), vault_enabled=False, **KEYS_BASE)
check("тенант, vault off → задачное RU без VAULT_MASTER_KEY", "временно недоступно" in html and "VAULT_MASTER_KEY" not in html)
check("тенант, vault off → ссылка поддержки", SUPPORT in html)

html = render("keys.html", session=Sess(is_platform=True, tid=1), vault_enabled=False, **KEYS_BASE)
check("платформа, vault off → техжаргон VAULT_MASTER_KEY", "VAULT_MASTER_KEY" in html)

html = render("keys.html", session=Sess(is_platform=False, tid=1), vault_enabled=True, **KEYS_BASE)
check("vault on → форма записи ключа", "Записать / заменить ключ" in html)


# ───────────────────────────── knowledge.html ───────────────────────
print("knowledge.html")
KB_BASE = dict(err=None, kb_saved=0, show_global_toggle=False, kb_enabled=False, kb_docs=[],
               kb_roles={}, kb_max_mb=10, support_url=SUPPORT, help_dismissed=True)
html = render("knowledge.html", session=Sess(is_platform=False, tid=1), has_tenant=True, embedder_enabled=False, **KB_BASE)
check("тенант, embedder off → задачное RU без EMBEDDER_URL", "временно недоступна" in html and "EMBEDDER_URL" not in html)
check("тенант, embedder off → ссылка поддержки", SUPPORT in html)

html = render("knowledge.html", session=Sess(is_platform=True, tid=1), has_tenant=True, embedder_enabled=False, **KB_BASE)
check("платформа, embedder off → техжаргон EMBEDDER_URL", "EMBEDDER_URL" in html)

html = render("knowledge.html", session=Sess(is_platform=False, tid=1), has_tenant=True, embedder_enabled=True, **KB_BASE)
check("embedder on → форма загрузки файла", 'action="/knowledge/upload"' in html)


# ─────────────────────────── subscription.html ──────────────────────
print("subscription.html")
SUB_BASE = dict(sub={"plan_key": None}, plans=[], invoices=[], receipt_required=False,
                contact_url="", period_days=30, paid_flash=False, canceled_flash=False,
                detached_flash=False, err=None, economics=None, support_url=SUPPORT)
html = render("subscription.html", session=Sess(is_platform=False), yookassa_enabled=False, **SUB_BASE)
check("тенант, ЮKassa off → задачное RU без YOOKASSA_*", "временно недоступна" in html and "YOOKASSA" not in html)
check("тенант, ЮKassa off → ссылка поддержки", SUPPORT in html)

html = render("subscription.html", session=Sess(is_platform=True), yookassa_enabled=False, **SUB_BASE)
check("платформа, ЮKassa off → техжаргон YOOKASSA_SHOP_ID", "YOOKASSA_SHOP_ID" in html)


# ───────────────────────────── products.html ────────────────────────
print("products.html")
PROD_BASE = dict(products=[], saved=False, archived=False, lm_flash=False, err=None,
                 kind_labels={}, active_lead_magnet=None, lead_magnet_candidates=[],
                 kassa={"connected": False, "fields": []}, kassa_webhook_url="",
                 value_max=4000, kassa_saved=False, support_url=SUPPORT)
html = render("products.html", session=Sess(is_platform=False, tid=None), has_tenant=False, vault_enabled=True, **PROD_BASE)
check("products тупик «кабинет не привязан» → ссылка поддержки", "Напишите в поддержку" in html and SUPPORT in html)

html = render("products.html", session=Sess(is_platform=False, tid=1), has_tenant=True, vault_enabled=False, **PROD_BASE)
check("products тупик «хранилище» → ссылка поддержки", "Напишите в поддержку" in html and SUPPORT in html)


print("\n" + ("ВСЕ ОК" if not FAILS else "ПРОВАЛЫ: " + ", ".join(FAILS)))
sys.exit(1 if FAILS else 0)
