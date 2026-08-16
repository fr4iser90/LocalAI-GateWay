from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage

from sqlalchemy.orm import Session

from .config import get_settings
from .crypto_util import decrypt_secret
from .data.models import SmtpConfig


class MailError(Exception):
    pass


def get_smtp(db: Session) -> SmtpConfig | None:
    return db.query(SmtpConfig).first()


def smtp_ready(cfg: SmtpConfig | None) -> bool:
    if cfg is None or not cfg.enabled:
        return False
    return bool(cfg.host and cfg.from_email and cfg.public_base_url)


def smtp_password_plain(cfg: SmtpConfig) -> str:
    settings = get_settings()
    try:
        return decrypt_secret(cfg.password or "", settings.session_secret)
    except ValueError as exc:
        raise MailError(str(exc)) from exc


def send_mail(
    db: Session,
    *,
    to_email: str,
    subject: str,
    body_text: str,
) -> None:
    cfg = get_smtp(db)
    if not smtp_ready(cfg):
        raise MailError("SMTP is not configured or disabled")
    assert cfg is not None
    password = smtp_password_plain(cfg)

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"{cfg.from_name} <{cfg.from_email}>" if cfg.from_name else cfg.from_email
    msg["To"] = to_email
    msg.set_content(body_text)

    try:
        if cfg.use_ssl:
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(cfg.host, cfg.port, context=context, timeout=15) as smtp:
                if cfg.username:
                    smtp.login(cfg.username, password)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(cfg.host, cfg.port, timeout=15) as smtp:
                smtp.ehlo()
                if cfg.use_tls:
                    context = ssl.create_default_context()
                    smtp.starttls(context=context)
                    smtp.ehlo()
                if cfg.username:
                    smtp.login(cfg.username, password)
                smtp.send_message(msg)
    except MailError:
        raise
    except Exception as exc:
        raise MailError(str(exc)) from exc
