# shared/ — код, общий для бота и панели (reseller-платформа, ТЗ §3).
# В образ копируется ДВАЖДЫ (Dockerfile): /app/shared (бот, cwd=/app) и
# /app/admin-panel/shared (панель, cwd=/app/admin-panel) — оба импортируют
# `from shared import vault` без правки sys.path/run_cmd.
