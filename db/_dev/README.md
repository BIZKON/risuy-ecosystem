# db/_dev — локальный dev-bootstrap (НЕ для прода)

- `roles_bootstrap.sql` — роли `panel_rw`/`engine_rw` + схема `engine` + заглушка `raw_messages`
  для эфемерного PG из `docker-compose.dev.yml`. В проде роли уже есть.
- `schema_snapshot.sql` — снапшот `pg_dump --schema-only` из `risuy_dev` (истинная схема risuy
  + RLS-политики). Пересоздать (контроллер, разовый DSN на risuy_dev):

      pg_dump --schema-only --no-owner --no-privileges \
        "postgresql://<owner>@81.31.246.136:5432/risuy_dev?sslmode=require" > db/_dev/schema_snapshot.sql

  Гард: файл не должен содержать `INSERT`/данных — только DDL. Роли применяются отдельно
  (`roles_bootstrap.sql`), т.к. `pg_dump --schema-only` роли не выгружает.
