-- Роль panel_rw для админ-панели лидов «Школа Лесова» (least-privilege).
-- Применить ОДИН РАЗ owner-DSN (роль-владелец БД), ПОСЛЕ db/schema_admin.sql:
--   psql "$OWNER_DATABASE_URL" -f db/panel_role.sql
-- Идемпотентно: CREATE ROLE обёрнут в guard, гранты безопасно переприменяются.
-- Пароль роли здесь НЕ задаётся (см. блок «ВЫДАЧА ПАРОЛЯ» в конце) — чтобы секрет
-- не попал ни в файл, ни в историю shell, ни в журнал запросов. psql-переменные
-- (:'panel_pw') внутри dollar-quoted ($$…$$) тела всё равно не раскрываются.
--
-- panel_rw — НЕ owner бота. Это делает append-only аудит честным: панель физически
-- не может UPDATE/DELETE admin_audit. Бот ходит под своей owner-ролью, эти гранты
-- его не касаются. Поверхность записи панели = ровно status/notes/erase_requested_at
-- на leads + INSERT в admin_audit + CRUD на admin_sessions/admin_login_throttle.

-- ── Создание роли (идемпотентно, без пароля — пароль выдаётся отдельно) ───────
do $$
begin
    if not exists (select 1 from pg_roles where rolname = 'panel_rw') then
        create role panel_rw login;
    end if;
end
$$;

-- ── Доступ к базе/схеме ──────────────────────────────────────────────────────
do $$ begin execute format('grant connect on database %I to panel_rw', current_database()); end $$;
grant usage   on schema   public            to panel_rw;

-- ⚠️ КРИТИЧНО: сброс ШИРОКИХ табличных грантов ПЕРЕД точечными.
-- panel_rw создан через Timeweb DBaaS admins API → это «управляемый» пользователь, и
-- реконсиляция Timeweb периодически выдаёт ему table-level arwd (insert/select/update/
-- delete) на ВСЕ таблицы (грант идёт от gen_user). Без этого revoke панель де-факто
-- может UPDATE/DELETE messages/leads(phone!)/broadcast_recipients/link_clicks/products.file_tg_id
-- в обход column-level грантов ниже — least-privilege ломается молча. Этот revoke + точечные
-- гранты ниже восстанавливают least-privilege на КАЖДОМ применении файла (идемпотентно).
-- ПРИМЕЧАНИЕ: между деплоями реконсиляция может снова выдать широкий грант — поэтому
-- настоящая граница — приложение (панель не делает этих операций) + перевыдача этого файла.
revoke all on all tables in schema public from panel_rw;

-- ── leads: read-всю строку, write — только status/notes/erase_requested_at ───
-- SELECT нужен на всю строку (включая phone): reveal и полный экспорт читают phone
-- легитимно по действию оператора с аудитом. Контроль приватности — на уровне
-- приложения (маска по умолчанию, reveal под аудитом), НЕ грантом.
grant select on leads to panel_rw;
grant update (status, notes, erase_requested_at) on leads to panel_rw;
-- НЕТ insert/delete на leads (лиды создаёт только бот). НЕТ update на phone,
-- consent, subscribed, phone_hash, follow_up_*, guide_sent_at, messenger, source, survey.

-- ── admin_audit: INSERT-only (append-only, §3.6) ─────────────────────────────
grant select, insert on admin_audit to panel_rw;
revoke update, delete on admin_audit from panel_rw;
-- bigserial id → нужен USAGE на его sequence, иначе INSERT упадёт.
grant usage on sequence admin_audit_id_seq to panel_rw;

-- ── admin_sessions: CRUD (выдача / idle-bump / ревокация) ────────────────────
-- sid имеет default gen_random_uuid(), но мы можем генерить sid в приложении;
-- CRUD достаточно. Sequence у uuid-pk нет.
grant select, insert, update, delete on admin_sessions to panel_rw;

-- ── admin_login_throttle: CRUD (upsert счётчика неудач) ──────────────────────
grant select, insert, update, delete on admin_login_throttle to panel_rw;

-- ── admin_users: SELECT (auth-lookup + список «Команда») + точечные INSERT/UPDATE ──
-- (объект в db/schema_team.sql — применять ПОСЛЕ него). DELETE НЕ выдаём: деактивация
-- через active=false (как append-only-философия аудита). text-PK → sequence не нужен.
-- env-админ в этой таблице НЕ хранится (bootstrap-суперюзер вне БД) — гранты его не касаются.
grant select on admin_users to panel_rw;
grant insert (username, password_hash, role, active, created_by) on admin_users to panel_rw;
grant update (password_hash, role, active, updated_at) on admin_users to panel_rw;

-- ── Гигиена: ничего лишнего ──────────────────────────────────────────────────
-- Явно отзываем доступ к прочим функциям/последовательностям схемы (точечный
-- USAGE на admin_audit_id_seq выдан выше и этим revoke не затрагивается, т.к.
-- он адресован конкретному объекту, а revoke — множеству existing-объектов;
-- если порядок применения важен, повторно подтверждаем грант ниже).
revoke all on all functions  in schema public from panel_rw;
revoke all on all sequences  in schema public from panel_rw;
grant usage on sequence admin_audit_id_seq to panel_rw;  -- подтвердить после массового revoke

-- ── Расширение панели: переписка / перехват / рассылки / аналитика (least-privilege) ──
-- (объекты создаются в db/schema_panel_ext.sql — применять ПОСЛЕ него.)
-- Инвариант: панель НЕ пишет в Telegram и не имеет BOT_TOKEN. «Отправка» = INSERT в очередь;
-- реально шлёт БОТ под owner-ролью. Поэтому write-поверхность панели сужена до постановки задач
-- и флагов, а фактические события (messages / материализация получателей / клики) пишет бот.
--
-- Матрица «кто что пишет» (канон §2 плана):
--   leads.bot_paused        UPDATE  — перехват переключает оператор
--   leads.unsubscribed_at   SELECT  — отписку ставит субъект через бота (152-ФЗ); панель только видит
--   messages                SELECT  — тред читает панель; пишет бот (вкл. зеркало операторских ответов)
--   outbox                  SELECT, INSERT — панель кладёт 'queued'; статусы ведёт бот
--   broadcasts              SELECT, INSERT, UPDATE — заявка + draft→queued→canceled; старт/итоги — бот
--   broadcast_recipients    SELECT  — материализацию и статусы ведёт бот (единый WHERE «кому можно»)
--   broadcast_files         SELECT, INSERT — панель кладёт байты; tg_file_id/обнуление bytes — бот
--   link_tokens             SELECT, INSERT — панель регистрирует токены + target_url при создании рассылки
--   link_clicks             SELECT  — пишет обработчик /r в боте

-- leads: добавить UPDATE только на bot_paused (поверх status/notes/erase_requested_at выше).
-- unsubscribed_at панель НЕ пишет — только ЧИТАЕТ (SELECT на leads уже выдан).
grant update (bot_paused) on leads to panel_rw;

-- Переписка: только чтение треда. Пишет бот (вход через middleware, исход через messaging-слой).
grant select on messages to panel_rw;

-- Очередь точечных ответов: панель кладёт 'queued', статусы ведёт бот.
-- update для отмены — опционален и по умолчанию НЕ выдаётся (сужаем write-поверхность).
grant select, insert on outbox to panel_rw;

-- Рассылки: панель создаёт заявку и двигает draft→queued→canceled;
-- started_at/finished_at/totals/recipient_count проставляет бот при исполнении.
grant select, insert, update on broadcasts to panel_rw;

-- Получатели: только SELECT. Материализацию и статусы ведёт БОТ (owner) — единый источник
-- истины «кому слать» (неотменяемый WHERE из §5.1 в одном месте). Панели INSERT/sequence НЕ нужны.
grant select on broadcast_recipients to panel_rw;

-- Файл рассылки: панель кладёт bytes; tg_file_id и обнуление bytes делает бот после заливки.
grant select, insert on broadcast_files to panel_rw;

-- Токены трекинг-ссылок: регистрирует панель (token + target_url) при создании рассылки.
grant select, insert on link_tokens to panel_rw;

-- Клики: только SELECT для аналитики. Пишет обработчик /r/<token> в БОТЕ (owner).
grant select on link_clicks to panel_rw;

-- ── Каталог продуктов (оферов) — объекты в db/schema_products.sql, применять ПОСЛЕ него ──
-- (Матрица «кто что пишет» для каталога — продолжение канона §2 плана:)
--   products              SELECT, INSERT, UPDATE — панель заводит/правит/архивирует офер
--                         и кладёт байты файла; КОЛОНКУ file_tg_id панель НЕ пишет —
--                         её проставляет БОТ (owner) после первой заливки файла в OPS_CHAT_ID.
--   broadcasts.product_id UPDATE(product_id) — панель привязывает офер к рассылке (поверх
--                         уже выданного выше `grant update on broadcasts`).
--   app_settings          SELECT, INSERT, UPDATE — singleton-настройки панели (бот ЧИТАЕТ).
--
-- products: SELECT на всю строку. INSERT/UPDATE — на КОЛОНКАХ, КРОМЕ file_tg_id
-- (его пишет бот, см. §2). Column-level грант — тот же приём, что `grant update
-- (status, notes, erase_requested_at) on leads` выше. id/created_at/updated_at в список
-- INSERT не включаем: id даёт sequence (default), created_at/updated_at — default+триггер;
-- их явная запись панели не нужна. updated_at бампается trg_products_updated_at — не трогаем.
grant select on products to panel_rw;
grant insert (name, kind, price, currency, caption, link, file, file_name, file_mime, status, created_by)
    on products to panel_rw;
-- upload_attempts/upload_error в UPDATE: панель СБРАСЫВАЕТ счётчик попыток заливки в 0
-- при ЗАМЕНЕ/снятии файла офера (новый файл заслуживает свежий бюджет попыток; иначе
-- исчерпанный старым битым файлом счётчик навсегда исключил бы новый годный файл из
-- очереди заливки). Это операционная retry-стейт-колонка, не доказательство заливки:
-- file_tg_id по-прежнему пишет ТОЛЬКО бот (column-level грант его НЕ включает).
grant update (name, kind, price, currency, caption, link, file, file_name, file_mime, status,
              upload_attempts, upload_error)
    on products to panel_rw;
-- НЕТ delete на products (архивируем через status='archived', строки не удаляем).
-- file_tg_id (и обнуление file после заливки) пишет БОТ под owner-ролью — не грантуется панели.

-- broadcasts.product_id: точечный UPDATE одной колонки поверх общего update on broadcasts.
-- (Общий `grant update on broadcasts` выше уже покрывает все колонки, включая product_id;
-- дублирующий column-grant безвреден и фиксирует намерение явно — привязка офера к рассылке.)
grant update (product_id) on broadcasts to panel_rw;

-- app_settings: singleton-настройки (active_lead_magnet_product_id и т.п.). Панель пишет
-- (upsert ключа), БОТ ЧИТАЕТ при выдаче воронки. updated_at — триггер, key/value пишет панель.
grant select, insert, update on app_settings to panel_rw;

-- ── Платежи / заказы (orders) — объекты в db/schema_orders.sql, применять ПОСЛЕ него ──
-- Phase 1A: панель фиксирует продажи руками (source='manual') и читает для дашборда.
--   orders  SELECT, INSERT, UPDATE — оператор записывает/правит заказ; статус paid|refunded.
-- Phase 1B (онлайн-оплата): строки orders провайдеров пишет БОТ (owner) из вебхука —
-- эти гранты его не касаются. id/created_at — default (uuid/now), в INSERT не включаем;
-- provider_payment_id панель не пишет (его проставит бот в 1B) — column-level грант его НЕ
-- включает. paid_at пишет панель (manual-продажа сразу оплачена).
grant select on orders to panel_rw;
grant insert (lead_id, product_id, amount, currency, status, source, note, created_by, paid_at)
    on orders to panel_rw;
grant update (status, note, paid_at, amount, currency, product_id)
    on orders to panel_rw;
-- НЕТ delete на orders (финансовую историю не удаляем; возврат = status='refunded').
-- UUID-PK (default gen_random_uuid()) → секвенса нет, грант usage on sequence не нужен.

-- ── Биллинг сервиса (service_invoices) — объекты в db/schema_service.sql, ПОСЛЕ него ──
-- B2B-абонентка школа→агентство. Панель INSERT счёта при «Оплатить» и UPDATE статуса
-- из вебхука ЮKassa (вебхук в процессе панели перепроверяет платёж через API ЮKassa).
--   service_invoices  SELECT, INSERT, UPDATE — выставление счёта тарифа + отметка
--   оплаты из вебхука (status/yookassa_payment_id/card_last4/paid_at).
grant select on service_invoices to panel_rw;
grant insert (period_start, period_end, plan_key, plan_name, quota, plan_amount,
              overage_count, overage_amount, amount, currency, status,
              yookassa_payment_id, created_by)
    on service_invoices to panel_rw;
grant update (status, yookassa_payment_id, card_last4, paid_at)
    on service_invoices to panel_rw;
-- НЕТ delete (финансовую историю не удаляем; отмена = status='canceled').
-- UUID-PK (default gen_random_uuid()) → секвенса нет, грант usage on sequence не нужен.

-- bigserial-PK с INSERT от панели → USAGE на их sequence (иначе INSERT упадёт).
-- ВАЖНО: выдаём ПОСЛЕ массового `revoke all on all sequences … from panel_rw` выше,
-- по образцу admin_audit_id_seq — иначе revoke снимет эти гранты.
grant usage on sequence outbox_id_seq          to panel_rw;
grant usage on sequence broadcasts_id_seq      to panel_rw;
grant usage on sequence broadcast_files_id_seq to panel_rw;
grant usage on sequence products_id_seq        to panel_rw;  -- INSERT нового офера панелью
-- messages / broadcast_recipients / link_clicks (bigserial) — туда пишет БОТ (owner),
-- панели их sequence НЕ нужен. link_tokens.token / app_settings.key — text PK, sequence нет.

-- ─────────────────────────────────────────────────────────────────────────────
-- ВЫДАЧА ПАРОЛЯ РОЛИ (НЕ в git, НЕ в этом файле, НЕ в shell-истории):
--
--   1) Сгенерировать стойкий пароль офлайн:
--        python -c "import secrets; print(secrets.token_urlsafe(32))"
--
--   2) Выставить пароль роли разово, owner-DSN. Запустить psql ИНТЕРАКТИВНО и
--      ввести команду вручную (начните строку с пробела + \set HISTCONTROL
--      ignorespace, чтобы пароль не осел в ~/.psql_history):
--        ALTER ROLE panel_rw PASSWORD 'СГЕНЕРИРОВАННЫЙ_ПАРОЛЬ';
--      NB: не подставляйте пароль через `psql -v … -f` — внутри do $$…$$ переменные
--      не раскрываются, а в shell-команде значение осядет в истории.
--
--   3) Собрать DSN панели и положить в Timeweb env DATABASE_URL (PATCH /apps/<id>),
--      тот же кластер/хост/порт/БД, что у бота, но user=panel_rw:
--        postgresql://panel_rw:СГЕНЕРИРОВАННЫЙ_ПАРОЛЬ@<host>:<port>/<db>?sslmode=verify-full
--
--   4) Ротация = повтор п.2 с новым паролем + обновить DATABASE_URL панели + redeploy.
--      Бот продолжает работать на своём owner-DSN — его не трогаем.
-- ─────────────────────────────────────────────────────────────────────────────
