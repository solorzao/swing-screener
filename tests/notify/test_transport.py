from swing_screener.notify import acs, smtp, transport


def test_resolve_sender_defaults_to_smtp_when_acs_unset(monkeypatch):
    # no SWING_ACS_ENDPOINT -> the SMTP send function is returned verbatim.
    monkeypatch.delenv("SWING_ACS_ENDPOINT", raising=False)
    monkeypatch.delenv("SWING_ACS_SENDER", raising=False)
    assert transport.resolve_sender() is smtp.send_email


def test_resolve_sender_returns_acs_bound_callable_when_configured(monkeypatch, tmp_path):
    monkeypatch.setenv("SWING_ACS_ENDPOINT", "https://res.communication.azure.com")
    monkeypatch.setenv("SWING_ACS_SENDER", "bot@acs.example.net")

    calls = []

    def recorder(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(acs, "send_email_acs", recorder)

    send = transport.resolve_sender()
    assert send is not smtp.send_email  # an ACS-bound callable, not the SMTP one

    pdf = tmp_path / "d.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    send(to="me@example.com", subject="S", text="t", html="<p>t</p>", attachments=[pdf])

    assert len(calls) == 1
    kw = calls[0]
    # the uniform send signature is forwarded ...
    assert kw["to"] == "me@example.com"
    assert kw["subject"] == "S"
    assert kw["text"] == "t"
    assert kw["html"] == "<p>t</p>"
    assert kw["attachments"] == [pdf]
    # ... with the configured sender + endpoint bound in.
    assert kw["sender"] == "bot@acs.example.net"
    assert kw["endpoint"] == "https://res.communication.azure.com"


def test_resolve_sender_fails_loudly_when_acs_sender_unset(monkeypatch):
    """SWING_ACS_ENDPOINT without SWING_ACS_SENDER used to bind an EMPTY
    senderAddress and fail downstream on every send -- construction must refuse
    with a message naming the missing env (2026-07-17 audit M4d)."""
    import pytest

    monkeypatch.setenv("SWING_ACS_ENDPOINT", "https://res.communication.azure.com")
    monkeypatch.delenv("SWING_ACS_SENDER", raising=False)
    with pytest.raises(RuntimeError, match="SWING_ACS_SENDER"):
        transport.resolve_sender()
