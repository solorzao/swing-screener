import base64

from swing_screener.notify import acs


class _FakePoller:
    def __init__(self):
        self.result_called = False

    def result(self):
        self.result_called = True
        return {"status": "Succeeded"}


class _FakeEmailClient:
    """Records the message dict passed to begin_send; never touches the network."""

    def __init__(self):
        self.message = None
        self.poller = _FakePoller()

    def begin_send(self, message):
        self.message = message
        return self.poller


def test_send_email_acs_builds_message_with_pdf_attachment(tmp_path):
    pdf = tmp_path / "digest.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    client = _FakeEmailClient()

    acs.send_email_acs(
        to="me@example.com", subject="Daily", text="hello", html="<p>hello</p>",
        attachments=[pdf], sender="bot@acs.example.net",
        endpoint="https://res.communication.azure.com", client=client,
    )

    msg = client.message
    assert msg is not None
    assert msg["senderAddress"] == "bot@acs.example.net"
    assert msg["recipients"]["to"] == [{"address": "me@example.com"}]
    assert msg["content"]["subject"] == "Daily"
    assert msg["content"]["plainText"] == "hello"
    assert msg["content"]["html"] == "<p>hello</p>"
    # the PDF attachment is base64-encoded with the right name + content type.
    atts = msg["attachments"]
    assert len(atts) == 1
    assert atts[0]["name"] == "digest.pdf"
    assert atts[0]["contentType"] == "application/pdf"
    assert base64.b64decode(atts[0]["contentInBase64"]) == b"%PDF-1.4 fake"
    # the poller's result() was awaited (block until accepted).
    assert client.poller.result_called is True


def test_send_email_acs_html_defaults_to_text_when_none(tmp_path):
    client = _FakeEmailClient()
    acs.send_email_acs(
        to="me@example.com", subject="S", text="plain body", html=None,
        attachments=[], sender="bot@acs.example.net",
        endpoint="https://res.communication.azure.com", client=client,
    )
    assert client.message["content"]["html"] == "plain body"


def test_send_email_acs_omits_or_empties_attachments_when_none():
    client = _FakeEmailClient()
    acs.send_email_acs(
        to="me@example.com", subject="S", text="t", html=None,
        attachments=[], sender="bot@acs.example.net",
        endpoint="https://res.communication.azure.com", client=client,
    )
    assert client.message.get("attachments", []) == []


def test_send_email_acs_refuses_an_empty_sender():
    """Belt-and-braces at the send seam: an empty senderAddress is a config bug,
    never a message worth submitting (2026-07-17 audit M4d)."""
    import pytest

    client = _FakeEmailClient()
    with pytest.raises(ValueError, match="sender"):
        acs.send_email_acs(
            to="me@example.com", subject="S", text="t", html=None,
            attachments=[], sender="", endpoint="https://res.example", client=client)
    assert client.message is None  # nothing was submitted
