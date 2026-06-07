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

-- bigserial-PK с INSERT от панели → USAGE на их sequence (иначе INSERT упадёт).
-- ВАЖНО: выдаём ПОСЛЕ массового `revoke all on all sequences … from panel_rw` выше,
-- по образцу admin_audit_id_seq — иначе revoke снимет эти гранты.
grant usage on sequence outbox_id_seq          to panel_rw;
grant usage on sequence broadcasts_id_seq      to panel_rw;
grant usage on sequence broadcast_files_id_seq to panel_rw;
-- messages / broadcast_recipients / link_clicks (bigserial) — туда пишет БОТ (owner),
-- панели их sequence НЕ нужен. link_tokens.token — text PK, sequence нет.

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
