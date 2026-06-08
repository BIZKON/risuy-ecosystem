-- Вложение файла/голосового в ЛИЧНЫЙ ответ оператора лиду (панель → outbox → бот).
-- Зеркало продуктовой заливки products.file* (schema_products.sql): панель кладёт в
-- outbox БАЙТЫ загруженного файла + явный kind, бот асинхронно (тик воркера 5с) заливает
-- их в OPS_CHAT_ID, получает file_id и обнуляет байты. Паттерн «байты → file_id» —
-- ОДИН-в-один с products.file → products.file_tg_id (см. _drain_product_uploads).
--
-- ПОРЯДОК ПРИМЕНЕНИЯ (строго, продолжение порядка из schema_products.sql):
--   db/schema.sql → db/schema_admin.sql → db/schema_panel_ext.sql → db/schema_products.sql
--     → db/schema_outbox_attach.sql → db/panel_role.sql
-- Этот файл — ПОСЛЕ schema_panel_ext.sql (создаёт таблицу outbox: id/kind/text/file_id),
-- иначе ALTER упадёт UndefinedTable. Держать ДО panel_role.sql, если в гранты добавятся
-- новые колонки outbox (грант по колонке требует её существования — как у products).
--
-- Применить ОДИН РАЗ owner-DSN (роль-владелец БД, не panel_rw — у неё нет прав на DDL):
--   psql "$OWNER_DATABASE_URL" -f db/schema_outbox_attach.sql
--
-- ⚠️ Деплой DDL ПЕРЕД кодом: сначала owner-DSN накатывает этот файл на РАБОТАЮЩЕМ боте
-- (всё add column if not exists / index if not exists; старый код новые колонки
-- игнорирует), и ТОЛЬКО потом выкатывается новый образ панели/бота. Иначе чтение
-- outbox.file_bytes/upload_attempts в новом коде упадёт UndefinedColumn.
--
-- ПДн-потоки: outbox несёт адрес лида (tg_user_id денорм) — это уже личная переписка,
-- в общем периметре outbox. file_bytes (bytea) обнуляется ботом после получения file_id
-- (гигиена места/однократность заливки, как products.file и broadcast_files.file).
--
-- ЗНАЧЕНИЯ outbox.kind (панель ставит ЯВНО при INSERT, бот их уважает):
--   'text'     — текстовый ответ (как сейчас, default);
--   'photo'    — image/* (sendPhoto);
--   'document' — pdf/doc/xls/... (sendDocument);
--   'voice'    — запись с микрофона → бот конвертит ffmpeg в ogg/opus (sendVoice);
--   'audio'    — fallback, если ffmpeg упал: шлём исходник как аудио (sendAudio).
-- Тип берётся из строки (it["kind"]), НЕ выводится из mime — иначе voice превратился бы
-- в document. CHECK на множество kind НЕ форсируем (текущая outbox его не имеет; не
-- ломаем поведение на работающем проде).

-- ── outbox.file_*: staging байтов вложения для асинхронной заливки ботом ──────
-- file_bytes: панель кладёт БАЙТЫ загруженного файла (опц.). Валидацию (magic-byte,
--   отказ exe — тот же sniff, что в /products) делает ПАНЕЛЬ до записи; БД хранит уже
--   проверенные байты. Бот обнуляет file_bytes после получения file_id.
-- file_name/file_mime: имя и MIME исходника (для sendDocument/sendAudio и диагностики).
-- upload_attempts/upload_error: кэп попыток заливки (симметрично products.upload_attempts
--   и outbox.attempts отправки): если upload_file_to_chat падает ПОСТОЯННО на валидном-по-
--   magic, но отвергаемом Telegram файле, без кэпа строка переселектировалась бы каждым
--   тиком воркера (5с) вечно. Бот инкрементит при неудаче и исключает строку из очереди
--   заливки при upload_attempts >= лимита (см. list_outbox_pending_upload).
alter table outbox add column if not exists file_bytes      bytea;
alter table outbox add column if not exists file_name       text;
alter table outbox add column if not exists file_mime       text;
alter table outbox add column if not exists upload_attempts integer not null default 0;
alter table outbox add column if not exists upload_error    text;

-- Очередь заливки файла ботом: строки с байтами, но ещё без file_id (см. воркер,
-- _drain_outbox_uploads). Кэп попыток (upload_attempts < лимит) бот накладывает в SELECT
-- (list_outbox_pending_upload), не в частичном индексе — литерал-лимит захардкодил бы
-- значение env и сломался бы при его смене. Индекс остаётся селективным (вложений мало).
-- Зеркало products_pending_upload_idx.
create index if not exists outbox_pending_upload_idx
    on outbox (id) where file_bytes is not null and file_id is null;
