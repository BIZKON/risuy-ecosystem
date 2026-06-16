-- Каталог переиспользуемых ПРОДУКТОВ (оферов) для рассылок «Школа Лесова».
-- Отдельный раздел «Продукты»: офер заводится ОДИН раз и переиспользуется в любом
-- числе рассылок (broadcasts.product_id). Продукт = name + kind (lead_magnet|tripwire|
-- main) + опц. цена + опц. подпись + опц. ВНЕШНЯЯ ссылка оплаты/материала + опц. файл.
-- Оплата — НЕ нативная: показываем цену+название в рассылке и ведём кнопкой/ссылкой на
-- внешний эквайринг через тот же трекинг /r (link_tokens.target_url). Лид-магнит-продукт
-- опционально становится выдачей воронки вместо GUIDE_URL-заглушки (см. app_settings).
--
-- ПОРЯДОК ПРИМЕНЕНИЯ (строго, дополняет §2 плана):
--   db/schema.sql → db/schema_admin.sql → db/schema_panel_ext.sql → db/schema_products.sql
--     → db/panel_role.sql
-- Этот файл — ПОСЛЕ schema_panel_ext.sql (ссылается на broadcasts), ДО panel_role.sql
-- (гранты panel_rw на products/app_settings и их sequence живут в panel_role.sql и
-- ссылаются на созданные здесь объекты).
--
-- Применить ОДИН РАЗ owner-DSN (роль-владелец БД, не panel_rw — у неё нет прав на DDL):
--   psql "$OWNER_DATABASE_URL" -f db/schema_products.sql
--
-- ⚠️ Деплой DDL ПЕРЕД кодом: сначала owner-DSN накатывает этот файл на РАБОТАЮЩЕМ боте
-- (всё if not exists / add column if not exists; старый код новые объекты/колонки
-- игнорирует), и ТОЛЬКО потом выкатывается новый образ панели/бота. Иначе чтение
-- products/broadcasts.product_id/app_settings в новом коде упадёт UndefinedTable/UndefinedColumn.
--
-- ⚠️ ПРИ ОБНОВЛЕНИИ (re-apply на уже существующей БД): этот файл идемпотентно ДОБАВЛЯЕТ
-- колонки products.upload_attempts / products.upload_error (кэп попыток заливки файла
-- офера). ПОРЯДОК тот же: сначала owner-DSN накатывает schema_products.sql (добавит
-- колонки), ПОТОМ panel_role.sql (там `grant update (... upload_attempts, upload_error)
-- on products` — без колонок грант упадёт UndefinedColumn), и лишь затем — новый образ.
--
-- ── Граница доступа (несущий инвариант, продолжение §2 плана) ─────────────────
-- Панель ходит под panel_rw, БЕЗ BOT_TOKEN. Бот — под owner-ролью (gen_user).
--   • products            — панель кладёт офер (name/kind/price/caption/link/file байты),
--                           правит и архивирует; file_tg_id проставляет БОТ после первой
--                           заливки файла в OPS_CHAT_ID (как broadcast_files.tg_file_id).
--   • broadcasts.product_id — панель привязывает офер к рассылке (UPDATE одной колонки).
--   • app_settings        — singleton-настройки (active_lead_magnet_product_id и т.п.):
--                           панель пишет, бот ЧИТАЕТ (выдача воронки).
--
-- ПДн-потоки: products НЕ несёт ПДн субъектов (это контент-офер, а не лид) → вне
-- retention-cron по лидам. files.file (bytea) обнуляется ботом после получения file_tg_id
-- (гигиена места/однократность заливки, как broadcast_files §6.5).

create extension if not exists "pgcrypto";

-- ── set_updated_at(): функция уже создана в db/schema.sql (применяется первым по
--    документированному порядку). Объявляем идемпотентно `create or replace`, чтобы
--    этот файл оставался самодостаточным, если когда-нибудь применится отдельно. ──
create or replace function set_updated_at() returns trigger as $$
begin
    new.updated_at = now();
    return new;
end;
$$ language plpgsql;


-- ── ПРОДУКТЫ (каталог оферов) ────────────────────────────────────────────────
-- id bigserial (как broadcasts: проще sequence-грант панели + сортировка/URL, не uuid).
-- kind: lead_magnet (бесплатная выдача/воронка) | tripwire (дешёвый трипваер) | main
--   (основной продукт). CHECK держим в синхроне с PRODUCT_KINDS в коде панели.
-- price numeric(12,2) NULL: цена опц. (у лид-магнита её обычно нет). currency NOT NULL
--   default 'RUB' — показывается рядом с ценой; меняется редко.
-- caption: подпись/описание офера (идёт в рассылку и/или как caption к файлу).
-- link: ВНЕШНЯЯ ссылка оплаты/материала (опц.). Трекинг /r строится поверх неё панелью
--   через link_tokens (как у рассылок) — здесь храним «сырой» целевой URL офера.
-- file/file_name/file_mime: панель кладёт БАЙТЫ загруженного файла (опц.). Валидацию
--   (расширение+MIME+magic-byte, размер ≤50 МБ Telegram-бота, отказ исполняемым) делает
--   ПАНЕЛЬ до записи — БД хранит уже проверенные байты. Тип отправки (photo/document):
--   бот выводит из file_mime (messaging.kind_for_mime), панель — через sniff_product_file
--   (security.py, allow-list+magic-byte → spec["send"]). Функционально эквивалентно.
-- file_tg_id: проставляет БОТ после первой заливки file в OPS_CHAT_ID; переиспользуется
--   во всех рассылках/выдачах. После проставления file (bytea) можно обнулить.
-- ИНВАРИАНТ «файл И/ИЛИ ссылка, но хотя бы одно» НЕ форсируем CHECK-ом на уровне БД:
--   у lead_magnet допустим только файл, у main — только ссылка, у tripwire — оба; «хотя
--   бы одно из (file_tg_id|file|link)» проверяет ПАНЕЛЬ при сохранении (UX-сообщение
--   лучше, чем сырой constraint; к тому же бот заливает file→file_tg_id асинхронно, и
--   жёсткий CHECK мешал бы промежуточному состоянию). Решение симметрично broadcast_files.
-- status: active | archived (архив скрывает офер из выбора, но не рвёт ссылки/историю).
-- created_by: логин оператора, как broadcasts.created_by.
create table if not exists products (
    id          bigserial   primary key,
    name        text        not null,
    kind        text        not null,                          -- lead_magnet|tripwire|main
    price       numeric(12,2),                                 -- опц. (NULL = цена не показывается)
    currency    text        not null default 'RUB',
    caption     text,                                          -- подпись/описание офера
    link        text,                                          -- внешняя ссылка оплаты/материала (опц.)
    file        bytea,                                         -- байты файла (опц.); бот обнуляет после file_tg_id
    file_name   text,
    file_mime   text,
    file_tg_id  text,                                          -- БОТ проставляет после первой заливки в OPS_CHAT_ID
    -- Потолок попыток заливки файла офера ботом (симметрично outbox.attempts/
    -- broadcast_recipients.attempts): если upload_file_to_chat падает ПОСТОЯННО на
    -- валидном-по-magic, но отвергаемом Telegram файле (напр. 0-байтовая картинка),
    -- без кэпа продукт переселектировался бы каждым тиком воркера (5с) вечно, тратя
    -- токен бакета и засоряя OPS_CHAT_ID. Бот инкрементит при неудаче и исключает
    -- офер из очереди заливки при upload_attempts >= лимита (см. list_products_pending_upload).
    upload_attempts integer  not null default 0,
    upload_error    text,                                      -- последняя ошибка заливки (диагностика)
    status      text        not null default 'active',         -- active|archived
    created_by  text,
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now(),
    constraint products_kind_chk   check (kind   in ('lead_magnet', 'tripwire', 'main')),
    constraint products_status_chk check (status in ('active', 'archived'))
);

-- Идемпотентная добавка колонок для уже существующей таблицы products (если schema
-- применялась до введения кэпа попыток заливки). Безопасно на работающем проде:
-- старый код их игнорирует, новый — использует. Держать ПОСЛЕ create table products.
alter table products add column if not exists upload_attempts integer not null default 0;
alter table products add column if not exists upload_error    text;

-- updated_at автоматически (тот же триггер-паттерн, что у leads).
drop trigger if exists trg_products_updated_at on products;
create trigger trg_products_updated_at
    before update on products
    for each row execute function set_updated_at();

-- Индексы: выбор активного офера по виду (композер рассылки) + общий список по дате.
create index if not exists products_status_kind_idx on products (status, kind);
create index if not exists products_created_idx     on products (created_at desc);
-- Очередь заливки файла ботом: продукты с байтами, но ещё без file_tg_id (см. воркер).
-- Кэп попыток (upload_attempts < лимит) бот накладывает в SELECT (list_products_pending_upload),
-- не в частичном индексе — литерал-лимит в индексе захардкодил бы значение и сломался бы
-- при смене env PRODUCT_UPLOAD_MAX_ATTEMPTS. Индекс остаётся селективным (каталог мал).
create index if not exists products_pending_upload_idx
    on products (id) where file is not null and file_tg_id is null;


-- ── broadcasts.product_id: привязка офера к рассылке (опц.) ───────────────────
-- ON DELETE SET NULL: архивируем оферы, а не удаляем; но если строку всё же удалят
-- owner-ролью, рассылка-история не рвётся (product_id обнуляется, не каскад-удаление).
alter table broadcasts
    add column if not exists product_id bigint references products(id) on delete set null;
create index if not exists broadcasts_product_idx on broadcasts (product_id)
    where product_id is not null;


-- ── app_settings: singleton-настройки панели (KV) ────────────────────────────
-- Зачем KV-таблица, а не колонка: настройка глобальная (одна на всю систему), а не
-- атрибут строки — отдельная одно-строчная config-таблица была бы анти-паттерном, а KV
-- расширяется без миграций под будущие singleton-флаги. Сейчас единственный ключ:
--   'active_lead_magnet_product_id' — id продукта-лид-магнита, которым бот ЗАМЕНЯЕТ
--   GUIDE_URL-заглушку при выдаче воронки. Пусто/нет строки/невалидный id → бот падает
--   на фолбэк GUIDE_URL (env остаётся источником истины по умолчанию, см. решение владельца).
-- value text: id храним строкой (KV-универсальность); бот валидирует и приводит к bigint,
--   проверяя, что продукт существует, kind='lead_magnet' и status='active'.
create table if not exists app_settings (
    key        text        primary key,
    value      text,
    updated_at timestamptz not null default now()
);

drop trigger if exists trg_app_settings_updated_at on app_settings;
create trigger trg_app_settings_updated_at
    before update on app_settings
    for each row execute function set_updated_at();
