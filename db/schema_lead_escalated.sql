-- A3: авто-эскалация горячего лида менеджерам. Дедуп — одна карточка на лид:
-- escalated_at ставится при первой эскалации, повторный маркер карточку не плодит.
-- EXPAND (nullable), безопасно при живом коде; RLS на leads уже включён — колонка под него
-- не влияет (это атрибут той же строки). Применять owner-DSN, СНАЧАЛА risuy_dev.
--   ~/.claude/scripts/twc-migrate.sh 4171827 81.31.246.136 risuy_dev gen_user db/schema_lead_escalated.sql
alter table leads add column if not exists escalated_at timestamptz;
