#!/usr/bin/env python3
"""Смоук парадной «ИИ-Агент Про» — self-serve регистрация + соц-вход — на risuy_dev.

Гоняет РЕАЛЬНЫЕ транзакции против risuy_dev (owner-DSN), без моков:
  1. create_client_account: 4 строки одной транзакцией (tenant provisioning + admin_user
     role=admin + membership owner + account_identity), username/slug уникальны;
  2. create_session РЕЗОЛВИТ СВОЙ тенант клиента (НЕ «первый глобальный») — регресс на фикс
     изоляции (клиент-admin не должен получить чужой тенант);
  3. уникальность (provider, external_id): повтор email → UniqueViolation + БЕЗ orphan-тенанта
     (транзакция откатывает уже вставленного tenant'а);
  4. verify_telegram_login: валидный HMAC — ок; подделка hash / просроченный auth_date — None;
  5. resolve_username_by_email: email → username; find_identity: маппинг есть.

Тестовые client_*/smoke удаляются в конце. На прод НЕ запускать.

Запуск: SIGNUP_SMOKE_DSN="postgresql://<owner>@<host>:5432/risuy_dev?sslmode=require" \
        PYTHONPATH=. python3 scripts/signup_smoke.py
"""
import asyncio
import hashlib
import hmac
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "admin-panel"))
os.environ.setdefault("DATABASE_URL", "postgresql://x/y")
os.environ.setdefault("SESSION_SECRET", "smoke-secret-only-for-import-aaaaaaaa")
os.environ.setdefault("ADMIN_USERNAME", "smoke-env-admin")
os.environ.setdefault("ADMIN_PASSWORD_HASH", "$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHQ$aGFzaHN0dWI")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "smoke-bot-token-1234567890")  # для verify_telegram_login

import asyncpg  # noqa: E402
import auth      # noqa: E402  (admin-panel/auth.py)
import config    # noqa: E402
import db         # noqa: E402  (admin-panel/db.py)

DSN = os.environ.get("SIGNUP_SMOKE_DSN")
if not DSN or "/risuy_dev" not in DSN.split("?")[0]:
    raise SystemExit("Задайте SIGNUP_SMOKE_DSN на risuy_dev.")

FAILS: list[str] = []
CREATED: list[tuple[str, str]] = []  # (username, tenant_id) для очистки


def check(name: str, cond: bool, detail="") -> None:
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail != "" else ""))
    if not cond:
        FAILS.append(name)


def _tg_payload(token: str, fields: dict) -> dict:
    """Собрать валидный payload Telegram Login Widget (с корректным hash)."""
    pairs = sorted(f"{k}={v}" for k, v in fields.items())
    dcs = "\n".join(pairs)
    secret = hashlib.sha256(token.encode()).digest()
    h = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    return {**fields, "hash": h}


async def _cleanup(c):
    for username, tenant_id in CREATED:
        await c.execute("delete from admin_sessions where actor = $1", username)
        # admin_users delete каскадит account_identities + memberships (FK on delete cascade).
        await c.execute("delete from admin_users where username = $1", username)
        await c.execute("delete from tenants where id = $1", tenant_id)


async def main() -> None:
    db.pool = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    try:
        # ── 1. create_client_account: атомарная учётка ────────────────────────
        print("1. create_client_account — 4 строки одной транзакцией:")
        emailA = "smoke+a@example.org"
        uA, tA = await db.create_client_account(
            provider="email", external_id=emailA, name=emailA,
            password_hash=await auth.hash_password("smoke-pass-123"), verified=False)
        CREATED.append((uA, tA))
        async with db.pool.acquire() as c:
            t_status = await c.fetchval("select status from tenants where id = $1", tA)
            u_role = await c.fetchval("select role from admin_users where username = $1", uA)
            m_role = await c.fetchval("select role from memberships where username = $1 and tenant_id = $2", uA, tA)
            ident = await c.fetchrow("select provider, external_id, verified from account_identities where username = $1", uA)
        check("tenant в статусе provisioning", t_status == "provisioning", repr(t_status))
        check("admin_user role=operator (НЕ admin — ревью)", u_role == "operator", repr(u_role))
        check("membership role=owner", m_role == "owner", repr(m_role))
        check("identity email/external_id/verified", bool(ident) and ident["provider"] == "email"
              and ident["external_id"] == emailA and ident["verified"] is False)

        # ── 2. create_session резолвит СВОЙ тенант (не «первый глобальный») ───
        print("2. create_session изолирует тенант клиента:")
        emailB = "smoke+b@example.org"
        uB, tB = await db.create_client_account(
            provider="email", external_id=emailB, name=emailB,
            password_hash=await auth.hash_password("smoke-pass-456"), verified=False)
        CREATED.append((uB, tB))
        sidA = await auth.create_session(uA, "admin")
        sidB = await auth.create_session(uB, "admin")
        async with db.pool.acquire() as c:
            seA = str(await c.fetchval("select active_tenant_id from admin_sessions where sid = $1", sidA))
            seB = str(await c.fetchval("select active_tenant_id from admin_sessions where sid = $1", sidB))
        check("сессия A → тенант A (не чужой/не первый)", seA == tA, f"{seA} vs {tA}")
        check("сессия B → тенант B", seB == tB, f"{seB} vs {tB}")
        check("A и B — разные тенанты", tA != tB)

        # ── 3. уникальность identity + откат orphan-тенанта ──────────────────
        print("3. повтор email → UniqueViolation, без orphan-тенанта:")
        async with db.pool.acquire() as c:
            tenants_before = await c.fetchval("select count(*) from tenants")
        raised = False
        try:
            await db.create_client_account(
                provider="email", external_id=emailA, name=emailA,
                password_hash=await auth.hash_password("x"), verified=False)
        except asyncpg.UniqueViolationError:
            raised = True
        async with db.pool.acquire() as c:
            tenants_after = await c.fetchval("select count(*) from tenants")
            ident_cnt = await c.fetchval("select count(*) from account_identities where provider='email' and external_id=$1", emailA)
        check("повтор email бросил UniqueViolation", raised)
        check("orphan-тенант НЕ создан (откат транзакции)", tenants_after == tenants_before, f"{tenants_before}→{tenants_after}")
        check("identity для email ровно одна", ident_cnt == 1, f"факт {ident_cnt}")

        # ── 4. verify_telegram_login ─────────────────────────────────────────
        print("4. verify_telegram_login (HMAC + свежесть):")
        token = config.TELEGRAM_BOT_TOKEN
        good = _tg_payload(token, {"id": "777", "first_name": "Тест", "auth_date": str(int(time.time()))})
        check("валидный payload принят", auth.verify_telegram_login(dict(good)) is not None)
        tampered = dict(good); tampered["hash"] = "00" + tampered["hash"][2:]
        check("подделанный hash отвергнут", auth.verify_telegram_login(tampered) is None)
        stale = _tg_payload(token, {"id": "777", "first_name": "Тест", "auth_date": str(int(time.time()) - 99999)})
        check("просроченный auth_date отвергнут", auth.verify_telegram_login(stale) is None)
        check("чужой токен → отказ", auth.verify_telegram_login(_tg_payload("OTHER", {"id": "1", "auth_date": str(int(time.time()))})) is None)

        # ── 5. резолв email→username + find_identity ─────────────────────────
        print("5. resolve_username_by_email + find_identity:")
        check("email → username", await db.resolve_username_by_email(emailA.upper()) == uA)
        check("несуществующий email → None", await db.resolve_username_by_email("nope@example.org") is None)
        fi = await db.find_identity("email", emailA)
        check("find_identity вернул учётку", bool(fi) and fi["username"] == uA)

        # ── 6. РЕГРЕСС РЕВЬЮ: клиент НЕ платформенный супер (изоляция тенантов) ──
        print("6. клиент-operator изолирован (ревью critical):")
        from auth import Session
        s_client = Session(sid="x", actor=uA, csrf_token="c", role="operator")
        s_env = Session(sid="y", actor=config.ADMIN_USERNAME, csrf_token="c", role="admin")
        check("Session.is_platform=False у клиента", s_client.is_platform is False)
        check("Session.is_platform=True у env-админа", s_env.is_platform is True)
        # list_tenants_for ветвится по личности: клиент видит РОВНО свой тенант
        client_tenants = {str(r["id"]) for r in await db.list_tenants_for(uA, "operator")}
        check("list_tenants_for(клиент) = только свой тенант", client_tenants == {tA},
              f"{client_tenants} vs {{{tA}}}")
        check("tenant_accessible(клиент, СВОЙ) = True", await db.tenant_accessible(uA, "operator", tA))
        check("tenant_accessible(клиент, ЧУЖОЙ) = False (нет /tenants/switch на чужой)",
              (await db.tenant_accessible(uA, "operator", tB)) is False)
        # env-админ видит ВСЕ живые тенанты (включая чужие)
        env_tenants = {str(r["id"]) for r in await db.list_tenants_for(config.ADMIN_USERNAME, "admin")}
        check("env-админ видит и tA, и tB", {tA, tB} <= env_tenants, f"{env_tenants}")

        # ── 7. TG anti-CSRF state seal/open ──────────────────────────────────
        print("7. Telegram state seal/open (anti-CSRF):")
        st = "tg-state-xyz"
        sealed = auth.seal_tg_state(st)
        check("open(seal(state)) == state", auth.open_tg_state(sealed) == st)
        check("подделанный state → None", auth.open_tg_state(sealed[:-3] + "zzz") is None)
        check("пустая cookie → None", auth.open_tg_state(None) is None)

    finally:
        async with db.pool.acquire() as c:
            await _cleanup(c)
        await db.pool.close()

    print()
    if FAILS:
        print(f"❌ ПРОВАЛЫ ({len(FAILS)}): " + ", ".join(FAILS))
        sys.exit(1)
    print("✅ signup smoke — все проверки зелёные")


if __name__ == "__main__":
    asyncio.run(main())
