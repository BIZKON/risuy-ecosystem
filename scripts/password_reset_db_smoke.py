#!/usr/bin/env python3
"""Smoke: жизненный цикл токенов сброса пароля (password_reset_tokens) на risuy_dev.
Throwaway admin_user + account_identity(email); чистка каскадом. Реальные письма НЕ шлём.

Запуск:
  RESET_SMOKE_DSN="postgresql://gen_user:<pw>@HOST:5432/risuy_dev?sslmode=require" \
  PYTHONPATH=admin-panel DATABASE_URL="$RESET_SMOKE_DSN" \
  SESSION_SECRET="smoke-session-secret-min-32-chars-long-xx" ADMIN_USERNAME=smokeadmin \
  ADMIN_PASSWORD_HASH='$argon2id$v=19$m=65536,t=3,p=4$c21va2VzYWx0c2FsdA$c21va2VoYXNoc21va2VoYXNoc21va2VoYXNoMDA' \
  ./.venv-smoke/bin/python scripts/password_reset_db_smoke.py
"""
import asyncio
import hashlib
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "admin-panel"))

DSN = os.environ.get("RESET_SMOKE_DSN") or os.environ.get("DATABASE_URL")
if not DSN or "risuy_dev" not in DSN:
    raise SystemExit("Задайте DSN на risuy_dev (защита от прода).")

import db  # noqa: E402
import auth  # noqa: E402

U = "smoke-reset-user"
EMAIL = "smoke-reset@example.com"


def h(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


async def main() -> None:
    await db.init()
    fails: list[str] = []
    async with db.pool.acquire() as c:
        async def drop() -> None:
            await c.execute("delete from password_reset_tokens where username=$1", U)
            await c.execute("delete from admin_sessions where actor=$1", U)
            await c.execute("delete from account_identities where username=$1", U)
            await c.execute("delete from admin_users where username=$1", U)

        await drop()
        await c.execute(
            "insert into admin_users (username,password_hash,role,active) "
            "values ($1,$2,'operator',true)", U, auth.hash_password("irrelevant-old-pw"))
        await c.execute(
            "insert into account_identities (provider,external_id,username,verified) "
            "values ('email',$1,$2,true)", EMAIL, U)

        try:
            # 1. email → активный username; неизвестный → None
            got = await db.get_active_username_by_email(EMAIL)
            if got != U:
                fails.append(f"get_active_username_by_email: {got!r} != {U!r}")
            if await db.get_active_username_by_email("nope@example.com") is not None:
                fails.append("неизвестный email должен давать None")

            # 2. неактивная учётка → None (анти-enumeration покрывает inactive)
            await c.execute("update admin_users set active=false where username=$1", U)
            if await db.get_active_username_by_email(EMAIL) is not None:
                fails.append("неактивная учётка должна давать None")
            await c.execute("update admin_users set active=true where username=$1", U)

            # 3. создание токена: строка есть, не использована, expires в будущем
            raw1 = "tok-one"
            await db.create_reset_token(U, h(raw1), ttl_min=30, request_ip="1.2.3.4")
            row = await c.fetchrow(
                "select used_at, expires_at>now() as future from password_reset_tokens where token_hash=$1", h(raw1))
            if row is None or row["used_at"] is not None or not row["future"]:
                fails.append(f"создание токена некорректно: {row}")

            # 4. повторный create гасит прежние неиспользованные
            raw2 = "tok-two"
            await db.create_reset_token(U, h(raw2), ttl_min=30, request_ip="1.2.3.4")
            used1 = await c.fetchval("select used_at is not null from password_reset_tokens where token_hash=$1", h(raw1))
            if used1 is not True:
                fails.append("прежний неиспользованный токен должен быть погашен новым запросом")

            # 5. peek валиден → True; consume возвращает username и гасит; повтор → None (one-use)
            if await db.peek_reset_token(h(raw2)) is not True:
                fails.append("peek валидного токена должен быть True")
            if await db.consume_reset_token(h(raw2)) != U:
                fails.append("consume валидного токена должен вернуть username")
            if await db.peek_reset_token(h(raw2)) is not False:
                fails.append("после consume peek должен быть False")
            if await db.consume_reset_token(h(raw2)) is not None:
                fails.append("повторный consume должен дать None (one-use)")

            # 6. истёкший токен: consume → None, peek → False
            raw3 = "tok-expired"
            await c.execute(
                "insert into password_reset_tokens (token_hash,username,expires_at) "
                "values ($1,$2, now()-interval '1 minute')", h(raw3), U)
            if await db.peek_reset_token(h(raw3)) is not False:
                fails.append("истёкший токен: peek должен быть False")
            if await db.consume_reset_token(h(raw3)) is not None:
                fails.append("истёкший токен: consume должен дать None")

            # 7. rate-limit счётчики растут
            by_user, by_ip = await db.recent_reset_counts(U, "1.2.3.4", window_min=15)
            if by_user < 1 or by_ip < 1:
                fails.append(f"recent_reset_counts не считает: user={by_user} ip={by_ip}")

            # 8. отзыв сессий (переиспользуем существующую revoke_all_sessions_with_audit)
            sid = await auth.create_session(U, "operator")
            n = await db.revoke_all_sessions_with_audit(U, keep_sid=None, ip="1.2.3.4", user_agent="smoke")
            revoked = await c.fetchval("select revoked from admin_sessions where sid=$1", sid)
            if n < 1 or revoked is not True:
                fails.append(f"отзыв сессий не сработал: n={n} revoked={revoked}")
        finally:
            await drop()

    if fails:
        print("\n".join("❌ " + f for f in fails))
        raise SystemExit(1)
    print("🟢 password_reset_db_smoke зелёный")


if __name__ == "__main__":
    asyncio.run(main())
