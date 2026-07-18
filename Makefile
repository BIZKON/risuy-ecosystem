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
# При наличии снапшота — ещё и передать владение не-суперюзеру gen_user (иначе смоук
# изоляции панели ложно проваливается: локальный owner-суперюзер обходит RLS даже при FORCE).
db-init:
	@test -f db/_dev/schema_snapshot.sql && cat db/_dev/schema_snapshot.sql | $(PG) || echo "нет schema_snapshot.sql — только roles_bootstrap (Task 5 сгенерит снапшот)"
	cat db/_dev/roles_bootstrap.sql | $(PG)
	@test -f db/_dev/schema_snapshot.sql && cat db/_dev/owner_reassign.sql | $(PG) || echo "снапшота нет — reassign владельца пропущен"

# Walking-skeleton: одно событие → одна строка в engine.raw_messages.
skeleton:
	$(COMPOSE) run --rm engine-stub
	@sleep 2
	$(PG) -c "select count(*) as rows_in_raw from engine.raw_messages;"

lint:
	ruff check engine/ scripts/engine_rw_leads_isolation_smoke.py

# db-смоуки против эфемерного PG: engine_rw-изоляция на leads + RLS панели. Хост-python
# без asyncpg → гоняем в python:3.12 на сети compose (postgres:5432). Панель-смоук — под
# не-суперюзером gen_user (иначе суперюзер обходит RLS даже при FORCE).
NET=$(shell basename $(CURDIR))_default
smoke:
	docker run --rm --network $(NET) -v "$(CURDIR)":/app -w /app \
	  -e ENGINE_RW_SMOKE_DSN="postgresql://engine_rw:engine_rw_local@postgres:5432/risuy_dev" \
	  python:3.12-slim sh -c "pip install -q asyncpg==0.30.0 && python scripts/engine_rw_leads_isolation_smoke.py"
	docker run --rm --network $(NET) -v "$(CURDIR)":/app -w /app \
	  -e RLS_SMOKE_DSN="postgresql://gen_user:gen_user_local@postgres:5432/risuy_dev" \
	  -e PYTHONPATH="admin-panel:." \
	  python:3.12-slim sh -c "pip install -q asyncpg==0.30.0 && python scripts/rls_leads_messages_smoke.py"
