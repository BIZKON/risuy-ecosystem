#!/usr/bin/env python3
"""Смоук раздела «Профиль» (личный кабинет: профиль, безопасность, способы входа) на risuy_dev.

Гоняет РЕАЛЬНЫЕ транзакции против risuy_dev (owner-DSN), без моков:
  1. get_account / list_account_identities — учётка + способы входа клиента;
  2. set_account_display_name_with_audit — правит display_name по ВСЕМ личностям; пустое → NULL;
     env-админ (нет личности) → False (нечего обновлять);
  3. change_own_password_with_audit + auth.authenticate — смена своего пароля:
     старый пароль больше НЕ подходит, новый подходит; env-админ (нет в admin_users) → False;
  4. подтверждение текущего пароля (гейт роута): authenticate(СВОЙ, неверный) → None, (СВОЙ, верный) → ок;
  5. revoke_all_sessions_with_audit — «выйти на других устройствах»: keep_sid оставляет текущую
     живой, прочие ревокает; без keep_sid → ревокает все (вкл. текущую);
  6. изоляция: смена имени/пароля бьёт ТОЛЬКО свою учётку (чужая не затронута);
  7. allow-list схем ссылки поддержки (config.SUPPORT_URL_SCHEMES): javascript:/data: отвергаются.

Тестовые client_*/smoke удаляются в конце. На прод НЕ запускать.

Запуск: ACCOUNT_SMOKE_DSN="postgresql://<owner>@<host>:5432/risuy_dev?sslmode=require" \
        PYTHONPATH=. ./.venv-smoke/bin/python scripts/account_smoke.py
"""
import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "admin-panel"))
os.environ.setdefault("DATABASE_URL", "postgresql://x/y")
os.environ.setdefault("SESSION_SECRET", "smoke-secret-only-for-import-aaaaaaaa")
os.environ.setdefault("ADMIN_USERNAME", "smoke-env-admin")
os.environ.setdefault("ADMIN_PASSWORD_HASH", "$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHQ$aGFzaHN0dWI")

import asyncpg  # noqa: E402
import auth      # noqa: E402  (admin-panel/auth.py)
import config    # noqa: E402
import db        # noqa: E402  (admin-panel/db.py)

DSN = os.environ.get("ACCOUNT_SMOKE_DSN")
if not DSN or "/risuy_dev" not in DSN.split("?")[0]:
    raise SystemExit("Задайте ACCOUNT_SMOKE_DSN на risuy_dev.")

FAILS: list[str] = []
CREATED: list[tuple[str, str]] = []  # (username, tenant_id) для очистки
TEAM_USERS: list[str] = []           # team-операторы без тенанта/личности


def check(name: str, cond: bool, detail="") -> None:
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail != "" else ""))
    if not cond:
        FAILS.append(name)


def _safe_support_url(url: str) -> str:
    """Зеркало app._safe_support_url — проверяем тот же allow-list схем (без импорта app:
    он монтирует StaticFiles и тянет cwd-зависимый путь)."""
    u = (url or "").strip()
    return u if u.startswith(config.SUPPORT_URL_SCHEMES) else ""


async def _cleanup(c):
    for username, tenant_id in CREATED:
        await c.execute("delete from admin_sessions where actor = $1", username)
        await c.execute("delete from admin_users where username = $1", username)  # каскад identities/memberships
        await c.execute("delete from tenants where id = $1", tenant_id)
    for username in TEAM_USERS:
        await c.execute("delete from admin_sessions where actor = $1", username)
        await c.execute("delete from admin_users where username = $1", username)


async def main() -> None:
    db.pool = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    try:
        PW_OLD = "smoke-old-pass-123"
        PW_NEW = "smoke-new-pass-456"
        emailA = "smoke+acc-a@example.org"
        emailB = "smoke+acc-b@example.org"

        uA, tA = await db.create_client_account(
            provider="email", external_id=emailA, name=emailA,
            password_hash=auth.hash_password(PW_OLD), display_name="Старое Имя", verified=False)
        CREATED.append((uA, tA))
        uB, tB = await db.create_client_account(
            provider="email", external_id=emailB, name=emailB,
            password_hash=auth.hash_password("other-pass-789"), display_name="Чужое Имя", verified=False)
        CREATED.append((uB, tB))

        # ── 1. get_account / list_account_identities ─────────────────────────
        print("1. get_account + list_account_identities:")
        acct = await db.get_account(uA)
        check("get_account вернул учётку", bool(acct) and acct["username"] == uA)
        check("роль клиента = operator", acct and acct["role"] == "operator", repr(acct["role"] if acct else None))
        check("password_hash НЕ отдан в get_account", acct is not None and "password_hash" not in acct.keys())
        idents = await db.list_account_identities(uA)
        check("одна личность (email)", len(idents) == 1 and idents[0]["provider"] == "email")
        check("get_account(env-админ) → None (вне admin_users)", await db.get_account(config.ADMIN_USERNAME) is None)

        # ── 2. set_account_display_name_with_audit ───────────────────────────
        print("2. правка отображаемого имени:")
        ok = await db.set_account_display_name_with_audit(uA, "Новое Имя", ip=None, user_agent=None)
        async with db.pool.acquire() as c:
            dn = await c.fetchval("select display_name from account_identities where username = $1", uA)
        check("имя обновлено", ok and dn == "Новое Имя", repr(dn))
        ok2 = await db.set_account_display_name_with_audit(uA, "   ", ip=None, user_agent=None)
        async with db.pool.acquire() as c:
            dn2 = await c.fetchval("select display_name from account_identities where username = $1", uA)
        check("пустое имя → NULL", ok2 and dn2 is None, repr(dn2))
        envret = await db.set_account_display_name_with_audit(config.ADMIN_USERNAME, "X", ip=None, user_agent=None)
        check("env-админ без личности → False", envret is False)

        # ── 3. смена своего пароля ───────────────────────────────────────────
        print("3. смена своего пароля:")
        check("старый пароль ПОДХОДИТ до смены", (await auth.authenticate(uA, PW_OLD)) == (uA, "operator"))
        okp = await db.change_own_password_with_audit(uA, auth.hash_password(PW_NEW), ip=None, user_agent=None)
        check("смена вернула True", okp)
        check("старый пароль больше НЕ подходит", await auth.authenticate(uA, PW_OLD) is None)
        check("новый пароль подходит", (await auth.authenticate(uA, PW_NEW)) == (uA, "operator"))
        check("смена пароля env-админа (нет строки) → False",
              (await db.change_own_password_with_audit(config.ADMIN_USERNAME, auth.hash_password("z"),
                                                       ip=None, user_agent=None)) is False)

        # ── 4. подтверждение текущего пароля (гейт роута) ────────────────────
        print("4. подтверждение текущего пароля:")
        check("authenticate(СВОЙ, неверный) → None", await auth.authenticate(uA, "totally-wrong") is None)
        check("authenticate(СВОЙ, верный) → (actor, role)", (await auth.authenticate(uA, PW_NEW))[0] == uA)

        # ── 5. revoke_all_sessions ───────────────────────────────────────────
        print("5. завершение сеансов:")
        sid1 = await auth.create_session(uA, "operator")
        sid2 = await auth.create_session(uA, "operator")  # create_session ревокает прежние → ревокаем sid1
        # Поднимем sid1 заново живым, чтобы реально иметь 2 живые сессии в одной учётке:
        async with db.pool.acquire() as c:
            await c.execute("update admin_sessions set revoked = false where sid = $1", sid1)
            live_before = await c.fetchval(
                "select count(*) from admin_sessions where actor = $1 and revoked = false", uA)
        check("две живые сессии до", live_before == 2, f"факт {live_before}")
        n = await db.revoke_all_sessions_with_audit(uA, keep_sid=sid2, ip=None, user_agent=None)
        async with db.pool.acquire() as c:
            rev1 = await c.fetchval("select revoked from admin_sessions where sid = $1", sid1)
            rev2 = await c.fetchval("select revoked from admin_sessions where sid = $1", sid2)
        check("keep_sid: текущая сессия жива", rev2 is False)
        check("keep_sid: прочая ревокнута", rev1 is True)
        check("revoke вернул count прочих (1)", n == 1, f"факт {n}")
        await db.revoke_all_sessions_with_audit(uA, keep_sid=None, ip=None, user_agent=None)
        async with db.pool.acquire() as c:
            live_after = await c.fetchval(
                "select count(*) from admin_sessions where actor = $1 and revoked = false", uA)
        check("без keep_sid: все ревокнуты", live_after == 0, f"факт {live_after}")

        # ── 6. изоляция: операции бьют ТОЛЬКО свою учётку ────────────────────
        print("6. изоляция (чужая учётка не затронута):")
        async with db.pool.acquire() as c:
            dnB = await c.fetchval("select display_name from account_identities where username = $1", uB)
        check("чужое имя не изменилось", dnB == "Чужое Имя", repr(dnB))
        check("чужой пароль не изменился", (await auth.authenticate(uB, "other-pass-789")) == (uB, "operator"))

        # ── 6b. team-оператор без личности (находка ревью #1) ────────────────
        print("6b. team-оператор без личности (admin_users без account_identities):")
        uTeam = "smoke-team-op"
        await db.create_admin_user_with_audit(
            uTeam, auth.hash_password("team-pass-123"), "operator",
            actor="smoke", ip=None, user_agent=None)
        TEAM_USERS.append(uTeam)
        check("get_account(team-оп) вернул учётку", bool(await db.get_account(uTeam)))
        check("у team-оп НЕТ личностей", (await db.list_account_identities(uTeam)) == [])
        check("правка имени team-оп → False (имени негде храниться)",
              (await db.set_account_display_name_with_audit(uTeam, "X", ip=None, user_agent=None)) is False)
        check("смена пароля team-оп → True (admin_users)",
              (await db.change_own_password_with_audit(uTeam, auth.hash_password("team-new-456"),
                                                       ip=None, user_agent=None)) is True)

        # ── 7. allow-list схем ссылки поддержки ──────────────────────────────
        print("7. allow-list схем ссылки поддержки:")
        check("https принят", _safe_support_url("https://t.me/support") == "https://t.me/support")
        check("tg принят", _safe_support_url("tg://resolve?domain=support").startswith("tg://"))
        check("mailto принят", _safe_support_url("mailto:help@example.org").startswith("mailto:"))
        check("javascript: отвергнут", _safe_support_url("javascript:alert(1)") == "")
        check("data: отвергнут", _safe_support_url("data:text/html,x") == "")
        check("http (без s) отвергнут", _safe_support_url("http://x") == "")
        check("пусто → пусто", _safe_support_url("") == "")

    finally:
        async with db.pool.acquire() as c:
            await _cleanup(c)
        await db.pool.close()

    print()
    if FAILS:
        print(f"❌ ПРОВАЛЫ ({len(FAILS)}): " + ", ".join(FAILS))
        sys.exit(1)
    print("✅ account smoke — все проверки зелёные")


if __name__ == "__main__":
    asyncio.run(main())
