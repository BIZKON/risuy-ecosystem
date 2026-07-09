-- tenant_brief: бриф-онбординг тенанта (опрос → черновик оркестратора → применение).
-- Аддитивно, идемпотентно (IF NOT EXISTS). Применение:
--   twc-migrate.sh 4171827 81.31.246.136 risuy_dev gen_user db/migrate_tenant_brief.sql
-- БЕЗ tenant-isolation RLS: кросс-тенантный платформенный артефакт (как tenants),
-- доступ гейтится приложением (is_platform) и секретностью token; бот читает по token.

CREATE TABLE IF NOT EXISTS tenant_brief (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    token        text NOT NULL UNIQUE,
    status       text NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending','submitted','proposed','applied','expired')),
    answers      jsonb,
    proposal     jsonb,
    applied      jsonb,
    created_by   text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    submitted_at timestamptz,
    proposed_at  timestamptz,
    applied_at   timestamptz,
    expires_at   timestamptz
);

CREATE INDEX IF NOT EXISTS tenant_brief_tenant_idx ON tenant_brief (tenant_id);
CREATE INDEX IF NOT EXISTS tenant_brief_status_idx ON tenant_brief (status);

-- Гранты. panel_rw — полный RW (кросс-тенантный платформенный раздел, без RLS).
-- Бот пишет ответы по token: та же роль, что читает tenant_settings в get_legal_doc_data.
-- Проверено (см. db/panel_role.sql, комментарий у tenant_settings): «панель пишет,
-- бот (owner) читает» — у tenant_settings НЕТ отдельной read-роли бота, бот ходит
-- под owner-DSN (bypass грантов). Поэтому доп. GRANT для роли бота на tenant_brief
-- не требуется — панельного грантa panel_rw ниже достаточно.
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'panel_rw') THEN
        GRANT SELECT, INSERT, UPDATE ON tenant_brief TO panel_rw;
    END IF;
END $$;
