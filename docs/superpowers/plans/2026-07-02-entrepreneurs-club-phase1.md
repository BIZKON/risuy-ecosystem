# «Клуб предпринимателей» — Фаза 1 (клуб тенанта, полный луп) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Рабочий кросс-маркетинговый клуб В ПРЕДЕЛАХ ОДНОГО ТЕНАНТА (Уровень 1): бизнес вступает по согласию → профиль (что даю/ищу + ЕГРЮЛ) → матчинг по цепочке потребления → знакомство по взаимному согласию → обмен контактами.

**Architecture:** Отдельный домен «Клуб» (новые таблицы `club_*` с RLS `tenant_isolation`), переиспользует ЕГРЮЛ (`dadata`/`prospects`), согласие (`consent_events`, append-only, `doc_type`) и нотификатор (`NOTIFIER_BOT_TOKEN`). Уровень 2 (cross-tenant биржа) — ВНЕ этого плана (План 2).

**Tech Stack:** Python (FastAPI/Jinja2 панель `admin-panel/`, aiogram-бот `bot-telegram/`, общий `shared/`), asyncpg, Postgres + RLS, смоуки `scripts/*_smoke.py` через `.venv-smoke`.

## Global Constraints

- 🇷🇺 Только русский (код-комментарии, UI, коммиты).
- **Прод-DDL СНАЧАЛА risuy_dev, ПЕРЕД кодом** (expand-contract): `twc-migrate.sh 4171827 81.31.246.136 risuy_dev gen_user db/<file>.sql`. Прод-накат + push/деплой — по явному «да».
- **RLS-паттерн (verbatim):** `using (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)` (как `db/migrate_consent_events.sql:37`). Каждая tenant-scoped таблица несёт `tenant_id`.
- **Согласия — в `consent_events`** (append-only, CHECK `action in ('granted','revoked')` НЕ менять): различаем по `doc_type` ∈ (`club_join`,`network_join`,`intro_accept`). Аддитивно `consent_events.member_id uuid`.
- **column-INSERT гранты** новых tenant-scoped таблиц → `db/panel_role.sql` (грабля §8 хендоффа; owner-смоук не ловит).
- **Красная линия:** только согласившиеся члены; первый обмен контактами — только по ВЗАИМНОМУ согласию (`intro_accept` с обеих сторон).
- **Уровень 2 (`network_opt_in`, cross-tenant) — ВНЕ scope Фазы 1.** Колонка `network_opt_in` заводится в DDL (дефолт false), но cross-tenant-функции НЕ реализуются здесь.
- Коммит явными файлами (НЕ `CLAUDE.md`/`.claude/`/`.gitignore` — graphify). Смоуки: DB — только risuy_dev (гард `TEAM_DSN`); db-смоуки контроллер гоняет сам inline (owner-DSN не в субагенты).
- **Тексты согласия выверяет юрист до боевого включения** — код лишь доставляет согласованный текст (как pre-launch gate DaData).

---

### Task 1: Прод-DDL домена клуба

**Files:**
- Create: `db/migrate_club.sql`
- Modify: `db/panel_role.sql` (добавить гранты `club_*`)
- Test: применение на risuy_dev (валидация)

**Interfaces:**
- Produces: таблицы `club_members`, `club_profiles`, `club_intros`; колонка `consent_events.member_id`.

- [ ] **Step 1: Написать `db/migrate_club.sql`**

```sql
-- Клуб предпринимателей — домен (Фаза 1, Уровень 1). RLS tenant_isolation по app.tenant_id
-- (как migrate_consent_events.sql). network_opt_in заводится, но Уровень 2 — вне Фазы 1.
create table if not exists club_members (
    id             uuid primary key default gen_random_uuid(),
    tenant_id      uuid not null references tenants(id) on delete cascade,
    lead_id        uuid references leads(id) on delete set null,   -- если промоушен лида
    inn            text,                                           -- связь с prospects (ЕГРЮЛ), опц.
    display_name   text not null,
    city           text,
    okved          text,
    status         text not null default 'active' check (status in ('active','paused','left')),
    network_opt_in boolean not null default false,                 -- Уровень 2 (вне Фазы 1)
    created_at     timestamptz not null default now()
);
create index if not exists club_members_tenant_idx     on club_members (tenant_id, status);
create index if not exists club_members_tenant_city_idx on club_members (tenant_id, city, okved);
create index if not exists club_members_tenant_lead_idx on club_members (tenant_id, lead_id);

create table if not exists club_profiles (
    member_id      uuid primary key references club_members(id) on delete cascade,
    tenant_id      uuid not null references tenants(id) on delete cascade,   -- для RLS
    offering       text,
    avg_check      integer,
    seeking        text,
    chain_position text check (chain_position in ('before','after','both')),
    okved_seek     text,
    description    text
);

create table if not exists club_intros (
    id           uuid primary key default gen_random_uuid(),
    tenant_id    uuid not null references tenants(id) on delete cascade,   -- RLS = инициатор
    from_member  uuid not null references club_members(id) on delete cascade,
    to_member    uuid not null references club_members(id) on delete cascade,
    to_tenant_id uuid references tenants(id) on delete set null,           -- Ур.2 (в Фазе 1 = tenant_id)
    status       text not null default 'requested'
                 check (status in ('requested','accepted','declined','cancelled')),
    message      text,
    created_at   timestamptz not null default now(),
    decided_at   timestamptz
);
create index if not exists club_intros_tenant_idx on club_intros (tenant_id, status, created_at desc);

alter table consent_events add column if not exists member_id uuid references club_members(id) on delete set null;

-- RLS tenant_isolation (паттерн migrate_consent_events.sql)
do $$
declare t text;
begin
  foreach t in array array['club_members','club_profiles','club_intros'] loop
    execute format('alter table %I enable row level security', t);
    if not exists (select 1 from pg_policies where tablename=t and policyname='tenant_isolation') then
      execute format($f$create policy tenant_isolation on %I for all
        using (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
        with check (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)$f$, t);
    end if;
  end loop;
end $$;
```

- [ ] **Step 2: Гранты `panel_rw` в `db/panel_role.sql`**

Найти в файле блок табличных грантов (по образцу соседних tenant-scoped таблиц, напр. `consent_events`) и добавить:

```sql
grant select, insert, update on club_members  to panel_rw;  -- update: status/network_opt_in/профильные поля
grant select, insert, update on club_profiles to panel_rw;
grant select, insert, update on club_intros   to panel_rw;  -- update: status/decided_at
-- consent_events уже покрыт (append-only); member_id — существующий table-level select/insert грант
```
*(delete не выдаём — «уход» = status='left'. Точный синтаксис/группировку — по образцу соседних строк файла.)*

- [ ] **Step 3: Применить на risuy_dev + валидировать**

Run (контроллер, по «да»): `bash ~/.claude/scripts/twc-migrate.sh 4171827 81.31.246.136 risuy_dev gen_user db/migrate_club.sql`
Валидация: `\d club_members` показывает RLS enabled + колонки; `select count(*) from club_members` = 0.
Expected: таблицы созданы, RLS включён, идемпотентно (повторный прогон без ошибок).

- [ ] **Step 4: Коммит**

```bash
cd ~/Downloads/risuy-ecosystem
git add db/migrate_club.sql db/panel_role.sql
git commit -m "feat(club): прод-DDL домена клуба (club_members/profiles/intros + RLS) + гранты"
```

---

### Task 2: DB-хелперы клуба + тексты согласия

**Files:**
- Modify: `admin-panel/db.py` (CRUD/каталог клуба)
- Create: `shared/club.py` (тексты согласия + константы полей)
- Test: Create `scripts/club_db_smoke.py`

**Interfaces:**
- Produces (`admin-panel/db.py`): `club_member_create(tenant_id, *, display_name, city, okved, lead_id=None, inn=None) -> str`; `club_profile_upsert(member_id, tenant_id, *, offering, seeking, chain_position, okved_seek, avg_check=None, description=None) -> None`; `club_member_list(tenant_id) -> list[dict]`; `club_member_get(member_id, tenant_id) -> dict | None`; `club_consent_record(tenant_id, *, doc_type, member_id=None, lead_id=None, text_hash, channel='web') -> None`.
- Produces (`shared/club.py`): `build_club_consent_text(kind, operator_name) -> str` (kind ∈ 'club_join'|'network_join'|'intro'); `CLUB_CHAIN_POSITIONS: list[tuple[str,str]]`.

- [ ] **Step 1: Прочитать существующие паттерны**

Run: `cd ~/Downloads/risuy-ecosystem && grep -n "async def consent_\|app.tenant_id\|set_active_tenant\|def _insert_audit\|prospect_upsert" admin-panel/db.py | head`
Читать: как соседние функции ставят `app.tenant_id` перед запросом (RLS) и как пишут в `consent_events`. Хелперы клуба ДОЛЖНЫ ставить `app.tenant_id` так же.

- [ ] **Step 2: Написать падающий смоук `scripts/club_db_smoke.py`**

```python
"""Смоук домена клуба (risuy_dev). Гард: TEAM_DSN на risuy_dev.
Запуск: TEAM_DSN=<owner-DSN risuy_dev> PYTHONPATH=admin-panel:. ./.venv-smoke/bin/python scripts/club_db_smoke.py
"""
import asyncio, os, sys
DSN = os.environ.get("TEAM_DSN", "")
if "risuy_dev" not in DSN:
    print("SKIP: нужен TEAM_DSN на risuy_dev"); sys.exit(0)

async def main():
    import asyncpg, importlib
    db = importlib.import_module("db")  # admin-panel/db.py
    db.pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
    # два тенанта для проверки RLS-изоляции
    async with db.pool.acquire() as c:
        rows = await c.fetch("select id from tenants order by created_at limit 2")
    assert len(rows) >= 2, "нужно >=2 тенанта на risuy_dev"
    ta, tb = str(rows[0]["id"]), str(rows[1]["id"])
    try:
        mid = await db.club_member_create(ta, display_name="ООО Тест-А", city="Москва", okved="62.01")
        await db.club_profile_upsert(mid, ta, offering="разработка", seeking="дизайн",
                                     chain_position="before", okved_seek="74.10")
        # RLS: тенант A видит, тенант B — нет
        assert await db.club_member_get(mid, ta) is not None, "A должен видеть свой club_member"
        assert await db.club_member_get(mid, tb) is None, "RLS: B НЕ должен видеть member тенанта A"
        assert any(m["id"] == mid for m in await db.club_member_list(ta)), "list(A) содержит member"
        assert all(m["id"] != mid for m in await db.club_member_list(tb)), "RLS: list(B) НЕ содержит"
        # согласие пишется в consent_events с doc_type
        await db.club_consent_record(ta, doc_type="club_join", member_id=mid, text_hash="abc", channel="web")
        async with db.pool.acquire() as c:
            await c.execute("set local app.tenant_id = $1", ta)
            n = await c.fetchval("select count(*) from consent_events where member_id=$1 and doc_type='club_join'", mid)
        assert n == 1, "согласие club_join записано"
    finally:
        async with db.pool.acquire() as c:
            await c.execute("set local app.tenant_id = $1", ta)
            await c.execute("delete from consent_events where member_id in (select id from club_members where tenant_id=$1)", ta)
            await c.execute("delete from club_members where tenant_id=$1 and display_name like 'ООО Тест-%'", ta)
        await db.pool.close()
    print("OK: club_db_smoke")
asyncio.run(main())
```

- [ ] **Step 3: Запустить смоук — падает** (контроллер, с dev-DSN)

Run: `TEAM_DSN=<risuy_dev> PYTHONPATH=admin-panel:. ./.venv-smoke/bin/python scripts/club_db_smoke.py`
Expected: FAIL (AttributeError: module 'db' has no attribute 'club_member_create').

- [ ] **Step 4: Реализовать хелперы в `admin-panel/db.py`**

По образцу соседних tenant-scoped функций (Step 1): каждая `acquire()` → `set local app.tenant_id = <tenant_id>` → запрос. Сигнатуры — как в **Interfaces**. `club_consent_record` пишет в `consent_events(tenant_id, member_id, lead_id, doc_type, action='granted', text_hash, channel)`. Реальный SQL — параметризованный asyncpg.

- [ ] **Step 5: Создать `shared/club.py` (тексты согласия + константы)**

```python
"""Клуб: тексты согласия (152-ФЗ ст.9 + ФЗ-38 — согласие на матчинг/партнёрские предложения)
и константы. ⚠️ ШАБЛОН: финально выверяет юрист оператора."""
CLUB_CHAIN_POSITIONS = [("before", "До меня в цепочке (даёт трафик)"),
                        ("after", "После меня (даёт допродажу)"),
                        ("both", "И до, и после")]

def build_club_consent_text(kind: str, operator_name: str) -> str:
    if kind == "club_join":
        return ("Вступая в клуб предпринимателей оператора " + operator_name + ", вы соглашаетесь на "
                "обработку данных вашего бизнеса для подбора комплементарных партнёров и получение "
                "предложений о партнёрстве в рамках клуба. Отозвать согласие можно в любой момент.")
    if kind == "network_join":
        return ("Дополнительно вы соглашаетесь сделать профиль вашего бизнеса видимым в общей бирже "
                "предпринимателей и получать предложения о партнёрстве от участников из других кабинетов.")
    if kind == "intro":
        return ("Принимая знакомство, вы соглашаетесь на обмен контактами со вторым участником для "
                "обсуждения партнёрства.")
    return ""
```

- [ ] **Step 6: Смоук зелёный + коммит**

Run: `TEAM_DSN=<risuy_dev> PYTHONPATH=admin-panel:. ./.venv-smoke/bin/python scripts/club_db_smoke.py` → PASS («OK: club_db_smoke»).
```bash
git add admin-panel/db.py shared/club.py scripts/club_db_smoke.py
git commit -m "feat(club): db-хелперы (RLS-scoped) + тексты согласия клуба"
```

---

### Task 3: Вход A — клуб-лидмагнит (регистрация бизнеса)

**Files:**
- Modify: `shared/leadmagnet.py` (тип воронки «club» или отдельные поля клуба)
- Modify: `bot-telegram/` (хендлер регистрации в клуб + запись согласия)
- Test: `scripts/club_signup_smoke.py`

**Interfaces:**
- Consumes: `db.club_member_create`, `db.club_profile_upsert`, `db.club_consent_record` (Task 2); `build_club_consent_text` (Task 2).
- Produces: воронка «Вступить в клуб» → создаёт `club_members`(без lead_id)+`club_profiles`+`consent_events(club_join,granted)`.

- [ ] **Step 1: Прочитать паттерн воронки лид-магнита**

Run: `cd ~/Downloads/risuy-ecosystem && grep -n "FUNNEL_FIELDS\|funnel_enabled\|build_consent_text\|leadmagnet_kind" shared/leadmagnet.py bot-telegram/*.py | head`
Понять, как бот ведёт пошаговый сбор (согласие→имя→...) и как добавить ветку «клуб» (собирает: название, город, ОКВЭД, что даю, что ищу, chain_position + согласие club_join).

- [ ] **Step 2: Смоук `scripts/club_signup_smoke.py` (падающий)**

Проверяет функцию сборки регистрации клуба (чистая, без сети): на вход — dict полей регистрации, на выходе — валидный набор для `club_member_create`+`club_profile_upsert`; согласие-текст непустой. (Полный бот-флоу — ручная проверка live в Task 8.) Assert'ы: валидация обязательных полей (название/город/ОКВЭД), маппинг chain_position, что consent-текст содержит «клуб предпринимателей».

- [ ] **Step 3: Реализовать ветку регистрации** (bot + shared): новый тип воронки/лид-магнита «club»; пошаговый сбор; по завершении — `club_member_create`+`club_profile_upsert`+`club_consent_record(doc_type='club_join', text_hash=sha256(текст))`. Канал = tg/vk/max.

- [ ] **Step 4: Смоук зелёный + py_compile + коммит**

```bash
git add shared/leadmagnet.py bot-telegram/<изменённые> scripts/club_signup_smoke.py
git commit -m "feat(club): вход клуб-лидмагнит — регистрация бизнеса + согласие club_join"
```

---

### Task 4: Вход B — промоушен лида в члены

**Files:**
- Modify: `admin-panel/app.py` (кнопка «Пригласить в клуб» в карточке лида + POST)
- Modify: `admin-panel/templates/` (карточка лида / диалоги)
- Modify: `bot-telegram/` (отправка лиду запроса согласия + обработка ответа)
- Test: `scripts/club_promote_smoke.py`

**Interfaces:**
- Consumes: `db.club_member_create(..., lead_id=<lead>)`, `db.club_consent_record`.
- Produces: лид → (согласие) → `club_members` с `lead_id`.

- [ ] **Step 1** Прочитать, как карточка лида шлёт действия боту (паттерн существующих кнопок в `/dialogs`), и как бот шлёт лиду сообщение (reuse messaging).
- [ ] **Step 2** Падающий render/unit-смоук: кнопка «Пригласить в клуб» видна для лида с согласием (opt-in), POST-роут гейтится (`_require_admin`/тенант + CSRF).
- [ ] **Step 3** Реализовать: POST `/club/invite-lead` → бот шлёт лиду запрос согласия (`build_club_consent_text('club_join')`) → при «да» лида: `club_member_create(lead_id=...)`+`club_consent_record(club_join, lead_id=...)`.
- [ ] **Step 4** Смоук зелёный + коммит (`feat(club): вход промоушен лида в члены клуба`).

---

### Task 5: Каталог `/club` (Уровень 1) + ЕГРЮЛ-обогащение

**Files:**
- Modify: `admin-panel/app.py` (GET `/club` + контекст)
- Create: `admin-panel/templates/club.html`
- Modify: `admin-panel/templates/base.html` (пункт меню «Клуб»)
- Test: `scripts/club_catalog_ui_smoke.py`

**Interfaces:**
- Consumes: `db.club_member_list(tenant_id)`; `dadata`/`prospect_for_*` для ЕГРЮЛ по `inn`.
- Produces: страница каталога (гейт `active_tenant_id`, A1-паттерн как `/companies`).

- [ ] **Step 1** Прочитать `/companies` (GET-хендлер + companies.html + nav в base.html) как эталон структуры/гейта/help_card.
- [ ] **Step 2** Падающий render-смоук (по образцу `agents_ai_inference_rf_ui_smoke`): каталог рендерит членов тенанта; фильтр город/ОКВЭД присутствует; empty-state «Выберите клиента»/«Пока нет участников»; help_card «Зачем клуб» (комплаенс-рамка: только согласившиеся).
- [ ] **Step 3** Реализовать GET `/club` + `club.html` + nav-пункт `club` → «Клуб».
- [ ] **Step 4** render-смоук зелёный + py_compile + коммит (`feat(club): каталог /club Уровень 1 + ЕГРЮЛ-обогащение`).

---

### Task 6: Матчинг по цепочке потребления

**Files:**
- Create: `admin-panel/club_match.py` (чистая логика скоринга)
- Modify: `admin-panel/app.py` (показ матчей в `/club`)
- Test: `scripts/club_match_smoke.py`

**Interfaces:**
- Produces: `score_match(me: dict, other: dict) -> tuple[int, str]` (скор 0..100 + человекочитаемая причина); `rank_matches(me: dict, candidates: list[dict]) -> list[dict]`.

- [ ] **Step 1: Падающий unit-смоук `scripts/club_match_smoke.py` (без БД)**

```python
import sys; sys.path.insert(0, "admin-panel")
from club_match import score_match, rank_matches
me = {"city": "Москва", "okved": "62.01", "chain_position": "after", "okved_seek": "73.11"}
before_same_city = {"city": "Москва", "okved": "73.11", "chain_position": "before", "okved_seek": "62.01"}
other_city = {"city": "Казань", "okved": "73.11", "chain_position": "before", "okved_seek": "62.01"}
s1, why1 = score_match(me, before_same_city)
s2, _ = score_match(me, other_city)
assert s1 > s2, "комплементарный партнёр в том же городе — выше"
assert "город" in why1.lower() or "цепочк" in why1.lower(), "причина человекочитаема"
ranked = rank_matches(me, [other_city, before_same_city])
assert ranked[0]["city"] == "Москва", "ранжирование: лучший первым"
print("OK: club_match_smoke")
```

- [ ] **Step 2** Run → FAIL (нет модуля).
- [ ] **Step 3: Реализовать `admin-panel/club_match.py`** — скор: комплементарность цепочки (my `after` ↔ his `before` = +высокий; `both` совместим с обоими) + пересечение `okved`/`okved_seek` + один город; причина строкой. Без БД, чистые функции.
- [ ] **Step 4** Run → PASS. Подключить `rank_matches` в `/club` (показ «Рекомендуем познакомиться»).
- [ ] **Step 5** Коммит (`feat(club): матчинг по цепочке потребления + причины`).

---

### Task 7: Знакомства (intro-флоу + нотификатор + взаимное согласие)

**Files:**
- Modify: `admin-panel/app.py` (POST `/club/{id}/intro`, accept/decline)
- Modify: `admin-panel/db.py` (`club_intro_create`, `club_intro_decide`)
- Modify: `bot-telegram/` (уведомление члену через нотификатор — reuse `escalation`/`NOTIFIER_BOT_TOKEN`)
- Test: `scripts/club_intro_smoke.py`

**Interfaces:**
- Consumes: `db.club_member_get`, нотификатор.
- Produces: `db.club_intro_create(tenant_id, from_member, to_member) -> str`; `db.club_intro_decide(intro_id, tenant_id, accept: bool) -> None` (accept → `consent_events(intro_accept,granted)` для обеих сторон + раскрытие контактов).

- [ ] **Step 1** Падающий db-смоук `scripts/club_intro_smoke.py` (risuy_dev): создать 2 членов у тенанта → `club_intro_create` (status='requested') → `club_intro_decide(accept=True)` → status='accepted' + `intro_accept` записан; проверить, что до accept контакты НЕ раскрываются (функция раскрытия отдаёт None пока status≠accepted).
- [ ] **Step 2** Run → FAIL.
- [ ] **Step 3** Реализовать хелперы + POST-роуты + уведомление члену-цели через нотификатор (текст «вам предлагают знакомство в клубе»). Взаимность: контакты раскрываются ТОЛЬКО при status='accepted'; `intro_accept`-согласие пишется в момент accept.
- [ ] **Step 4** db-смоук зелёный (контроллер, dev-DSN) + py_compile + коммит (`feat(club): знакомства — intro-флоу, нотификатор, обмен контактами по взаимному согласию`).

---

### Task 8: Верификация + 3-линзовое ревью + деплой-гейт

**Files:** нет (гейт).

- [ ] **Step 1** Прогнать все смоуки: `club_db_smoke` (risuy_dev), `club_signup_smoke`, `club_promote_smoke`, `club_catalog_ui_smoke`, `club_match_smoke`, `club_intro_smoke` (risuy_dev) + регрессы (`consent_text_smoke`, `agents_ai_inference_rf_ui_smoke`).
- [ ] **Step 2** py_compile всех затронутых модулей.
- [ ] **Step 3** 3-линзовое адверсариальное ревью (Workflow): (1) корректность/проводка; (2) **изоляция — RLS на всех `club_*`, `app.tenant_id` ставится везде, НЕТ cross-tenant утечки (Ур.2 не реализован — проверить, что `network_opt_in` нигде не открывает cross-tenant доступ)**; (3) комплаенс — согласия (club_join/intro) пишутся append-only, красная линия (только согласившиеся, взаимное согласие на контакты). Внести подтверждённые findings, повторить смоуки.
- [ ] **Step 4** Прод-DDL накат `migrate_club.sql` на прод `risuy` (по «да»). Push `git push origin docs/security-audit:main` (по «да») → авто-редеплой → поллинг `commit_sha`/`start_time` до active.
- [ ] **Step 5** Пост-деплой: `/club` открывается (панель live); клуб-лидмагнит регистрирует; матчинг показывает партнёра; intro-флоу проходит. ⚠️ **Боевое включение тенантам — после юр-выверки текстов согласия** (§12 спеки).

---

## Self-Review (проведён)

- **Покрытие спеки:** §3 домен → Task 1; §4.3 согласие → Task 2; §5 входы → Task 3+4; §6 каталог/матчинг/знакомства → Task 5+6+7; §9 DDL → Task 1; §10 тесты → каждая задача + Task 8. **Уровень 2 (§4.2) — сознательно ВНЕ Фазы 1** (План 2). Пробелов по Фазе 1 нет.
- **Плейсхолдеры:** DDL, хелпер-сигнатуры, матчинг-логика, смоук-код — конкретны; для route/template boilerplate дано «читать эталон `/companies`/`/agents`» (легитимная опора на существующий паттерн в живой кодовой базе).
- **Согласованность типов:** `club_member_create/club_profile_upsert/club_member_get/club_member_list/club_consent_record/club_intro_create/club_intro_decide`, `score_match/rank_matches`, `build_club_consent_text` — единые имена во всех задачах.
- **Известное ограничение:** Уровень 2 (`network_opt_in`, cross-tenant биржа) — отдельный План 2; колонка заводится, но cross-tenant-функции не реализуются; Task 8 ревью проверяет, что она нигде не открывает изоляцию.
