#!/usr/bin/env python3
"""Smoke СКВОЗНОЙ: полный путь тенант-воронки через РЕАЛЬНЫЙ funnel.dispatch + БД (risuy_dev).
Замкнутый цикл 152-ФЗ «дал → реестр → выдали → отозвал → реестр»:
  настройка воронки (tenant_settings) → get_funnel_config → согласие (consent_events granted +
  leads.consent) → выдача лид-магнита (deliver_url + status=guide_sent) → повторный ход не
  обрабатывается (dispatch=False) → отзыв /revoke (erase_requested + consent_events revoked +
  бот молчит) → публичные юр-данные тенанта доступны (get_legal_doc_data).

Канал-агностично через FakeChannel (как dispatch в проде, без aiogram/сети). Throwaway-тенант+лид,
чистка по порядку (consent_events → leads → tenant; FK без cascade у leads).

Запуск:
  FUNNEL_SMOKE_DSN="postgresql://gen_user:<pw>@HOST:5432/risuy_dev?sslmode=require" \
  PYTHONPATH=bot-telegram BOT_TOKEN=x DATABASE_URL="$FUNNEL_SMOKE_DSN" CHANNEL_ID=-100 \
  CHANNEL_URL=https://t.me/x GUIDE_URL=https://x ./.venv-smoke/bin/python scripts/funnel_e2e_smoke.py
"""
import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import db       # noqa: E402  (bot-telegram на PYTHONPATH)
import funnel   # noqa: E402

DSN = os.environ.get("FUNNEL_SMOKE_DSN") or os.environ.get("DATABASE_URL")
if not DSN or "risuy_dev" not in DSN:
    raise SystemExit("Задайте DSN на risuy_dev (защита от прода).")

SLUG = "smoke-funnel-e2e"
TG = 990222333
LM_URL = "https://e2e.example.ru/guide.pdf"
SETTINGS = {
    "funnel_enabled": "1",
    "operator_name": "ООО Смоук E2E",
    "operator_inn": "7700000000",
    "operator_email": "smoke@e2e.ru",
    "leadmagnet_kind": "link",
    "leadmagnet_url": LM_URL,
    "leadmagnet_caption": "Ваш материал:",
}


class FakeChannel:
    """Канал-адаптер как в проде: записывает исходящие вызовы воронки (без aiogram/сети)."""
    messenger = "tg"
    uid = TG

    def __init__(self):
        self.calls = []

    async def send_text(self, t): self.calls.append(("text", t))
    async def send_consent(self, t, p): self.calls.append(("consent", t))
    async def ask_phone(self, t): self.calls.append(("ask_phone", t))
    async def ask_gate(self, t, u): self.calls.append(("ask_gate", t))
    async def check_subscription(self, g, uid): return True
    async def deliver_text(self, t): self.calls.append(("deliver_text", t))
    async def deliver_url(self, c, u): self.calls.append(("deliver_url", u))
    async def deliver_file(self, c, p): self.calls.append(("deliver_file", None)); return True
    async def deliver_video_note(self, f): self.calls.append(("video", None))


async def main() -> None:
    await db.init()
    fails: list[str] = []
    async with db.pool.acquire() as c:
        async def drop() -> None:
            await c.execute("delete from consent_events where tenant_id in "
                            "(select id from tenants where slug=$1)", SLUG)
            await c.execute("delete from leads where tenant_id in "
                            "(select id from tenants where slug=$1)", SLUG)
            await c.execute("delete from tenants where slug=$1", SLUG)

        async def lead_dict(tid):
            r = await c.fetchrow(
                "select status, consent, phone, subscribed from leads "
                "where tenant_id=$1 and tg_user_id=$2", tid, TG)
            return dict(r) if r else {}

        await drop()
        tid = await c.fetchval(
            "insert into tenants (slug,name,status) values ($1,'SMOKE e2e','active') returning id", SLUG)
        for k, v in SETTINGS.items():
            await c.execute("insert into tenant_settings (tenant_id,key,value) values ($1,$2,$3)", tid, k, v)
        await c.execute(
            "insert into leads (tenant_id,messenger,source,tg_user_id,status) "
            "values ($1,'tg','other',$2,'new')", tid, TG)
        try:
            cfg = await db.get_funnel_config(tid)
            if not cfg.get("enabled"):
                fails.append("cfg.enabled=False при funnel_enabled=1")
            if not (cfg.get("consent_text") or "").strip():
                fails.append("consent_text пуст (реквизиты заданы — должен собраться)")

            ch = FakeChannel()
            tok = db.current_tenant_id.set(tid)
            try:
                # 1) Нажал «Даю согласие» → согласие в реестр + сразу выдача (нет phone/gate)
                handled = await funnel.dispatch(ch, cfg, await lead_dict(tid), {"consent_pressed": True})
                if handled is not True:
                    fails.append(f"dispatch(consent) вернул {handled}, ожидался True")

                lead = await lead_dict(tid)
                if lead.get("consent") is not True:
                    fails.append(f"leads.consent не true после согласия: {lead.get('consent')}")
                if lead.get("status") != "guide_sent":
                    fails.append(f"status не guide_sent после выдачи: {lead.get('status')}")
                if ("deliver_url", LM_URL) not in ch.calls:
                    fails.append(f"лид-магнит (ссылка) не выдан, calls={ch.calls}")

                granted = await c.fetch(
                    "select action from consent_events where tenant_id=$1 order by occurred_at", tid)
                if [r["action"] for r in granted] != ["granted"]:
                    fails.append(f"реестр после согласия: ожидал ['granted'], получил {[r['action'] for r in granted]}")

                # 2) Лид уже прошёл воронку → следующий ход НЕ обрабатывается воронкой
                again = await funnel.dispatch(ch, cfg, await lead_dict(tid), {"text": "привет"})
                if again is not False:
                    fails.append(f"dispatch после guide_sent вернул {again}, ожидался False")

                # 3) Отзыв согласия (/revoke) → erase + revoked в реестр + бот молчит
                await db.request_erase(TG, channel="tg")
                if not await db.is_erase_requested(TG):
                    fails.append("is_erase_requested=False после отзыва (бот должен молчать)")
            finally:
                db.current_tenant_id.reset(tok)

            row = await c.fetchrow(
                "select erase_requested_at, unsubscribed_at from leads where tenant_id=$1 and tg_user_id=$2", tid, TG)
            if row["erase_requested_at"] is None:
                fails.append("erase_requested_at не проставлен после /revoke")
            if row["unsubscribed_at"] is None:
                fails.append("unsubscribed_at не проставлен после /revoke")

            actions = [r["action"] for r in await c.fetch(
                "select action from consent_events where tenant_id=$1 order by occurred_at", tid)]
            if actions != ["granted", "revoked"]:
                fails.append(f"реестр после отзыва: ожидал ['granted','revoked'], получил {actions}")

            # 4) Публичные юр-данные тенанта доступны (для /legal/{slug}/privacy)
            legal = await db.get_legal_doc_data(SLUG)
            if not legal or SETTINGS["operator_name"] not in (legal.get("operator_name") or ""):
                fails.append(f"get_legal_doc_data не отдал реквизиты оператора: {legal}")
        finally:
            await drop()

    if fails:
        print("\n".join("❌ " + f for f in fails))
        raise SystemExit(1)
    print("🟢 funnel_e2e_smoke зелёный (согласие→реестр→выдача→отзыв→реестр→юр-данные)")


if __name__ == "__main__":
    asyncio.run(main())
