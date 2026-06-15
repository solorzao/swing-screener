"""Send digest mail via Azure Communication Services (ACS) Email.

An alternative to the Gmail SMTP transport that authenticates with a managed
identity instead of a stored password: ``DefaultAzureCredential`` picks up the
user-assigned identity via ``AZURE_CLIENT_ID`` in the job env, so NO email
secret is stored anywhere. Selected by ``notify.transport.resolve_sender`` when
``SWING_ACS_ENDPOINT`` is set, mirroring how ``storage/blob.py`` is gated on
``SWING_BLOB_ACCOUNT_URL``.

The ``azure-communication-email`` / ``azure-identity`` packages are an optional
extra that is NOT installed in CI, so they are imported LAZILY inside the send
function -- never at module import time. The ``EmailClient`` is injectable so
tests pass a fake and never import azure or hit the network.
"""

import base64
from pathlib import Path


def send_email_acs(
    *,
    to: str,
    subject: str,
    text: str,
    html: str | None,
    attachments: list[Path],
    sender: str,
    endpoint: str,
    client: object | None = None,
) -> None:
    """Send an email via ACS Email, blocking until the request is accepted.

    Builds the ACS message dict (sender, single ``to`` recipient, plain-text +
    HTML content, base64-encoded PDF attachments) and submits it via
    ``begin_send``, then blocks on the returned poller's ``result()``. When
    ``html`` is None the plain text is reused as the HTML body. The attachments
    key is omitted when there are none.

    ``client`` is an injectable seam: when None an ``EmailClient`` is built
    lazily with ``DefaultAzureCredential`` (which resolves the user-assigned
    identity via ``AZURE_CLIENT_ID``). Tests pass a fake client so azure is never
    imported and the network is never touched.
    """
    if client is None:
        # Imported LAZILY -- the azure extra is not installed in CI. Isolated
        # here so tests that inject a fake client never import azure.
        from azure.communication.email import EmailClient
        from azure.identity import DefaultAzureCredential

        client = EmailClient(endpoint, DefaultAzureCredential())

    message: dict[str, object] = {
        "senderAddress": sender,
        "recipients": {"to": [{"address": to}]},
        "content": {"subject": subject, "plainText": text, "html": html or text},
    }
    encoded = []
    for path in attachments:
        path = Path(path)
        encoded.append({
            "name": path.name,
            "contentType": "application/pdf",
            "contentInBase64": base64.b64encode(path.read_bytes()).decode("ascii"),
        })
    if encoded:
        message["attachments"] = encoded

    poller = client.begin_send(message)  # type: ignore[union-attr]
    poller.result()  # block until the send request is accepted
