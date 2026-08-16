# Партнёрский кабинет — план реализации

> **Для агентов-исполнителей:** обязательный суб-скилл — `superpowers:subagent-driven-development`
> или `superpowers:executing-plans`. Шаги отмечаются чекбоксами (`- [ ]`).

**Цель:** партнёр видит свои начисления и выплаты в собственном кабинете, а начисление
происходит в момент прихода денег — без участия человека.

**Архитектура:** три новые таблицы (`partner_accruals`, `partner_payouts`,
`partner_invites`) плюс семь колонок к существующей `partners`. Атрибуция и платежи уже
есть в базе, поэтому строим только денежный слой. Начисление врезается в две уже
идемпотентные функции отметки оплаты и ходит через SECURITY DEFINER, потому что в
платформенном контуре RLS иначе скрывает платформенного партнёра от вебхука тенанта.

**Стек:** Python 3.12, FastAPI, Jinja2, asyncpg, PostgreSQL с RLS, ruff.

**Спека:** `docs/superpowers/specs/2026-08-16-partner-cabinet-design.md` — читать целиком
перед началом. Ниже реализуется она, а не собственные представления о партнёрках.

## Глобальные ограничения

- **Только русский** — код, комментарии, docstring, UI, коммиты, доки. Латиница только в
  идентификаторах, именах файлов, ключах и SQL.
- **Ставка продавца 20%, наставника 5% сверх, глубина ровно 1, срок 12 месяцев** от
  `joined_at` подопечного. Значения — константы в `admin-panel/config.py`, не литералы.
- **Наставники только в платформенном контуре.** `PARTNER_MENTORS_TENANT_ENABLED = False`.
- **Миграции идемпотентные, expand-contract:** сначала `risuy_dev`, expand до кода,
  contract после деплоя кода. `db:generate` в проекте нет — SQL пишется руками.
- **Прод-DDL, push (= деплой) и правка прод-env — только по явному «да» владельца.**
  `auto_deploy=True`: push в `main` публикует прод.
- **Смоуки вместо pytest** — в проекте 137 смоук-скриптов и ни одного pytest. Следуем
  сложившемуся: `scripts/<имя>_smoke.py`, функция `check(name, cond, detail)`, список
  `FAILS`, выход `sys.exit(1)` при непустом `FAILS`.
- **Интерпретатор для смоуков:** `/Users/konstantin/Downloads/risuy-ecosystem/.venv-smoke/bin/python`
  (в worktree своего venv нет; в этом уже стоит `asyncpg`). Ставить пакеты не нужно.
- **Деньги — `Decimal`**, никогда `float`. В базе `numeric(12,2)`, asyncpg отдаёт `Decimal`.

---

### Задача 1: Константы и чистые расчёты

Ядро денег без базы. Отдельная задача потому, что здесь чужие деньги: ошибка не падает,
а тихо занижает выплату, и обнаруживается, когда партнёр придёт спорить.

**Файлы:**
- Изменить: `admin-panel/config.py` (добавить блок констант в конец)
- Создать: `admin-panel/partner_money.py`
- Создать: `scripts/partner_money_calc_smoke.py`

**Интерфейсы:**
- Отдаёт наружу: `PartnerNode(id, parent_id, joined_at, rate_percent)`,
  `Accrual(partner_id, level, rate_percent, amount_rub, reason)`,
  `accruals_for_payment(*, amount_rub, seller, mentor, at, mentors_enabled=True) -> list[Accrual]`,
  `mentor_still_earns(seller_joined_at, at) -> bool`,
  `partner_balance(accrued_rows, payout_rows) -> tuple[Decimal, Decimal, Decimal]`,
  `would_create_cycle(partner_id, parent_id, parent_of) -> bool`,
  `round_rub(value) -> Decimal`.
  Задачи 3, 5, 6 и 7 пользуются этими именами.

- [ ] **Шаг 1: Написать падающий смоук**

Создать `scripts/partner_money_calc_smoke.py`:

```python
#!/usr/bin/env python3
"""Смоук чистых расчётов партнёрской программы — БЕЗ базы:
  PYTHONPATH=admin-panel:. \
    /Users/konstantin/Downloads/risuy-ecosystem/.venv-smoke/bin/python \
    scripts/partner_money_calc_smoke.py
"""
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "admin-panel"))
os.environ.setdefault("DATABASE_URL", "postgresql://x/y")
os.environ.setdefault("SESSION_SECRET", "smoke-secret-padding-0123456789abcdef")
os.environ.setdefault("ADMIN_USERNAME", "smoke")
os.environ.setdefault("ADMIN_PASSWORD_HASH", "$argon2id$v=19$m=65536,t=3,p=4$c21va2U$c21va2U")

import partner_money as pm  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


NOW = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)


def node(pid, parent=None, joined=NOW, rate="20"):
    return pm.PartnerNode(id=pid, parent_id=parent, joined_at=joined, rate_percent=Decimal(rate))


print("1. Продавец получает 20% от платежа:")
seller = node("s1")
rows = pm.accruals_for_payment(amount_rub=Decimal("7500"), seller=seller, mentor=None, at=NOW)
check("одна строка", len(rows) == 1, f"строк={len(rows)}")
check("1500.00 продавцу", rows[0].amount_rub == Decimal("1500.00"), str(rows[0].amount_rub))
check("уровень 0", rows[0].level == 0)
check("причина sale", rows[0].reason == "sale")

print("2. Ставка берётся из партнёра, а не из конфига:")
rows = pm.accruals_for_payment(amount_rub=Decimal("7500"), seller=node("s1", rate="30"),
                               mentor=None, at=NOW)
check("2250.00 при ставке 30", rows[0].amount_rub == Decimal("2250.00"), str(rows[0].amount_rub))
check("ставка записана в строку", rows[0].rate_percent == Decimal("30"))

print("3. Наставник получает 5% СВЕРХ, продавец не теряет:")
seller = node("s1", parent="m1")
mentor = node("m1")
rows = pm.accruals_for_payment(amount_rub=Decimal("7500"), seller=seller, mentor=mentor, at=NOW)
check("две строки", len(rows) == 2, f"строк={len(rows)}")
check("продавцу по-прежнему 1500.00", rows[0].amount_rub == Decimal("1500.00"))
check("наставнику 375.00", rows[1].amount_rub == Decimal("375.00"), str(rows[1].amount_rub))
check("уровень 1", rows[1].level == 1)
check("причина mentor", rows[1].reason == "mentor")

print("4. Наставник наставника не получает ничего:")
rows = pm.accruals_for_payment(amount_rub=Decimal("7500"), seller=node("s1", parent="m1"),
                               mentor=node("m2"), at=NOW)
check("чужой наставник не начисляется", len(rows) == 1, f"строк={len(rows)}")

print("5. Срок наставнических — 12 месяцев от регистрации подопечного:")
joined = datetime(2025, 8, 16, 12, 0, tzinfo=timezone.utc)
check("день в день ещё идёт", pm.mentor_still_earns(joined, NOW))
check("на следующий день прекращается",
      not pm.mentor_still_earns(joined, NOW + timedelta(days=1)))
check("за месяц до — идёт", pm.mentor_still_earns(joined, NOW - timedelta(days=30)))
rows = pm.accruals_for_payment(amount_rub=Decimal("7500"), seller=node("s1", parent="m1", joined=joined),
                               mentor=node("m1"), at=NOW + timedelta(days=1))
check("после срока строки наставника нет", len(rows) == 1, f"строк={len(rows)}")

print("6. В тенантском контуре наставнических нет вовсе:")
rows = pm.accruals_for_payment(amount_rub=Decimal("7500"), seller=node("s1", parent="m1"),
                               mentor=node("m1"), at=NOW, mentors_enabled=False)
check("только продавец", len(rows) == 1, f"строк={len(rows)}")

print("7. Баланс = начислено − выплачено, сторно уменьшает начисленное:")
accrued, paid, due = pm.partner_balance(
    [Decimal("1500.00"), Decimal("375.00"), Decimal("-1500.00")],
    [Decimal("200.00"), Decimal("100.00")])
check("начислено 375.00", accrued == Decimal("375.00"), str(accrued))
check("выплачено 300.00", paid == Decimal("300.00"), str(paid))
check("к выплате 75.00", due == Decimal("75.00"), str(due))

print("8. Цикл в дереве наставников отбивается:")
parent_of = {"a": None, "b": "a"}
check("A не может стать подопечным B", pm.would_create_cycle("a", "b", parent_of))
check("сам себе наставник запрещён", pm.would_create_cycle("a", "a", parent_of))
check("нормальная привязка проходит", not pm.would_create_cycle("c", "a", parent_of))
check("без наставника цикла нет", not pm.would_create_cycle("c", None, parent_of))

print("9. Копейки округляются вверх по половине:")
rows = pm.accruals_for_payment(amount_rub=Decimal("3750"), seller=node("s1"), mentor=None, at=NOW)
check("750.00 с Эконома", rows[0].amount_rub == Decimal("750.00"), str(rows[0].amount_rub))
check("round_rub(0.125) = 0.13", pm.round_rub(Decimal("0.125")) == Decimal("0.13"))

print()
if FAILS:
    print(f"ПРОВАЛЕНО: {len(FAILS)} — {FAILS}")
    sys.exit(1)
print("ВСЁ ЗЕЛЁНОЕ")
```

- [ ] **Шаг 2: Убедиться, что смоук падает**

```bash
cd /Users/konstantin/Downloads/risuy-wt-landing && PYTHONPATH=admin-panel:. /Users/konstantin/Downloads/risuy-ecosystem/.venv-smoke/bin/python scripts/partner_money_calc_smoke.py
```

Ожидаем: `ModuleNotFoundError: No module named 'partner_money'`.

- [ ] **Шаг 3: Добавить константы в конец `admin-panel/config.py`**

Сначала убедиться, что `from decimal import Decimal` есть в шапке файла; если нет —
добавить к остальным импортам.

```python
# --- Партнёрская программа (спека 2026-08-16) ------------------------------------
# Ставка продавца. Источник истины — здесь; default 20 в колонке partners.rate_percent
# существует только для строк, заведённых до этого кода.
PARTNER_RATE_PERCENT = Decimal(os.environ.get("PARTNER_RATE_PERCENT", "20"))
# Наставнику — СВЕРХ доли продавца, из нашей маржи. Продавец не теряет ничего.
MENTOR_RATE_PERCENT = Decimal(os.environ.get("MENTOR_RATE_PERCENT", "5"))
# Срок наставнических от joined_at ПОДОПЕЧНОГО. Без срока обязательство становится
# бессрочной рентой: привёл однажды — получаешь через пять лет.
MENTOR_BONUS_MONTHS = int(os.environ.get("MENTOR_BONUS_MONTHS", "12"))
# 🔴 Наставники — только в НАШЕМ контуре. Многоуровневая механика, выданная третьим
# лицам, ст. 172.2 УК делает проблемой площадки, то есть нашей. Спека §10.1.
PARTNER_MENTORS_TENANT_ENABLED = (
    os.environ.get("PARTNER_MENTORS_TENANT_ENABLED", "false").strip().lower() == "true"
)
# Сколько живёт одноразовая ссылка-приглашение партнёра.
PARTNER_INVITE_TTL_HOURS = int(os.environ.get("PARTNER_INVITE_TTL_HOURS", "72"))
```

- [ ] **Шаг 4: Создать `admin-panel/partner_money.py`**

```python
"""Деньги партнёрской программы (спека 2026-08-16).

🔴 Вынесено в чистые функции намеренно: это чужие деньги. Ошибка здесь не падает с
трассировкой, а тихо занижает или завышает выплату — и обнаруживается, когда партнёр
пересчитает вручную и придёт спорить. Всё, что можно проверить без базы, проверяется
без базы: scripts/partner_money_calc_smoke.py.
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

import config

CENT = Decimal("0.01")


@dataclass(frozen=True)
class PartnerNode:
    """Партнёр в дереве. rate_percent — его текущая ставка; в начисление она попадает
    КОПИЕЙ, чтобы правка настроек не переписала прошлое."""

    id: str
    parent_id: str | None
    joined_at: datetime
    rate_percent: Decimal


@dataclass(frozen=True)
class Accrual:
    """Одна строка начисления. level: 0 — продавец, 1 — наставник."""

    partner_id: str
    level: int
    rate_percent: Decimal
    amount_rub: Decimal
    reason: str


def round_rub(value: Decimal) -> Decimal:
    """Рубли с копейками: доли процентов не должны копиться в невидимых хвостах."""
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def _add_months(moment: datetime, months: int) -> datetime:
    """Прибавить месяцы, не съезжая на конец месяца (31 января + 1 месяц = 28/29 февраля)."""
    year, month = divmod(moment.month - 1 + months, 12)
    year += moment.year
    month += 1
    day = min(moment.day, calendar.monthrange(year, month)[1])
    return moment.replace(year=year, month=month, day=day)


def mentor_still_earns(seller_joined_at: datetime, at: datetime) -> bool:
    """Идут ли ещё наставнические с продаж этого партнёра.

    🔴 Срок считается от регистрации ПРИВЕДЁННОГО партнёра, а не от даты продажи: год
    даётся на то, чтобы наставник окупил своё участие, а не на каждую сделку заново.
    День в день срок ещё действует — граница трактуется в пользу партнёра.
    """
    return at <= _add_months(seller_joined_at, config.MENTOR_BONUS_MONTHS)


def accruals_for_payment(
    *,
    amount_rub: Decimal,
    seller: PartnerNode,
    mentor: PartnerNode | None,
    at: datetime,
    mentors_enabled: bool = True,
) -> list[Accrual]:
    """Что начисляется с ОДНОГО платежа клиента.

    Продавцу — доля по ЕГО ставке. Наставнику — сверх, из нашей маржи, и только пока не
    истёк срок. Возвращает список строк, а не сумму: у платежа несколько получателей, и
    каждому нужна своя запись с зафиксированной ставкой.
    """
    amount = Decimal(amount_rub)
    rows = [
        Accrual(
            partner_id=seller.id,
            level=0,
            rate_percent=seller.rate_percent,
            amount_rub=round_rub(amount * seller.rate_percent / 100),
            reason="sale",
        )
    ]
    if not mentors_enabled or mentor is None:
        return rows
    # Наставник — ровно один уровень вверх. Наставник наставника не получает ничего:
    # дерево без дна мы строить не договаривались.
    if seller.parent_id != mentor.id:
        return rows
    if not mentor_still_earns(seller.joined_at, at):
        return rows
    rate = Decimal(config.MENTOR_RATE_PERCENT)
    rows.append(
        Accrual(
            partner_id=mentor.id,
            level=1,
            rate_percent=rate,
            amount_rub=round_rub(amount * rate / 100),
            reason="mentor",
        )
    )
    return rows


def partner_balance(accrued_rows, payout_rows) -> tuple[Decimal, Decimal, Decimal]:
    """Начислено, выплачено, к выплате.

    Колонки «баланс» в базе нет: сохранённое вычисляемое значение расходится с фактом при
    первой же правке задним числом. Сторно приходит ОТРИЦАТЕЛЬНОЙ строкой начисления —
    поэтому суммируем как есть, без фильтров по знаку.
    """
    accrued = round_rub(sum((Decimal(v) for v in accrued_rows), Decimal(0)))
    paid = round_rub(sum((Decimal(v) for v in payout_rows), Decimal(0)))
    return accrued, paid, round_rub(accrued - paid)


def would_create_cycle(partner_id: str, parent_id: str | None, parent_of: dict) -> bool:
    """A привёл B — B не может стать наставником A.

    Без этой проверки два партнёра при глубине 1 начисляли бы друг другу с каждой продажи,
    а дерево перестало бы иметь корень.
    """
    if parent_id is None:
        return False
    seen: set[str] = set()
    cursor: str | None = parent_id
    while cursor is not None and cursor not in seen:
        if cursor == partner_id:
            return True
        seen.add(cursor)
        cursor = parent_of.get(cursor)
    return False
```

- [ ] **Шаг 5: Прогнать смоук — должен позеленеть**

```bash
cd /Users/konstantin/Downloads/risuy-wt-landing && PYTHONPATH=admin-panel:. /Users/konstantin/Downloads/risuy-ecosystem/.venv-smoke/bin/python scripts/partner_money_calc_smoke.py
```

Ожидаем: `ВСЁ ЗЕЛЁНОЕ`, код возврата 0. Проверить код возврата отдельной командой
`echo $?` — конвейер вроде `| tail` съедает его и красное выглядит зелёным.

- [ ] **Шаг 6: Линт**

```bash
cd /Users/konstantin/Downloads/risuy-wt-landing && ruff check admin-panel/partner_money.py scripts/partner_money_calc_smoke.py
```

- [ ] **Шаг 7: Коммит**

```bash
git add admin-panel/partner_money.py admin-panel/config.py scripts/partner_money_calc_smoke.py && git commit -m "feat(partners): чистые расчёты начислений и константы программы"
```

---

### Задача 2: Миграция expand и SECURITY DEFINER-функции

**Файлы:**
- Создать: `db/migrate_partner_money.sql`
- Изменить: `db/panel_role.sql` (гранты новых таблиц и функций)

**Интерфейсы:**
- Потребляет: ничего из кода.
- Отдаёт наружу: таблицы `partner_accruals`, `partner_payouts`, `partner_invites`;
  колонки `partners.owner_tenant_id/parent_id/rate_percent/joined_at/login_actor/email/tax_status`;
  функции `partner_pair_for_client(text, uuid)` и
  `insert_partner_accrual(uuid, uuid, text, uuid, text, uuid, smallint, numeric, numeric, text)`.
  Задача 3 вызывает обе.

- [ ] **Шаг 1: Написать миграцию `db/migrate_partner_money.sql`**

```sql
-- Партнёрский кабинет: начисления, выплаты, приглашения (спека 2026-08-16).
-- EXPAND-шаг. Аддитивно и идемпотентно: старый код новые объекты игнорирует.
-- CONTRACT (включение RLS) — db/migrate_partner_money_rls.sql, ПОСЛЕ деплоя кода.
--
-- ПРИМЕНЕНИЕ (сначала risuy_dev!):
--   bash ~/.claude/scripts/twc-migrate.sh 4171827 81.31.246.136 risuy_dev gen_user \
--       /Users/konstantin/Downloads/risuy-wt-landing/db/migrate_partner_money.sql

-- ── 1. Расширение реестра партнёров ──────────────────────────────────────────────
-- owner_tenant_id: null = НАША платформенная программа, не-null = программа тенанта.
-- Все существующие строки платформенные, бэкфилл не нужен.
alter table partners add column if not exists owner_tenant_id uuid references tenants(id);
-- parent_id: наставник. Фиксируется при регистрации и НЕ перепривязывается — смена
-- наставника задним числом переписала бы историю начислений.
alter table partners add column if not exists parent_id    uuid references partners(id);
alter table partners add column if not exists rate_percent numeric(5,2) not null default 20;
alter table partners add column if not exists joined_at    timestamptz not null default now();
alter table partners add column if not exists login_actor  text;
alter table partners add column if not exists email        text;
alter table partners add column if not exists tax_status   text;

create index if not exists partners_owner_idx  on partners (owner_tenant_id);
create index if not exists partners_parent_idx on partners (parent_id) where parent_id is not null;
create unique index if not exists partners_login_actor_uq
    on partners (login_actor) where login_actor is not null;

-- ── 2. Начисления ────────────────────────────────────────────────────────────────
create table if not exists partner_accruals (
    id              uuid primary key default gen_random_uuid(),
    partner_id      uuid not null references partners(id),
    owner_tenant_id uuid references tenants(id),   -- копия контура: для RLS и отчётов
    source_kind     text not null,                 -- service_invoice | order
    source_id       uuid not null,
    client_kind     text not null,                 -- tenant | lead
    client_id       uuid not null,
    level           smallint not null,             -- 0 продавец, 1 наставник
    rate_percent    numeric(5,2) not null,         -- КОПИЯ ставки на момент начисления
    amount_rub      numeric(12,2) not null,        -- отрицательная при сторно
    reason          text not null,                 -- sale | mentor | refund
    created_at      timestamptz not null default now(),
    constraint partner_accruals_level_chk  check (level in (0,1)),
    constraint partner_accruals_reason_chk check (reason in ('sale','mentor','refund')),
    constraint partner_accruals_skind_chk  check (source_kind in ('service_invoice','order')),
    constraint partner_accruals_ckind_chk  check (client_kind in ('tenant','lead'))
);

-- Повторный вебхук не начислит дважды.
create unique index if not exists partner_accruals_source_uq
    on partner_accruals (source_kind, source_id, partner_id, level)
    where reason <> 'refund';

-- 🔴 «Только первый платёж» — правило в БАЗЕ, а не в коде. Проверка «а не начисляли ли
-- мы уже» в Python была бы правдой ровно до первой параллельной оплаты.
create unique index if not exists partner_accruals_first_sale_uq
    on partner_accruals (client_kind, client_id, level)
    where reason in ('sale','mentor');

create index if not exists partner_accruals_partner_idx on partner_accruals (partner_id, created_at desc);

-- ── 3. Выплаты ───────────────────────────────────────────────────────────────────
-- Колонки «баланс» нет намеренно: сохранённое вычисляемое значение расходится с фактом
-- при первой правке задним числом. К выплате = сумма начислений − сумма выплат.
create table if not exists partner_payouts (
    id              uuid primary key default gen_random_uuid(),
    partner_id      uuid not null references partners(id),
    owner_tenant_id uuid references tenants(id),
    amount_rub      numeric(12,2) not null check (amount_rub > 0),
    paid_at         timestamptz not null default now(),
    method          text,
    note            text,
    created_by      text not null,
    created_at      timestamptz not null default now()
);
create index if not exists partner_payouts_partner_idx on partner_payouts (partner_id, paid_at desc);

-- ── 4. Одноразовые приглашения (образец: password_reset_tokens) ──────────────────
-- В базе лежит ХЕШ токена, не токен: утечка дампа не должна давать вход в кабинет.
create table if not exists partner_invites (
    token_hash text primary key,
    partner_id uuid not null references partners(id),
    expires_at timestamptz not null,
    used_at    timestamptz,
    created_by text not null,
    created_at timestamptz not null default now()
);
create index if not exists partner_invites_partner_idx on partner_invites (partner_id);

-- ── 5. 🔴 SECURITY DEFINER: без них платформенный контур мёртв ───────────────────
-- Тенант X платит абонплату → вебхук ставит app.tenant_id = X. Партнёр, приведший X, —
-- ПЛАТФОРМЕННЫЙ (owner_tenant_id is null). После включения RLS политика его не покажет и
-- не даст записать начисление: партнёр не получит ничего, ошибки в логах не будет.
-- Та же грабля, что чинили на orders (db/migrate_rls_discovery_fns.sql).
-- Функции исполняются под владельцем таблиц; владелец не подчиняется RLS, пока стоит
-- ENABLE, а не FORCE. EXECUTE выдаём ТОЛЬКО panel_rw.
--
-- Утечки между тенантами нет: функция скоупится своими аргументами — партнёр берётся по
-- атрибуции ЭТОГО клиента, а не поиском по таблице.

create or replace function partner_pair_for_client(p_client_kind text, p_client_id uuid)
returns table (
    role_level      smallint,
    partner_id      uuid,
    parent_id       uuid,
    joined_at       timestamptz,
    rate_percent    numeric,
    status          text,
    owner_tenant_id uuid
)
language sql
security definer
set search_path = public
as $$
    with seller as (
        select p.* from partners p
        where p.id = case
            when p_client_kind = 'tenant' then (select t.partner_id from tenants t where t.id = p_client_id)
            when p_client_kind = 'lead'   then (select l.partner_id from leads   l where l.id = p_client_id)
        end
    )
    select 0::smallint, s.id, s.parent_id, s.joined_at, s.rate_percent, s.status, s.owner_tenant_id
      from seller s
    union all
    select 1::smallint, m.id, m.parent_id, m.joined_at, m.rate_percent, m.status, m.owner_tenant_id
      from seller s join partners m on m.id = s.parent_id
$$;

create or replace function insert_partner_accrual(
    p_partner_id      uuid,
    p_owner_tenant_id uuid,
    p_source_kind     text,
    p_source_id       uuid,
    p_client_kind     text,
    p_client_id       uuid,
    p_level           smallint,
    p_rate            numeric,
    p_amount          numeric,
    p_reason          text
) returns uuid
language sql
security definer
set search_path = public
as $$
    insert into partner_accruals (partner_id, owner_tenant_id, source_kind, source_id,
                                  client_kind, client_id, level, rate_percent, amount_rub, reason)
    values (p_partner_id, p_owner_tenant_id, p_source_kind, p_source_id,
            p_client_kind, p_client_id, p_level, p_rate, p_amount, p_reason)
    on conflict do nothing
    returning id
$$;

-- ── 6. Гранты ────────────────────────────────────────────────────────────────────
-- delete не даём нигде: начисления и выплаты не удаляются, а сторнируются.
do $$ begin
    if exists (select 1 from pg_roles where rolname='panel_rw') then
        grant select, insert, update on partner_accruals, partner_payouts, partner_invites to panel_rw;
        grant execute on function partner_pair_for_client(text, uuid) to panel_rw;
        grant execute on function insert_partner_accrual(uuid, uuid, text, uuid, text, uuid, smallint, numeric, numeric, text) to panel_rw;
    end if;
end $$;
```

- [ ] **Шаг 2: Зеркалировать гранты в `db/panel_role.sql`**

Файл — канон прав `panel_rw`; миграция даёт права здесь и сейчас, а `panel_role.sql`
хранит их для пересоздания роли с нуля. Добавить в конец, рядом с прочими грантами:

```sql
-- Партнёрский кабинет (спека 2026-08-16). delete не даём: сторно, а не удаление.
grant select, insert, update on partner_accruals, partner_payouts, partner_invites to panel_rw;
grant execute on function partner_pair_for_client(text, uuid) to panel_rw;
grant execute on function insert_partner_accrual(uuid, uuid, text, uuid, text, uuid, smallint, numeric, numeric, text) to panel_rw;
```

⚠️ В рабочем дереве `db/panel_role.sql` уже изменён другой сессией (метринг). Перед
правкой выполнить `git status --short` и добавлять СВОЙ блок, не трогая чужие строки; в
коммит класть только его.

- [ ] **Шаг 3: Применить на `risuy_dev` и убедиться, что применилось**

```bash
bash ~/.claude/scripts/twc-migrate.sh 4171827 81.31.246.136 risuy_dev gen_user /Users/konstantin/Downloads/risuy-wt-landing/db/migrate_partner_money.sql
```

Ожидаем: команды проходят без ошибок. Повторный прогон обязан пройти так же — миграция
идемпотентна.

- [ ] **Шаг 4: Коммит**

```bash
git add db/migrate_partner_money.sql db/panel_role.sql && git commit -m "feat(partners): миграция денежного слоя и SECURITY DEFINER для начислений"
```

---

### Задача 3: Начисление в момент оплаты

**Файлы:**
- Изменить: `admin-panel/db.py` — добавить `accrue_for_payment`, врезать в
  `_apply_order_paid` (около L3010) и `mark_service_invoice_paid_by_payment` (около L3412)
- Создать: `scripts/partner_money_db_smoke.py`

**Интерфейсы:**
- Потребляет: `partner_money.accruals_for_payment`, `partner_money.PartnerNode` (задача 1);
  `partner_pair_for_client`, `insert_partner_accrual` (задача 2).
- Отдаёт наружу: `db.accrue_for_payment(c, *, source_kind, source_id, client_kind,
  client_id, amount_rub, at=None) -> list[str]` — список id созданных начислений.
  Задача 4 вызывает её же для сторно.

- [ ] **Шаг 1: Написать падающий DB-смоук `scripts/partner_money_db_smoke.py`**

```python
#!/usr/bin/env python3
"""DB-смоук начислений (risuy_dev):
  PARTNER_MONEY_SMOKE_DSN="...risuy_dev..." PYTHONPATH=admin-panel:. \
    /Users/konstantin/Downloads/risuy-ecosystem/.venv-smoke/bin/python \
    scripts/partner_money_db_smoke.py
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
import db  # noqa: E402

DSN = os.environ.get("PARTNER_MONEY_SMOKE_DSN")
if not DSN or "/risuy_dev" not in DSN.split("?")[0]:
    raise SystemExit("Задайте PARTNER_MONEY_SMOKE_DSN на risuy_dev")

FAILS = []
PSELLER = "СМОУК Продавец"
PMENTOR = "СМОУК Наставник"
TNAME = "СМОУК Тенант-плательщик"


def check(name, cond, detail=""):
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


async def _cleanup(c):
    await c.execute("""delete from partner_accruals where partner_id in
                       (select id from partners where name in ($1,$2))""", PSELLER, PMENTOR)
    await c.execute("""delete from partner_payouts where partner_id in
                       (select id from partners where name in ($1,$2))""", PSELLER, PMENTOR)
    await c.execute("update partners set parent_id = null where name in ($1,$2)", PSELLER, PMENTOR)
    await c.execute("delete from partners where name in ($1,$2)", PSELLER, PMENTOR)
    await c.execute("delete from tenants where name = $1", TNAME)


async def main():
    db.pool = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    src = "11111111-1111-1111-1111-111111111111"
    try:
        async with db.pool.acquire() as c:
            await _cleanup(c)
            mentor_id = await c.fetchval(
                "insert into partners (name, ref_code) values ($1, $2) returning id",
                PMENTOR, "smokem01")
            seller_id = await c.fetchval(
                "insert into partners (name, ref_code, parent_id) values ($1,$2,$3) returning id",
                PSELLER, "smokes01", mentor_id)
            tenant_id = await c.fetchval(
                "insert into tenants (name, slug, status, partner_id) "
                "values ($1,$2,'active',$3) returning id", TNAME, "smoke-payer", seller_id)

            print("1. Первый платёж начисляет продавцу и наставнику:")
            async with c.transaction():
                ids = await db.accrue_for_payment(
                    c, source_kind="service_invoice", source_id=src,
                    client_kind="tenant", client_id=tenant_id, amount_rub=Decimal("7500"))
            check("создано две строки", len(ids) == 2, f"строк={len(ids)}")
            rows = await c.fetch(
                "select level, amount_rub, reason from partner_accruals "
                "where client_id = $1 order by level", tenant_id)
            check("продавцу 1500.00", rows[0]["amount_rub"] == Decimal("1500.00"), str(rows[0]["amount_rub"]))
            check("наставнику 375.00", rows[1]["amount_rub"] == Decimal("375.00"), str(rows[1]["amount_rub"]))

            print("2. Повторный вебхук того же платежа не начисляет дважды:")
            async with c.transaction():
                again = await db.accrue_for_payment(
                    c, source_kind="service_invoice", source_id=src,
                    client_kind="tenant", client_id=tenant_id, amount_rub=Decimal("7500"))
            total = await c.fetchval(
                "select count(*) from partner_accruals where client_id = $1", tenant_id)
            check("новых строк нет", not again, f"вернул={again}")
            check("в базе по-прежнему 2", total == 2, f"строк={total}")

            print("3. ВТОРОЙ платёж того же клиента не начисляет ничего (первый платёж):")
            async with c.transaction():
                second = await db.accrue_for_payment(
                    c, source_kind="service_invoice",
                    source_id="22222222-2222-2222-2222-222222222222",
                    client_kind="tenant", client_id=tenant_id, amount_rub=Decimal("7500"))
            total = await c.fetchval(
                "select count(*) from partner_accruals where client_id = $1", tenant_id)
            check("вернул пусто", not second, f"вернул={second}")
            check("в базе по-прежнему 2", total == 2, f"строк={total}")

            print("4. Гонка: 20 параллельных начислений дают ровно одну пару строк:")
            await c.execute("delete from partner_accruals where client_id = $1", tenant_id)

            async def one():
                async with db.pool.acquire() as cc:
                    async with cc.transaction():
                        return await db.accrue_for_payment(
                            cc, source_kind="service_invoice", source_id=src,
                            client_kind="tenant", client_id=tenant_id, amount_rub=Decimal("7500"))

            got = await asyncio.gather(*[one() for _ in range(20)], return_exceptions=True)
            errs = [g for g in got if isinstance(g, Exception)]
            total = await c.fetchval(
                "select count(*) from partner_accruals where client_id = $1", tenant_id)
            check("исключений нет", not errs, str(errs[:1]))
            check("ровно 2 строки после 20 попыток", total == 2, f"строк={total}")

            print("5. Отключённый партнёр не получает ничего:")
            await c.execute("delete from partner_accruals where client_id = $1", tenant_id)
            await c.execute("update partners set status='disabled' where id=$1", seller_id)
            async with c.transaction():
                off = await db.accrue_for_payment(
                    c, source_kind="service_invoice", source_id=src,
                    client_kind="tenant", client_id=tenant_id, amount_rub=Decimal("7500"))
            check("вернул пусто", not off, f"вернул={off}")
            await c.execute("update partners set status='active' where id=$1", seller_id)

            print("6. Клиент без партнёра не порождает начислений:")
            await c.execute("update tenants set partner_id = null where id = $1", tenant_id)
            async with c.transaction():
                none_rows = await db.accrue_for_payment(
                    c, source_kind="service_invoice", source_id=src,
                    client_kind="tenant", client_id=tenant_id, amount_rub=Decimal("7500"))
            check("вернул пусто", not none_rows, f"вернул={none_rows}")

            await _cleanup(c)
    finally:
        await db.pool.close()

    print()
    if FAILS:
        print(f"ПРОВАЛЕНО: {len(FAILS)} — {FAILS}")
        sys.exit(1)
    print("ВСЁ ЗЕЛЁНОЕ")


asyncio.run(main())
```

- [ ] **Шаг 2: Убедиться, что смоук падает**

```bash
cd /Users/konstantin/Downloads/risuy-wt-landing && PARTNER_MONEY_SMOKE_DSN="<dsn risuy_dev>" PYTHONPATH=admin-panel:. /Users/konstantin/Downloads/risuy-ecosystem/.venv-smoke/bin/python scripts/partner_money_db_smoke.py
```

Ожидаем: `AttributeError: module 'db' has no attribute 'accrue_for_payment'`.

- [ ] **Шаг 3: Добавить `accrue_for_payment` в `admin-panel/db.py`**

Разместить рядом с `_apply_order_paid` (около L3010), чтобы обе точки врезки и сама
функция читались вместе. Убедиться, что в шапке `db.py` есть `import partner_money` —
если нет, добавить к остальным импортам модулей панели.

```python
async def accrue_for_payment(
    c, *, source_kind: str, source_id, client_kind: str, client_id,
    amount_rub, at=None, reason_override: str | None = None,
) -> list[str]:
    """Начислить партнёру с ОДНОГО принятого платежа. Возвращает id созданных строк.

    Вызывается ВНУТРИ открытой транзакции c, рядом с отметкой «оплачено»: это деньги, а
    не уведомление — если начисление упало, платёж не должен считаться принятым.

    🔴 Ходит через SECURITY DEFINER-функции, а не прямыми SELECT/INSERT: в платформенном
    контуре app.tenant_id равен тенанту-плательщику, а партнёр платформенный
    (owner_tenant_id is null) — политика RLS не показала бы его и не дала записать
    начисление. Молча, без ошибки. Та же грабля, что чинили на orders.

    Повтор — норма, а не сбой: вебхуки приходят по два раза, поэтому конфликт по любому
    уникальному индексу гасится в insert_partner_accrual через on conflict do nothing.
    """
    moment = at or datetime.now(timezone.utc)
    rows = await c.fetch("select * from partner_pair_for_client($1, $2)", client_kind, client_id)
    if not rows:
        return []  # клиент пришёл сам — обычный случай, не ошибка
    by_level = {r["role_level"]: r for r in rows}
    seller_row = by_level.get(0)
    if seller_row is None or seller_row["status"] != "active":
        return []

    def _node(row):
        return partner_money.PartnerNode(
            id=str(row["partner_id"]),
            parent_id=str(row["parent_id"]) if row["parent_id"] else None,
            joined_at=row["joined_at"],
            rate_percent=Decimal(row["rate_percent"]),
        )

    seller = _node(seller_row)
    mentor_row = by_level.get(1)
    mentor = _node(mentor_row) if mentor_row is not None and mentor_row["status"] == "active" else None
    # Наставнические — только в НАШЕМ контуре (спека §10.1).
    mentors_enabled = seller_row["owner_tenant_id"] is None or config.PARTNER_MENTORS_TENANT_ENABLED

    accruals = partner_money.accruals_for_payment(
        amount_rub=Decimal(amount_rub), seller=seller, mentor=mentor,
        at=moment, mentors_enabled=mentors_enabled,
    )
    created: list[str] = []
    for a in accruals:
        new_id = await c.fetchval(
            "select insert_partner_accrual($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)",
            a.partner_id, seller_row["owner_tenant_id"], source_kind, source_id,
            client_kind, client_id, a.level, a.rate_percent,
            -a.amount_rub if reason_override == "refund" else a.amount_rub,
            reason_override or a.reason,
        )
        if new_id:
            created.append(str(new_id))
    return created
```

- [ ] **Шаг 4: Врезать в `_apply_order_paid`**

В `admin-panel/db.py`, внутри `_apply_order_paid`, сразу ПОСЛЕ блока конвертации лида и
ДО `_insert_audit`, добавить:

```python
    # Партнёрское начисление — в той же транзакции, что и отметка оплаты (спека §6).
    if upd["lead_id"] is not None:
        await accrue_for_payment(
            c, source_kind="order", source_id=upd["id"],
            client_kind="lead", client_id=upd["lead_id"], amount_rub=upd["amount"],
        )
```

- [ ] **Шаг 5: Врезать в `mark_service_invoice_paid_by_payment`**

Там же, в `admin-panel/db.py`. Сначала расширить `select ... for update`, чтобы получить
сумму: заменить

```python
                "select id, status from service_invoices "
                "where yookassa_payment_id = $1 for update",
```

на

```python
                "select id, status, amount from service_invoices "
                "where yookassa_payment_id = $1 for update",
```

Затем сразу после `upd = await c.fetchrow(...)` и ДО `_insert_audit` добавить:

```python
            # Партнёрское начисление — в той же транзакции (спека §6). tenant_id уже
            # проставлен в GUC выше, но партнёр может быть платформенным — поэтому
            # accrue_for_payment ходит через SECURITY DEFINER.
            await accrue_for_payment(
                c, source_kind="service_invoice", source_id=upd["id"],
                client_kind="tenant", client_id=tenant_id, amount_rub=row["amount"],
            )
```

- [ ] **Шаг 6: Прогнать DB-смоук — должен позеленеть**

```bash
cd /Users/konstantin/Downloads/risuy-wt-landing && PARTNER_MONEY_SMOKE_DSN="<dsn risuy_dev>" PYTHONPATH=admin-panel:. /Users/konstantin/Downloads/risuy-ecosystem/.venv-smoke/bin/python scripts/partner_money_db_smoke.py; echo "код возврата: $?"
```

Ожидаем `ВСЁ ЗЕЛЁНОЕ` и код возврата 0.

- [ ] **Шаг 7: Прогнать смоук расчётов и `partners_smoke.py` — убедиться, что ничего не сломано**

```bash
cd /Users/konstantin/Downloads/risuy-wt-landing && PYTHONPATH=admin-panel:. /Users/konstantin/Downloads/risuy-ecosystem/.venv-smoke/bin/python scripts/partner_money_calc_smoke.py && PARTNERS_SMOKE_DSN="<dsn risuy_dev>" PYTHONPATH=admin-panel:. /Users/konstantin/Downloads/risuy-ecosystem/.venv-smoke/bin/python scripts/partners_smoke.py
```

- [ ] **Шаг 8: Коммит**

```bash
git add admin-panel/db.py scripts/partner_money_db_smoke.py && git commit -m "feat(partners): начисление в момент оплаты, обе точки приёма денег"
```

---

### Задача 4: Сторно при возврате

**Файлы:**
- Изменить: `admin-panel/db.py` — `set_order_status_with_audit` (около L2960)
- Изменить: `scripts/partner_money_db_smoke.py` (добавить блок 7)

**Интерфейсы:**
- Потребляет: `db.accrue_for_payment(..., reason_override="refund")` (задача 3).
- Отдаёт наружу: ничего нового.

- [ ] **Шаг 1: Дописать проверку в смоук**

Добавить в `scripts/partner_money_db_smoke.py` перед финальным `_cleanup`:

```python
            print("7. Возврат уменьшает начисленное отрицательными строками:")
            await c.execute("delete from partner_accruals where client_id = $1", tenant_id)
            await c.execute("update tenants set partner_id = $1 where id = $2", seller_id, tenant_id)
            async with c.transaction():
                await db.accrue_for_payment(
                    c, source_kind="service_invoice", source_id=src,
                    client_kind="tenant", client_id=tenant_id, amount_rub=Decimal("7500"))
            async with c.transaction():
                back = await db.accrue_for_payment(
                    c, source_kind="service_invoice", source_id=src,
                    client_kind="tenant", client_id=tenant_id, amount_rub=Decimal("7500"),
                    reason_override="refund")
            check("сторно по обоим уровням", len(back) == 2, f"строк={len(back)}")
            total = await c.fetchval(
                "select coalesce(sum(amount_rub),0) from partner_accruals where client_id = $1",
                tenant_id)
            check("итог по клиенту обнулился", total == Decimal("0.00"), str(total))
```

- [ ] **Шаг 2: Прогнать смоук — блок 7 обязан упасть**

```bash
cd /Users/konstantin/Downloads/risuy-wt-landing && PARTNER_MONEY_SMOKE_DSN="<dsn risuy_dev>" PYTHONPATH=admin-panel:. /Users/konstantin/Downloads/risuy-ecosystem/.venv-smoke/bin/python scripts/partner_money_db_smoke.py; echo "код возврата: $?"
```

Ожидаем FAIL на «сторно по обоим уровням»: `reason_override` уже поддержан задачей 3,
поэтому проверить надо именно связку — если блок зелёный сразу, значит смоук написан
неверно и не проверяет возврат в реальном месте.

- [ ] **Шаг 3: Врезать сторно в `set_order_status_with_audit`**

В `admin-panel/db.py`, внутри `set_order_status_with_audit`, после UPDATE статуса
добавить ветку возврата. Строки пишутся только когда статус ДЕЙСТВИТЕЛЬНО меняется на
`refunded` — от двойного сторно защищает переход, а не индекс (спека §6.1):

```python
            # Сторно партнёру: возвращаем клиенту — снимаем и у продавца, и у наставника.
            # Иначе на возврате партнёрская сеть зарабатывает, а мы платим дважды.
            # Пишем ТОЛЬКО при реальном переходе в refunded: повторный вызов увидит
            # refunded и сюда не дойдёт — тем же приёмом, что _apply_order_paid.
            if new_status == "refunded" and prev_status != "refunded" and upd["lead_id"] is not None:
                await accrue_for_payment(
                    c, source_kind="order", source_id=upd["id"],
                    client_kind="lead", client_id=upd["lead_id"],
                    amount_rub=upd["amount"], reason_override="refund",
                )
```

⚠️ Перед правкой прочитать текущее тело функции: имена локальных переменных
(`new_status`, `prev_status`, `upd`) привести к тем, что там есть на самом деле, и
убедиться, что `select ... for update` возвращает `lead_id` и `amount` — если нет,
дополнить список колонок.

- [ ] **Шаг 4: Прогнать смоук — должен позеленеть целиком**

```bash
cd /Users/konstantin/Downloads/risuy-wt-landing && PARTNER_MONEY_SMOKE_DSN="<dsn risuy_dev>" PYTHONPATH=admin-panel:. /Users/konstantin/Downloads/risuy-ecosystem/.venv-smoke/bin/python scripts/partner_money_db_smoke.py; echo "код возврата: $?"
```

- [ ] **Шаг 5: Коммит**

```bash
git add admin-panel/db.py scripts/partner_money_db_smoke.py && git commit -m "feat(partners): сторно при возврате по всем уровням"
```

---

### Задача 5: Принципал партнёра — приглашение, вход, белый список маршрутов

Самая опасная задача плана: в панель вводится новый вид пользователя, а гейты во всех
существующих маршрутах писались в мире, где его не было.

**Файлы:**
- Изменить: `admin-panel/db.py` — функции приглашений
- Изменить: `admin-panel/auth.py` — гейт роли `partner`
- Изменить: `admin-panel/app.py` — маршруты `/partners/{id}/invite`, `/partner/join/{token}`
- Создать: `admin-panel/templates/partner_join.html`
- Создать: `scripts/partner_gate_smoke.py`

**Интерфейсы:**
- Потребляет: `db.get_partner` (существует), `auth.hash_password` (существует),
  `db.create_admin_user_with_audit` (существует, около L2448 — прочитать сигнатуру перед
  использованием).
- Отдаёт наружу: `db.create_partner_invite(partner_id, *, actor) -> str` (сырой токен,
  в базу уходит хеш); `db.consume_partner_invite(token) -> uuid | None`;
  `db.partner_by_login_actor(actor) -> Record | None`. Задачи 6 и 7 пользуются третьей.

- [ ] **Шаг 1: Написать падающий смоук гейта `scripts/partner_gate_smoke.py`**

Смоук перебирает ВСЕ маршруты приложения и требует 403 везде, кроме `/partner/*`. Это не
проверка пары страниц: маршрут, добавленный завтра другой сессией, обязан быть закрыт по
умолчанию.

```python
#!/usr/bin/env python3
"""Смоук гейта принципала «партнёр» — БЕЗ базы: проверяет РЕШЕНИЕ гейта на всех
маршрутах FastAPI, а не ответы сервера.
  PYTHONPATH=admin-panel:. \
    /Users/konstantin/Downloads/risuy-ecosystem/.venv-smoke/bin/python \
    scripts/partner_gate_smoke.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "admin-panel"))
os.environ.setdefault("DATABASE_URL", "postgresql://x/y")
os.environ.setdefault("SESSION_SECRET", "smoke-secret-padding-0123456789abcdef")
os.environ.setdefault("ADMIN_USERNAME", "smoke")
os.environ.setdefault("ADMIN_PASSWORD_HASH", "$argon2id$v=19$m=65536,t=3,p=4$c21va2U$c21va2U")

import auth  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


print("1. Партнёру открыт только его префикс:")
check("/partner открыт", auth.partner_may_access("/partner"))
check("/partner/team открыт", auth.partner_may_access("/partner/team"))
check("/partner/payouts открыт", auth.partner_may_access("/partner/payouts"))

print("2. Всё остальное закрыто — включая то, чего ещё нет:")
for path in ["/", "/companies", "/partners", "/partners/123", "/tenants", "/subscription",
             "/agents", "/brief-center/1", "/leads", "/orders", "/shop",
             "/partnership-secret-new-route", "/partnerX", "/partner-admin",
             "/../partner", "/api/demo-chat"]:
    check(f"{path} закрыт", not auth.partner_may_access(path))

print("3. Перебор ВСЕХ маршрутов приложения — открытым может быть только /partner/*:")
import app as panel_app  # noqa: E402

opened = [r.path for r in panel_app.app.routes
          if getattr(r, "path", None) and auth.partner_may_access(r.path)]
stray = [p for p in opened if not (p == "/partner" or p.startswith("/partner/"))]
check("посторонних открытых маршрутов нет", not stray, f"лишние: {stray}")
print(f"  (всего маршрутов: {len(panel_app.app.routes)}, открыто партнёру: {len(opened)})")

print()
if FAILS:
    print(f"ПРОВАЛЕНО: {len(FAILS)} — {FAILS}")
    sys.exit(1)
print("ВСЁ ЗЕЛЁНОЕ")
```

- [ ] **Шаг 2: Убедиться, что смоук падает**

```bash
cd /Users/konstantin/Downloads/risuy-wt-landing && PYTHONPATH=admin-panel:. /Users/konstantin/Downloads/risuy-ecosystem/.venv-smoke/bin/python scripts/partner_gate_smoke.py
```

Ожидаем: `AttributeError: module 'auth' has no attribute 'partner_may_access'`.

- [ ] **Шаг 3: Добавить гейт в `admin-panel/auth.py`**

```python
# Единственный префикс, открытый принципалу «партнёр». Спека §8.2.
PARTNER_PREFIX = "/partner"


def partner_may_access(path: str) -> bool:
    """Пускать ли партнёрскую сессию на этот путь.

    🔴 БЕЛЫЙ список, а не чёрный. Гейты в панели писались в мире, где принципала
    «партнёр» не существовало; правило «партнёру запрещено вот это» пропустит всё, что
    добавят завтра. Здесь наоборот: закрыто всё, кроме явно открытого.

    Проверяем точное совпадение или границу сегмента: «/partnerX» и «/partner-admin»
    начинаются с «/partner», но это ДРУГИЕ маршруты.
    """
    if not path or not path.startswith("/"):
        return False
    if ".." in path:
        return False
    return path == PARTNER_PREFIX or path.startswith(PARTNER_PREFIX + "/")
```

Затем в `require_session` (после загрузки сессии) добавить:

```python
    # Партнёр живёт только в своём разделе. Сессия партнёра не имеет active_tenant_id и
    # не может его переключить: тенантские данные ему не принадлежат.
    if session.role == "partner" and not partner_may_access(request.url.path):
        raise HTTPException(status_code=403, detail="Раздел недоступен")
```

⚠️ Прочитать текущую сигнатуру `require_session` перед правкой: если `request` в неё не
передаётся, взять путь из уже имеющегося аргумента, а не добавлять новый параметр —
`require_session` висит зависимостью на десятках маршрутов.

- [ ] **Шаг 4: Прогнать смоук гейта — должен позеленеть**

```bash
cd /Users/konstantin/Downloads/risuy-wt-landing && PYTHONPATH=admin-panel:. /Users/konstantin/Downloads/risuy-ecosystem/.venv-smoke/bin/python scripts/partner_gate_smoke.py; echo "код возврата: $?"
```

- [ ] **Шаг 5: Функции приглашений в `admin-panel/db.py`**

```python
async def create_partner_invite(partner_id, *, actor: str) -> str:
    """Одноразовая ссылка-приглашение партнёру. Возвращает СЫРОЙ токен — в базу уходит
    только его sha256: утечка дампа не должна давать вход в чужой кабинет (образец —
    password_reset_tokens)."""
    raw = secrets.token_urlsafe(32)
    digest = hashlib.sha256(raw.encode()).hexdigest()
    expires = datetime.now(timezone.utc) + timedelta(hours=config.PARTNER_INVITE_TTL_HOURS)
    async with pool.acquire() as c:
        async with c.transaction():
            await c.execute(
                "insert into partner_invites (token_hash, partner_id, expires_at, created_by) "
                "values ($1,$2,$3,$4)", digest, partner_id, expires, actor)
            await _insert_audit(c, actor=actor, action="partner_invite_created",
                                detail={"partner_id": str(partner_id)})
    return raw


async def consume_partner_invite(token: str):
    """Погасить приглашение. Возвращает partner_id или None, если токен неверный,
    просроченный или уже использован. Гашение и проверка — одной транзакцией, иначе две
    вкладки заведут два аккаунта на одно приглашение."""
    digest = hashlib.sha256(token.encode()).hexdigest()
    async with pool.acquire() as c:
        async with c.transaction():
            row = await c.fetchrow(
                "select partner_id, expires_at, used_at from partner_invites "
                "where token_hash = $1 for update", digest)
            if row is None or row["used_at"] is not None:
                return None
            if row["expires_at"] < datetime.now(timezone.utc):
                return None
            await c.execute("update partner_invites set used_at = now() where token_hash = $1", digest)
            return row["partner_id"]


async def partner_by_login_actor(actor: str):
    """Партнёр по имени учётной записи панели. Доступ в кабинет даёт СТРОКА в partners, а
    не роль: партнёр не член команды тенанта и в memberships не попадает."""
    async with pool.acquire() as c:
        return await c.fetchrow(
            "select * from partners where login_actor = $1 and status = 'active'", actor)
```

Убедиться, что в шапке `db.py` есть `import hashlib` и `import secrets`; если нет — добавить.

- [ ] **Шаг 6: Маршруты в `admin-panel/app.py`**

Рядом с существующими `/partners/*` (около L6873):

```python
@app.post("/partners/{partner_id}/invite")
async def partners_invite(request: Request, partner_id: uuid.UUID,
                          session: auth.Session = Depends(require_session),
                          csrf_token: str = Form(...)):
    """Выдать партнёру одноразовую ссылку для заведения входа."""
    auth.check_csrf(session, csrf_token)
    if not session.is_platform:
        raise HTTPException(status_code=403, detail="Только владелец платформы")
    partner = await db.get_partner(partner_id)
    if not partner:
        return RedirectResponse(url="/partners?err=not_found", status_code=303)
    raw = await db.create_partner_invite(partner_id, actor=session.actor)
    base = config.SERVICE_SITE_URL
    return RedirectResponse(url=f"/partners/{partner_id}?invite={raw}", status_code=303)


@app.get("/partner/join/{token}", response_class=HTMLResponse)
async def partner_join_form(request: Request, token: str):
    """Страница заведения входа по приглашению. Без сессии: у партнёра её ещё нет."""
    return templates.TemplateResponse(request, "partner_join.html",
                                      {"token": token, "err": request.query_params.get("err")})


@app.post("/partner/join/{token}")
async def partner_join(request: Request, token: str,
                       email: str = Form(...), password: str = Form(...)):
    """Погасить приглашение и завести учётную запись партнёра.

    Пароль партнёр ставит сам — мы его не видим никогда. Сброс дальше идёт существующим
    механизмом /forgot-password.
    """
    partner_id = await db.consume_partner_invite(token)
    if partner_id is None:
        return RedirectResponse(url=f"/partner/join/{token}?err=bad_token", status_code=303)
    if len(password) < 10:
        return RedirectResponse(url=f"/partner/join/{token}?err=weak", status_code=303)
    actor = email.strip().lower()
    await db.create_admin_user_with_audit(
        username=actor, password_hash=await auth.hash_password(password),
        role="partner", actor="partner-join", ip=_ip(request), user_agent=_ua(request))
    await db.set_partner_login(partner_id, actor=actor, email=actor)
    return RedirectResponse(url="/login?saved=partner_joined", status_code=303)
```

⚠️ `create_admin_user_with_audit` (L2448) — прочитать реальную сигнатуру и вызвать по
ней; имена параметров выше могут не совпасть. Добавить `db.set_partner_login(partner_id,
*, actor, email)` — простой UPDATE `partners set login_actor=$1, email=$2 where id=$3` с
аудитом, по образцу `set_partner_chat_id` (L5980).

⚠️ Погашенное приглашение при ошибке создания учётки пропадёт. Поэтому `consume` и
создание учётки должны идти в одной транзакции — если по коду это неудобно, вернуть
приглашение в неиспользованное состояние в блоке `except` и записать это в аудит.

- [ ] **Шаг 7: Шаблон `admin-panel/templates/partner_join.html`**

```html
{% extends "base.html" %}
{% block title %}Вход партнёра{% endblock %}
{% block content %}
<div class="page-head">
  <h1 class="page-head__title">Заведите вход в кабинет</h1>
  <p class="page-head__hint">Ссылка одноразовая и действует трое суток. Пароль придумайте
    сами — мы его не видим и восстановить не сможем, но сможем прислать ссылку на смену.</p>
</div>
{% if err == 'bad_token' %}<p class="flash flash--error">Ссылка недействительна,
  просрочена или уже использована. Попросите новую.</p>{% endif %}
{% if err == 'weak' %}<p class="flash flash--error">Пароль короче 10 символов.</p>{% endif %}
<section class="card">
  <form method="post" action="/partner/join/{{ token }}" autocomplete="off">
    <label class="field">
      <span class="field__label">Ваш email</span>
      <span class="field__hint">Он же — логин. На него придёт ссылка, если забудете пароль.</span>
      <input class="field__input" type="email" name="email" required maxlength="200">
    </label>
    <label class="field">
      <span class="field__label">Пароль</span>
      <span class="field__hint">От 10 символов.</span>
      <input class="field__input" type="password" name="password" required minlength="10">
    </label>
    <div class="form-actions"><button class="btn btn--dark" type="submit">Создать вход</button></div>
  </form>
</section>
{% endblock %}
```

- [ ] **Шаг 8: Прогнать оба смоука и линт**

```bash
cd /Users/konstantin/Downloads/risuy-wt-landing && PYTHONPATH=admin-panel:. /Users/konstantin/Downloads/risuy-ecosystem/.venv-smoke/bin/python scripts/partner_gate_smoke.py && ruff check admin-panel/auth.py admin-panel/db.py admin-panel/app.py scripts/partner_gate_smoke.py; echo "код возврата: $?"
```

- [ ] **Шаг 9: Коммит**

```bash
git add admin-panel/auth.py admin-panel/db.py admin-panel/app.py admin-panel/templates/partner_join.html scripts/partner_gate_smoke.py && git commit -m "feat(partners): вход партнёра по одноразовой ссылке и белый список маршрутов"
```

---

### Задача 6: Кабинет партнёра

**Файлы:**
- Изменить: `admin-panel/db.py` — чтения кабинета
- Изменить: `admin-panel/app.py` — маршруты `/partner`, `/partner/team`
- Создать: `admin-panel/templates/partner_cabinet.html`, `admin-panel/templates/partner_team.html`

**Интерфейсы:**
- Потребляет: `db.partner_by_login_actor` (задача 5), `partner_money.partner_balance` (задача 1).
- Отдаёт наружу: `db.partner_cabinet_data(partner_id) -> dict` с ключами
  `accruals`, `payouts`, `clients`, `totals`. Задача 7 переиспользует `partner_totals`.

- [ ] **Шаг 1: Чтения в `admin-panel/db.py`**

🔴 Суммы считаем ОТДЕЛЬНЫМИ группировками и сшиваем словарём в Python. Коррелированный
подзапрос в `select` на этом же классе задач молча вернул нули при верных данных в базе —
партнёр увидел бы «начислено 0» при оплаченном счёте.

```python
async def partner_totals(partner_ids: list) -> dict:
    """Начислено / выплачено / к выплате по каждому партнёру. Два запроса с group by
    вместо подзапроса на строку: предсказуемо и не врёт нулями."""
    if not partner_ids:
        return {}
    async with pool.acquire() as c:
        acc = await c.fetch(
            "select partner_id, coalesce(sum(amount_rub),0) s from partner_accruals "
            "where partner_id = any($1::uuid[]) group by partner_id", partner_ids)
        pay = await c.fetch(
            "select partner_id, coalesce(sum(amount_rub),0) s from partner_payouts "
            "where partner_id = any($1::uuid[]) group by partner_id", partner_ids)
    accrued = {str(r["partner_id"]): r["s"] for r in acc}
    paid = {str(r["partner_id"]): r["s"] for r in pay}
    out = {}
    for pid in [str(p) for p in partner_ids]:
        a, p = accrued.get(pid, Decimal(0)), paid.get(pid, Decimal(0))
        out[pid] = {"accrued": a, "paid": p, "due": a - p}
    return out


async def partner_cabinet_data(partner_id) -> dict:
    """Всё, что видит партнёр о себе: начисления, выплаты, приведённые клиенты."""
    async with pool.acquire() as c:
        accruals = await c.fetch(
            "select source_kind, client_kind, client_id, level, rate_percent, amount_rub, "
            "       reason, created_at from partner_accruals "
            "where partner_id = $1 order by created_at desc limit 200", partner_id)
        payouts = await c.fetch(
            "select amount_rub, paid_at, method, note from partner_payouts "
            "where partner_id = $1 order by paid_at desc limit 100", partner_id)
        clients = await c.fetch(
            "select t.id, t.name, t.created_at from tenants t "
            "where t.partner_id = $1 order by t.created_at desc", partner_id)
    totals = (await partner_totals([partner_id])).get(str(partner_id),
                                                      {"accrued": Decimal(0), "paid": Decimal(0), "due": Decimal(0)})
    return {"accruals": accruals, "payouts": payouts, "clients": clients, "totals": totals}


async def partner_team_data(partner_id) -> list:
    """Приведённые партнёры и начисленное С НИХ.

    ⚠️ Чужих клиентов по именам наставник не видит: он получает процент с оборота, а не
    доступ к чужой клиентской базе (спека §8.3).
    """
    async with pool.acquire() as c:
        team = await c.fetch(
            "select id, name, joined_at, status from partners where parent_id = $1 "
            "order by joined_at desc", partner_id)
        earned = await c.fetch(
            "select coalesce(sum(amount_rub),0) s from partner_accruals "
            "where partner_id = $1 and level = 1", partner_id)
    return [dict(r) for r in team], (earned[0]["s"] if earned else Decimal(0))
```

- [ ] **Шаг 2: Маршруты в `admin-panel/app.py`**

```python
def _require_partner(session: auth.Session):
    """Кабинет открывает СТРОКА в partners, а не роль: партнёр не член команды."""
    if session.role != "partner":
        raise HTTPException(status_code=403, detail="Раздел партнёра")


@app.get("/partner", response_class=HTMLResponse)
async def partner_cabinet(request: Request, session: auth.Session = Depends(require_session)):
    _require_partner(session)
    partner = await db.partner_by_login_actor(session.actor)
    if not partner:
        raise HTTPException(status_code=403, detail="Партнёр не найден или отключён")
    data = await db.partner_cabinet_data(partner["id"])
    return templates.TemplateResponse(request, "partner_cabinet.html", {
        "partner": partner, "base_url": config.SERVICE_SITE_URL, "session": session,
        "csrf_token": session.csrf_token, "active": "partner", **data})


@app.get("/partner/team", response_class=HTMLResponse)
async def partner_team(request: Request, session: auth.Session = Depends(require_session)):
    _require_partner(session)
    partner = await db.partner_by_login_actor(session.actor)
    if not partner:
        raise HTTPException(status_code=403, detail="Партнёр не найден или отключён")
    team, earned = await db.partner_team_data(partner["id"])
    return templates.TemplateResponse(request, "partner_team.html", {
        "partner": partner, "team": team, "earned": earned, "session": session,
        "csrf_token": session.csrf_token, "active": "partner"})
```

- [ ] **Шаг 3: Шаблон `admin-panel/templates/partner_cabinet.html`**

Пустой кабинет показывает онбординг, а не пустую таблицу (спека §8.3).

```html
{% extends "base.html" %}
{% block title %}Мой кабинет{% endblock %}
{% block content %}
<div class="page-head">
  <h1 class="page-head__title">{{ partner.name|e }}</h1>
  <p class="page-head__hint">Ваша ссылка:
    <code class="mono">{{ base_url }}/p/{{ partner.ref_code }}</code>
    — отдайте её клиенту. Когда он оплатит, вознаграждение появится здесь само.</p>
</div>

<section class="section">
  <div class="stat-row">
    <div class="stat"><span class="stat__label">Начислено</span>
      <span class="stat__value mono">{{ '%.2f'|format(totals.accrued) }} ₽</span></div>
    <div class="stat"><span class="stat__label">Выплачено</span>
      <span class="stat__value mono">{{ '%.2f'|format(totals.paid) }} ₽</span></div>
    <div class="stat"><span class="stat__label">К выплате</span>
      <span class="stat__value mono">{{ '%.2f'|format(totals.due) }} ₽</span></div>
  </div>
</section>

{% if not clients and not accruals %}
<section class="card">
  <h2 class="card__title">Пока пусто — и это нормально</h2>
  <p class="card__note">Отдайте ссылку выше клиенту. Он назовёт компанию, мы заведём
    кабинет, а вознаграждение начислится в момент первой оплаты — 20% от неё. Спрашивать
    нас не нужно: сумма появится на этой странице.</p>
</section>
{% else %}
<section class="section" aria-label="Клиенты">
  <h2 class="section__title section__title--lg">Приведённые клиенты</h2>
  <div class="table-wrap"><table class="table table--zebra">
    <thead><tr><th>Компания</th><th>Заведён</th></tr></thead>
    <tbody>{% for c in clients %}
      <tr><td>{{ c.name|e }}</td>
          <td class="nowrap muted">{{ c.created_at.strftime('%d.%m.%Y') }}</td></tr>
    {% endfor %}</tbody>
  </table></div>
</section>

<section class="section" aria-label="Начисления">
  <h2 class="section__title section__title--lg">Начисления</h2>
  <div class="table-wrap"><table class="table table--zebra">
    <thead><tr><th>Дата</th><th>За что</th><th>Ставка</th><th>Сумма</th></tr></thead>
    <tbody>{% for a in accruals %}
      <tr>
        <td class="nowrap muted">{{ a.created_at.strftime('%d.%m.%Y') }}</td>
        <td>{% if a.reason == 'sale' %}Ваша продажа
            {% elif a.reason == 'mentor' %}Продажа вашего партнёра
            {% else %}Возврат клиенту{% endif %}</td>
        <td class="mono">{{ '%.0f'|format(a.rate_percent) }}%</td>
        <td class="mono">{{ '%.2f'|format(a.amount_rub) }} ₽</td>
      </tr>
    {% endfor %}</tbody>
  </table></div>
</section>

<section class="section" aria-label="Выплаты">
  <h2 class="section__title section__title--lg">Выплаты</h2>
  {% if payouts %}
  <div class="table-wrap"><table class="table table--zebra">
    <thead><tr><th>Дата</th><th>Способ</th><th>Сумма</th></tr></thead>
    <tbody>{% for p in payouts %}
      <tr><td class="nowrap muted">{{ p.paid_at.strftime('%d.%m.%Y') }}</td>
          <td>{{ p.method|e if p.method else '—' }}</td>
          <td class="mono">{{ '%.2f'|format(p.amount_rub) }} ₽</td></tr>
    {% endfor %}</tbody>
  </table></div>
  {% else %}<p class="hint muted">Выплат пока не было.</p>{% endif %}
</section>
{% endif %}
{% endblock %}
```

- [ ] **Шаг 4: Шаблон `admin-panel/templates/partner_team.html`**

```html
{% extends "base.html" %}
{% block title %}Мои партнёры{% endblock %}
{% block content %}
<nav class="crumbs"><a href="/partner">← Мой кабинет</a></nav>
<div class="page-head">
  <h1 class="page-head__title">Мои партнёры</h1>
  <p class="page-head__hint">С их продаж вам идёт 5% сверх — из нашей доли, их
    вознаграждение от этого не уменьшается. Начисления идут 12 месяцев с даты, когда
    партнёр к вам присоединился.</p>
</div>
<section class="section">
  <div class="stat"><span class="stat__label">Начислено с их продаж</span>
    <span class="stat__value mono">{{ '%.2f'|format(earned) }} ₽</span></div>
</section>
{% if team %}
<div class="table-wrap"><table class="table table--zebra">
  <thead><tr><th>Партнёр</th><th>С нами с</th><th>Статус</th></tr></thead>
  <tbody>{% for t in team %}
    <tr><td>{{ t.name|e }}</td>
        <td class="nowrap muted">{{ t.joined_at.strftime('%d.%m.%Y') }}</td>
        <td>{% if t.status == 'active' %}<span class="pill pill--ok">Активен</span>
            {% else %}<span class="pill pill--bad">Отключён</span>{% endif %}</td></tr>
  {% endfor %}</tbody>
</table></div>
{% else %}
<section class="card">
  <h2 class="card__title">Вы пока никого не пригласили</h2>
  <p class="card__note">Если приведёте партнёра, с каждой его продажи вам будет идти 5%
    сверх — первые 12 месяцев. Напишите нам, и мы выдадим ему доступ.</p>
</section>
{% endif %}
{% endblock %}
```

- [ ] **Шаг 5: Прогнать смоук гейта — новые маршруты обязаны остаться единственными открытыми**

```bash
cd /Users/konstantin/Downloads/risuy-wt-landing && PYTHONPATH=admin-panel:. /Users/konstantin/Downloads/risuy-ecosystem/.venv-smoke/bin/python scripts/partner_gate_smoke.py; echo "код возврата: $?"
```

- [ ] **Шаг 6: Коммит**

```bash
git add admin-panel/db.py admin-panel/app.py admin-panel/templates/partner_cabinet.html admin-panel/templates/partner_team.html && git commit -m "feat(partners): кабинет партнёра — начисления, выплаты, приведённые партнёры"
```

---

### Задача 7: Экраны владельца

**Файлы:**
- Изменить: `admin-panel/db.py` — `create_partner_payout`, `set_partner_rate`, `set_partner_parent`
- Изменить: `admin-panel/app.py` — маршруты выплаты, ставки, наставника
- Изменить: `admin-panel/templates/partner_detail.html`, `admin-panel/templates/partners.html`

**Интерфейсы:**
- Потребляет: `db.partner_totals` (задача 6), `partner_money.would_create_cycle` (задача 1),
  `db.create_partner_invite` (задача 5).
- Отдаёт наружу: ничего для последующих задач.

- [ ] **Шаг 1: Дописать проверку цикла и выплаты в `scripts/partner_money_db_smoke.py`**

```python
            print("8. Наставника нельзя привязать так, чтобы получился цикл:")
            ok = await db.set_partner_parent(mentor_id, seller_id, actor="smoke")
            check("привязка, дающая цикл, отбита", ok is False, f"вернул={ok}")
            ok2 = await db.set_partner_parent(seller_id, mentor_id, actor="smoke")
            check("нормальная привязка проходит", ok2 is True, f"вернул={ok2}")

            print("9. Выплата уменьшает «к выплате»:")
            await db.create_partner_payout(seller_id, Decimal("500.00"),
                                           method="СБП", note=None, actor="smoke")
            t = (await db.partner_totals([seller_id]))[str(seller_id)]
            check("выплачено 500.00", t["paid"] == Decimal("500.00"), str(t["paid"]))
            check("к выплате = начислено − выплачено",
                  t["due"] == t["accrued"] - t["paid"], f"{t}")
```

- [ ] **Шаг 2: Прогнать смоук — блоки 8–9 обязаны упасть**

```bash
cd /Users/konstantin/Downloads/risuy-wt-landing && PARTNER_MONEY_SMOKE_DSN="<dsn risuy_dev>" PYTHONPATH=admin-panel:. /Users/konstantin/Downloads/risuy-ecosystem/.venv-smoke/bin/python scripts/partner_money_db_smoke.py; echo "код возврата: $?"
```

Ожидаем `AttributeError: module 'db' has no attribute 'set_partner_parent'`.

- [ ] **Шаг 3: Функции в `admin-panel/db.py`**

```python
async def create_partner_payout(partner_id, amount_rub, *, method, note, actor: str) -> str:
    """Отметить выплату. Перевод делает человек: PayPal, Stripe и Wise в РФ не работают,
    писать под них интеграцию бессмысленно. В системе — только отметка."""
    async with pool.acquire() as c:
        async with c.transaction():
            owner = await c.fetchval("select owner_tenant_id from partners where id = $1", partner_id)
            new_id = await c.fetchval(
                "insert into partner_payouts (partner_id, owner_tenant_id, amount_rub, method, "
                "                             note, created_by) values ($1,$2,$3,$4,$5,$6) returning id",
                partner_id, owner, amount_rub, method, note, actor)
            await _insert_audit(c, actor=actor, action="partner_payout_created",
                                detail={"partner_id": str(partner_id), "amount": str(amount_rub)})
    return str(new_id)


async def set_partner_rate(partner_id, rate_percent, *, actor: str) -> bool:
    """Сменить ставку партнёра. Прошлое не трогает: в начислениях лежит КОПИЯ ставки на
    момент начисления, и правка настроек её не переписывает."""
    async with pool.acquire() as c:
        async with c.transaction():
            res = await c.execute("update partners set rate_percent = $1 where id = $2",
                                  rate_percent, partner_id)
            await _insert_audit(c, actor=actor, action="partner_rate_changed",
                                detail={"partner_id": str(partner_id), "rate": str(rate_percent)})
    return res.endswith("1")


async def set_partner_parent(partner_id, parent_id, *, actor: str) -> bool:
    """Назначить наставника. Возвращает False, если привязка создала бы цикл.

    Дерево читаем целиком: партнёров десятки, а не миллионы, и рекурсивный CTE ради этого
    не нужен. Проверка живёт в чистой функции и покрыта смоуком без базы.
    """
    async with pool.acquire() as c:
        rows = await c.fetch("select id, parent_id from partners")
        parent_of = {str(r["id"]): (str(r["parent_id"]) if r["parent_id"] else None) for r in rows}
        if partner_money.would_create_cycle(str(partner_id),
                                            str(parent_id) if parent_id else None, parent_of):
            await _insert_audit(c, actor=actor, action="partner_parent_cycle_rejected",
                                detail={"partner_id": str(partner_id), "parent_id": str(parent_id)})
            return False
        async with c.transaction():
            await c.execute("update partners set parent_id = $1 where id = $2", parent_id, partner_id)
            await _insert_audit(c, actor=actor, action="partner_parent_set",
                                detail={"partner_id": str(partner_id), "parent_id": str(parent_id)})
    return True
```

- [ ] **Шаг 4: Маршруты в `admin-panel/app.py`**

Рядом с существующими `/partners/*`; каждый начинается с `auth.check_csrf` и проверки
`session.is_platform` — по образцу `partners_set_status` (L6847).

```python
@app.post("/partners/{partner_id}/payout")
async def partners_payout(request: Request, partner_id: uuid.UUID,
                          session: auth.Session = Depends(require_session),
                          csrf_token: str = Form(...), amount: str = Form(...),
                          method: str = Form(""), note: str = Form("")):
    auth.check_csrf(session, csrf_token)
    if not session.is_platform:
        raise HTTPException(status_code=403, detail="Только владелец платформы")
    try:
        value = Decimal(amount.replace(",", ".").strip())
    except (InvalidOperation, AttributeError):
        return RedirectResponse(url=f"/partners/{partner_id}?err=bad_amount", status_code=303)
    if value <= 0:
        return RedirectResponse(url=f"/partners/{partner_id}?err=bad_amount", status_code=303)
    await db.create_partner_payout(partner_id, value, method=method.strip() or None,
                                   note=note.strip() or None, actor=session.actor)
    return RedirectResponse(url=f"/partners/{partner_id}?saved=payout", status_code=303)


@app.post("/partners/{partner_id}/rate")
async def partners_rate(request: Request, partner_id: uuid.UUID,
                        session: auth.Session = Depends(require_session),
                        csrf_token: str = Form(...), rate: str = Form(...)):
    auth.check_csrf(session, csrf_token)
    if not session.is_platform:
        raise HTTPException(status_code=403, detail="Только владелец платформы")
    try:
        value = Decimal(rate.replace(",", ".").strip())
    except (InvalidOperation, AttributeError):
        return RedirectResponse(url=f"/partners/{partner_id}?err=bad_rate", status_code=303)
    if not (0 <= value <= 100):
        return RedirectResponse(url=f"/partners/{partner_id}?err=bad_rate", status_code=303)
    await db.set_partner_rate(partner_id, value, actor=session.actor)
    return RedirectResponse(url=f"/partners/{partner_id}?saved=rate", status_code=303)


@app.post("/partners/{partner_id}/parent")
async def partners_parent(request: Request, partner_id: uuid.UUID,
                          session: auth.Session = Depends(require_session),
                          csrf_token: str = Form(...), parent_id: str = Form("")):
    auth.check_csrf(session, csrf_token)
    if not session.is_platform:
        raise HTTPException(status_code=403, detail="Только владелец платформы")
    parent = uuid.UUID(parent_id) if parent_id.strip() else None
    ok = await db.set_partner_parent(partner_id, parent, actor=session.actor)
    if not ok:
        return RedirectResponse(url=f"/partners/{partner_id}?err=cycle", status_code=303)
    return RedirectResponse(url=f"/partners/{partner_id}?saved=parent", status_code=303)
```

Убедиться, что в шапке `app.py` есть `from decimal import Decimal, InvalidOperation`.
Также расширить `partner_detail` (L6874), чтобы он отдавал в шаблон начисления, выплаты,
итоги (`db.partner_totals`), список партнёров для выбора наставника и `invite` из
query-параметров.

- [ ] **Шаг 5: Дописать `partner_detail.html`**

Добавить после существующего блока тенантов. У каждого поля — раскрывающееся «Зачем это
поле» по-русски: клиент не читает наши доки и не может спросить разработчика.

```html
<section class="section" aria-label="Деньги партнёра">
  <h2 class="section__title section__title--lg">Начислено и выплачено</h2>
  <div class="stat-row">
    <div class="stat"><span class="stat__label">Начислено</span>
      <span class="stat__value mono">{{ '%.2f'|format(totals.accrued) }} ₽</span></div>
    <div class="stat"><span class="stat__label">Выплачено</span>
      <span class="stat__value mono">{{ '%.2f'|format(totals.paid) }} ₽</span></div>
    <div class="stat"><span class="stat__label">К выплате</span>
      <span class="stat__value mono">{{ '%.2f'|format(totals.due) }} ₽</span></div>
  </div>

  {% if invite %}
  <p class="flash flash--ok">Ссылка для входа (покажите её партнёру, повторно она не
    откроется): <code class="mono">{{ base_url }}/partner/join/{{ invite }}</code></p>
  {% endif %}
  {% if err == 'cycle' %}<p class="flash flash--error">Так нельзя: этот партнёр уже
    находится выше по цепочке, получился бы замкнутый круг.</p>{% endif %}
  {% if err == 'bad_amount' %}<p class="flash flash--error">Сумма должна быть больше нуля.</p>{% endif %}
  {% if err == 'bad_rate' %}<p class="flash flash--error">Ставка — число от 0 до 100.</p>{% endif %}

  <form method="post" action="/partners/{{ partner.id }}/payout" class="form-row">
    <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
    <label class="field"><span class="field__label">Сумма выплаты, ₽</span>
      <input class="field__input mono" name="amount" required inputmode="decimal"></label>
    <label class="field"><span class="field__label">Способ</span>
      <span class="field__hint">Карта, СБП, счёт ИП — свободный текст, для вашей же памяти.</span>
      <input class="field__input" name="method" maxlength="80"></label>
    <button class="btn btn--dark" type="submit">Отметить выплату</button>
  </form>
  <details class="hint"><summary>Зачем это поле</summary>
    Перевод вы делаете сами — картой, по СБП или на счёт ИП. Здесь только отметка, чтобы
    «к выплате» уменьшилось и партнёр видел ту же цифру, что и вы. Если ошиблись — не
    удаляйте строку, а сделайте новую отметку и напишите это в примечании.</details>

  <form method="post" action="/partners/{{ partner.id }}/rate" class="form-row">
    <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
    <label class="field"><span class="field__label">Ставка, %</span>
      <input class="field__input mono" name="rate" value="{{ '%.0f'|format(partner.rate_percent) }}"></label>
    <button class="btn btn--soft" type="submit">Сохранить ставку</button>
  </form>
  <details class="hint"><summary>Зачем это поле</summary>
    Доля партнёра с первой оплаты приведённого клиента. Прошлые начисления не изменятся:
    в каждом из них лежит ставка на момент начисления. Пусто оставить нельзя — по
    умолчанию 20%.</details>

  <form method="post" action="/partners/{{ partner.id }}/parent" class="form-row">
    <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
    <label class="field"><span class="field__label">Наставник</span>
      <select class="field__input" name="parent_id">
        <option value="">— нет —</option>
        {% for p in all_partners if p.id != partner.id %}
        <option value="{{ p.id }}" {% if partner.parent_id == p.id %}selected{% endif %}>{{ p.name|e }}</option>
        {% endfor %}
      </select></label>
    <button class="btn btn--soft" type="submit">Сохранить наставника</button>
  </form>
  <details class="hint"><summary>Зачем это поле</summary>
    Кто привёл и обучил этого партнёра. Наставнику идёт 5% сверх с продаж подопечного —
    из нашей доли, партнёр от этого не теряет ничего. Срок — 12 месяцев с даты, когда
    подопечный присоединился. Если поле пусто, наставнических нет.</details>

  <form method="post" action="/partners/{{ partner.id }}/invite">
    <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
    <button class="btn btn--soft" type="submit">Выдать доступ в кабинет</button>
  </form>
  <details class="hint"><summary>Зачем это нужно</summary>
    Создаёт одноразовую ссылку на трое суток. Партнёр по ней сам заведёт email и пароль —
    пароля мы не видим. Пока доступ не выдан, партнёр своих начислений не видит.</details>
</section>

<section class="section" aria-label="Начисления">
  <h2 class="section__title section__title--lg">Начисления</h2>
  <div class="table-wrap"><table class="table table--zebra">
    <thead><tr><th>Дата</th><th>Причина</th><th>Уровень</th><th>Ставка</th><th>Сумма</th></tr></thead>
    <tbody>{% for a in accruals %}
      <tr><td class="nowrap muted">{{ a.created_at.strftime('%d.%m.%Y') }}</td>
          <td>{{ {'sale':'продажа','mentor':'наставнические','refund':'возврат'}[a.reason] }}</td>
          <td class="mono">{{ a.level }}</td>
          <td class="mono">{{ '%.0f'|format(a.rate_percent) }}%</td>
          <td class="mono">{{ '%.2f'|format(a.amount_rub) }} ₽</td></tr>
    {% endfor %}</tbody>
  </table></div>
</section>
```

- [ ] **Шаг 6: Дописать колонки в `partners.html`**

В `<thead>` после «Тенантов» добавить `<th scope="col">Начислено</th><th scope="col">К выплате</th>`,
в теле строки — соответствующие ячейки из `totals[p.id|string]`. `partners_page` (L6825)
дополнить вызовом `db.partner_totals([p["id"] for p in partners])`.

- [ ] **Шаг 7: Прогнать все три смоука и линт**

```bash
cd /Users/konstantin/Downloads/risuy-wt-landing && PARTNER_MONEY_SMOKE_DSN="<dsn risuy_dev>" PYTHONPATH=admin-panel:. /Users/konstantin/Downloads/risuy-ecosystem/.venv-smoke/bin/python scripts/partner_money_db_smoke.py && PYTHONPATH=admin-panel:. /Users/konstantin/Downloads/risuy-ecosystem/.venv-smoke/bin/python scripts/partner_money_calc_smoke.py && PYTHONPATH=admin-panel:. /Users/konstantin/Downloads/risuy-ecosystem/.venv-smoke/bin/python scripts/partner_gate_smoke.py && ruff check admin-panel/ scripts/partner_money_db_smoke.py; echo "код возврата: $?"
```

- [ ] **Шаг 8: Коммит**

```bash
git add admin-panel/db.py admin-panel/app.py admin-panel/templates/partner_detail.html admin-panel/templates/partners.html scripts/partner_money_db_smoke.py && git commit -m "feat(partners): экраны владельца — выплаты, ставка, наставник, выдача доступа"
```

---

### Задача 8: Contract-миграция RLS

Выполняется **после** деплоя кода задач 1–7, а не вместе с ним: до деплоя политика
отрезала бы чтения, которые старый код делает напрямую.

**Файлы:**
- Создать: `db/migrate_partner_money_rls.sql`
- Создать: `scripts/partner_rls_smoke.py`

**Интерфейсы:**
- Потребляет: таблицы задачи 2.
- Отдаёт наружу: ничего.

- [ ] **Шаг 1: Написать `db/migrate_partner_money_rls.sql`**

```sql
-- CONTRACT-шаг партнёрского кабинета: включение RLS (спека §5.5).
-- 🔴 ПРИМЕНЯТЬ ТОЛЬКО ПОСЛЕ деплоя кода, который ходит за начислениями через
-- SECURITY DEFINER (partner_pair_for_client / insert_partner_accrual). До этого политика
-- отрежет вебхуку платформенного партнёра, и начисления молча перестанут появляться.
--
-- ENABLE, а не FORCE: владелец таблиц (gen_user) обязан обходить политику, иначе
-- SECURITY DEFINER-функции перестанут работать — ровно как на orders.
--
--   bash ~/.claude/scripts/twc-migrate.sh 4171827 81.31.246.136 risuy_dev gen_user \
--       /Users/konstantin/Downloads/risuy-wt-landing/db/migrate_partner_money_rls.sql

alter table partners          enable row level security;
alter table partner_accruals  enable row level security;
alter table partner_payouts   enable row level security;
alter table partner_invites   enable row level security;

-- nullif: пустой app.tenant_id не должен давать 22P02 (урок tenant_agents, 03.08).
-- Платформенные строки (owner_tenant_id is null) видны только при ПУСТОМ GUC, то есть
-- платформенному владельцу; тенант видит ровно свои.
drop policy if exists partner_scope on partners;
create policy partner_scope on partners
    using      (owner_tenant_id is not distinct from nullif(current_setting('app.tenant_id', true), '')::uuid)
    with check (owner_tenant_id is not distinct from nullif(current_setting('app.tenant_id', true), '')::uuid);

drop policy if exists partner_accruals_scope on partner_accruals;
create policy partner_accruals_scope on partner_accruals
    using      (owner_tenant_id is not distinct from nullif(current_setting('app.tenant_id', true), '')::uuid)
    with check (owner_tenant_id is not distinct from nullif(current_setting('app.tenant_id', true), '')::uuid);

drop policy if exists partner_payouts_scope on partner_payouts;
create policy partner_payouts_scope on partner_payouts
    using      (owner_tenant_id is not distinct from nullif(current_setting('app.tenant_id', true), '')::uuid)
    with check (owner_tenant_id is not distinct from nullif(current_setting('app.tenant_id', true), '')::uuid);

-- Приглашения контура не имеют: гасятся ДО появления сессии, когда GUC пуст в принципе.
-- Изоляция здесь — секретность самого токена (в базе только sha256).
drop policy if exists partner_invites_open on partner_invites;
create policy partner_invites_open on partner_invites using (true) with check (true);
```

**Снимок для отката** записать в хендофф до применения: до этой миграции RLS на всех
четырёх таблицах выключен, политик нет. Откат — `alter table … disable row level security`
плюс `drop policy`.

- [ ] **Шаг 2: Смоук изоляции `scripts/partner_rls_smoke.py`**

Проверяет под ролью `panel_rw` (не владельцем — владелец обходит политику):
тенант не видит платформенных партнёров и чужих начислений; пустой GUC не даёт 22P02;
начисление платформенному партнёру всё ещё проходит через SECURITY DEFINER при
выставленном чужом `app.tenant_id`. Структура — как у `partner_money_db_smoke.py`, DSN в
`PARTNER_RLS_SMOKE_DSN`, подключение под `panel_rw`.

- [ ] **Шаг 3: Применить на `risuy_dev` и прогнать смоук**

```bash
bash ~/.claude/scripts/twc-migrate.sh 4171827 81.31.246.136 risuy_dev gen_user /Users/konstantin/Downloads/risuy-wt-landing/db/migrate_partner_money_rls.sql && cd /Users/konstantin/Downloads/risuy-wt-landing && PARTNER_RLS_SMOKE_DSN="<dsn panel_rw на risuy_dev>" PYTHONPATH=admin-panel:. /Users/konstantin/Downloads/risuy-ecosystem/.venv-smoke/bin/python scripts/partner_rls_smoke.py; echo "код возврата: $?"
```

- [ ] **Шаг 4: Перепрогнать смоук начислений — он обязан остаться зелёным под RLS**

```bash
cd /Users/konstantin/Downloads/risuy-wt-landing && PARTNER_MONEY_SMOKE_DSN="<dsn risuy_dev>" PYTHONPATH=admin-panel:. /Users/konstantin/Downloads/risuy-ecosystem/.venv-smoke/bin/python scripts/partner_money_db_smoke.py; echo "код возврата: $?"
```

- [ ] **Шаг 5: Коммит**

```bash
git add db/migrate_partner_money_rls.sql scripts/partner_rls_smoke.py && git commit -m "feat(partners): изоляция партнёрских таблиц по контуру"
```

---

### Задача 9: Живой прогон на проде

🔴 Выполняется **только по явному «да» владельца** на каждое из трёх действий: прод-DDL,
push (= деплой) и создание проверочных записей в боевой базе.

Из трёх серьёзных дефектов аналогичной работы в ProAgent AI смоуки поймали ноль: нули в
списках, 404 при выставленном флаге и застрявшая строка — все три видны только на живых
данных. Поэтому прогон обязателен, а не желателен.

- [ ] **Шаг 1: Спросить владельца тремя отдельными вопросами** — DDL, деплой, тестовые
  записи. Разрешение на одно не означает разрешения на другое.
- [ ] **Шаг 2: Снять снимок ДО** — количество строк в `partners`, `partner_accruals`,
  `partner_payouts`; записать в хендофф.
- [ ] **Шаг 3: Применить expand-миграцию на прод** (`risuy` вместо `risuy_dev`).
- [ ] **Шаг 4: Задеплоить код** и проверить, что панель и бот `active` на новом коммите
  (`twc apps get 205025` / `201859`).
- [ ] **Шаг 5: Прогнать цикл целиком:** создать партнёра с именем, начинающимся на
  `ПРОВЕРКА `, выдать доступ, войти под ним, привязать тестового тенанта, провести
  платёж, увидеть начисление в кабинете, отметить выплату, увидеть уменьшение «к выплате».
- [ ] **Шаг 6: Проверить, что второй платёж того же клиента начислений НЕ создал** —
  это главное правило, и на проде оно проверяется первый раз.
- [ ] **Шаг 7: Применить contract-миграцию RLS** и повторить шаг 5 на втором проверочном
  партнёре — политика включается после кода, и проверять надо после неё.
- [ ] **Шаг 8: Убрать за собой** одним условием: `delete … where name like 'ПРОВЕРКА %'`
  в порядке начисления → выплаты → партнёр. Сверить снимок ПОСЛЕ со снимком ДО.
- [ ] **Шаг 9: Записать в хендофф** результат, снимок для отката и всё, что вылезло.

---

## Самопроверка плана

**Покрытие спеки.** §3 — задачи 1–2 (не переносим лишнее). §4 два контура — задача 2
(`owner_tenant_id`) и задача 3 (`mentors_enabled` по контуру). §5.1–5.4 схема — задача 2.
§5.5 RLS — задача 8. §5.6 SECURITY DEFINER — задачи 2 и 3. §6 врезки — задача 3. §6.1
сторно — задача 4. §7 чистые расчёты — задача 1. §8.1 вход — задача 5. §8.2 белый список —
задача 5. §8.3 кабинет — задача 6. §8.4 экраны владельца — задача 7. §9 экономика —
решение владельца, кода не требует. §10 красная линия — задача 2 (check-ограничения,
начисление только из платежа) и задача 3 (`mentors_enabled`). §11 тесты — распределены по
задачам. §12 порядок — совпадает. §13 чего не делаем — не делается. §14 открытые вопросы —
за владельцем, кода не требуют.

**Не покрыто кодом намеренно:** §14.5 (атрибуция задним числом) — отдельная задача после
ответа владельца, поскольку это правка истории и требует решения про аудит.

**Заглушек нет.** Все шаги содержат код или точную команду. Единственные подстановки —
`<dsn risuy_dev>` и `<dsn panel_rw на risuy_dev>`: DSN содержит пароль и в файл плана не
кладётся.

**Согласованность имён** проверена сквозь задачи: `accruals_for_payment`, `PartnerNode`,
`partner_balance`, `would_create_cycle`, `accrue_for_payment`, `partner_totals`,
`partner_by_login_actor`, `partner_pair_for_client`, `insert_partner_accrual` названы
одинаково в определении и во всех вызовах.
