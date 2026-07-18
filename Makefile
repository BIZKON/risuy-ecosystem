# S0M локальный каркас. Требует запущенного Docker daemon.
COMPOSE=docker compose -f docker-compose.dev.yml
PG=docker compose -f docker-compose.dev.yml exec -T postgres psql -U gen_user_local -d risuy_dev -v ON_ERROR_STOP=1

.PHONY: up up-infra down db-init skeleton smoke lint

up-infra:
	$(COMPOSE) up -d --wait postgres redis

# Корректный порядок: инфра (--wait до healthy) → схема+роли → потребитель. Иначе
# engine-ingest стартует раньше, чем db-init создаст роль engine_rw, и падает.
up: up-infra db-init
	$(COMPOSE) up -d engine-ingest

down:
	$(COMPOSE) down -v

# Применить снапшот схемы risuy (если есть) + роли/схему engine к эфемерному PG.
db-init:
	@test -f db/_dev/schema_snapshot.sql && cat db/_dev/schema_snapshot.sql | $(PG) || echo "нет schema_snapshot.sql — только roles_bootstrap (Task 5 сгенерит снапшот)"
	cat db/_dev/roles_bootstrap.sql | $(PG)

# Walking-skeleton: одно событие → одна строка в engine.raw_messages.
skeleton:
	$(COMPOSE) run --rm engine-stub
	@sleep 2
	$(PG) -c "select count(*) as rows_in_raw from engine.raw_messages;"

lint:
	ruff check engine/ scripts/engine_rw_leads_isolation_smoke.py

# db-смоуки: RLS панели + engine_rw-изоляция на leads (см. Task 6 плана). Гонит КОНТРОЛЛЕР.
smoke:
	@echo "см. Task 6 плана: RLS_SMOKE_DSN + engine_rw_leads_isolation_smoke"
