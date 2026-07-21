# Токен-биллинг · Этап 1 (бэкенд-ядро) — план реализации

> **Для агентов-исполнителей:** ОБЯЗАТЕЛЬНЫЙ СУБ-СКИЛЛ — используй superpowers:subagent-driven-development (рекоменд.) или superpowers:executing-plans, задача-за-задачей. Шаги в формате чек-боксов (`- [ ]`).

**Цель:** перевести биллинг «ИИ-Агент Про» на единый токен-пул (per-resource прайс + 2 бакета кошелька + схлопнуть два счётчика + закрыть утечки + двухчековая фискализация + proration), не сломав Школу Лесова и не допустив двойного списания.

**Архитектура:** переиспользуем `credit_wallets`/`usage_ledger`/`charge_usage()` (единая точка списания); расширяем кошелёк на 2 бакета; курс токена + наценки выносим в `resource_pricing`; старый счётчик «сообщений» депрекейтим через идемпотентную процедуру разреза. Всё expand-first, тесты — смоуки на `risuy_dev`.

**Стек:** Python 3.12 · asyncpg · Postgres (Neon-совместимый на Timeweb) · ЮKassa · смоук-скрипты (не pytest).

**Спека:** `docs/superpowers/specs/2026-07-20-token-billing-tariffs-design.md` (v2).

## Global Constraints (неявно входят в КАЖДУЮ задачу)
- Деньги — ТОЛЬКО целые µRUB (1 ₽ = 1 000 000 µRUB); единственная точка округления — `shared/money.py::ceil_mul` (всегда вверх); никаких float.
- `charge_usage()` — ЕДИНСТВЕННАЯ точка списания; три гвоздя незыблемы: `unique(idempotence_key)`, `SELECT … FOR UPDATE` кошелька, целые µRUB. Рабочие списания — постфактум (`allow_negative=True`).
- Курс/наценка/цена — ТОЛЬКО с сервера (`resource_pricing`/`plans`/`config`), НИКОГДА из клиентского запроса.
- Школа Лесова (`db.default_tenant_id()`) не блокируется при любом балансе (§8.7); переходник без плана не ломается при balance<0.
- `usage_ledger` — append-only: грант panel_rw ТОЛЬКО select+insert (update/delete не выдавать); новые kind — через `'embedding'`/`'other'` + `meta.resource`, без ALTER CHECK.
- Все DDL — **expand-first**, СНАЧАЛА `risuy_dev`, потом прод: `~/.claude/scripts/twc-migrate.sh 4171827 81.31.246.136 {risuy_dev|risuy} gen_user <file.sql>`; expand ПЕРЕД деплоем кода, CONTRACT (unique/drop/NOT NULL) — ПОСЛЕ; **каждый прод-DDL и прод-запуск скрипта — за явным «да» владельца**.
- Гранты новых таблиц/колонок зеркалить И в `schema_*.sql`, И в `db/panel_role.sql`.
- Тесты — смоуки `scripts/*_smoke.py`: своя `*_SMOKE_DSN`, жёсткий гард `'/risuy_dev'` (SystemExit иначе), `check(name,cond,detail)`/`FAILS[]`/`sys.exit`, тест-тенанты `slug 'smoke-%'` create+cleanup. Запуск `X_SMOKE_DSN=… PYTHONPATH=. python3 scripts/x_smoke.py` или `make smoke`.
- E2E платежей/чеков — на ТЕСТ-магазине ЮKassa **1379463** (фискальный прод — 1378536); `SERVICE_RECEIPT_ENABLED=1` только после прогона на тесте; `SERVICE_RENEWAL_ENABLED=0` (D3).
- Всё на русском. Push в main — за явным «да» (классификатор гейтит каждый коммит).

## Порядок под-этапов
**1A прайс-слой** и **1B бакеты** — фундамент (независимы друг от друга, делать первыми). Затем **1C разрез** (зависит 1A+1B), **1D утечки** (зависит 1A+1B), ~~**1E фискализация**~~ (⚠️ ОТЛОЖЕНА — решение №4, вынесена из Этапа 1), **1F жизненный цикл** (зависит 1B). **T-1F-1 (снять автосписания, D3) можно катить ПЕРВЫМ** — независим.

## ✅ Решения владельца (РАЗРЕШЕНО 2026-07-21, сессия 17)
1. **Курс токена → СТРОКА В БД (версионируемая, `effective_from`), НЕ const.** Курс = цена оферты → снимок курса в `usage_ledger` (новая колонка) + аудит смены. Меняет T-1A-1/T-1A-2: `resource_pricing` держит И наценку per-resource, И курс продажи с `effective_from`; `charge_usage` читает курс из БД (паттерн `order by effective_from desc limit 1`, как `model_prices`).
2. **Калибровка — ДАННЫЕ ВЛАДЕЛЬЦА (НЕ блокер старта).** Готовый SQL по gateway-строкам `usage_ledger`. ⚠️ прод-путь `cloud-ai` даёт лишь суммарный объём (`units={tokens_total}`), долю вход/выход НЕ отдаёт → `AI_OUT_TOKENS_SHARE=0.5` останется оценкой, если gateway-строк нет. Маржа 76,5% — приёмка, не код.
3. **Эмбеддер = self-host TEI** (`intfloat/multilingual-e5-base`, 768-dim) — вендорной цены за токен НЕТ (амортизация VM). Ключ `model_prices`: `provider='timeweb-tei'`, `model='multilingual-e5-base'`, `kind='embedding'`. Цифры — из ЛК Timeweb (аренда VM + сторедж ₽/мес; ЛИБО managed-цена ₽/млн). Блокирует только T-1D-1. Плюс дыра: gateway-LLM-модели тоже отсутствуют в `model_prices` → добить в T-1D-1.
4. **Фискализация 1E — ОТЛОЖЕНА** (SERVICE_RECEIPT_ENABLED=OFF, касса/ОФД не задействованы). Под-этап 1E ВЫНОСИТСЯ из Этапа 1 в отдельный будущий этап. Когда возьмёмся: топап→чек аванса (`full_prepayment`/`payment_subject=payment`), зачётный чек — БАТЧ агрегатом за период (день/месяц) одной услугой (НЕ per-списание: `charge_usage` помессажный), новый `yookassa.create_receipt` (~15 строк, POST /v3/receipts, `settlements=prepayment`), сохранять `receipt_email` на топап-платеже. Требует подтверждения бухгалтера по периоду зачёта + тарифу ОФД.
5. **B2B-гейт → РАЗДЕЛЬНО:** identity организации (`buyer_inn`/`buyer_ogrnip`/`buyer_subject_type`) → `tenants`; снимок согласия (`is_entrepreneur`+ИНН+версия оферты+ts) → `payments`. НЕ только на `subscriptions` (затрётся UPDATE'ом при смене тарифа §9). ⚠️ `prospects.inn` = целевые лиды тенанта, НЕ покупатель — не переиспользовать. Валидация с контрольными суммами (ИНН-10/12, ОГРНИП-15). **По умолчанию (владелец не возразил): ИНН обязателен, без чек-бокса «предпринимательская деятельность» оплата не стартует (чистый B2B).** Меняет T-1F-3.
6. **Pre-tenant провижининг → ИЗ ВЕБХУКА ПО EMAIL после оплаты**, анонимная покупка с лендинга разрешена. НЕ пред-создавать черновик (брошенная корзина занимает `unique(email)`). `create_client_account` (НЕ `create_tenant_admin`), серверная таблица `pending_service_purchase` (email+ИНН, НЕ в metadata ЮKassa — 152-ФЗ), claim-письмо через `password_reset_tokens`. ⚠️ добить: `activate_subscription_from_payment` НЕ переводит `tenants.status`→active (баннер горит) — исправить в T-1F-3/1F-2. Меняет T-1F-3.
7. **Даунгрейд → ПУЛ СГОРАЕТ**, возврата разницы абонплаты нет (D1: пул=абонплата 429.4). НЕ возвращать в кошелёк-аванс (иначе абонплата→аванс, сгорание оспоримо). Возвратен по требованию только кошелёк-аванс (782). Требует 2-бакета (1B). Меняет T-1F-2 (+ инвариант «одна живая подписка»).

> Разведка кода (сессия 17, 4 агента) подтвердила: баг §9 реален (`activate_subscription_from_payment` вставляет новую active-строку на каждую оплату + не трогает `tenants.status`); эмбеддер и gateway-модели НЕ тарифицируются (утечка §7.5); Receipts API не обёрнут; `credit_wallets` пока 1 бакет.

---

## Под-этап 1A — Прайс-слой (курс отдельно + per-resource наценки) — ✅ ЗАВЕРШЁН на risuy_dev (сессия 17; прод-DDL risuy — за «да»)

### T-1A-1 · DDL: `resource_pricing` (наценки) + `billing_token_rate` (курс, версионируемый) + снимок курса в `usage_ledger` ⚠️ ПРОД-DDL
**Files:** create `db/schema_billing_v2_pricing.sql` ✅ написан; modify `db/panel_role.sql` ✅
**Решение #1 (курс в БД):** курс продажи токена — ВЕРСИОНИРУЕМАЯ строка `billing_token_rate` (НЕ const), читается `where effective_from <= now() order by effective_from desc limit 1`; снимок курса пишется в `usage_ledger.token_rate_microrub_per_1k` на LLM-списании (аудит смены цены оферты).
**Interfaces (produces):**
- `resource_pricing(resource text pk, markup_multiplier numeric(6,3) not null, note text)`; seed `llm=1.000, dadata=3.000, voice=2.000, embedding=3.000`.
- `billing_token_rate(id identity pk, effective_from timestamptz default now(), rate_microrub_per_1k bigint not null, note, unique(effective_from))`; seed `1_500_000` (0,0015 ₽/ток).
- `usage_ledger.token_rate_microrub_per_1k bigint` (nullable; заполняется только для `kind='llm'`).
Инвариант §7.1: курс × наценка = целевая (LLM 4,26 — вшит в курс, множитель=1; DaData ×3=66,7%; голос ×2=50%).
**Migration (expand):** идемпотентно `create table if not exists …` + seed (`on conflict do nothing` / `where not exists`) + `alter table usage_ledger add column if not exists token_rate_microrub_per_1k bigint` + гранты panel_rw (resource_pricing: select,insert,update; billing_token_rate: select,insert). Платформенные справочники — без RLS (как `model_prices`).
- [x] Написать `scripts/pricing_smoke.py` (гард `/risuy_dev`).
- [x] Прогнать смоук на `risuy_dev` → RED (5 провалов — таблиц/колонки/грантов нет). ✅
- [x] Написать `db/schema_billing_v2_pricing.sql` + зеркалить гранты в `db/panel_role.sql`.
- [x] Применить на `risuy_dev` (`twc-migrate.sh`). ✅ ⚠️ прод `risuy` — за ОТДЕЛЬНЫМ «да» (expand-first, ещё НЕ применено).
- [x] Смоук GREEN на `risuy_dev`. ✅ Тест поймал Timeweb default-priv (panel_rw авто-получил update/delete) → добавлен `revoke update,delete on billing_token_rate` (как consent_events).
- [x] Коммит (локально, после GREEN на dev). ⚠️ Побочно: `usage_ledger` тоже имеет update/delete у panel_rw (append-only не защищён revoke) — отдельная задача.

### T-1A-2 · `charge_usage`: per-resource расчёт `charged` + курс из БД
**Files:** modify `shared/metering.py`
**Interfaces:** сигнатура `charge_usage(conn, tenant_id, cost_microrub:int, meta:dict, idempotence_key:str, *, allow_negative=False)->asyncpg.Record` СОХРАНЯЕТСЯ. В транзакции (metering.py:116) загрузить: наценки `resource_pricing` + текущий курс `select rate_microrub_per_1k from billing_token_rate where effective_from <= now() order by effective_from desc limit 1`. Вместо `ceil_mul(cost, plan['markup_multiplier'])` (строки 135-138): `resource = meta.get('resource') or meta.get('kind')`; `kind=='llm'` → `charged = ceil(units['tokens_total'] × rate / 1000)`, снимок `token_rate_microrub_per_1k=rate`, `multiplier=resource_pricing['llm']` (1.00); иначе → `ceil_mul(cost_microrub, resource_pricing[resource])`, `token_rate=NULL`. INSERT usage_ledger добавляет колонку `token_rate_microrub_per_1k`. Ветку `per_message` ПОКА оставить (снимется в T-1C-1). Потребляет T-1A-1. ⚠️ реализовывать после применения DDL на risuy_dev (иначе metering_smoke не прогнать RED→GREEN — нужен dev-DSN).
- [x] Расширить `scripts/metering_smoke.py`: LLM 5000 ток × курс → `charged==7_500_000` + снимок курса; DaData `cost×3`→`22_500_000`; Voice `×2` (доказывает чтение resource_pricing, не плана); маржа ≥ полов.
- [x] RED (8 провалов на старом cost×3) → реализовать per-resource + курс из БД + снимок → GREEN (24/24; идемпотентность/FOR-UPDATE не регрессируют). ✅ на risuy_dev.
- [x] Коммит (локально).

## Под-этап 1B — Два бакета кошелька — ✅ ЗАВЕРШЁН на risuy_dev (сессия 17; прод-DDL/деплой — за «да»)

### T-1B-1 · DDL: `credit_wallets` → 2 бакета + бэкфилл ⚠️ ПРОД-DDL
**Files:** create `db/schema_metering_v2_buckets.sql`; modify `db/panel_role.sql`
**Interfaces (produces):** `credit_wallets.included_microrub bigint not null default 0`, `included_period_end timestamptz`, `topup_microrub bigint not null default 0`. `balance_microrub` оставляем (депрекейт позже). Наследует RLS `tenant_isolation`.
**Migration (expand-safe):** `alter table credit_wallets add column if not exists …; update credit_wallets set topup_microrub = balance_microrub where balance_microrub <> 0 and topup_microrub = 0;`
- [x] `scripts/wallet_buckets_smoke.py` (гард `/risuy_dev`).
- [x] RED (3 колонки нет + бэкфилл падает UndefinedColumn). ✅
- [x] Написать DDL. `panel_role.sql` НЕ нужен: грант credit_wallets table-level (select,insert,update) — новые колонки покрыты. ✅
- [x] Применить на `risuy_dev`. ✅ ⚠️ прод `risuy` — за отдельным «да».
- [x] Смоук GREEN на `risuy_dev`: 3 колонки/типы/дефолты + бэкфилл balance→topup + идемпотентность. Коммит (локально).

### T-1B-2 · `charge_usage`: списание пул→кошелёк, сгорание по `period_end`
**Files:** modify `shared/metering.py`
**Interfaces:** секция списания (строки 129-149): `SELECT included_microrub, included_period_end, topup_microrub … FOR UPDATE`; доступный пул = `included_microrub` если `period_end>now()` иначе 0 (лениво обнуляем сгоревший); гасим сперва пул, остаток — `topup`. `balance_after` = сумма остатков. `not allow_negative и (пул+аванс)<charged` → `InsufficientCreditsError` (бакеты не тронуты); `allow_negative=True` → минус на ПОСЛЕДНЕМ бакете. Три гвоздя сохраняются. Потребляет T-1B-1, T-1A-2.
- [x] `wallet_buckets_smoke.py` секция 3: (1) пул1000+аванс500,charged1200→пул0/аванс300; (2) period_end в прошлом→пул игнор+обнулён; (3) оба≤0 allow_negative=False→ошибка+бакеты целы+леджер пуст; (4) allow_negative=True→минус на авансе.
- [x] RED (5 провалов) → реализовать (SELECT бакетов FOR UPDATE, expiry в SQL, списание пул→аванс, balance зеркало) → GREEN (+ регресс metering_smoke 24/24). ✅ на risuy_dev. Коммит (локально).

### T-1B-3 · Начисление: топап→аванс; activate/renew→пул с `period_end` (сгорание)
**Files:** modify `admin-panel/db.py`
**Interfaces:** `mark_topup_succeeded(tenant_id, yookassa_payment_id:str, raw:dict)->bool` (db.py:4262): `balance_microrub +=` → `topup_microrub +=`. `activate_subscription_from_payment(…)->bool` (db.py:4439) и `renew_subscription(…)->bool` (db.py:4365): `included_credits_microrub` → `included_microrub` c `SET included_period_end=current_period_end`; на renew пул **ПЕРЕЗАПИСЫВАТЬ** (сгорание остатка), не прибавлять. Снятие `ai_wallet_blocked` (db.py:4296/4402/4492) по сумме бакетов. Потребляет T-1B-1.
- [x] `scripts/billing_tenant_smoke.py` секция 9: топап→topup (пул цел); activate→included+period_end (аванс цел); renew при непустом пуле→пул=новый (сгорание, не сумма); блок ИИ снят.
- [x] RED (6 провалов) → реализовать mark_topup/activate/renew (пул перезаписывается, аванс цел, balance зеркало) → GREEN (секции 1–8 регресс целы). ✅ на risuy_dev. Коммит (локально).

### T-1B-4 · Хард-стоп по ОБОИМ бакетам (Школа исключена)
**Files:** modify `bot-telegram/metering_worker.py`, `bot-telegram/ai.py`
**Interfaces:** `_maybe_block_wallet(conn, tenant, plan:dict)->tuple|None` (metering_worker.py:344) и gateway-ветка (ai.py:351-359): условие `balance_microrub<=0` → `(included_available + topup_microrub) <= 0` (учёт истёкшего `period_end`). Сохранить исключение `tenant == db.default_tenant_id()` (§8.7) и prepaid-гейт.
- [x] `scripts/metering_worker_smoke.py` секция H: оба≤0→блок; пул>0 при аванс≤0→НЕ блок; истёкший пул→блок; Школа→ops-алерт не блок.
- [x] RED (H3 истёкший пул) → реализовать (условие `(доступный_пул+аванс)<=0` в metering_worker.py + ai.py) → GREEN A–H. ✅ на risuy_dev. Коммит (локально).
- [x] ⚠️ ПОБОЧНО поймано: B/D смоука кодировали старую LLM-цену cost×3 → обновлены на tokens×курс (T-1A-2 корректно распространился на cloud-ai путь; metering_worker_smoke не гонялся в T-1A-2).

## Под-этап 1C — Схлопнуть два счётчика + разрез

### T-1C-1 · Депрекейт `billing_mode='per_message'` ⚠️ ПРОД-DDL
**Files:** create `db/migrate_plans_drop_per_message.sql`; modify `shared/metering.py`, `bot-telegram/metering_worker.py`
**Interfaces:** `charge_usage` — удалить ветку `per_message` (остаётся per-resource из T-1A-2). `metering_worker`: убрать `_scan_per_message` из `_tick`, заморозить `metering_msg_hwm`. DDL: планы econom/start → `cost_multiplier`, `per_message_microrub=null`.
**Migration:** `update plans set billing_mode='cost_multiplier', per_message_microrub=null where code in ('econom','start'); alter table plans drop constraint if exists plans_per_message_chk;` (expand на dev; CONTRACT-ужесточение CHECK — после деплоя кода).
- [ ] `metering_smoke.py`: переписать кейс 4 (per_message 7,5₽) на `cost_multiplier econom` → charged по per-resource; `_scan_per_message` не пишет `'msg:%'`.
- [ ] RED → ⚠️ DDL dev→прод по «да» → реализовать → GREEN (+ регресс `metering_worker_smoke`). Коммит.

### T-1C-2 · Депрекейт quota/overage на витрине `/subscription`
**Files:** modify `admin-panel/app.py`, `admin-panel/db.py`
**Interfaces:** `subscription_select` (app.py:3664-3689): убрать блок `prev_used=count_ai_messages(…)→overage`. `count_ai_messages(period_start, period_end=None)->int` (db.py:3278) → пометить legacy (исторический показатель, не биллинг). ⚠️ `bot-telegram/db.py:1605 count_ai_messages(tg_user_id)` — ОМОНИМ (порог суммаризации), НЕ трогать. Колонки `service_invoices.quota/overage_*` остаются как фискально-легаси.
- [ ] `billing_tenant_smoke.py`: оплата тарифа не создаёт overage-строку; `usage_ledger` — единственный счётчик.
- [ ] RED → реализовать → GREEN. Коммит.

### T-1C-3 · Процедура разреза (cutover) + shadow-diff смоук
**Files:** create `scripts/cut_over_metering.py`, `scripts/cutover_shadow_diff_smoke.py`
**Interfaces:** `cut_over_metering.py` (ops, гард окружения): per-тенант на границе периода — (1) заморозить `metering_msg_hwm`, (2) финальный overage-счёт СТАРОЙ единицей (`count_ai_messages`+`service_invoices`), (3) перенести теневой минус в `topup_microrub`, (4) выставить `included_microrub`+`period_end` (T-1B-3). Маркер `tenant_settings key='billing_cutover_done'`; запрет пересечения окон. Потребляет T-1B-3, T-1C-1.
- [ ] `cutover_shadow_diff_smoke.py` (гард `/risuy_dev`): на окне разреза `shadow-diff(старый счётчик, сумма usage_ledger) == 0` (приёмка §15); повтор `cut_over_metering` по маркеру = no-op; теневой минус перенесён без потери.
- [ ] RED → реализовать → GREEN (dev-прогон; прод-запуск — за «да»). Коммит.

## Под-этап 1D — Закрыть утечки (§7.5)

### T-1D-1 · Добить `model_prices` (эмбеддер + все модели в обороте)
**Files:** create `db/migrate_model_prices_leaks.sql`
**Interfaces:** строки `model_prices(provider, model, price_in_microrub_per_1k, price_out_microrub_per_1k)` для эмбеддера и всех LLM provider `timeweb-ai-gateway`/`timeweb-cloud-ai` на проде (иначе `_capture` пропускает списание — ai.py:323, metering_worker.py:215). **Guardrail (Открытое решение №3):** цены сверить в ЛК Timeweb, не выдумывать.
- [ ] `pricing_smoke.py`: для каждой активной пары provider/model в usage-путях есть строка; эмбеддинг не пропускается.
- [ ] RED → вписать цены (по данным из ЛК) → dev→прод по «да» → GREEN. Коммит.

### T-1D-2 · Тарифицировать эмбеддинги/RAG (`kind='embedding'`)
**Files:** modify `bot-telegram/kb.py`, `bot-telegram/multiplex.py`, `bot-telegram/memory.py`, `bot-telegram/handlers.py`, `admin-panel/kb.py`, `admin-panel/app.py`
**Interfaces:** обернуть точки эмбеддинга `charge_usage(conn, tenant_id, cost_microrub, {kind:'embedding', resource:'embedding', provider, model, units:{tokens}}, idempotence_key='emb:{tenant}:{kind}:{hash}', allow_negative=True)`: `kb.py:59/64` (multiplex.py:254-257, handlers.py:942, memory.py:56/117), `admin-panel/kb.py:110` (app.py:4838). `cost_microrub` из `model_prices` эмбеддера × токены. Потребляет T-1D-1, T-1B-2.
- [ ] `scripts/embeddings_metering_smoke.py`: RAG-путь пишет `usage_ledger kind='embedding'` с ненулевым charged; индексация БЗ списывает; повтор idem = одно списание.
- [ ] RED → реализовать → GREEN. Коммит.

### T-1D-3 · Тарифицировать DaData (`resource='dadata'`, 7,5₽×3)
**Files:** modify `admin-panel/app.py`, `admin-panel/dadata.py`, `admin-panel/db.py`
**Interfaces:** после `find_party(q)` (app.py:4645) и `suggest_party(q)` (app.py:4666) — `charge_usage(conn, session.active_tenant_id, cost_microrub=7_500_000, {kind:'other', resource:'dadata', provider:'dadata', units:{requests:1}}, idempotence_key='dadata:{tenant}:{inn|q}:{date}', allow_negative=True)` → charged `ceil(7,5₽×3)=22,5₽`. Глобальный `dadata_quota_take` (db.py:5127) остаётся как rate-limit. Потребляет T-1A-2, T-1B-2. *(Открытое решение №6: гранулярность списания.)*
- [ ] `scripts/dadata_metering_smoke.py`: вызов пишет `usage_ledger kind='other' resource='dadata' charged==22_500_000`; per-tenant; суточная квота дополняется, не заменяется.
- [ ] RED → реализовать → GREEN. Коммит.

## Под-этап 1E — Двухчековая фискализация 54-ФЗ

### T-1E-1 · Параметризовать чеки: топап→«Аванс», тариф→«услуга»
**Files:** modify `admin-panel/app.py`, `admin-panel/renewal.py`
**Interfaces:** `_service_receipt(email, description, amount, *, mode:str='service')->dict|None` (app.py:3505): `mode='advance'` → `payment_mode='advance'`, `payment_subject='payment'`; `mode='service'` → текущие. `/wallet/topup` (app.py:6816) → `mode='advance'`; оплата тарифа (app.py:3707) → `mode='service'`. `vat_code=config.SERVICE_VAT_CODE` сохраняется.
- [ ] `scripts/kassa_smoke.py`: `mode='advance'` даёт advance/payment; `mode='service'` — full_payment/service; None при `SERVICE_RECEIPT_ENABLED=0`.
- [ ] RED → реализовать → GREEN. Коммит.

### T-1E-2 · Отгрузочный чек при списании (зачёт аванса) + номенклатура
**Files:** create `admin-panel/receipts.py`; modify `shared/metering.py`, `admin-panel/app.py`
**Interfaces:** `KIND_TO_RECEIPT_NAME = {'llm':'Использование ИИ','embedding':'Индексация/поиск по базе знаний','message':'Сообщение ИИ','other':'Сервисная операция'}`. При списании из аванса — отгрузочный чек `payment_mode='full_payment'` с зачётом, позиция из `meta.kind`. *(Открытое решение №4: на каждое списание vs батч; нужен ОФД/ЮKassa receipt-API.)* Потребляет T-1E-1, T-1B-2.
- [ ] `kassa_smoke.py`: маппинг kind→номенклатура; структура отгрузочного чека валидна; прогон на тест-магазине 1379463.
- [ ] RED → реализовать → GREEN. Коммит.

### T-1E-3 · Включить `SERVICE_RECEIPT_ENABLED=1` в проде ⚠️ конфиг-прод
**Files:** modify `admin-panel/config.py` (+ .env панели 205025)
**Interfaces:** флаг (config.py:410, дефолт OFF) → 1 после верификации двухчековой схемы. Не код-задача.
- [ ] E2E на тест-магазине 1379463: топап→чек «Аванс», списание→чек зачёта.
- [ ] По «да» — включить в проде (фискальный 1378536). Коммит конфига.

## Под-этап 1F — Жизненный цикл подписки

### T-1F-1 · D3: снять автосписания (можно ПЕРВЫМ)
**Files:** modify `admin-panel/app.py`, `admin-panel/config.py`
**Interfaces:** убрать `save_payment_method=True` из `subscription_select` (app.py:3709 — единственное место). Держать `SERVICE_RENEWAL_ENABLED=0` (config.py:300, дефолт False; `renewal.run()` ранний выход renewal.py:63). Тогда `yookassa_payment_method_id` не заполняется → `list_due_renewals` пуст → claim «без автосписаний» честен.
- [ ] `scripts/b5_payments_smoke.py`/ручная: тело запроса ЮKassa без `save_payment_method`; `list_due_renewals` пуст.
- [ ] RED → реализовать → GREEN. Коммит.

### T-1F-2 · Proration + ровно одна живая подписка ⚠️ ПРОД-DDL (CONTRACT)
**Files:** modify `admin-panel/db.py`; create `db/migrate_subscriptions_one_live.sql`
**Interfaces:** `activate_subscription_from_payment(…)->bool` (db.py:4439): вместо INSERT новой active (4473-4478) — найти живую (`trialing/active/past_due`), деактивировать (`status='canceled'`), UPDATE/создать ОДНУ; смена тарифа → пропорция цены по остатку периода + корректировка `included_microrub` на разницу (не полный пул). Образец UPDATE — `renew_subscription` (db.py:4365). Инвариант: `list_due_renewals`/`get_tenant_plan` видят ОДНУ живую. Потребляет T-1B-3. *(Открытое решение №7: даунгрейд-политика.)*
**Migration (CONTRACT):** сперва дедуп живых строк (ops), затем `create unique index … subscriptions_one_live_idx on subscriptions (tenant_id) where status in ('trialing','active','past_due');` — ПОСЛЕ деплоя кода.
- [ ] `billing_tenant_smoke.py`: две activate → одна active (пул не удвоен); смена тарифа → пропорциональный `included`; `list_due_renewals` = одна; повтор по yk_payment_id идемпотентен.
- [ ] RED → реализовать → ⚠️ дедуп+index dev→прод по «да» → GREEN. Коммит.

### T-1F-3 · Починить `/service/subscribe`: tenant_id + вебхук + B2B-гейт ⚠️ ПРОД-DDL
**Files:** modify `admin-panel/app.py`, `admin-panel/db.py`; create `db/migrate_subscriptions_b2b.sql`
**Interfaces:** `service_subscribe` (app.py:3743): форма собирает ИНН/ОГРНИП + чек-бокс «предпринимательская деятельность» (D2); metadata (app.py:3777) вместо `{'kind':'service_landing','plan'}` несёт `tenant_id`(pre-tenant) + email + inn; детерминированный idem-key. Вебхук `yookassa_webhook` (app.py:3884-3937): ветка `kind=='service_landing'` → провижининг тенанта/подписки/кошелька (образец platform_subscription + `activate_subscription_from_payment` из T-1F-2). *(Открытые решения №5, №6.)*
**Migration (expand):** `alter table subscriptions add column if not exists buyer_inn text, buyer_ogrnip text, is_entrepreneur boolean;` (место B2B-полей уточнить).
- [ ] `scripts/subscribe_provision_smoke.py`: эмуляция succeeded-вебхука `service_landing` → тенант + одна subscription + wallet с included; без ИНН/чек-бокса оплата не стартует; повтор идемпотентен.
- [ ] RED → реализовать → ⚠️ DDL dev→прод по «да» → GREEN. Коммит.

### T-1F-4 · Реконсиляция осиротевших платежей (F13)
**Files:** create `scripts/reconcile_yookassa.py`; modify `admin-panel/yookassa.py`
**Interfaces:** `reconcile_yookassa.py` (ops): по succeeded-платежам магазина за окно (`yookassa.list_payments` — добавить) восстановить `payments`+провижининг для платежей без записи (образец `mark_topup_succeeded`/`activate_subscription_from_payment`/ветка service_landing T-1F-3). Идемпотентно по `yookassa_payment_id`.
- [ ] `scripts/reconcile_smoke.py`: засеять succeeded без provision → восстановлена одна строка payments + провижининг; повтор = no-op.
- [ ] RED → реализовать → GREEN (прод-запуск за «да»). Коммит.

---

## Self-review (покрытие спеки)
- §7.1 per-resource прайс → 1A. §7.2 бакеты → 1B. §7.3 разрез → 1C. §7.5 утечки → 1D. §8 фискализация → 1E. §9 proration + §7.4 subscribe/реконсиляция → 1F. §6 сгорание/аванс → 1B. D2 B2B-гейт → T-1F-3. D3 автосписания → T-1F-1. D4 маржа → 1A. D5 фискализация → 1E.
- Приёмка §15: shadow-diff=0 → T-1C-3; 2 бакета → T-1B-2/3; маржа-полы → T-1A-2; одна подписка → T-1F-2; эмбеддинги/DaData → 1D; аванс+зачёт → 1E; Школа не встаёт → T-1B-4.
- Открытые решения (7) требуют ответа владельца до соответствующих задач (см. блок выше).

## Дальше
Этапы 2 (кабинет) и 3 (лендинг+оферта) — отдельные планы после Этапа 1.
