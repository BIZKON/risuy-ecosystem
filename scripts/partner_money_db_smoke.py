#!/usr/bin/env python3
"""DB-смоук начислений (risuy_dev):
  PARTNER_MONEY_SMOKE_DSN="...risuy_dev..." PYTHONPATH=admin-panel:. \
    /Users/konstantin/Downloads/risuy-ecosystem/.venv-smoke/bin/python \
    scripts/partner_money_db_smoke.py
"""
import asyncio
import os
import sys
from decimal import Decimal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "admin-panel"))
os.environ.setdefault("DATABASE_URL", "postgresql://x/y")
os.environ.setdefault("SESSION_SECRET", "smoke-secret-padding-0123456789abcdef")
os.environ.setdefault("ADMIN_USERNAME", "smoke")
os.environ.setdefault("ADMIN_PASSWORD_HASH", "$argon2id$v=19$m=65536,t=3,p=4$c21va2U$c21va2U")

import asyncpg  # noqa: E402

import db  # noqa: E402

DSN = os.environ.get("PARTNER_MONEY_SMOKE_DSN")
if not DSN or "/risuy_dev" not in DSN.split("?")[0]:
    raise SystemExit("Задайте PARTNER_MONEY_SMOKE_DSN на risuy_dev")

FAILS = []
PSELLER = "СМОУК Продавец"
PMENTOR = "СМОУК Наставник"
TNAME = "СМОУК Тенант-плательщик"
PLOGIN = "smoke-partner@example.test"
SRC1 = "11111111-1111-1111-1111-111111111111"
SRC2 = "22222222-2222-2222-2222-222222222222"


def check(name, cond, detail=""):
    print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


async def _cleanup(c):
    await c.execute("""delete from partner_accruals where partner_id in
                       (select id from partners where name in ($1,$2))""", PSELLER, PMENTOR)
    await c.execute("""delete from partner_payouts where partner_id in
                       (select id from partners where name in ($1,$2))""", PSELLER, PMENTOR)
    await c.execute("""delete from implementations where tenant_id in
                       (select id from tenants where name = $1)""", TNAME)
    await c.execute("delete from tenants where name = $1", TNAME)
    await c.execute("update partners set parent_id = null where name in ($1,$2)", PSELLER, PMENTOR)
    await c.execute("""delete from partner_invites where partner_id in
                       (select id from partners where name in ($1,$2))""", PSELLER, PMENTOR)
    await c.execute("delete from admin_users where username = $1", PLOGIN)
    await c.execute("delete from partners where name in ($1,$2)", PSELLER, PMENTOR)


async def main():
    db.pool = await asyncpg.create_pool(DSN, min_size=1, max_size=8)
    try:
        async with db.pool.acquire() as c:
            await _cleanup(c)
            mentor_id = await c.fetchval(
                "insert into partners (name, ref_code) values ($1, $2) returning id",
                PMENTOR, "smokem01")
            seller_id = await c.fetchval(
                "insert into partners (name, ref_code, parent_id) values ($1,$2,$3) returning id",
                PSELLER, "smokes01", mentor_id)
            tenant_id = await c.fetchval(
                "insert into tenants (name, slug, status, partner_id) "
                "values ($1,$2,'active',$3) returning id", TNAME, "smoke-payer", seller_id)

            print("1. Первый платёж начисляет продавцу и наставнику:")
            async with c.transaction():
                ids = await db.accrue_for_payment(
                    c, source_kind="service_invoice", source_id=SRC1,
                    client_kind="tenant", client_id=tenant_id, amount_rub=Decimal("7500"))
            check("создано две строки", len(ids) == 2, f"строк={len(ids)}")
            rows = await c.fetch(
                "select level, amount_rub, reason, rate_percent from partner_accruals "
                "where client_id = $1 order by level", tenant_id)
            check("продавцу 1500.00", rows[0]["amount_rub"] == Decimal("1500.00"),
                  str(rows[0]["amount_rub"]))
            check("наставнику 375.00", rows[1]["amount_rub"] == Decimal("375.00"),
                  str(rows[1]["amount_rub"]))
            check("ставка скопирована в строку", rows[0]["rate_percent"] == Decimal("20.00"),
                  str(rows[0]["rate_percent"]))

            print("2. Повторный вебхук того же платежа не начисляет дважды:")
            async with c.transaction():
                again = await db.accrue_for_payment(
                    c, source_kind="service_invoice", source_id=SRC1,
                    client_kind="tenant", client_id=tenant_id, amount_rub=Decimal("7500"))
            total = await c.fetchval(
                "select count(*) from partner_accruals where client_id = $1", tenant_id)
            check("новых строк нет", not again, f"вернул={again}")
            check("в базе по-прежнему 2", total == 2, f"строк={total}")

            print("3. ВТОРОЙ платёж того же клиента не начисляет ничего (первый платёж):")
            async with c.transaction():
                second = await db.accrue_for_payment(
                    c, source_kind="service_invoice", source_id=SRC2,
                    client_kind="tenant", client_id=tenant_id, amount_rub=Decimal("7500"))
            total = await c.fetchval(
                "select count(*) from partner_accruals where client_id = $1", tenant_id)
            check("вернул пусто", not second, f"вернул={second}")
            check("в базе по-прежнему 2", total == 2, f"строк={total}")

            print("4. Гонка: 20 параллельных начислений дают ровно одну пару строк:")
            await c.execute("delete from partner_accruals where client_id = $1", tenant_id)

            async def one():
                async with db.pool.acquire() as cc:
                    async with cc.transaction():
                        return await db.accrue_for_payment(
                            cc, source_kind="service_invoice", source_id=SRC1,
                            client_kind="tenant", client_id=tenant_id, amount_rub=Decimal("7500"))

            got = await asyncio.gather(*[one() for _ in range(20)], return_exceptions=True)
            errs = [g for g in got if isinstance(g, Exception)]
            total = await c.fetchval(
                "select count(*) from partner_accruals where client_id = $1", tenant_id)
            check("исключений нет", not errs, str(errs[:1]))
            check("ровно 2 строки после 20 попыток", total == 2, f"строк={total}")

            print("5. Отключённый партнёр не получает ничего:")
            await c.execute("delete from partner_accruals where client_id = $1", tenant_id)
            await c.execute("update partners set status='disabled' where id=$1", seller_id)
            async with c.transaction():
                off = await db.accrue_for_payment(
                    c, source_kind="service_invoice", source_id=SRC1,
                    client_kind="tenant", client_id=tenant_id, amount_rub=Decimal("7500"))
            check("вернул пусто", not off, f"вернул={off}")
            await c.execute("update partners set status='active' where id=$1", seller_id)

            print("6. Клиент без партнёра не порождает начислений:")
            await c.execute("update tenants set partner_id = null where id = $1", tenant_id)
            async with c.transaction():
                none_rows = await db.accrue_for_payment(
                    c, source_kind="service_invoice", source_id=SRC1,
                    client_kind="tenant", client_id=tenant_id, amount_rub=Decimal("7500"))
            check("вернул пусто", not none_rows, f"вернул={none_rows}")

            print("7. Возврат сторнирует по всем уровням и обнуляет баланс:")
            await c.execute("delete from partner_accruals where client_id = $1", tenant_id)
            await c.execute("update tenants set partner_id = $1 where id = $2", seller_id, tenant_id)
            async with c.transaction():
                await db.accrue_for_payment(
                    c, source_kind="service_invoice", source_id=SRC1,
                    client_kind="tenant", client_id=tenant_id, amount_rub=Decimal("7500"))
            # 🔴 Ставка меняется МЕЖДУ продажей и возвратом: сторно обязано отражать
            # фактические строки, а не пересчитывать от новой ставки.
            await c.execute("update partners set rate_percent = 35 where id = $1", seller_id)
            async with c.transaction():
                back = await db.storno_for_source(c, source_kind="service_invoice", source_id=SRC1)
            check("сторно по обоим уровням", back == 2, f"строк={back}")
            total = await c.fetchval(
                "select coalesce(sum(amount_rub),0) from partner_accruals where client_id = $1",
                tenant_id)
            check("баланс обнулился, несмотря на смену ставки",
                  total == Decimal("0.00"), str(total))

            print("8. Повторный возврат не уводит партнёра в минус:")
            async with c.transaction():
                twice = await db.storno_for_source(c, source_kind="service_invoice", source_id=SRC1)
            total = await c.fetchval(
                "select coalesce(sum(amount_rub),0) from partner_accruals where client_id = $1",
                tenant_id)
            check("второе сторно ничего не создало", twice == 0, f"строк={twice}")
            check("баланс по-прежнему ноль", total == Decimal("0.00"), str(total))
            await c.execute("update partners set rate_percent = 20 where id = $1", seller_id)

            print("9. Приглашение одноразовое и заводит вход одной транзакцией:")
            token = await db.create_partner_invite(seller_id, actor="smoke")
            res = await db.register_partner_login(token, PLOGIN, "$argon2id$fake$hash")
            check("вход заведён", res == "ok", res)
            who = await db.partner_by_login_actor(PLOGIN)
            check("партнёр находится по логину", who is not None and str(who["id"]) == str(seller_id))
            role = await c.fetchval("select role from admin_users where username = $1", PLOGIN)
            check("роль partner", role == "partner", str(role))

            print("10. Повторное использование того же токена отбивается:")
            again = await db.register_partner_login(token, "other@example.test", "$argon2id$fake$hash")
            check("вернул bad_token", again == "bad_token", again)
            leftover = await c.fetchval(
                "select count(*) from admin_users where username = $1", "other@example.test")
            check("вторая учётка не создана", leftover == 0, f"строк={leftover}")

            print("11. Просроченное приглашение не работает:")
            t2 = await db.create_partner_invite(seller_id, actor="smoke")
            import hashlib as _h
            await c.execute("update partner_invites set expires_at = now() - interval '1 hour' "
                            "where token_hash = $1", _h.sha256(t2.encode()).hexdigest())
            check("вернул bad_token",
                  await db.register_partner_login(t2, "late@example.test", "$argon2id$fake$hash")
                  == "bad_token")

            print("12. Занятый логин НЕ сжигает приглашение:")
            await c.execute("delete from admin_users where username = $1", PLOGIN)
            await c.execute("insert into admin_users (username, password_hash, role) "
                            "values ($1, $2, 'operator')", PLOGIN, "$argon2id$fake$hash")
            t3 = await db.create_partner_invite(seller_id, actor="smoke")
            busy = await db.register_partner_login(t3, PLOGIN, "$argon2id$fake$hash")
            check("вернул exists", busy == "exists", busy)
            used = await c.fetchval("select used_at from partner_invites where token_hash = $1",
                                    _h.sha256(t3.encode()).hexdigest())
            check("приглашение осталось непогашенным", used is None, str(used))

            print("13. Наставника нельзя привязать так, чтобы получился цикл:")
            # seller уже подопечный mentor'а → сделать seller наставником mentor'а нельзя.
            bad = await db.set_partner_parent(mentor_id, seller_id, actor="smoke")
            check("привязка, дающая цикл, отбита", bad is False, f"вернул={bad}")
            still = await c.fetchval("select parent_id from partners where id = $1", mentor_id)
            check("наставник наставника не проставился", still is None, str(still))
            ok2 = await db.set_partner_parent(seller_id, mentor_id, actor="smoke")
            check("нормальная привязка проходит", ok2 is True, f"вернул={ok2}")

            print("14. Выплата уменьшает «к выплате»:")
            await c.execute("delete from partner_accruals where client_id = $1", tenant_id)
            await c.execute("update tenants set partner_id = $1 where id = $2", seller_id, tenant_id)
            async with c.transaction():
                await db.accrue_for_payment(
                    c, source_kind="service_invoice", source_id=SRC1,
                    client_kind="tenant", client_id=tenant_id, amount_rub=Decimal("7500"))
            await db.create_partner_payout(seller_id, Decimal("500.00"), method="СБП",
                                           note=None, actor="smoke")
            t = (await db.partner_totals([seller_id]))[str(seller_id)]
            check("начислено 1500.00", t["accrued"] == Decimal("1500.00"), str(t["accrued"]))
            check("выплачено 500.00", t["paid"] == Decimal("500.00"), str(t["paid"]))
            check("к выплате 1000.00", t["due"] == Decimal("1000.00"), str(t["due"]))

            print("15. Смена ставки не переписывает прошлые начисления:")
            await db.set_partner_rate(seller_id, Decimal("40"), actor="smoke")
            was = await c.fetchval(
                "select rate_percent from partner_accruals "
                "where partner_id = $1 and reason = 'sale'", seller_id)
            check("в начислении осталась прежняя ставка", was == Decimal("20.00"), str(was))
            await db.set_partner_rate(seller_id, Decimal("20"), actor="smoke")

            print("16. ВНЕДРЕНИЕ — единственный источник партнёрских денег:")
            await c.execute("delete from partner_accruals where client_id = $1", tenant_id)
            impl_id = await db.create_implementation(
                tenant_id, "СМОУК Внедрение", Decimal("60000"), note=None, actor="smoke")
            paid = await db.mark_implementation_paid_by_payment(
                "pay-smoke-impl-1", implementation_id=impl_id, expected_amount="60000.00")
            check("внедрение отмечено оплаченным", paid is not None and paid["status"] == "paid")
            rows = await c.fetch(
                "select level, amount_rub, source_kind from partner_accruals "
                "where client_id = $1 order by level", tenant_id)
            check("две строки начисления", len(rows) == 2, f"строк={len(rows)}")
            check("продавцу 12000.00", rows and rows[0]["amount_rub"] == Decimal("12000.00"),
                  str(rows[0]["amount_rub"]) if rows else "нет")
            check("наставнику 3000.00", len(rows) > 1 and rows[1]["amount_rub"] == Decimal("3000.00"),
                  str(rows[1]["amount_rub"]) if len(rows) > 1 else "нет")
            check("источник — внедрение", rows and rows[0]["source_kind"] == "implementation",
                  str(rows[0]["source_kind"]) if rows else "нет")

            print("17. Повторный вебхук того же внедрения ничего не меняет:")
            again = await db.mark_implementation_paid_by_payment(
                "pay-smoke-impl-1", implementation_id=impl_id, expected_amount="60000.00")
            total = await c.fetchval(
                "select count(*) from partner_accruals where client_id = $1", tenant_id)
            check("вернул строку без изменений", again is not None and again["status"] == "paid")
            check("начислений по-прежнему 2", total == 2, f"строк={total}")

            print("18. 🔴 ВТОРОЕ внедрение тому же клиенту НЕ начисляет:")
            impl2 = await db.create_implementation(
                tenant_id, "СМОУК Внедрение 2", Decimal("60000"), note=None, actor="smoke")
            await db.mark_implementation_paid_by_payment(
                "pay-smoke-impl-2", implementation_id=impl2, expected_amount="60000.00")
            total = await c.fetchval(
                "select count(*) from partner_accruals where client_id = $1", tenant_id)
            st2 = await c.fetchval("select status from implementations where id = $1", impl2)
            check("второе внедрение оплачено", st2 == "paid", str(st2))
            check("а начислений по-прежнему 2", total == 2, f"строк={total}")

            print("19. Чужая сумма не помечает внедрение оплаченным:")
            impl3 = await db.create_implementation(
                tenant_id, "СМОУК Внедрение 3", Decimal("60000"), note=None, actor="smoke")
            bad = await db.mark_implementation_paid_by_payment(
                "pay-smoke-impl-3", implementation_id=impl3, expected_amount="100.00")
            st3 = await c.fetchval("select status from implementations where id = $1", impl3)
            check("вернул None", bad is None, str(bad))
            check("статус остался pending", st3 == "pending", str(st3))

            print("20. Возврат внедрения сторнирует долю:")
            ok = await db.set_implementation_status(impl_id, "refunded", actor="smoke")
            check("статус сменён", ok is True)
            summ = await c.fetchval(
                "select coalesce(sum(amount_rub),0) from partner_accruals where client_id = $1",
                tenant_id)
            check("начисленное обнулилось", summ == Decimal("0.00"), str(summ))

            await _cleanup(c)
    finally:
        await db.pool.close()

    print()
    if FAILS:
        print(f"ПРОВАЛЕНО: {len(FAILS)} — {FAILS}")
        sys.exit(1)
    print("ВСЁ ЗЕЛЁНОЕ")


asyncio.run(main())
