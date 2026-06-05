-- Схема админ-панели лидов «Школа Лесова» (внутренняя, server-rendered).
-- Дополняет db/schema.sql — таблицу leads НЕ пересоздаёт, только добавляет колонку.
-- Идемпотентно (IF NOT EXISTS) — применять можно повторно без ошибок.
--
-- Применить ОДИН РАЗ owner-DSN (роль-владелец БД, не panel_rw — у неё нет прав на DDL):
--   psql "$OWNER_DATABASE_URL" -f db/schema_admin.sql
--
-- ПОРЯДОК: сначала этот файл (создаёт таблицы + колонку), затем db/panel_role.sql
-- (создаёт роль panel_rw и выдаёт ей минимальные гранты на эти объекты).
--
-- Оператор ПДн: ИП Иванов Игорь Вадимович (ИНН 781009636071, ОГРНИП 325784700090452).
-- Срок уничтожения/обезличивания ПДн — ≤30 дней (landing/privacy.html §6.5, consent.html §6).
-- Кластер тот же, ru-1 (СПб); ПДн не покидают РФ.

create extension if not exists "pgcrypto";  -- gen_random_uuid() для admin_sessions.sid

-- ── Серверные сессии ─────────────────────────────────────────────────────────
-- Единственный источник правды по ревокации (§3.2 плана). Cookie несёт только
-- случайный sid; всё состояние (скользящий idle / жёсткий потолок / revoked) — здесь.
-- На каждом защищённом запросе: SELECT строки → отказ если нет/revoked/expires_at<now()/
-- last_seen<now()-idle → UPDATE last_seen. Logout = revoked=true.
-- «Выйти везде» = ревокнуть все строки оператора (по actor).
--
-- КОНТРАКТ КОЛОНОК — единый с auth.py (auth.create_session/load_session/revoke_session):
--   actor      — логин оператора; single-session ревокация «выйти везде» идёт по нему
--                (auth.create_session: update ... where actor=$1; revoke по actor).
--   issued_at  — момент выдачи (= логин); жёсткий потолок считается как issued_at..expires_at.
-- sid генерится в приложении (uuid4), но оставляем default gen_random_uuid() на случай
-- прямых вставок. Любое расхождение имён ⇒ UndefinedColumnError на КАЖДОМ логине.
--
-- search_phone_hash — серверное состояние поиска по телефону (§3.10). Хранится в
-- сессии (single-operator/single-session), чтобы ОБРАТИМЫЙ unsalted sha256(phone)
-- НЕ попадал в query-string/историю браузера/логи LB. В URL едет лишь opaque-маркер
-- qid=phone без криптосвязи с номером; сервер берёт хеш отсюда. NULL = поиска нет.
create table if not exists admin_sessions (
    sid               uuid        primary key default gen_random_uuid(),
    actor             text        not null,                -- логин оператора (ревокация «выйти везде» по нему)
    issued_at         timestamptz not null default now(),  -- момент выдачи (логин); потолок = issued_at + SESSION_MAX_HOURS
    last_seen         timestamptz not null default now(),  -- бампается на каждом запросе (скользящий idle)
    expires_at        timestamptz not null,                -- жёсткий потолок (логин + SESSION_MAX_HOURS), не продлевается
    revoked           boolean     not null default false,
    ip                inet,                                -- advisory (X-Forwarded-For, best-effort), не security-контроль
    ua                text,                                -- User-Agent на момент логина, advisory
    search_phone_hash text                                 -- серверное состояние поиска по телефону (хеш НЕ в URL), §3.10
);

-- Идемпотентная докатка колонок (если таблица уже создана прежней версией схемы).
-- actor добавляем nullable, затем бэкфиллим и ставим NOT NULL — чтобы ALTER не упал
-- на существующих строках; на пустой таблице оба шага мгновенны.
alter table admin_sessions add column if not exists actor             text;
alter table admin_sessions add column if not exists issued_at         timestamptz not null default now();
alter table admin_sessions add column if not exists search_phone_hash text;
update admin_sessions set actor = '_legacy' where actor is null;
alter table admin_sessions alter column actor set not null;

-- Быстрый отбор живых сессий (по сроку, среди невыданных ревокаций).
create index if not exists admin_sessions_expires_idx
    on admin_sessions (expires_at)
    where revoked = false;

-- Ревокация «выйти везде» / ротация при логине идут по actor — индексируем.
create index if not exists admin_sessions_actor_idx
    on admin_sessions (actor)
    where revoked = false;

-- ── Троттлинг логина ─────────────────────────────────────────────────────────
-- В БД, не в памяти: переживает редеплой контейнера (in-memory сбрасывался бы при
-- каждом push). Стратегия — экспоненциальный tarpit (sleep), а не жёсткий
-- account-lock, чтобы неаутентифицированный флудер не заблокировал оператора.
-- locked_until — опциональная мягкая отметка окна.
--
-- КОНТРАКТ КОЛОНОК — единый с auth.py (login_tarpit_delay/register_login_failure/
-- reset_login_throttle): ключ = account (lowercased submitted username), счётчик =
-- fail_count. ON CONFLICT (account) требует, чтобы account был PRIMARY KEY.
-- Любое расхождение имён ⇒ UndefinedColumnError на КАЖДОМ POST /login (tarpit зовётся
-- до проверки пароля) ⇒ логин полностью сломан, анти-брутфорс не включается.
create table if not exists admin_login_throttle (
    account      text        primary key,            -- lowercased submitted username (см. app.py::login_submit)
    fail_count   int         not null default 0,
    locked_until timestamptz                          -- nullable: окно мягкой блокировки/тарпита
);

-- ── Аудит (append-only) ──────────────────────────────────────────────────────
-- Роль panel_rw имеет ТОЛЬКО INSERT (см. db/panel_role.sql); UPDATE/DELETE отозваны
-- → панель не может переписать собственный след (§3.6). detail (jsonb) — БЕЗ ПДн:
--   • lead_update по notes → {field:'notes', changed:true, len_old, len_new} (не текст);
--   • status                → {field:'status', old, new} (не ПДн);
--   • phone_revealed        → только факт (lead_id), номер НЕ кладём;
--   • actor / ip(advisory) / ua / filter-params / row_count — служебка для расследования.
-- action ∈ login_ok|login_fail|logout|lead_view|phone_revealed|lead_update|
--          export|export_full|lead_erase_requested|lead_erased
--
-- КОНТРАКТ КОЛОНОК — единый с db.py::_insert_audit, который пишет
--   (actor, action, lead_id, ip, user_agent, detail).
-- `at` имеет default now() и в INSERT не указывается. Колонки actor/ip/user_agent
-- ОБЯЗАТЕЛЬНЫ: без них КАЖДЫЙ аудит-INSERT падает UndefinedColumnError, а так как
-- аудит fail-closed и идёт ДО reveal/export и сразу после login_ok — вся панель
-- неработоспособна и не оставляет следа доступа к ПДн (нарушение 152-ФЗ).
create table if not exists admin_audit (
    id         bigserial   primary key,
    at         timestamptz not null default now(),
    actor      text        not null,                              -- кто совершил действие (логин оператора)
    action     text        not null,
    lead_id    uuid        references leads(id) on delete set null,  -- null для login_*/logout
    ip         inet,                                              -- advisory (X-Forwarded-For, best-effort)
    user_agent text,                                              -- advisory (UA на момент действия)
    detail     jsonb
);

-- Идемпотентная докатка колонок (если таблица создана прежней версией схемы).
alter table admin_audit add column if not exists actor      text;
alter table admin_audit add column if not exists ip         inet;
alter table admin_audit add column if not exists user_agent text;
update admin_audit set actor = '_legacy' where actor is null;
alter table admin_audit alter column actor set not null;

-- Аудит обычно читается «последние сверху» и фильтруется по лиду (чистка PII +30d).
create index if not exists admin_audit_at_idx      on admin_audit (at desc);
create index if not exists admin_audit_lead_id_idx on admin_audit (lead_id);
create index if not exists admin_audit_action_idx  on admin_audit (action);

-- ── 152-ФЗ: маркер запроса на удаление/обезличивание ─────────────────────────
-- Выставляется ДЕЙСТВИЕМ оператора «Принять отзыв согласия / запрос субъекта»
-- (§3.9 плана), а также при consent=false. Cron обезличивает строки, где
-- erase_requested_at + 30d <= now() (name/phone/notes/phone_hash → null), пишет
-- аудит action='lead_erased' (доказательство срока для РКН) и чистит PII-историю
-- в admin_audit по этому lead_id. НЕ авто-удаляем по status='lost' (он обратим).
alter table leads add column if not exists erase_requested_at timestamptz;

-- Счётчик/фильтр «К удалению» и выборка cron'а по сроку — частичный индекс.
create index if not exists leads_erase_requested_at_idx
    on leads (erase_requested_at)
    where erase_requested_at is not null;
