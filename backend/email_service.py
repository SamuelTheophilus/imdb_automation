from __future__ import annotations

import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

_TEMPLATES_DIR = Path(__file__).parent.parent / "core" / "emails"
_env = Environment(loader=FileSystemLoader(str(_TEMPLATES_DIR)))


def _render(template_name: str, **ctx) -> str:
    return _env.get_template(template_name).render(**ctx)


def _gmail_credentials() -> tuple[str, str]:
    user = os.getenv("GMAIL_USER", "")
    password = os.getenv("GMAIL_APP_PASSWORD", "")
    if not user or not password:
        raise RuntimeError(
            "GMAIL_USER and GMAIL_APP_PASSWORD must be set in your .env file. "
            "Generate an App Password at: https://myaccount.google.com/apppasswords"
        )
    return user, password


def _send(to_email: str, subject: str, html: str) -> None:
    """Send an HTML email via Gmail SMTP."""
    gmail_user, gmail_password = _gmail_credentials()
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"IMDB AutoFill <{gmail_user}>"
    msg["To"] = to_email
    msg.attach(MIMEText(html, "html"))
    with smtplib.SMTP("smtp.gmail.com", 587) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.login(gmail_user, gmail_password)
        smtp.sendmail(gmail_user, to_email, msg.as_string())


def send_password_reset(to_email: str, code: str) -> None:
    """Send a password reset code via Gmail SMTP."""
    html = _render("password_reset.j2", code=code)
    _send(to_email, "Your IMDB AutoFill password reset code", html)


def send_batch_complete(
    to_email: str,
    username: str,
    result_count: int,
    job_id: int,
) -> None:
    """Notify the user that their bulk batch job has completed."""
    html = _render("batch_complete.j2", username=username, result_count=result_count, job_id=job_id)
    subject = f"IMDB AutoFill — batch extraction complete ({result_count} products)"
    _send(to_email, subject, html)
