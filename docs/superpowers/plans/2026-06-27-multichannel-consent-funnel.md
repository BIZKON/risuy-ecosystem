# Мультиканальный сбор согласия 152-ФЗ + порт воронки на VK/MAX/Web — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Собирать и фиксировать согласие 152-ФЗ (`consent_events`) на каналах VK, MAX и Web — портировать пер-тенантную воронку лид-магнита с Telegram на все каналы канал-агностично.

**Architecture:** Чистые хелперы `funnel.py` уже канал-агностичны; выносим I/O воронки за тонкий per-channel адаптер (как `triggers.TriggerCtx`/`escalation.escalate(messenger=)`). Машина состояний — DB-state-driven (флаги `leads`), без FSM. db-сеттеры обобщаются на гибрид-идентичность `_user_col(messenger)`.

**Tech Stack:** Python 3 / asyncpg / aiogram (TG) / aiohttp (VK/MAX-драйверы). Тесты — standalone smoke-скрипты в `.venv-smoke` (без pytest/aiogram/aiohttp).

**Спека:** `docs/superpowers/specs/2026-06-27-multichannel-consent-funnel-design.md`

## Global Constraints

- 🇷🇺 Только русский: код-комментарии, docstrings, UI-тексты, коммиты, сообщения смоуков. Латиница — только идентификаторы/SQL/ключи.
- **Telegram-путь байт-в-байт не менять.** Все обобщаемые db-функции получают `messenger="tg"` дефолтом → прежний SQL для TG-вызовов.
- **Школа (`bot-telegram/handlers.py`, env-бот) — не трогать.** Воронка тенанта живёт только в мультиплексе.
- Канальный код ОБЯЗАН передавать `messenger` и явно скоупить tenant (`db.current_tenant_id.set(tid)`; бот=owner, RLS обходит).
- Драйверы импортируют `aiohttp` ЛЕНИВО; новые чистые функции тестируемы в `.venv-smoke` без сети.
- Смоуки: `PYTHONPATH=bot-telegram ./.venv-smoke/bin/python scripts/<name>_smoke.py`; DB-смоуки требуют `FUNNEL_SMOKE_DSN` на `risuy_dev` (гард от прода: `"risuy_dev" in DSN`).
- **Без новых зависимостей** без явного «да» владельца.
- **DDL / прод-деплой (push) / прод-write — за владельцем под явное «да».** План доводит до зелёных смоуков на `risuy_dev`; деплой — отдельно.

## File Structure

| Файл | Действие | Ответственность |
|---|---|---|
| `bot-telegram/db.py` | Modify | Обобщить 8 funnel-сеттеров на `messenger`; `get_funnel_product_bytes`; gate-поля в `get_funnel_config`; не обнулять байты lead_magnet |
| `bot-telegram/funnel.py` | Modify | Чистые хелперы + async-шаги через адаптер; `looks_like_phone`; `dispatch_step` (DB-state→шаг) |
| `bot-telegram/funnel_channels.py` | Create | `FunnelChannel`-протокол + `TgFunnelChannel`/`VkFunnelChannel`/`MaxFunnelChannel` |
| `bot-telegram/multiplex.py` | Modify | Диспетчер воронки в `_vk_respond`/`_max_respond`/`_max_callback`; захват согласия/телефона/отзыва |
| `bot-telegram/vk_driver.py` | Modify | `is_member(group_id, user_id)` (groups.isMember) |
| `bot-telegram/max_driver.py` | Modify | `is_channel_member(chat_id, user_id)` (fail-closed) |
| `bot-telegram/bot.py` | Modify | `_demo_chat`: web-согласие (галочка → `set_consent(channel='web')`, гард) |
| `shared/leadmagnet.py` | Modify | `FUNNEL_FIELDS` += `vk_gate_group_id`/`max_gate_chat_id`; валидация |
| `admin-panel/templates/lead_magnet.html` | Modify | Input-блоки новых полей гейта |
| `service-site/index.html`, `service-site/styles.css` | Modify | Галочка согласия в виджете `#demo-chat` |
| `scripts/*_smoke.py` | Create | Смоуки по задачам |

---

## Task 1: Обобщение funnel-сеттеров на гибрид-идентичность

**Files:**
- Modify: `bot-telegram/db.py` — `set_consent` (L110), `request_erase` (L139), `is_erase_requested` (L161), `set_name` (L170), `set_phone` (L178), `set_subscribed` (L187), `get_lead_status` (L195), `mark_guide_sent` (L205)
- Test: `scripts/db_channel_setters_smoke.py`

**Interfaces:**
- Consumes: `db._user_col(messenger)` (L88), `db.current_tenant_id`, `db.tenant_id()`.
- Produces (новые сигнатуры; `tg`-дефолт сохраняет TG-вызовы):
  - `set_consent(uid, value, *, consent_text=None, doc_version=1, channel="tg", messenger="tg")`
  - `request_erase(uid, *, channel="tg", messenger="tg") -> uuid|None`
  - `is_erase_requested(uid, *, messenger="tg") -> bool`
  - `set_name(uid, name, *, messenger="tg")`, `set_phone(uid, phone, phone_hash, *, messenger="tg")`
  - `set_subscribed(uid, value, *, messenger="tg")`, `get_lead_status(uid, *, messenger="tg") -> str|None`
  - `mark_guide_sent(uid, *, messenger="tg")`

- [ ] **Step 1: Failing smoke** — `scripts/db_channel_setters_smoke.py`

```python
#!/usr/bin/env python3
"""Smoke: funnel-сеттеры канал-агностичны (messenger через _user_col). TG-регрессия (tg_user_id)
+ VK/MAX (vk_user_id/max_user_id). risuy_dev, throwaway-тенант, чистка каскадом.

Запуск:
  FUNNEL_SMOKE_DSN="postgresql://gen_user:<pw>@HOST:5432/risuy_dev?sslmode=require" \
  PYTHONPATH=bot-telegram BOT_TOKEN=x DATABASE_URL="$FUNNEL_SMOKE_DSN" CHANNEL_ID=-100 \
  CHANNEL_URL=https://t.me/x GUIDE_URL=https://x ./.venv-smoke/bin/python scripts/db_channel_setters_smoke.py
"""
import asyncio, os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import db  # noqa: E402

DSN = os.environ.get("FUNNEL_SMOKE_DSN") or os.environ.get("DATABASE_URL")
if not DSN or "risuy_dev" not in DSN:
    raise SystemExit("Задайте DSN на risuy_dev (защита от прода).")
SLUG = "smoke-chan-setters"


async def main() -> None:
    await db.init()
    fails: list[str] = []
    async with db.pool.acquire() as c:
        async def drop():
            await c.execute("delete from consent_events where tenant_id in (select id from tenants where slug=$1)", SLUG)
            await c.execute("delete from leads where tenant_id in (select id from tenants where slug=$1)", SLUG)
            await c.execute("delete from tenants where slug=$1", SLUG)

        async def under(tid, f):
            tok = db.current_tenant_id.set(tid)
            try:
                return await f()
            finally:
                db.current_tenant_id.reset(tok)

        await drop()
        tid = await c.fetchval("insert into tenants (slug,name,status) values ($1,'SMOKE setters','active') returning id", SLUG)
        # VK-лид (идентичность vk_user_id) + MAX-лид (max_user_id)
        await c.execute("insert into leads (tenant_id,messenger,source,vk_user_id,status) values ($1,'vk','vk',$2,'new')", tid, 555001)
        await c.execute("insert into leads (tenant_id,messenger,source,max_user_id,status) values ($1,'max','max',$2,'new')", tid, 555002)
        try:
            await under(tid, lambda: db.set_consent(555001, True, consent_text="VK-СОГЛАСИЕ", channel="vk", messenger="vk"))
            await under(tid, lambda: db.set_phone(555001, "+7 999 000-11-22", "deadbeef", messenger="vk"))
            st = await under(tid, lambda: db.get_lead_status(555001, messenger="vk"))
            if st is None:
                fails.append("get_lead_status(vk) вернул None — не нашёл vk-лида по vk_user_id")
            ev = await c.fetchval("select count(*) from consent_events where tenant_id=$1 and channel='vk' and action='granted'", tid)
            if ev != 1:
                fails.append(f"ожидалась 1 granted(vk), получено {ev}")
            await under(tid, lambda: db.mark_guide_sent(555002, messenger="max"))
            st2 = await c.fetchval("select status from leads where tenant_id=$1 and max_user_id=$2", tid, 555002)
            if st2 != "guide_sent":
                fails.append(f"mark_guide_sent(max) не выставил status (got {st2})")
        finally:
            await drop()
    if fails:
        print("\n".join("❌ " + f for f in fails)); raise SystemExit(1)
    print("🟢 db_channel_setters_smoke зелёный")

if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: Run, verify FAIL**

Run: `FUNNEL_SMOKE_DSN="<risuy_dev owner DSN>" PYTHONPATH=bot-telegram BOT_TOKEN=x DATABASE_URL="$FUNNEL_SMOKE_DSN" CHANNEL_ID=-100 CHANNEL_URL=https://t.me/x GUIDE_URL=https://x ./.venv-smoke/bin/python scripts/db_channel_setters_smoke.py`
Expected: FAIL — `set_consent() got an unexpected keyword argument 'messenger'`.

- [ ] **Step 3: Обобщить сеттеры** — в `bot-telegram/db.py` заменить `where tg_user_id = $1` на `where {col} = $1`, добавить `messenger="tg"` и `col = _user_col(messenger)`. Полные новые тела:

```python
async def set_consent(tg_user_id: int, value: bool, *, consent_text: str | None = None,
                      doc_version: int = 1, channel: str = "tg", messenger: str = "tg") -> None:
    text_hash = (hashlib.sha256(consent_text.encode("utf-8")).hexdigest() if consent_text else None)
    col = _user_col(messenger)
    async with pool.acquire() as c:
        async with c.transaction():
            lead_id = await c.fetchval(
                f"update leads set consent = $2, "
                f"  erase_requested_at = case when $2 then null else erase_requested_at end, "
                f"  unsubscribed_at    = case when $2 then null else unsubscribed_at end "
                f"where {col} = $1 and tenant_id = $3 returning id",
                tg_user_id, value, tenant_id())
            if value and lead_id is not None:
                await c.execute(
                    "insert into consent_events (tenant_id, lead_id, doc_type, doc_version, text_hash, action, channel) "
                    "values ($1, $2, 'consent', $3, $4, 'granted', $5)",
                    tenant_id(), lead_id, doc_version, text_hash, channel)


async def request_erase(tg_user_id: int, *, channel: str = "tg", messenger: str = "tg"):
    col = _user_col(messenger)
    async with pool.acquire() as c:
        async with c.transaction():
            lead_id = await c.fetchval(
                f"update leads set erase_requested_at = coalesce(erase_requested_at, now()), "
                f"                 unsubscribed_at    = coalesce(unsubscribed_at, now()) "
                f"where {col} = $1 and tenant_id = $2 returning id",
                tg_user_id, tenant_id())
            if lead_id is not None:
                await c.execute(
                    "insert into consent_events (tenant_id, lead_id, doc_type, action, channel) "
                    "values ($1, $2, 'consent', 'revoked', $3)",
                    tenant_id(), lead_id, channel)
    return lead_id


async def is_erase_requested(tg_user_id: int, *, messenger: str = "tg") -> bool:
    col = _user_col(messenger)
    async with pool.acquire() as c:
        return bool(await c.fetchval(
            f"select erase_requested_at is not null from leads where {col} = $1 and tenant_id = $2",
            tg_user_id, tenant_id()))


async def set_name(tg_user_id: int, name: str, *, messenger: str = "tg") -> None:
    col = _user_col(messenger)
    async with pool.acquire() as c:
        await c.execute(f"update leads set name = $2 where {col} = $1 and tenant_id = $3",
                        tg_user_id, name, tenant_id())


async def set_phone(tg_user_id: int, phone: str, phone_hash: str, *, messenger: str = "tg") -> None:
    col = _user_col(messenger)
    async with pool.acquire() as c:
        await c.execute(
            f"update leads set phone = $2, phone_hash = $3 where {col} = $1 and tenant_id = $4",
            tg_user_id, phone, phone_hash, tenant_id())


async def set_subscribed(tg_user_id: int, value: bool, *, messenger: str = "tg") -> None:
    col = _user_col(messenger)
    async with pool.acquire() as c:
        await c.execute(f"update leads set subscribed = $2 where {col} = $1 and tenant_id = $3",
                        tg_user_id, value, tenant_id())


async def get_lead_status(tg_user_id: int, *, messenger: str = "tg") -> str | None:
    col = _user_col(messenger)
    async with pool.acquire() as c:
        return await c.fetchval(
            f"select status from leads where {col} = $1 and tenant_id = $2", tg_user_id, tenant_id())


async def mark_guide_sent(tg_user_id: int, *, messenger: str = "tg") -> None:
    col = _user_col(messenger)
    async with pool.acquire() as c:
        await c.execute(
            f"update leads set status = 'guide_sent', guide_sent_at = coalesce(guide_sent_at, now()) "
            f"where {col} = $1 and tenant_id = $2", tg_user_id, tenant_id())
```

⚠️ Безопасность: `col` берётся ТОЛЬКО из `_user_col` (whitelist колонок), не из пользовательского ввода — SQL-инъекции нет. Параметры значений — по-прежнему через `$N`.

- [ ] **Step 4: Run, verify PASS** — та же команда. Expected: `🟢 db_channel_setters_smoke зелёный`.
- [ ] **Step 5: TG-регрессия** — прогнать существующие `consent_revoke_smoke.py`, `funnel_flow_smoke.py`. Expected: оба зелёные (TG-вызовы без `messenger` работают как раньше).
- [ ] **Step 6: Commit**

```bash
git add bot-telegram/db.py scripts/db_channel_setters_smoke.py
git commit -m "feat(funnel): обобщить funnel-сеттеры на messenger (_user_col) для VK/MAX/web

TG-вызовы байт-в-байт (messenger='tg' дефолт). set_consent/request_erase/
is_erase_requested/set_name/set_phone/set_subscribed/get_lead_status/mark_guide_sent.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: Адаптер воронки + TG-адаптер (рефактор без смены поведения)

**Files:**
- Create: `bot-telegram/funnel_channels.py`
- Modify: `bot-telegram/funnel.py` — `start`/`after_consent`/`after_phone`/`go_to_gate`/`deliver` принимают `ch: FunnelChannel` вместо `bot`/`message`
- Modify: `bot-telegram/multiplex.py` — `t_start`/`t_consent`/`t_contact`/`t_check_sub` строят `TgFunnelChannel` и зовут шаги через него
- Test: `scripts/funnel_adapter_smoke.py`

**Interfaces:**
- Produces:
  - `FunnelChannel` (protocol): `send_text(text)`, `send_consent(text, privacy_url)`, `ask_phone(text)`, `ask_gate(text, channel_url)`, `check_subscription(gate_cfg, uid) -> bool`, `deliver_text(text)`, `deliver_url(caption, url)`, `deliver_file(caption, product) -> bool`, `deliver_video_note(file_id)`; атрибут `uid`, `messenger`.
  - `funnel.start(ch, cfg)`, `funnel.after_consent(ch, cfg)`, `funnel.after_phone(ch, cfg)`, `funnel.go_to_gate(ch, cfg)`, `funnel.deliver(ch, cfg)`.
  - `funnel_channels.TgFunnelChannel(bot, uid)`.
- Consumes: `messaging.send_text/send_video_note/send_by_kind/kind_for_mime`, `funnel._consent_kb/_phone_kb/_gate_kb/_guide_kb`, `db.set_subscribed/mark_guide_sent/get_funnel_product`.

- [ ] **Step 1: Failing smoke** — `scripts/funnel_adapter_smoke.py` (чистый, без БД/сети)

```python
#!/usr/bin/env python3
"""Smoke: funnel-шаги диспетчатся через адаптер (без aiogram/сети). FakeChannel записывает вызовы;
проверяем маршруты after_consent: phone-step → ask_phone; gate → check_subscription; иначе → deliver.

Запуск: PYTHONPATH=bot-telegram ./.venv-smoke/bin/python scripts/funnel_adapter_smoke.py
"""
import asyncio, os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import funnel  # noqa: E402


class FakeChannel:
    messenger = "vk"
    uid = 1
    def __init__(self, subscribed=True):
        self.calls = []
        self._sub = subscribed
    async def send_text(self, t): self.calls.append(("text", t))
    async def send_consent(self, t, p): self.calls.append(("consent", t))
    async def ask_phone(self, t): self.calls.append(("ask_phone", t))
    async def ask_gate(self, t, u): self.calls.append(("ask_gate", t))
    async def check_subscription(self, g, uid): self.calls.append(("check_sub", uid)); return self._sub
    async def deliver_text(self, t): self.calls.append(("deliver_text", t))
    async def deliver_url(self, c, u): self.calls.append(("deliver_url", u))
    async def deliver_file(self, c, p): self.calls.append(("deliver_file", None)); return True
    async def deliver_video_note(self, f): self.calls.append(("video", f))


async def main():
    fails = []
    # phone-step включён → after_consent зовёт ask_phone
    ch = FakeChannel()
    await funnel.after_consent(ch, {"phone_step": True, "gate": {"enabled": False}})
    if ("ask_phone", funnel.ASK_PHONE) not in ch.calls:
        fails.append(f"after_consent(phone) не позвал ask_phone: {ch.calls}")
    # без телефона, gate выкл → deliver (лид-магнит не настроен → deliver_text NOT_CONFIGURED)
    ch = FakeChannel()
    await funnel.after_consent(ch, {"phone_step": False, "gate": {"enabled": False}, "leadmagnet": {}})
    if not any(c[0] == "deliver_text" for c in ch.calls):
        fails.append(f"after_consent(deliver) не дошёл до выдачи: {ch.calls}")
    if fails:
        print("\n".join("❌ " + f for f in fails)); raise SystemExit(1)
    print("🟢 funnel_adapter_smoke зелёный")

asyncio.run(main())
```

- [ ] **Step 2: Run, verify FAIL** — `PYTHONPATH=bot-telegram ./.venv-smoke/bin/python scripts/funnel_adapter_smoke.py` → FAIL (`after_consent` ещё принимает `bot, user_id`).

- [ ] **Step 3: Создать `bot-telegram/funnel_channels.py`** — TG-адаптер (обёртка над текущим I/O, поведение TG сохраняется):

```python
"""Канальные адаптеры воронки лид-магнита (I/O за единым интерфейсом). Машина состояний и тексты
живут в funnel.py и от канала не зависят. TG-адаптер обёртывает текущий messaging.* + aiogram-
клавиатуры (поведение Telegram сохраняется); VK/MAX — драйверы (Задачи 4/5). aiogram/messaging —
ленивый импорт (адаптер VK/MAX тестируем без них)."""
import logging
import db
import funnel

logger = logging.getLogger(__name__)


class TgFunnelChannel:
    """Адаптер Telegram: bot + user_id. Зеркалит прежний прямой код multiplex/funnel."""
    messenger = "tg"

    def __init__(self, bot, uid: int):
        self.bot = bot
        self.uid = uid

    async def send_text(self, text: str) -> None:
        import messaging
        await messaging.send_text(self.bot, self.uid, text, source="funnel")

    async def send_consent(self, text: str, privacy_url: str | None) -> None:
        import messaging
        await messaging.send_text(self.bot, self.uid, text, source="funnel",
                                  reply_markup=funnel._consent_kb({"privacy_url": privacy_url}))

    async def ask_phone(self, text: str) -> None:
        import messaging
        await messaging.send_text(self.bot, self.uid, text, source="funnel", reply_markup=funnel._phone_kb())

    async def ask_gate(self, text: str, channel_url: str | None) -> None:
        import messaging
        await messaging.send_text(self.bot, self.uid, text, source="funnel",
                                  reply_markup=funnel._gate_kb({"gate": {"channel_url": channel_url}}))

    async def check_subscription(self, gate_cfg: dict, uid: int) -> bool:
        return await funnel.is_subscribed(self.bot, (gate_cfg or {}).get("channel_id"), uid)

    async def deliver_text(self, text: str) -> None:
        import messaging
        await messaging.send_text(self.bot, self.uid, text, source="funnel")

    async def deliver_url(self, caption: str, url: str) -> None:
        import messaging
        await messaging.send_text(self.bot, self.uid, f"{caption}\n\n{url}", source="funnel",
                                  reply_markup=funnel._guide_kb(url))

    async def deliver_file(self, caption: str, product: dict) -> bool:
        """TG-выдача файла по file_tg_id. Возвращает True при успехе (для пометки guide_sent)."""
        import messaging
        if product.get("file_tg_id"):
            kind = messaging.kind_for_mime(product.get("file_mime"))
            await messaging.send_by_kind(self.bot, self.uid, kind, file_id=product["file_tg_id"],
                                         caption=caption, source="funnel")
            return True
        if product.get("link"):
            await self.deliver_url(caption, product["link"])
            return True
        return False

    async def deliver_video_note(self, file_id: str) -> None:
        import messaging
        await messaging.send_video_note(self.bot, self.uid, file_id, source="funnel")
```

- [ ] **Step 4: Рефактор `funnel.py` async-шагов на адаптер.** Заменить тела `start/after_consent/after_phone/go_to_gate/deliver` (L121-238) на версии через `ch`. Полные новые тела:

```python
async def start(ch, cfg: dict) -> None:
    """Приветствие + согласие (вызывается из диспетчера канала при enabled)."""
    privacy = (cfg.get("privacy_url") or cfg.get("legal_privacy_url") or "")
    await ch.send_consent(start_text(cfg), privacy or None)


async def after_consent(ch, cfg: dict) -> None:
    step = next_after_consent(cfg)
    if step == "phone":
        await ch.ask_phone(ASK_PHONE)
    elif step == "gate":
        await go_to_gate(ch, cfg)
    else:
        await deliver(ch, cfg)


async def after_phone(ch, cfg: dict) -> None:
    await ch.send_text(PHONE_OK)
    if next_after_phone(cfg) == "gate":
        await go_to_gate(ch, cfg)
    else:
        await deliver(ch, cfg)


async def go_to_gate(ch, cfg: dict) -> None:
    gate = cfg.get("gate") or {}
    if await ch.check_subscription(gate, ch.uid):
        await db.set_subscribed(ch.uid, True, messenger=ch.messenger)
        await deliver(ch, cfg)
    else:
        await ch.ask_gate(ASK_SUBSCRIBE, gate.get("channel_url"))


async def deliver(ch, cfg: dict) -> None:
    plan = deliver_plan(cfg)
    if plan["has_video"]:
        try:
            await ch.deliver_video_note((cfg.get("video_note_file_id") or "").strip())
        except Exception as e:  # noqa: BLE001 — видео не критично
            logger.warning("funnel: видео-кружок не отправлен: %s", e)
    if not plan["configured"]:
        await ch.deliver_text(NOT_CONFIGURED)
        return
    if plan["kind"] == "file":
        prod = None
        if plan["product_id"]:
            try:
                prod = await db.get_funnel_product(int(plan["product_id"]))
            except (TypeError, ValueError):
                prod = None
        if prod is not None:
            if prod.get("file_tg_id") or prod.get("link"):
                if await ch.deliver_file(plan["caption"], prod):
                    await db.mark_guide_sent(ch.uid, messenger=ch.messenger)
                    return
            else:
                await ch.deliver_text(FILE_PREPARING)
                return
        if plan["file_id"]:
            try:
                if await ch.deliver_file(plan["caption"], {"file_tg_id": plan["file_id"], "file_mime": None}):
                    await db.mark_guide_sent(ch.uid, messenger=ch.messenger)
                    return
            except Exception as e:  # noqa: BLE001
                logger.warning("funnel: файл-лид-магнит (file_id) не выдан (%s) — фолбэк", e)
    if plan["url"]:
        await ch.deliver_url(plan["caption"], plan["url"])
    else:
        await ch.deliver_text(plan["caption"])
    await db.mark_guide_sent(ch.uid, messenger=ch.messenger)
```

(Чистые хелперы `start_text/next_after_*/deliver_plan/phone_hash/_consent_kb/_phone_kb/_gate_kb/_guide_kb/is_subscribed` — без изменений.)

- [ ] **Step 5: Перевести TG-обработчики мультиплекса на адаптер.** В `bot-telegram/multiplex.py`: `t_start` → `await funnel.start(funnel_channels.TgFunnelChannel(message.bot, message.from_user.id), cfg)`; `t_consent` → после `set_consent(...)`: `await funnel.after_consent(funnel_channels.TgFunnelChannel(cb.bot, cb.from_user.id), cfg)`; `t_contact` → `after_phone(TgFunnelChannel(message.bot, message.from_user.id), cfg)`; `t_check_sub` → `funnel.deliver(TgFunnelChannel(cb.bot, cb.from_user.id), cfg)`. Добавить `import funnel_channels`.

- [ ] **Step 6: Run, verify PASS** — `funnel_adapter_smoke.py` зелёный.
- [ ] **Step 7: TG-регрессия** — `funnel_flow_smoke.py` зелёный (если он гоняет шаги — адаптировать его вызовы под адаптер; иначе оставить).
- [ ] **Step 8: Commit**

```bash
git add bot-telegram/funnel_channels.py bot-telegram/funnel.py bot-telegram/multiplex.py scripts/funnel_adapter_smoke.py
git commit -m "feat(funnel): канал-адаптер I/O + TgFunnelChannel (рефактор TG без смены поведения)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: Телефон-текстом + диспетчер шага по DB-state

**Files:**
- Modify: `bot-telegram/funnel.py` — `looks_like_phone(text) -> bool`, `requisites_filled(cfg) -> bool`, `dispatch(ch, cfg, lead, incoming) -> bool`
- Test: `scripts/funnel_dispatch_smoke.py`

**Interfaces:**
- Produces:
  - `funnel.looks_like_phone(text: str) -> bool` (≥10 цифр)
  - `funnel.requisites_filled(cfg: dict) -> bool` (есть `consent_text`)
  - `funnel.dispatch(ch, cfg, lead: dict, incoming: dict) -> bool` — гоняет шаг по состоянию лида; `incoming={"text":..,"consent_pressed":bool}`. Возвращает `True` если воронка обработала ход (Лию не звать), `False` если лид прошёл воронку (`status='guide_sent'`).
- Consumes: `funnel.start/after_consent/after_phone`, `db.set_consent/set_phone/set_name/phone_hash`.

- [ ] **Step 1: Failing smoke** — `scripts/funnel_dispatch_smoke.py` (чистый)

```python
#!/usr/bin/env python3
"""Smoke: диспетчер шага воронки по DB-state (канал-агностично). FakeChannel + fake-lead dict.
Запуск: PYTHONPATH=bot-telegram ./.venv-smoke/bin/python scripts/funnel_dispatch_smoke.py"""
import asyncio, os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import funnel  # noqa: E402

class FakeChannel:
    messenger = "vk"; uid = 7
    def __init__(self): self.calls = []
    async def send_text(self, t): self.calls.append(("text", t))
    async def send_consent(self, t, p): self.calls.append(("consent", t))
    async def ask_phone(self, t): self.calls.append(("ask_phone", t))
    async def ask_gate(self, t, u): self.calls.append(("ask_gate", t))
    async def check_subscription(self, g, uid): return True
    async def deliver_text(self, t): self.calls.append(("deliver_text", t))
    async def deliver_url(self, c, u): self.calls.append(("deliver_url", u))
    async def deliver_file(self, c, p): return True
    async def deliver_video_note(self, f): pass

async def main():
    fails = []
    if not funnel.looks_like_phone("+7 (999) 000-11-22"): fails.append("looks_like_phone отверг валидный")
    if funnel.looks_like_phone("привет"): fails.append("looks_like_phone принял мусор")
    if funnel.requisites_filled({"consent_text": ""}): fails.append("requisites_filled true без consent_text")
    if fails:
        print("\n".join("❌ " + f for f in fails)); raise SystemExit(1)
    print("🟢 funnel_dispatch_smoke зелёный (хелперы)")

asyncio.run(main())
```

- [ ] **Step 2: Run, verify FAIL** → `AttributeError: looks_like_phone`.
- [ ] **Step 3: Реализовать в `funnel.py`:**

```python
def looks_like_phone(text: str) -> bool:
    """Эвристика «это номер телефона» для каналов без request_contact (VK/MAX): ≥10 цифр."""
    return sum(ch.isdigit() for ch in (text or "")) >= 10


def requisites_filled(cfg: dict) -> bool:
    """Есть ли из чего собрать согласие (реквизиты оператора заполнены → consent_text непустой)."""
    return bool((cfg.get("consent_text") or "").strip())


REVOKE_WORDS = ("отозвать согласие", "отзываю согласие", "/revoke")


def is_revoke(text: str) -> bool:
    t = (text or "").strip().lower()
    return any(w in t for w in REVOKE_WORDS)


async def dispatch(ch, cfg: dict, lead: dict, incoming: dict) -> bool:
    """Гоняет шаг воронки по состоянию лида. Возвращает True, если воронка обработала ход
    (Лию/продажи на этот ход НЕ зовём), False — если лид уже прошёл воронку (status='guide_sent')."""
    if (lead.get("status") or "") == "guide_sent":
        return False
    consent = bool(lead.get("consent"))
    text = (incoming.get("text") or "").strip()
    # Шаг 1: согласие
    if not consent:
        if incoming.get("consent_pressed"):
            await db.set_consent(ch.uid, True, consent_text=cfg.get("consent_text") or None,
                                 channel=ch.messenger, messenger=ch.messenger)
            await after_consent(ch, cfg)
        else:
            await start(ch, cfg)   # (повторно) показать приветствие+согласие
        return True
    # Шаг 2: телефон (текстом, если phone_step и ещё нет номера)
    if cfg.get("phone_step") and not lead.get("phone"):
        if text and looks_like_phone(text):
            await db.set_phone(ch.uid, text, phone_hash(text), messenger=ch.messenger)
            await after_phone(ch, cfg)
        else:
            await ch.ask_phone(ASK_PHONE)
        return True
    # Шаг 3: гейт / выдача
    if (cfg.get("gate") or {}).get("enabled") and not lead.get("subscribed"):
        await go_to_gate(ch, cfg)
    else:
        await deliver(ch, cfg)
    return True
```

- [ ] **Step 4: Run, verify PASS** → `🟢 funnel_dispatch_smoke зелёный (хелперы)`.
- [ ] **Step 5: Commit**

```bash
git add bot-telegram/funnel.py scripts/funnel_dispatch_smoke.py
git commit -m "feat(funnel): диспетчер шага по DB-state + телефон-текстом + детект отзыва

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: Интеграция воронки в VK

**Files:**
- Modify: `bot-telegram/funnel_channels.py` — `VkFunnelChannel`
- Modify: `bot-telegram/multiplex.py` — `_vk_respond`: диспетчер до продаж/Лии; payload-согласие; отзыв
- Modify: `bot-telegram/vk_driver.py` — кнопка согласия (используем `send_keyboard` с payload `{"cmd":"consent_yes"}`)
- Test: `scripts/vk_funnel_smoke.py`

**Interfaces:**
- Produces: `funnel_channels.VkFunnelChannel(vkbot, peer_id, uid)`; consent-payload контракт `{"cmd":"consent_yes"}`.
- Consumes: `vk_driver.VKBot.send/send_keyboard/send_link/send_document/is_member`, `funnel.dispatch`, `db.get_funnel_config/get_lead_snapshot`.

- [ ] **Step 1: `get_lead_snapshot` в db.py** (состояние лида для диспетчера) — добавить:

```python
async def get_lead_snapshot(uid: int, *, messenger: str = "tg") -> dict | None:
    """Состояние лида для диспетчера воронки (consent/phone/subscribed/status). None — лида нет."""
    col = _user_col(messenger)
    async with pool.acquire() as c:
        row = await c.fetchrow(
            f"select consent, phone, subscribed, status from leads where {col} = $1 and tenant_id = $2",
            uid, tenant_id())
    return dict(row) if row else None
```

- [ ] **Step 2: `VkFunnelChannel` в funnel_channels.py:**

```python
class VkFunnelChannel:
    """Адаптер VK: vkbot + peer_id (адрес ответа) + uid (=from_id, идентичность vk_user_id)."""
    messenger = "vk"

    def __init__(self, vkbot, peer_id: int, uid: int):
        self.bot = vkbot
        self.peer_id = peer_id
        self.uid = uid

    async def send_text(self, text: str) -> None:
        await self.bot.send(self.peer_id, text)

    async def send_consent(self, text: str, privacy_url: str | None) -> None:
        await self.bot.send_keyboard(self.peer_id, text, [{"label": funnel.CONSENT_BTN, "payload": {"cmd": "consent_yes"}}])
        if privacy_url:
            await self.bot.send_link(self.peer_id, funnel.PRIVACY_BTN, privacy_url, funnel.PRIVACY_BTN)

    async def ask_phone(self, text: str) -> None:
        await self.bot.send(self.peer_id, text + "\n\nНапишите номер телефона сообщением 🙂")

    async def ask_gate(self, text: str, channel_url: str | None) -> None:
        msg = text + (f"\n\n{channel_url}" if channel_url else "")
        await self.bot.send(self.peer_id, msg + "\n\nПодпишитесь и напишите «я подписался».")

    async def check_subscription(self, gate_cfg: dict, uid: int) -> bool:
        gid = (gate_cfg or {}).get("vk_gate_group_id")
        if not gid:
            return False  # fail-closed: гейт держит, пока VK-сообщество не настроено
        return await self.bot.is_member(int(gid), uid)

    async def deliver_text(self, text: str) -> None:
        await self.bot.send(self.peer_id, text)

    async def deliver_url(self, caption: str, url: str) -> None:
        await self.bot.send(self.peer_id, f"{caption}\n\n{url}")

    async def deliver_file(self, caption: str, product: dict) -> bool:
        if product.get("file_bytes"):
            return await self.bot.send_document(self.peer_id, product["file_bytes"],
                                                filename=product.get("file_name") or "file", caption=caption)
        if product.get("link"):
            await self.deliver_url(caption, product["link"]); return True
        return False

    async def deliver_video_note(self, file_id: str) -> None:
        return  # видео-кружок — TG-only, на VK пропуск
```

- [ ] **Step 3: Диспетчер в `_vk_respond`.** В `bot-telegram/multiplex.py` после `upsert_start`/unsub, ДО `selling`:

```python
        # 152-ФЗ: воронка/согласие до продаж и Лии (полный порт). Отзыв — в любой момент.
        if funnel.is_revoke(text):
            await db.request_erase(from_id, channel="vk", messenger="vk")
            await vkbot.send(peer_id, texts.REVOKE_OK)
            return
        if await db.is_erase_requested(from_id, messenger="vk"):
            return  # субъект отозвал согласие → молчим
        fcfg = await db.get_funnel_config(tenant_id)
        if fcfg["enabled"] and funnel.requisites_filled(fcfg):
            lead = await db.get_lead_snapshot(from_id, messenger="vk") or {}
            if (lead.get("status") or "") != "guide_sent":
                ch = funnel_channels.VkFunnelChannel(vkbot, peer_id, from_id)
                consent_pressed = bool(payload and payload.get("cmd") == "consent_yes")
                await funnel.dispatch(ch, fcfg, lead, {"text": text, "consent_pressed": consent_pressed})
                return
```

(добавить `import funnel`, `import funnel_channels` вверху multiplex.py, если их там ещё нет; `texts.REVOKE_OK` уже есть — Задача s26.)

- [ ] **Step 4: Smoke** `scripts/vk_funnel_smoke.py` — FakeVKBot записывает send/send_keyboard; гоняем `funnel.dispatch` через `VkFunnelChannel` на fake-lead (нет согласия → send_keyboard с payload consent_yes; consent_pressed=True → ask_phone). Структура как `funnel_adapter_smoke.py`, но через `VkFunnelChannel(FakeVKBot(), 10, 10)`.

```python
#!/usr/bin/env python3
"""Smoke: VkFunnelChannel + dispatch (без сети). Запуск: PYTHONPATH=bot-telegram ./.venv-smoke/bin/python scripts/vk_funnel_smoke.py"""
import asyncio, os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import funnel, funnel_channels  # noqa: E402

class FakeVKBot:
    def __init__(self): self.calls = []
    async def send(self, peer, text, **kw): self.calls.append(("send", text))
    async def send_keyboard(self, peer, text, btns): self.calls.append(("kb", btns[0]["payload"]))
    async def send_link(self, peer, text, url, label): self.calls.append(("link", url))
    async def send_document(self, peer, b, **kw): self.calls.append(("doc", None)); return True
    async def is_member(self, gid, uid): return True

async def main():
    fails = []
    # монкей-патчим db-писатели (без БД): set_consent/set_phone no-op
    import db
    async def _noop(*a, **k): return None
    db.set_consent = _noop; db.set_phone = _noop
    cfg = {"enabled": True, "consent_text": "СОГЛАСИЕ VK", "phone_step": True,
           "gate": {"enabled": False}, "leadmagnet": {}, "privacy_url": None, "legal_privacy_url": None}
    bot = FakeVKBot()
    ch = funnel_channels.VkFunnelChannel(bot, 10, 10)
    # нет согласия, кнопка не нажата → показать согласие (send_keyboard payload consent_yes)
    await funnel.dispatch(ch, cfg, {"consent": False, "status": "new"}, {"text": "привет", "consent_pressed": False})
    if not any(c[0] == "kb" and c[1] == {"cmd": "consent_yes"} for c in bot.calls):
        fails.append(f"не показал кнопку согласия VK: {bot.calls}")
    # согласие нажато → ask_phone
    bot2 = FakeVKBot(); ch2 = funnel_channels.VkFunnelChannel(bot2, 10, 10)
    await funnel.dispatch(ch2, cfg, {"consent": False, "status": "new"}, {"text": "", "consent_pressed": True})
    if not any("Напишите номер" in (c[1] or "") for c in bot2.calls if c[0] == "send"):
        fails.append(f"после согласия VK не спросил телефон: {bot2.calls}")
    if fails:
        print("\n".join("❌ " + f for f in fails)); raise SystemExit(1)
    print("🟢 vk_funnel_smoke зелёный")

asyncio.run(main())
```

- [ ] **Step 5: Run FAIL → реализовать → PASS** (Steps 2-3 дают код; запустить smoke).
- [ ] **Step 6: Commit**

```bash
git add bot-telegram/funnel_channels.py bot-telegram/multiplex.py bot-telegram/db.py scripts/vk_funnel_smoke.py
git commit -m "feat(funnel): порт воронки на VK — согласие/телефон/выдача + отзыв (channel='vk')

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: Интеграция воронки в MAX

**Files:**
- Modify: `bot-telegram/funnel_channels.py` — `MaxFunnelChannel`
- Modify: `bot-telegram/multiplex.py` — `_max_respond` (диспетчер/телефон/отзыв) + `_max_callback` (consent_yes)
- Test: `scripts/max_funnel_smoke.py`

**Interfaces:**
- Produces: `funnel_channels.MaxFunnelChannel(maxbot, chat_id, uid)`.
- Consumes: `max_driver.MAXBot.send/send_keyboard/send_link/send_media/answer_callback/is_channel_member`.

- [ ] **Step 1: `MaxFunnelChannel`** (зеркало VK; ответ на `chat_id`, идентичность `user_id`):

```python
class MaxFunnelChannel:
    """Адаптер MAX: maxbot + chat_id (адрес ответа, ≠ uid в личке) + uid (=user_id, max_user_id)."""
    messenger = "max"

    def __init__(self, maxbot, chat_id: int, uid: int):
        self.bot = maxbot
        self.chat_id = chat_id
        self.uid = uid

    async def send_text(self, text: str) -> None:
        await self.bot.send(self.chat_id, text)

    async def send_consent(self, text: str, privacy_url: str | None) -> None:
        await self.bot.send_keyboard(self.chat_id, text, [{"label": funnel.CONSENT_BTN, "payload": {"cmd": "consent_yes"}}])
        if privacy_url:
            await self.bot.send_link(self.chat_id, funnel.PRIVACY_BTN, privacy_url, funnel.PRIVACY_BTN)

    async def ask_phone(self, text: str) -> None:
        await self.bot.send(self.chat_id, text + "\n\nНапишите номер телефона сообщением 🙂")

    async def ask_gate(self, text: str, channel_url: str | None) -> None:
        msg = text + (f"\n\n{channel_url}" if channel_url else "")
        await self.bot.send(self.chat_id, msg + "\n\nПодпишитесь и напишите «я подписался».")

    async def check_subscription(self, gate_cfg: dict, uid: int) -> bool:
        cid = (gate_cfg or {}).get("max_gate_chat_id")
        if not cid:
            return False
        return await self.bot.is_channel_member(int(cid), uid)

    async def deliver_text(self, text: str) -> None:
        await self.bot.send(self.chat_id, text)

    async def deliver_url(self, caption: str, url: str) -> None:
        await self.bot.send(self.chat_id, f"{caption}\n\n{url}")

    async def deliver_file(self, caption: str, product: dict) -> bool:
        if product.get("file_bytes"):
            mt = "image" if (product.get("file_mime") or "").startswith("image/") else "file"
            return await self.bot.send_media(self.chat_id, media_type=mt, content=product["file_bytes"],
                                             caption=caption, filename=product.get("file_name") or "file")
        if product.get("link"):
            await self.deliver_url(caption, product["link"]); return True
        return False

    async def deliver_video_note(self, file_id: str) -> None:
        return  # видео-кружок — TG-only
```

- [ ] **Step 2: Диспетчер в `_max_respond`** (зеркало VK Step 3, `messenger="max"`, адрес `chat_id`): отзыв → `request_erase(channel="max")`; `is_erase_requested(messenger="max")`→молчим; `get_funnel_config` + `requisites_filled` + `get_lead_snapshot(messenger="max")` → `MaxFunnelChannel(maxbot, chat_id, user_id)` → `funnel.dispatch(ch, fcfg, lead, {"text": text, "consent_pressed": False})` → return.
- [ ] **Step 3: consent в `_max_callback`** — после `upsert_start`/`note_max_chat_id`, ветка:

```python
        if payload and payload.get("cmd") == "consent_yes":
            fcfg = await db.get_funnel_config(tenant_id)
            if fcfg["enabled"] and funnel.requisites_filled(fcfg):
                lead = await db.get_lead_snapshot(user_id, messenger="max") or {}
                ch = funnel_channels.MaxFunnelChannel(maxbot, chat_id, user_id)
                await funnel.dispatch(ch, fcfg, lead, {"text": "", "consent_pressed": True})
            await maxbot.answer_callback(callback_id)
            return
```

- [ ] **Step 4: Smoke** `scripts/max_funnel_smoke.py` — зеркало `vk_funnel_smoke.py` через `MaxFunnelChannel(FakeMAXBot(), 20, 20)`.
- [ ] **Step 5: Run FAIL → реализовать → PASS.**
- [ ] **Step 6: Commit**

```bash
git add bot-telegram/funnel_channels.py bot-telegram/multiplex.py scripts/max_funnel_smoke.py
git commit -m "feat(funnel): порт воронки на MAX — согласие(callback)/телефон/выдача + отзыв

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 6: Хранение байтов лид-магнита + файл-выдача на VK/MAX

**Files:**
- Modify: `bot-telegram/db.py` — `set_product_file_id` (не обнулять `file` для `kind='lead_magnet'`); `get_funnel_product` (+ `file_bytes`/`file_name`)
- Test: `scripts/leadmagnet_bytes_smoke.py`

**Interfaces:**
- Produces: `get_funnel_product(product_id)` теперь возвращает `{"file_tg_id","file_mime","link","file_bytes","file_name"}` (`file_bytes` — `bytes|None`).

- [ ] **Step 1: Failing smoke** `scripts/leadmagnet_bytes_smoke.py` (risuy_dev) — создать продукт `kind='lead_magnet'` с `file` (bytea), вызвать `set_product_file_id(pid, 'TGID')`, проверить, что `file` НЕ обнулён (lead_magnet) и `get_funnel_product` отдаёт `file_bytes`.

```python
#!/usr/bin/env python3
"""Smoke: байты лид-магнита переживают заливку (для VK/MAX-выдачи). risuy_dev.
Запуск: FUNNEL_SMOKE_DSN=... PYTHONPATH=bot-telegram BOT_TOKEN=x DATABASE_URL="$FUNNEL_SMOKE_DSN" \
  CHANNEL_ID=-100 CHANNEL_URL=https://t.me/x GUIDE_URL=https://x ./.venv-smoke/bin/python scripts/leadmagnet_bytes_smoke.py"""
import asyncio, os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import db  # noqa: E402
DSN = os.environ.get("FUNNEL_SMOKE_DSN") or os.environ.get("DATABASE_URL")
if not DSN or "risuy_dev" not in DSN:
    raise SystemExit("Задайте DSN на risuy_dev.")
SLUG = "smoke-lm-bytes"

async def main():
    await db.init(); fails = []
    async with db.pool.acquire() as c:
        async def drop():
            await c.execute("delete from products where tenant_id in (select id from tenants where slug=$1)", SLUG)
            await c.execute("delete from tenants where slug=$1", SLUG)
        await drop()
        tid = await c.fetchval("insert into tenants (slug,name,status) values ($1,'SMOKE lm','active') returning id", SLUG)
        pid = await c.fetchval(
            "insert into products (tenant_id,name,kind,status,file,file_name,file_mime) "
            "values ($1,'ЛМ','lead_magnet','active',$2,'g.pdf','application/pdf') returning id",
            tid, b"%PDF-1.4 test")
        try:
            tok = db.current_tenant_id.set(tid)
            try:
                await db.set_product_file_id(pid, "TG123")
                prod = await db.get_funnel_product(pid)
            finally:
                db.current_tenant_id.reset(tok)
            left = await c.fetchval("select file from products where id=$1", pid)
            if left is None:
                fails.append("байты lead_magnet обнулены после заливки (VK/MAX выдать файл не смогут)")
            if not prod or not prod.get("file_bytes"):
                fails.append("get_funnel_product не отдал file_bytes")
        finally:
            await drop()
    if fails:
        print("\n".join("❌ " + f for f in fails)); raise SystemExit(1)
    print("🟢 leadmagnet_bytes_smoke зелёный")
asyncio.run(main())
```

- [ ] **Step 2: Run, verify FAIL** (байты обнуляются; `file_bytes` нет).
- [ ] **Step 3: Реализовать.** `set_product_file_id` — не обнулять `file`, если `kind='lead_magnet'`:

```python
async def set_product_file_id(product_id: int, file_tg_id: str) -> None:
    """Проставить file_tg_id. Байты (file) обнуляем для обычных продуктов; для lead_magnet ОСТАВЛЯЕМ —
    их переливают VK/MAX (там TG file_id не годится). Частичный индекс заливки опирается на file_tg_id is null,
    поэтому повторно в очередь продукт не попадёт даже с сохранёнными байтами."""
    async with pool.acquire() as c:
        await c.execute(
            "update products set file_tg_id = $2, upload_error = null, "
            "  file = case when kind = 'lead_magnet' then file else null end "
            "where id = $1", product_id, file_tg_id)
```

`get_funnel_product` — добавить байты:

```python
        row = await c.fetchrow(
            "select file_tg_id, file_mime, file_name, file, link from products "
            "where id = $1 and tenant_id = $2 and kind = 'lead_magnet'", product_id, tenant_id())
    if row is None:
        return None
    return {"file_tg_id": row["file_tg_id"], "file_mime": row["file_mime"],
            "file_name": row["file_name"], "file_bytes": row["file"],
            "link": (row["link"] or "").strip() or None}
```

⚠️ В `funnel.deliver` для VK/MAX ветка `prod.get("file_tg_id") or prod.get("link")` не сработает (file_tg_id есть, но это TG-id) — адаптер VK/MAX в `deliver_file` игнорит `file_tg_id` и шлёт `file_bytes`. Проверить: `funnel.deliver` отдаёт в `ch.deliver_file(caption, prod)` весь dict, адаптеры сами выбирают (TG→file_tg_id, VK/MAX→file_bytes). Это уже так в Задачах 2/4/5.

- [ ] **Step 4: Run, verify PASS.**
- [ ] **Step 5: Commit**

```bash
git add bot-telegram/db.py scripts/leadmagnet_bytes_smoke.py
git commit -m "feat(funnel): хранить байты лид-магнита после TG-заливки (файл-выдача на VK/MAX)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 7: Per-channel гейт подписки (VK groups.isMember; MAX-канал)

**Files:**
- Modify: `bot-telegram/vk_driver.py` — `is_member`
- Modify: `bot-telegram/max_driver.py` — `is_channel_member` (fail-closed)
- Modify: `shared/leadmagnet.py` — `FUNNEL_FIELDS` += `vk_gate_group_id`/`max_gate_chat_id`; `validate_funnel_fields`
- Modify: `bot-telegram/db.py` — `get_funnel_config`: ключи + `gate` dict
- Test: `scripts/gate_member_smoke.py`

**Interfaces:**
- Produces: `VKBot.is_member(group_id:int, user_id:int) -> bool`; `MAXBot.is_channel_member(chat_id:int, user_id:int) -> bool`; gate-конфиг `gate.{vk_gate_group_id, max_gate_chat_id}`.

- [ ] **Step 1: VK `is_member` (vk_driver.py)** — fail-closed:

```python
    async def is_member(self, group_id: int, user_id: int) -> bool:
        """groups.isMember — подписан ли user на сообщество-гейт. Fail-closed: ошибка/нет права → False."""
        import aiohttp
        try:
            res = await self._api("groups.isMember", group_id=int(group_id), user_id=int(user_id))
            return bool(res) if isinstance(res, int) else bool((res or {}).get("member"))
        except (aiohttp.ClientError, asyncio.TimeoutError, VKError, Exception) as e:  # noqa: BLE001
            logger.warning("VK is_member fail-closed (group=%s user=%s): %s", group_id, user_id, e)
            return False
```

- [ ] **Step 2: MAX `is_channel_member` (max_driver.py)** — ⚠️ endpoint не верифицирован вживую; fail-closed + лог сырого ответа:

```python
    async def is_channel_member(self, chat_id: int, user_id: int) -> bool:
        """Подписан ли user на MAX-канал-гейт. ⚠️ endpoint проверки членства MAX вживую НЕ подтверждён —
        пробуем GET /chats/{chat_id}/members/{user_id}; любой не-200/ошибка → False (fail-closed, гейт держит).
        Сырой ответ логируем для точечной правки по живому тесту."""
        import aiohttp
        try:
            async with self._session.get(f"{MAX_API}/chats/{int(chat_id)}/members/{int(user_id)}") as r:
                txt = await r.text()
                if r.status != 200:
                    logger.info("MAX is_channel_member HTTP %s: %s", r.status, txt[:200])
                    return False
                data = await self._safe_json(txt)
                return bool((data or {}).get("user_id")) or bool((data or {}).get("is_member"))
        except (aiohttp.ClientError, asyncio.TimeoutError, Exception) as e:  # noqa: BLE001
            logger.warning("MAX is_channel_member fail-closed (chat=%s user=%s): %s", chat_id, user_id, e)
            return False

    @staticmethod
    def _safe_json(txt: str):
        try:
            return json.loads(txt)
        except (ValueError, TypeError):
            return None
```

- [ ] **Step 3: Поля гейта в `shared/leadmagnet.py`** — в `FUNNEL_FIELDS` после `gate_channel_url`:

```python
    {"key": "vk_gate_group_id", "label": "ID VK-сообщества для гейта (VK-канал)", "kind": "text", "required": False},
    {"key": "max_gate_chat_id", "label": "ID MAX-канала для гейта", "kind": "text", "required": False},
```

(`FUNNEL_KEYS` обновится автоматически — он `[f["key"] for f in FUNNEL_FIELDS]`.) В `validate_funnel_fields` ничего обязательного не добавляем (поля опциональны; пустой VK/MAX-гейт → канал-адаптер fail-closed держит гейт).

- [ ] **Step 4: `get_funnel_config` (db.py)** — добавить ключи в `keys` и в `gate` dict:

```python
        "gate_enabled", "gate_channel_id", "gate_channel_url", "vk_gate_group_id", "max_gate_chat_id",
```
```python
        "gate": {
            "enabled": bool(s("gate_enabled")),
            "channel_id": s("gate_channel_id") or None,
            "channel_url": s("gate_channel_url") or None,
            "vk_gate_group_id": s("vk_gate_group_id") or None,
            "max_gate_chat_id": s("max_gate_chat_id") or None,
        },
```

- [ ] **Step 5: Smoke** `scripts/gate_member_smoke.py` (чистый) — FakeVK с `_api`-стабом → `is_member` True/False + fail-closed на исключении; проверить, что `validate_funnel_fields` не требует новые поля.
- [ ] **Step 6: Run FAIL → реализовать → PASS.**
- [ ] **Step 7: TG-регрессия** — `funnel_config_smoke.py` зелёный (новые ключи не ломают форму конфига).
- [ ] **Step 8: Commit**

```bash
git add bot-telegram/vk_driver.py bot-telegram/max_driver.py shared/leadmagnet.py bot-telegram/db.py scripts/gate_member_smoke.py
git commit -m "feat(funnel): per-channel гейт — VK groups.isMember + MAX-канал (fail-closed) + поля конфига

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 8: Поля per-channel гейта в конструкторе панели

**Files:**
- Modify: `admin-panel/templates/lead_magnet.html` — input-блоки `vk_gate_group_id`/`max_gate_chat_id` (рядом с `gate_channel_url`, L135)
- Test: `scripts/funnel_panel_smoke.py` (существующий — расширить)

**Interfaces:** Consumes `leadmagnet.FUNNEL_KEYS` (уже включает новые ключи из Задачи 7). `get_funnel_config_panel`/`set_funnel_config` (admin-panel/db.py) ходят по `FUNNEL_KEYS` — новые поля сохраняются/предзаполняются автоматически.

- [ ] **Step 1: Расширить `funnel_panel_smoke.py`** — после сохранения конфига с `vk_gate_group_id="123"` проверить, что `get_funnel_config_panel` его возвращает (round-trip нового поля).
- [ ] **Step 2: Run, verify FAIL** (если smoke ещё не знает поле — добавить ассерт; поле сохранится через FUNNEL_KEYS, но проверим явно).
- [ ] **Step 3: Добавить input-блоки в `lead_magnet.html`** (после `gate_channel_url`, L135):

```html
      <label class="field">
        <span class="field__label">ID VK-сообщества для гейта (VK-канал)</span>
        <input class="field__input" type="text" name="vk_gate_group_id" inputmode="numeric"
               value="{{ values.vk_gate_group_id|e }}" placeholder="123456789" maxlength="32">
      </label>
      <label class="field">
        <span class="field__label">ID MAX-канала для гейта</span>
        <input class="field__input" type="text" name="max_gate_chat_id" inputmode="numeric"
               value="{{ values.max_gate_chat_id|e }}" placeholder="-100..." maxlength="32">
      </label>
```

- [ ] **Step 4: Run, verify PASS** — `funnel_panel_smoke.py` зелёный (round-trip нового поля). Pure-проверка: рендер `lead_magnet.html` с `values` содержащими ключи не падает.
- [ ] **Step 5: Commit**

```bash
git add admin-panel/templates/lead_magnet.html scripts/funnel_panel_smoke.py
git commit -m "feat(panel): поля VK/MAX-гейта в конструкторе лид-магнита

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 9: Согласие в веб-виджете

**Files:**
- Modify: `service-site/index.html` — галочка согласия в `#demo-chat`; `consent:true` в `/api/demo-chat`
- Modify: `service-site/styles.css` — стиль галочки
- Modify: `bot-telegram/bot.py` — `_demo_chat`: гард согласия + `set_consent(sid, True, channel="web")`
- Test: `scripts/web_consent_smoke.py`

**Interfaces:** Consumes `db.set_consent(sid, True, channel="web", messenger="web")` (Задача 1 — `messenger="web"` через `_user_col`).

- [ ] **Step 1: Гард в `_demo_chat` (bot.py)** — после нормализации `msgs`, до `ask_gateway`:

```python
    consent = bool(body.get("consent")) if isinstance(body, dict) else False
    sid = body.get("session_id") if isinstance(body, dict) else None
    has_sid = isinstance(sid, str) and 8 <= len(sid) <= 80
    if not consent:
        return _cors(web.json_response({"error": "consent_required",
            "reply": "Чтобы продолжить, отметьте согласие на обработку персональных данных 🙏"}))
```

В блоке персистенции (где `cfg.get("tid")` и `current_tenant_id.set`) — записать согласие раз на сессию:

```python
            await db.upsert_start(sid, "web", messenger="web")
            await db.set_consent(sid, True, consent_text=None, channel="web", messenger="web")
            _lid = await db.get_lead_id(sid, messenger="web")
```

(`set_consent` идемпотентна — повторное `granted` пишет ещё событие; чтобы не плодить — допустимо для v1, либо добавить гард «если consent уже стоит — не писать». Для v1 — пишем при каждом consented-запросе сессии; уточнить с владельцем, нужен ли дедуп.)

⚠️ Уточнение дедупа согласия web — вынести в ревью реализации (не плодить granted-события на каждое сообщение). Базовый вариант: проверить `get_lead_snapshot(sid, messenger="web").consent` перед `set_consent`.

- [ ] **Step 2: Галочка в виджете `service-site/index.html`** — над полем ввода `#demo-chat`:

```html
<label class="dc-consent">
  <input type="checkbox" id="dc-consent-cb">
  <span>Согласен на обработку <a href="/legal/demo-sandbox/privacy" target="_blank" rel="noopener">персональных данных</a></span>
</label>
```

JS: кнопка «Отправить» `disabled`, пока `#dc-consent-cb` не отмечен; в fetch-теле `/api/demo-chat` добавить `consent: document.getElementById('dc-consent-cb').checked`; после первого согласия запомнить в `localStorage` (`x10_demo_consent`) и не показывать галочку повторно.

- [ ] **Step 3: Стиль `service-site/styles.css`** — `.dc-consent{display:flex;gap:8px;align-items:flex-start;font-size:13px;margin:8px 0}` (подогнать под токены сайта).
- [ ] **Step 4: Smoke** `scripts/web_consent_smoke.py` — pure: проверить, что `_demo_chat`-гард отвергает запрос без `consent` (можно через прямой вызов с фейковым `aiohttp`-request или вынести чистую функцию `_consent_required(body)->bool` и тестировать её).

```python
#!/usr/bin/env python3
"""Smoke: веб-чат требует согласие до ответа. Тестируем чистый гард _consent_required.
Запуск: PYTHONPATH=bot-telegram ./.venv-smoke/bin/python scripts/web_consent_smoke.py"""
import os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import bot  # noqa: E402

fails = []
if bot._consent_required({"messages": [{"role": "user", "content": "хай"}]}) is not True:
    fails.append("без consent гард должен требовать согласие")
if bot._consent_required({"consent": True, "messages": []}) is not False:
    fails.append("с consent=true гард не должен блокировать")
if fails:
    print("\n".join("❌ " + f for f in fails)); raise SystemExit(1)
print("🟢 web_consent_smoke зелёный")
```

(для этого вынести в `bot.py` чистую `def _consent_required(body: dict) -> bool: return not bool(isinstance(body, dict) and body.get("consent"))` и использовать её в `_demo_chat`.)

- [ ] **Step 5: Run FAIL → реализовать → PASS.**
- [ ] **Step 6: Commit**

```bash
git add bot-telegram/bot.py service-site/index.html service-site/styles.css scripts/web_consent_smoke.py
git commit -m "feat(web): галочка согласия 152-ФЗ в веб-виджете → consent_events(channel='web')

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Финальная проверка (после всех задач)

- [ ] Все новые смоуки зелёные + TG-регрессия (`consent_revoke`, `funnel_flow`, `funnel_config`, `funnel_panel`, `c0_identity`).
- [ ] `git --no-pager diff --stat` — TG-путь (`handlers.py`) не тронут.
- [ ] Передать владельцу: прод-деплой (push) + живой тест VK/MAX (боевые токены) + проверка веб-галочки на сайте. MAX-гейт endpoint — подтвердить вживую (fail-closed до подтверждения).

---

## Self-Review (выполнено автором)

**Покрытие спеки:** §4.1 → Task 1; §4.2 → Task 2; §4.3 → Tasks 3-5; §4.4 → Task 7; §4.5 → Task 6; §4.6 → Task 9; §4.7 → Tasks 7-8; §6 (отзыв) → Tasks 1/4/5; §7 (тесты) → смоуки в каждой задаче. Пробелов нет.

**Плейсхолдеры:** код приведён для всех шагов, меняющих код; команды запуска и ожидаемый результат указаны. Два места помечены как «уточнить в ревью реализации» (дедуп web-согласия) — это явное решение, не заглушка.

**Согласованность типов:** `FunnelChannel`-методы (`send_text/send_consent/ask_phone/ask_gate/check_subscription/deliver_text/deliver_url/deliver_file/deliver_video_note`, атрибуты `uid/messenger`) одинаковы в Tasks 2/4/5. `dispatch(ch, cfg, lead, incoming)` и `get_lead_snapshot(uid, messenger=)` согласованы между Tasks 3/4/5. Сеттеры с `messenger=` (Task 1) используются в Tasks 3-6/9 единообразно.
