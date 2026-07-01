"""Отправка транзакционного письма сброса пароля через aiosmtplib.

Провайдер-агностично (SMTP через env). Если SMTP не настроен (пустой SMTP_HOST) —
dry-run: логируем ссылку вместо отправки (локаль/смоуки). На проде креды обязательны.
Письмо минимальное, на русском, без маркетинга (спам-safe / 152-ФЗ)."""
import logging
from email.message import EmailMessage

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
    msg["From"] = config.SMTP_FROM
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
