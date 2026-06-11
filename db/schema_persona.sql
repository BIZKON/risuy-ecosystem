-- «ИИ-сотрудник на диалог»: персона, выбранная оператором для КОНКРЕТНОГО лида.
-- Применять owner-DSN ПЕРЕД деплоем кода:
--   bash ~/.claude/scripts/twc-migrate.sh 4171827 81.31.246.136 risuy gen_user db/schema_persona.sql
-- Идемпотентно (add column if not exists + grant безопасно переприменяется).
--
-- Приоритет выбора ИИ-сотрудника (бот, get_ai_overrides):
--   leads.ai_persona (этот диалог, ручной выбор оператора)  >  канал (ai_*__<source>)  >  глобальная настройка.
-- NULL = «нет ручного выбора» → наследуется канал/глобал. Колонка стирается вместе с
-- лидом (152-ФЗ erase) автоматически — отдельной очистки не требует. Слаг персоны
-- валидируется приложением (config.PERSONA_PRESETS); БД хранит произвольный text.

alter table leads add column if not exists ai_persona text;

-- panel_rw (least-privilege): панель ПИШЕТ выбор сотрудника диалога. Бот ходит под owner —
-- ему грант не нужен. Зеркалится в db/panel_role.sql (перевыдаётся при реконсиляции Timeweb).
grant update (ai_persona) on leads to panel_rw;
