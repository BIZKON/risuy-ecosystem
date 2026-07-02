#!/usr/bin/env python3
"""Unit-смоук мастер-гейта DaData (prospects §14 legal gate): наличие токена в env САМО ПО СЕБЕ
НЕ включает живой lookup — нужен ещё явный DADATA_ENABLED=true. Без сети/БД.
  PYTHONPATH=. ./.venv-smoke/bin/python scripts/dadata_gate_smoke.py"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "admin-panel"))

# Заглушки обязательных env (unit-тест без БД). DADATA_* НЕ ставим → проверяем дефолт.
os.environ.setdefault("DATABASE_URL", "postgresql://x/y")
os.environ.setdefault("SESSION_SECRET", "smoke-secret-only-for-import-aaaaaaaa")
os.environ.setdefault("ADMIN_USERNAME", "smoke")
os.environ.setdefault("ADMIN_PASSWORD_HASH", "$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHQ$aGFzaHN0dWI")

import config  # noqa: E402
import dadata  # noqa: E402

FAILS = []


def check(name, cond):
    print(f"  {'OK ' if cond else 'FAIL'} {name}")
    if not cond:
        FAILS.append(name)


def main():
    check("дефолт DADATA_ENABLED=False (env не задан)", config.DADATA_ENABLED is False)

    # Токен+секрет ЕСТЬ, но флаг ВЫКЛ → lookup закрыт (ключевое требование §14).
    config.DADATA_ENABLED = False
    config.DADATA_API_KEY = "test-token"
    config.DADATA_SECRET_KEY = "test-secret"
    check("токен+секрет есть, флаг OFF → is_configured()=False", dadata.is_configured() is False)

    # Флаг ВКЛ, но токена нет → закрыт.
    config.DADATA_ENABLED = True
    config.DADATA_API_KEY = ""
    check("флаг ON, токена нет → is_configured()=False", dadata.is_configured() is False)

    # Флаг ВКЛ, токен есть, секрета нет → закрыт.
    config.DADATA_API_KEY = "test-token"
    config.DADATA_SECRET_KEY = ""
    check("флаг ON, секрета нет → is_configured()=False", dadata.is_configured() is False)

    # Все три условия → открыт.
    config.DADATA_ENABLED = True
    config.DADATA_API_KEY = "test-token"
    config.DADATA_SECRET_KEY = "test-secret"
    check("флаг ON + токен + секрет → is_configured()=True", dadata.is_configured() is True)

    print("\nВСЕ ОК" if not FAILS else "\nПРОВАЛЫ: " + ", ".join(FAILS))
    sys.exit(1 if FAILS else 0)


main()
