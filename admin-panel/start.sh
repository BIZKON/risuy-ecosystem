#!/bin/sh
# Launcher панели на Timeweb App Platform (общий мультиплекс-образ с ботом).
#
# WORKDIR образа = /app (так нужно боту: bot.py лежит плоско в /app). Панель — в
# /app/admin-panel и обращается к templates/static ОТНОСИТЕЛЬНЫМИ путями, поэтому
# перед стартом переходим в её каталог.
#
# run_cmd приложения-панели в Timeweb = ровно «sh /app/admin-panel/start.sh» —
# без кавычек и шелл-операторов на уровне Timeweb (он непредсказуемо оборачивает
# run_cmd в свой sh -c, из-за чего вложенные кавычки/`&&` рвут парсинг). Вся
# логика запуска инкапсулирована здесь, где её парсит уже наш sh.
set -e
cd /app/admin-panel
exec uvicorn app:app --host 0.0.0.0 --port "${PORT:-8080}"
