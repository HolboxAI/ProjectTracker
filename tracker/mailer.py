"""Outgoing email, standard library only.

`smtplib` is in the Python standard library, so none of this needs a
dependency and the instance does not need a pip step. What it does need is
credentials, which arrive through the environment:

    SMTP_HOST       smtp.gmail.com, email-smtp.us-west-2.amazonaws.com, ...
    SMTP_PORT       587 (default), 465 for implicit TLS
    SMTP_USER       the account to authenticate as
    SMTP_PASSWORD   its password or app password
    SMTP_FROM       the From: address, e.g. "Covex-Board <tracker@holbox.ai>"
    SMTP_SECURITY   starttls (default) | ssl | none
    APP_BASE_URL    used for the link in the message, default https://tracker.holbox.ai

With no SMTP_HOST or SMTP_FROM, `configured()` is False and nothing is sent.
That is the right behaviour for a laptop and for the tests: the app must not
need a mail server to run, and it must not fail when one is missing.
"""

from __future__ import annotations

import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, make_msgid, parseaddr

DEFAULT_APP_URL = "https://tracker.holbox.ai"


def _env(name, default=""):
    return (os.environ.get(name) or default).strip()


def settings():
    """SMTP settings, or None when mail is not configured."""
    host = _env("SMTP_HOST")
    sender = _env("SMTP_FROM")
    if not host or not sender:
        return None
    try:
        port = int(_env("SMTP_PORT", "587"))
    except ValueError:
        port = 587
    security = _env("SMTP_SECURITY", "ssl" if port == 465 else "starttls").lower()
    if security not in ("starttls", "ssl", "none"):
        security = "starttls"
    return {
        "host": host,
        "port": port,
        "user": _env("SMTP_USER"),
        "password": _env("SMTP_PASSWORD"),
        "sender": sender,
        "security": security,
    }


def configured() -> bool:
    return settings() is not None


def app_url(path="") -> str:
    base = _env("APP_BASE_URL", DEFAULT_APP_URL).rstrip("/")
    return base + path


def clean_header(value, limit=200) -> str:
    """A single line, safe to put in a Subject.

    An issue title is user text and goes into a mail header. A newline in it
    would let somebody append headers of their own -- Bcc, most usefully -- so
    line breaks collapse to spaces here rather than being trusted.
    """
    text = " ".join(str(value or "").split())
    return text[:limit]


def send(to_address, subject, body, *, settings_override=None):
    """Send one plain-text message. Raises on failure -- the caller records it.

    Plain text on purpose: this is a notification that says what happened and
    links to the ticket, and an HTML part would be nine lines of styling to
    say the same thing.
    """
    config = settings_override or settings()
    if config is None:
        raise RuntimeError("SMTP is not configured (set SMTP_HOST and SMTP_FROM)")

    _,recipient = parseaddr(str(to_address))
    if not recipient:
        raise ValueError("No recipient address")

    message = EmailMessage()
    message["From"] = config["sender"]
    message["To"] = recipient
    message["Subject"] = clean_header(subject)
    message["Message-ID"] = make_msgid(domain=(recipient.split("@")[-1] or "localhost"))
    # A ticket notification is not a newsletter; there is nothing to unsubscribe
    # from, and a stray click of a bulk action should not skip the inbox.
    message["Auto-Submitted"] = "auto-generated"
    message.set_content(body)

    if config["security"] == "ssl":
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(config["host"], config["port"], context=context, timeout=20) as smtp:
            if config["user"]:
                smtp.login(config["user"], config["password"])
            smtp.send_message(message)
        return

    with smtplib.SMTP(config["host"], config["port"], timeout=20) as smtp:
        smtp.ehlo()
        if config["security"] == "starttls":
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo()
        if config["user"]:
            smtp.login(config["user"], config["password"])
        smtp.send_message(message)
