"""Pluggable, env-driven email transport selection.

The digest pipeline calls one uniform ``send(to=, subject=, text=, html=,
attachments=)`` callable; this module decides which transport backs it. When
``SWING_ACS_ENDPOINT`` is set (mirroring how ``storage/blob.py`` is gated on
``SWING_BLOB_ACCOUNT_URL``), mail goes out via Azure Communication Services
authenticated by managed identity -- so NO email password is stored. Otherwise
it falls back to the Gmail SMTP transport.

The returned ACS callable wraps :func:`acs.send_email_acs`, binding the
configured sender + endpoint while keeping the SAME keyword signature as
:func:`smtp.send_email`, so the rest of the pipeline is transport-agnostic. No
azure import lives here; the lazy import happens inside ``acs.send_email_acs``.
"""

from collections.abc import Callable
from pathlib import Path

from swing_screener.notify import acs, smtp
from swing_screener.settings import load_settings


def resolve_sender() -> Callable[..., None]:
    """Return the active email send callable, chosen from the current env.

    If ``SWING_ACS_ENDPOINT`` is configured, return an ACS-bound callable with
    the same keyword signature as :func:`smtp.send_email`; otherwise return
    :func:`smtp.send_email` itself. Read at call time so tests can set env and
    observe the change.
    """
    settings = load_settings()
    endpoint = settings.acs_endpoint
    if not endpoint:
        return smtp.send_email
    sender = settings.acs_sender
    if not sender:
        # Binding an empty senderAddress would fail DOWNSTREAM on every send (an
        # ACS reject long after construction) -- refuse loudly here instead, naming
        # the missing env (2026-07-17 audit M4d).
        raise RuntimeError(
            "SWING_ACS_ENDPOINT is set but SWING_ACS_SENDER is not: the ACS email "
            "transport needs the configured sender address (a MailFrom address on "
            "the ACS resource); set SWING_ACS_SENDER or unset SWING_ACS_ENDPOINT "
            "to fall back to SMTP")

    def _send_acs(
        *,
        to: str,
        subject: str,
        text: str,
        html: str | None = None,
        attachments: list[Path] | None = None,
    ) -> None:
        acs.send_email_acs(
            to=to, subject=subject, text=text, html=html,
            attachments=list(attachments or []), sender=sender, endpoint=endpoint,
        )

    return _send_acs
