"""Provider-neutral SMTP delivery for workforce account lifecycle messages."""

from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage
from urllib.parse import quote


class NotificationUnavailable(RuntimeError):
    """Raised when account mail cannot be delivered safely."""


def ensure_account_email_configured() -> None:
    """Fail before account state changes when outbound account email is unavailable."""
    required = (
        "SHELFWISE_SMTP_HOST",
        "SHELFWISE_SMTP_FROM",
        "SHELFWISE_PUBLIC_APP_URL",
    )
    if any(not os.getenv(name, "").strip() for name in required):
        raise NotificationUnavailable("Work-account email delivery is not configured")


def send_account_link(
    *,
    recipient: str,
    given_name: str,
    purpose: str,
    token: str,
) -> None:
    """Send a token in a URL fragment so it does not enter ordinary server access logs."""
    ensure_account_email_configured()
    if purpose not in {"activate", "reset-password"}:
        raise ValueError("Unsupported account email purpose")
    base_url = os.environ["SHELFWISE_PUBLIC_APP_URL"].strip().rstrip("/")
    link = f"{base_url}/#{purpose}={quote(token, safe='')}"
    action = "activate your work account" if purpose == "activate" else "reset your password"
    message = EmailMessage()
    message["From"] = os.environ["SHELFWISE_SMTP_FROM"].strip()
    message["To"] = recipient
    message["Subject"] = f"ShelfWise: {action}"
    message.set_content(
        f"Hello {given_name},\n\nUse this single-use link to {action}:\n{link}\n\n"
        "If you did not expect this message, contact your ShelfWise owner."
    )
    _deliver(message)


def _deliver(message: EmailMessage) -> None:
    host = os.environ["SHELFWISE_SMTP_HOST"].strip()
    try:
        port = int(os.getenv("SHELFWISE_SMTP_PORT", "587"))
    except ValueError as exc:
        raise NotificationUnavailable("Work-account email delivery is misconfigured") from exc
    username = os.getenv("SHELFWISE_SMTP_USERNAME", "").strip()
    password = os.getenv("SHELFWISE_SMTP_PASSWORD", "")
    try:
        with smtplib.SMTP(host, port, timeout=15) as smtp:
            if os.getenv("SHELFWISE_SMTP_STARTTLS", "true").strip().lower() != "false":
                smtp.starttls()
            if username:
                smtp.login(username, password)
            smtp.send_message(message)
    except (OSError, smtplib.SMTPException) as exc:
        raise NotificationUnavailable("Work-account email could not be delivered") from exc
