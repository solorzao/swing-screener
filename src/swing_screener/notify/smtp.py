"""Send digest mail via Gmail SMTP over SSL.

The digest orchestrator uses this to deliver the summary email (optionally with
a PDF attachment) and exit alerts. Credentials default to the ``GMAIL_ADDRESS``
and ``GMAIL_APP_PASSWORD`` environment variables but can be overridden by args.
"""

import smtplib
from collections.abc import Sequence
from email.message import EmailMessage
from pathlib import Path

from swing_screener.config_secrets import require_secret


def send_email(
    *,
    to: str,
    subject: str,
    text: str,
    html: str | None = None,
    attachments: Sequence[Path] = (),
    sender: str | None = None,
    password: str | None = None,
) -> None:
    """Send an email via Gmail SMTP_SSL. PDF attachments are attached by path.

    ``sender``/``password`` fall back to the ``GMAIL_ADDRESS`` and
    ``GMAIL_APP_PASSWORD`` environment variables. A plain-text body is always
    set; when ``html`` is given it is added as an alternative part. Each path in
    ``attachments`` is read and attached as an ``application/pdf`` part.
    """
    # Function args take precedence; only the fallback resolves via the secrets
    # layer (env first, then Key Vault). require_secret is called only when the
    # arg is missing, so passing explicit creds never touches the resolver.
    sender = sender or require_secret("GMAIL_ADDRESS")
    password = password or require_secret("GMAIL_APP_PASSWORD")

    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(text)
    if html is not None:
        msg.add_alternative(html, subtype="html")
    for path in attachments:
        path = Path(path)
        msg.add_attachment(
            path.read_bytes(),
            maintype="application",
            subtype="pdf",
            filename=path.name,
        )

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(sender, password)
        server.send_message(msg)
