"""The one mail this app sends: a password reset link (spec FR-059, C-13).

Plain SMTP from the standard library, configured by ``ORDERING_SMTP_*`` and
``ORDERING_APP_URL``. With no host configured the app sends nothing and the
sign-in page offers no reset link — an admin sets a temporary password under
Users instead. The mail's body is never logged: it carries the link.
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage

from app.config import get_settings

log = logging.getLogger("ordering.mailer")

SEND_TIMEOUT_SECONDS = 20


def mail_configured() -> bool:
    """Whether a reset link can be both built and sent."""
    settings = get_settings()
    return bool(settings.smtp_host and settings.smtp_from and settings.app_url)


def reset_link(token: str) -> str:
    """The page that sets a new password, with the token after the ``#``.

    A fragment is the one part of an address a browser never sends (audit
    F-29): it is not in the request line, so no access log on the way, nginx's
    or uvicorn's, can keep it, and it is never part of a Referer. The page
    reads it from there (or from ``?token=`` in a link mailed before the move)
    and then clears it from the address bar.
    """
    base = get_settings().app_url.rstrip("/")
    return f"{base}/reset-password#token={token}"


def send_mail(to: str, subject: str, body: str) -> None:
    """Send one plain-text mail; raises on any failure (the caller decides what to say)."""
    settings = get_settings()
    message = EmailMessage()
    message["From"] = settings.smtp_from
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=SEND_TIMEOUT_SECONDS) as smtp:
        if settings.smtp_starttls:
            # A default context verifies the server's certificate and name;
            # starttls() without one takes any certificate at all, and the
            # link in this mail signs its reader in.
            smtp.starttls(context=ssl.create_default_context())
        if settings.smtp_user:
            smtp.login(settings.smtp_user, settings.smtp_password)
        smtp.send_message(message)


def send_password_reset(to: str, token: str) -> None:
    minutes = get_settings().password_reset_minutes
    link = reset_link(token)
    body = (
        "Someone asked to reset the password of this account on the Ordering Suite.\n\n"
        f"Open this link to choose a new password (it works for {minutes} minutes and once):\n\n"
        f"{link}\n\n"
        "If that was not you, ignore this mail; nothing changes.\n"
    )
    send_mail(to, "Reset your Ordering Suite password", body)
    log.info("password reset mail sent to %s", to)
