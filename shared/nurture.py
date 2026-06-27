"""Контракт ДОЖИМА (nurture) — ЕДИНЫЙ источник истины для бота (чтение конфига) и панели (форма+валидация).

Хранение: tenant_settings.nurture_enabled ('1'/'') + tenant_settings.nurture_steps — JSON-массив
[{"delay_seconds": int>0, "text": str}, ...] (до 3 шагов). delay_seconds — пауза ПЕРЕД касанием:
для 1-го шага отсчитывается от момента, когда лид замолчал (последнее входящее), для шага N>1 — от
предыдущего касания (кумулятивная цепочка). Те же шаги уходят по всем поднятым каналам тенанта (TG/VK/MAX).

Два уровня:
- parse_nurture_steps — ЛЕНИВЫЙ парсер (бот при чтении + панель при предзаполнении): молча отбрасывает
  мусор (битый JSON → []). Поведение совпадает с прежним инлайном в bot-telegram/db.get_tenant_nurture.
- normalize_and_validate — СТРОГАЯ валидация ввода формы панели: НЕ отбрасывает молча, на кривой
  присутствующий шаг возвращает человекочитаемую ошибку (принцип «ничего не пропадает без объяснения»).
"""
import json

NURTURE_MAX_STEPS = 3
NURTURE_TEXT_MAX = 1500  # кап на текст касания (анти-распухание БД; UI maxlength зеркалит)
NURTURE_MIN_DELAY_SECONDS = 60  # движок дожима тикает раз в минуту → задержки < 1 мин бессмысленны
                                # и не представимы обратным конвертером единиц панели (минуты/часы/дни)


def parse_nurture_steps(raw) -> list[dict]:
    """ЛЕНИВЫЙ парсер шагов: raw — JSON-строка ИЛИ список. Возвращает ≤3 валидных шага
    [{"delay_seconds": int>0, "text": непустой}], молча отбрасывая мусор (битый JSON → [])."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "[]")
        except Exception:  # noqa: BLE001 — битый JSON → пусто (не угадываем)
            return []
    steps: list[dict] = []
    for s in (raw if isinstance(raw, list) else [])[:NURTURE_MAX_STEPS]:
        if not isinstance(s, dict):
            continue
        try:
            d = int(s.get("delay_seconds") or 0)
        except (TypeError, ValueError):
            continue
        t = (s.get("text") or "").strip()
        if d > 0 and t:
            steps.append({"delay_seconds": d, "text": t})
    return steps


def normalize_and_validate(enabled: bool, raw_steps: list[dict]) -> tuple[list[dict], list[str]]:
    """Строгая валидация шагов ИЗ ФОРМЫ панели. raw_steps — [{"delay_seconds": int|None, "text": str}]
    (задержка уже переведена панелью в секунды). «Присутствующим» считаем шаг с непустым текстом ИЛИ
    заданной задержкой — такой шаг обязан быть полным, иначе ошибка (молча НЕ роняем). Возвращает
    (нормализованные_шаги, список_ошибок); пустой список ошибок = успех."""
    clean: list[dict] = []
    errs: list[str] = []
    gap_seen = False  # встретили пустой шаг → последующий заполненный = пропуск (порядок без дыр)
    for i, s in enumerate(raw_steps[:NURTURE_MAX_STEPS], start=1):
        d = s.get("delay_seconds")
        t = (s.get("text") or "").strip()
        present = bool(t) or (d is not None)
        if not present:
            gap_seen = True
            continue  # полностью пустой шаг — просто не задан, не ошибка (если дальше тоже пусто)
        bad = False
        if gap_seen:
            errs.append(f"Касание {i}: заполняйте касания по порядку, без пропусков "
                        f"(предыдущее касание оставлено пустым).")
            bad = True
        if d is None or d <= 0:
            errs.append(f"Касание {i}: укажите задержку больше нуля.")
            bad = True
        elif d < NURTURE_MIN_DELAY_SECONDS:
            errs.append(f"Касание {i}: минимальная задержка — 1 минута.")
            bad = True
        if not t:
            errs.append(f"Касание {i}: введите текст сообщения.")
            bad = True
        if not bad:
            clean.append({"delay_seconds": int(d), "text": t[:NURTURE_TEXT_MAX]})
    if enabled and not clean and not errs:
        errs.append("Дожим включён, но не задано ни одного касания — добавьте хотя бы одно "
                    "(задержка больше нуля и текст) либо выключите тумблер.")
    return clean, errs
