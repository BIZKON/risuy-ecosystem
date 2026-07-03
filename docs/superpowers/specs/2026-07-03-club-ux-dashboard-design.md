# Спека: Клуб-UX над членами — фильтры + CSV-выгрузка + дашборд (v1)

**Дата:** 2026-07-03 · **Сессия:** 8 · **Ветка:** `docs/security-audit` (= main)
**Источник задачи:** хендофф сессии 7, FOLLOW-UP п.4 («Клуб-UX над opt-in ЧЛЕНАМИ»).
**Статус:** дизайн одобрен владельцем (@profysales) в брейншторме.

---

## 1. Контекст и цель

Клуб предпринимателей (Фаза 1, Уровень 1, per-tenant) задеплоен: `club_members/club_profiles/club_intros`
+ RLS, каталог `/club`, ЕГРЮЛ-обогащение через `prospects`, двусторонние знакомства. Но у оператора
нет инструментов **работать со списком членов**: нельзя отфильтровать, выгрузить, увидеть срез клуба.

**Цель v1:** дать оператору тенанта над **opt-in членами** его клуба:
1. **Фильтры** каталога — город / ОКВЭД / тип (ИП·ЮЛ·Гос) / статус.
2. **CSV-выгрузку** — только бизнес-поля (контакты intro-gated НЕ включаем).
3. **Дашборд** — сводка + распределения + динамика роста + воронка знакомств.

Ценность: оператор видит структуру своего клуба, отбирает сегменты (для будущих легальных рычагов
роста — п.5 хендоффа), выгружает бизнес-срез без утечки ПДн.

---

## 2. Область v1 (scope)

**В scope:**
- Серверная фильтрация каталога `/club` по `city` / `okved` / `entity_type` / `status`.
- Роут `/club/export.csv` — потоковый CSV бизнес-полей по активному фильтру.
- Вкладка `/club/dashboard` — полный дашборд по всему клубу тенанта.
- Чистый модуль аналитики `shared/club_analytics.py` (тип, нормализация города, сводка, CSV-строки).
- Смоук-скрипты (unit + db + ui).

**Вне scope (явно):**
- ❌ **Прод-DDL** — не требуется (все нужные колонки уже есть, см. §5).
- ❌ **Боевое включение клуба тенантам** — остаётся gated за юр-выверкой согласий (FOLLOW-UP п.1);
  эта фича — операторский инструментарий, не меняет юр-гейт.
- ❌ **Уровень 2 (cross-tenant биржа)** — отдельный План 2, `network_opt_in` не трогаем.
- ❌ **DaData-API нормализация городов** — только read-time каноникализация без вызовов API
  (бот не сконфигурен под DaData, квота 10k/сут). DaData-API-нормализация — бэклог.
- ❌ **Рычаги роста** (батч-приглашение / рефералы / карта пробелов — п.5 хендоффа) — отдельный инкремент.
- ❌ Экспорт контактов членов в любом виде — intro-gated, красная линия 152-ФЗ.

---

## 3. Зафиксированные решения из брейншторма

1. **Агрегации — гибрид (подход C):** одна обогащённая выборка `club_members ⟕ club_profiles ⟕ prospects`
   → распределения/тип/город/цепочка/чек считаем в Python (чистый модуль, юнит-тест без БД);
   рост и воронка знакомств — дешёвыми SQL-агрегатами по датам/статусам.
2. **Города — read-time каноникализация без DaData-API** (`normalize_city()`, детерминизм, 0 квоты).
3. **Дашборд — полный** (KPI + распределения + покрытие цепочки + средний чек + рост по неделям/месяцам
   + воронка знакомств).
4. **Объём — всё сразу** (фильтры + CSV + дашборд одной спекой/планом/деплоем; общий query-слой).
5. **Разделение поверхностей:**
   - `/club` (каталог) — фильтры + KPI-полоска, **отражающая активный фильтр**.
   - `/club/dashboard` (вкладка) — аналитика по **всему** клубу (без фильтра).
   - `/club/export.csv` — экспорт по **активному фильтру** каталога.
6. **Прод-DDL не требуется.**

---

## 4. Модель данных (существующая — без изменений)

Читаем из уже задеплоенных таблиц (RLS `tenant_isolation` по `app.tenant_id`; бот/панель коннектятся
owner-DSN `gen_user` → RLS обходится → изоляция держится **in-query backstop**: явный `tenant_id`
в каждом WHERE и JOIN, как в `prospect_*`/`club_*`).

**`club_members`** — `id, tenant_id, lead_id?, inn?, display_name, city?, okved?, status('active'|'paused'|'left'), network_opt_in, created_at`.
Индексы: `(tenant_id, status)`, `(tenant_id, city, okved)`, `(tenant_id, lead_id)`.

**`club_profiles`** (PK `member_id`) — `tenant_id, offering?, avg_check?, seeking?, chain_position('before'|'after'|'both')?, okved_seek?, description?`.

**`club_intros`** — `id, tenant_id, from_member, to_member, status('requested'|'accepted'|'declined'|'cancelled'), from_accepted_at?, to_accepted_at?, created_at, decided_at?`.

**`prospects`** (ЕГРЮЛ, tenant-scoped, `UNIQUE(tenant_id, inn)`, пишет панель) — `inn, subject_type('legal'|'individual'), name_short?, name_full?, opf?, okved?, okved_name?, city?, region?, status?, …`.
Санитайзер `dadata.py` уже вырезает телефоны/email/ФИО-физлиц до записи; `management`/`address` (ИП) —
ПДн, в экспорт НЕ идут (§8).

**Связь члена с ЕГРЮЛ:** `club_members.inn = prospects.inn` в рамках `tenant_id` (LEFT JOIN — у члена
может не быть ИНН или ЕГРЮЛ-карточки).

---

## 5. Компонент A — чистый модуль `shared/club_analytics.py`

Без БД, без сети. Юнит-тестируемый. Стиль зеркалит `shared/anon.py`. Импортируется панелью
(и потенциально ботом — функции чистые).

### 5.1 Деривация типа субъекта
```
GOV_OPF_SHORT: frozenset — короткие ОПФ гос/муниципальных форм
  (ГУП, МУП, ФГУП, ФКУ, ГКУ, МКУ, ГБУ, МБУ, ГАУ, МАУ, ФГБУ, ФКП, ГКОУ, МКОУ, …)
GOV_OPF_SUBSTR: кортеж подстрок для полного ОПФ/имени
  («государственн», «муниципальн», «казённое»/«казенное», «бюджетн… учрежд»,
   «автономн… учрежд», «администрация», «департамент», «министерство», «комитет … власти»)

entity_type(inn: str|None, opf: str|None) -> 'ИП' | 'ЮЛ' | 'Гос' | 'не указан'
  - inn пуст/None                         → 'не указан'
  - len(inn)==12 (цифры)                  → 'ИП'
  - len(inn)==10 (цифры):
      opf ∈ GOV_OPF_SHORT или подстрока   → 'Гос'
      иначе                               → 'ЮЛ'
  - иначе (мусор)                          → 'не указан'
```
Best-effort: если у члена нет `prospects`-строки (opf=None), 10-значный ИНН = 'ЮЛ', «Гос» не
определяется — это принятая деградация (§9).

### 5.2 Нормализация города
```
normalize_city(raw: str|None) -> str
  - None/'' → 'Не указан'
  - strip, схлопнуть пробелы, убрать префиксы «г.»/«город»/«гор.»
  - привести регистр (Title-case по словам, дефисы сохранить: «Ростов-на-Дону»)
  - CITY_ALIASES: карта частых вариантов → канон
      (мск/Москва/г Москва→'Москва'; спб/СПб/С-Петербург/Санкт Петербург→'Санкт-Петербург';
       нн→'Нижний Новгород'; ект/екб→'Екатеринбург'; …)
```
Используется и для фасета фильтра, и для группировки в дашборде/CSV.

### 5.3 Сводка
```
summarize(rows: list[dict]) -> dict
  {
    'kpi': {total, active, paused, left, with_egrul, cities, with_profile},
    'by_city':  [(город_норм, count), …]  # sorted desc, топ-N + «прочие»
    'by_okved': [(okved|okved_name, count), …]
    'by_type':  {'ИП': n, 'ЮЛ': n, 'Гос': n, 'не указан': n}
    'chain':    {'before': n, 'after': n, 'both': n, 'нет профиля': n}
    'avg_check':{count, min, median, max}   # None-safe; если нет данных — нули
  }
```

### 5.4 CSV бизнес-полей
```
CSV_HEADERS = [display_name, город(норм.), тип, ИНН, ОКВЭД, ОКВЭД-название,
               краткое имя ЕГРЮЛ, offering, средний чек, seeking, цепочка, статус, дата регистрации]
csv_business_rows(rows) -> Iterable[list[str]]
  - каждое поле через csv_safe() (formula-guard из shared/anon.py)
  - контакты/ПДн НЕ включаются (см. §8)
```

---

## 6. Компонент B — хелперы `admin-panel/db.py` (tenant-scoped, in-query backstop)

```
club_member_list_enriched(tenant_id, *, city=None, okved=None, entity_type=None,
                          status=None, chain=None) -> list[dict]
  SELECT m.*, p.offering, p.avg_check, p.seeking, p.chain_position,
         pr.opf, pr.subject_type, pr.name_short, pr.okved_name
  FROM club_members m
  LEFT JOIN club_profiles p ON p.member_id = m.id AND p.tenant_id = $tenant
  LEFT JOIN prospects   pr ON pr.inn = m.inn     AND pr.tenant_id = $tenant
  WHERE m.tenant_id = $tenant
    [AND m.status = $status]                    -- server-side (точный, индекс (tenant_id,status))
    [AND m.okved  = $okved]                     -- server-side (точный, индекс (tenant_id,city,okved))
  ORDER BY m.created_at DESC
  # city (по normalize_city) и entity_type (по opf+len(inn)) — фильтруются в Python поверх
  #   выборки: держим ОДИН источник правды (тот же normalize_city/entity_type, что в фасете и дашборде).

club_growth(tenant_id, period: 'week'|'month') -> list[(bucket_date, count)]
  SELECT date_trunc($period, created_at) d, count(*) FROM club_members
  WHERE tenant_id=$tenant GROUP BY d ORDER BY d

club_intro_funnel(tenant_id) -> dict
  SELECT status, count(*) FROM club_intros WHERE tenant_id=$tenant GROUP BY status
  → {requested, accepted, declined, cancelled, both_accepted?}   # both_accepted: from_ и to_accepted_at NOT NULL
```
**Разделение фильтров (важно для консистентности):**
- `status`, `okved` — **в SQL** (точные значения, ложатся на индексы `(tenant_id,status)`/`(tenant_id,city,okved)`).
- `city` — **в Python** по `normalize_city()` равенству (иначе выбор «Москва» из фасета не поймал бы «мск»/«г. Москва»).
- `entity_type` — **в Python** по `entity_type(inn, opf)` (не дублируем гос-ОПФ-логику в SQL).

Роут применяет Python-фильтры (city/type) поверх `club_member_list_enriched` **перед** рендером каталога
и перед CSV — один и тот же отфильтрованный набор. Клуб per-tenant мал (десятки-сотни) → перф не проблема.
Фасет городов/типов для формы фильтра берётся из `summarize().by_city`/`by_type` полного набора.

---

## 7. Компонент C — поверхности `admin-panel/app.py` + шаблоны

Все гейты — `active_tenant` (платформа-под-клиента), CSRF на POST (здесь POST нет — только GET),
как у `/companies`/`/club`.

- **`GET /club`** (есть, `club_page` L4228) → добавить:
  - панель фильтров (форма GET: `city`, `okved`, `type`, `status`);
  - KPI-полоску над списком (из `summarize()` по **отфильтрованным** строкам);
  - кнопку «Выгрузить CSV» (ведёт на `/club/export.csv` с теми же query-params);
  - ссылку на вкладку «Дашборд».
- **`GET /club/dashboard`** (новый) → полный дашборд по всему клубу (фильтр не применяется):
  KPI-плитки, распределения (город/ОКВЭД/тип — списки-бары), покрытие цепочки, средний чек,
  рост по неделям/месяцам (`club_growth`), воронка знакомств (`club_intro_funnel`).
  Пустой клуб → нули, не падает.
- **`GET /club/export.csv`** (новый) → `StreamingResponse` (`text/csv; charset=utf-8`,
  `Content-Disposition: attachment`), зеркало `export_full`/`stream_export_full`:
  `club_member_list_enriched(фильтр)` → `csv_business_rows()` → потоковая запись. BOM для Excel-кириллицы.

Навигация: вкладки в шапке `/club` ↔ `/club/dashboard` (как разделы панели). Аудит-лог операторского
экспорта — по образцу `export_full` (кто/когда выгрузил), без ПДн в строке лога.

---

## 8. Комплаенс-гейты (152-ФЗ, defense-in-depth)

**CSV и дашборд НИКОГДА не содержат:**
- контакты членов: `tg_user_id`/`vk_user_id`/`max_user_id`, контакты связанного лида — **intro-gated**
  (раскрываются только при обоюдном согласии знакомства, `club_intro_reveal`);
- `prospects.management` (ФИО руководителя ЮЛ = ПДн);
- `prospects.address` для ИП (адрес места жительства = ПДн; берём только `city`).

**Включаются** (бизнес-идентичность, не контакты): `display_name` (самоназвание при регистрации, уже
видно в каталоге), нормализованный город, тип, ИНН, ОКВЭД(+имя), краткое имя ЕГРЮЛ, offering, avg_check,
seeking, chain_position, статус, дата регистрации.

Формула-гард `csv_safe()` (префикс `'` для `= + - @`) — против CSV-инъекций. Экспорт — операторское
действие над **своим** tenant-scoped клубом (opt-in члены), не выходит за изоляцию тенанта.

Фича **не** снимает юр-гейт боевого включения клуба (FOLLOW-UP п.1) — работает над теми членами,
что уже вступили; новых потоков согласия не создаёт.

---

## 9. Обработка ошибок и деградация

- Член без `inn` или без `prospects`-строки → тип по длине ИНН, «Гос» не определяется, ОКВЭД-имя пусто;
  bucket «не указан». Виджеты не падают.
- Пустой клуб → `summarize` даёт нули, дашборд рисует «Пока нет членов», CSV = только заголовки.
- `avg_check` None у части профилей → статистика None-safe (медиана по имеющимся).
- Битый/нестандартный ИНН → 'не указан' (без исключений).
- Нормализация города детерминирована и никогда не бросает.

---

## 10. Тестирование (smoke-скрипты, канон risuy)

- **`club_analytics_smoke.py`** (unit, без БД, гоняет имплементер): таблицы кейсов `entity_type`
  (12/10/гос-ОПФ/пусто/мусор), `normalize_city` (алиасы/префиксы/дефисы), `summarize`
  (KPI/распределения/чек None-safe), `csv_business_rows` (formula-guard + **отсутствие** контактных полей).
- **`club_dashboard_db_smoke.py`** (risuy_dev, **гонит КОНТРОЛЛЕР** inline с `TEAM_DSN`, owner-DSN НЕ в
  субагенты): `club_member_list_enriched` с каждым фильтром + пустой; `club_growth` week/month;
  `club_intro_funnel`. Сидинг → проверка → очистка.
- **`club_dashboard_ui_smoke.py`** / **`club_export_csv_smoke.py`** (render `/club` с фильтрами +
  `/club/dashboard`; CSV-заголовки + гарантия, что контактных колонок нет). PYTHONPATH=admin-panel:.

Регрессия: существующие `club_*` смоуки (`club_catalog_ui`, `club_db`, `club_intro`, …) остаются зелёными.

---

## 11. Порядок выкатки (канон risuy)

1. **DDL нет** → шаг миграции пропускаем.
2. Код + смоуки на `docs/security-audit`; db-смоуки на `risuy_dev` гонит контроллер inline.
3. Финальное ревью (per-task + адверсариальное, SDD subagent-driven; коммиттеры ПОСЛЕДОВАТЕЛЬНО).
4. **Деплой** = `git push origin docs/security-audit:main` → авто-редеплой панели 205025
   (бот НЕ трогаем — чистая панель). **Только по явному «да» владельца** (auth-классификатор гейтит).
5. Сверка `twc apps get 205025 -o json | grep commit_sha` + `status=active`; владелец глазами открывает
   `cabinet.pro-agent-ai.ru` (HTTP-live из среды агента недоступен).

---

## 12. Открытые вопросы / бэклог

- DaData-API нормализация городов (точность vs квота) — апгрейд, если справочник алиасов окажется мал.
- Фильтр по среднему чеку / диапазону — если попросит оператор (v1 — только распределение в дашборде).
- Экспорт в XLSX — v1 только CSV.
- Топ-N в распределениях (город/ОКВЭД) — параметр; по умолчанию 10 + «прочие».
- CSS клуб-классов (`.club-card`/`.club-match`/`.intro-*`) — прежний долг (п.6 хендоффа); дашборд-виджеты
  оформить в том же проходе, чтобы не плодить голый HTML.
