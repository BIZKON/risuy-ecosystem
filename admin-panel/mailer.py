"""Отправка транзакционного письма сброса пароля через aiosmtplib.

Провайдер-агностично (SMTP через env). Если SMTP не настроен (пустой SMTP_HOST) —
dry-run: логируем ссылку вместо отправки (локаль/смоуки). На проде креды обязательны.
Письмо минимальное, на русском, без маркетинга (спам-safe / 152-ФЗ)."""
import logging
from email.message import EmailMessage
from email.utils import formataddr

import aiosmtplib

import config

log = logging.getLogger("mailer")


def is_configured() -> bool:
    """SMTP настроен для реальной отправки?"""
    return bool(config.SMTP_HOST and config.SMTP_FROM)


async def send_password_reset(to_email: str, reset_url: str, *, ttl_min: int) -> None:
    """Отправить письмо со ссылкой сброса. Dry-run (лог) при ненастроенном SMTP."""
    text = (
        "Вы запросили сброс пароля к панели.\n\n"
        f"Чтобы задать новый пароль, откройте ссылку (действительна {ttl_min} минут):\n"
        f"{reset_url}\n\n"
        "Если вы этого не запрашивали — просто проигнорируйте письмо. Пароль не изменится."
    )
    if not is_configured():
        # Fail-safe: по умолчанию НЕ логируем рабочий токен/email (утечка секрета в логи прода
        # при пустом SMTP). Полную ссылку показываем только при явном MAILER_DEBUG_LOG_URL (локаль).
        if config.MAILER_DEBUG_LOG_URL:
            log.warning("SMTP не настроен (dry-run). Ссылка сброса для %s: %s", to_email, reset_url)
        else:
            log.error("SMTP не настроен — письмо сброса НЕ отправлено (email/ссылка скрыты). "
                      "Задайте SMTP_HOST/SMTP_FROM.")
        return

    msg = EmailMessage()
    # Русское имя отправителя (клиент видит его крупно), латинский адрес — второстепенно.
    msg["From"] = formataddr((config.SMTP_FROM_NAME, config.SMTP_FROM))
    msg["To"] = to_email
    msg["Subject"] = "Восстановление доступа"
    msg.set_content(text)

    await aiosmtplib.send(
        msg,
        hostname=config.SMTP_HOST,
        port=config.SMTP_PORT,
        username=config.SMTP_USER or None,
        password=config.SMTP_PASS or None,
        start_tls=config.SMTP_STARTTLS,
    )


def _ttl_human(ttl_min: int) -> str:
    return f"{ttl_min // 1440} дней" if ttl_min >= 1440 else f"{ttl_min} минут"


async def send_account_claim(to_email: str, claim_url: str, *, ttl_min: int) -> None:
    """T-1F-3b: письмо «аккаунт создан после оплаты — задайте пароль» (claim). Копирайт ОТЛИЧАЕТСЯ
    от сброса (ревью: send_password_reset «вы запросили сброс» неверно для покупки). Dry-run при
    ненастроенном SMTP. Та же ссылка /reset-password (первичная установка пароля)."""
    # Гард АБСОЛЮТНОСТИ ссылки: claim-письмо шлют вебхук и ops-реконсиляция — у них НЕТ
    # request-контекста, и base берётся только из config.PANEL_PUBLIC_BASE_URL. Если он пуст,
    # получится относительный "/reset-password?token=…" — нерабочая ссылка, а токен при этом
    # уже выпущен (и погасил прежние). Лучше НЕ отправить и громко сказать, чем отправить
    # клиенту мёртвую ссылку после оплаты.
    if not claim_url.startswith(("http://", "https://")):
        log.error(
            "claim-письмо НЕ отправлено: ссылка не абсолютная (%r). Задайте PANEL_PUBLIC_BASE_URL "
            "в env панели (напр. https://admin.pro-agent-ai.ru) — иначе клиент получит мёртвую ссылку.",
            claim_url)
        return

    text = (
        "Спасибо за оплату! Мы создали ваш аккаунт в панели «ИИ-Агент Про».\n\n"
        f"Чтобы войти, задайте пароль по ссылке (действительна {_ttl_human(ttl_min)}):\n"
        f"{claim_url}\n\n"
        "После установки пароля входите по этому e-mail и паролю. "
        "Если вы не совершали оплату — просто проигнорируйте это письмо."
    )
    if not is_configured():
        if config.MAILER_DEBUG_LOG_URL:
            log.warning("SMTP не настроен (dry-run). Claim-ссылка для %s: %s", to_email, claim_url)
        else:
            log.error("SMTP не настроен — claim-письмо НЕ отправлено (email/ссылка скрыты). "
                      "Задайте SMTP_HOST/SMTP_FROM.")
        return

    msg = EmailMessage()
    msg["From"] = formataddr((config.SMTP_FROM_NAME, config.SMTP_FROM))
    msg["To"] = to_email
    msg["Subject"] = "Ваш аккаунт «ИИ-Агент Про» — задайте пароль"
    msg.set_content(text)

    await aiosmtplib.send(
        msg,
        hostname=config.SMTP_HOST,
        port=config.SMTP_PORT,
        username=config.SMTP_USER or None,
        password=config.SMTP_PASS or None,
        start_tls=config.SMTP_STARTTLS,
    )
