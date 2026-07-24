import smtplib
from typing import ClassVar

from swing_screener.notify import smtp


class _FakeSMTP:
    instances: ClassVar[list["_FakeSMTP"]] = []

    def __init__(self, host, port):
        self.host, self.port = host, port
        self.logged_in = None
        self.sent = None
        _FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def login(self, user, password):
        self.logged_in = (user, password)

    def send_message(self, msg):
        self.sent = msg


def test_send_email_with_pdf_attachment(tmp_path, monkeypatch):
    _FakeSMTP.instances.clear()
    monkeypatch.setattr(smtplib, "SMTP_SSL", _FakeSMTP)
    pdf = tmp_path / "digest.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")

    smtp.send_email(
        to="me@example.com", subject="Daily", text="hello", html="<p>hello</p>",
        attachments=[pdf], sender="bot@gmail.com", password="app-pw",
    )

    assert len(_FakeSMTP.instances) == 1
    server = _FakeSMTP.instances[0]
    assert server.host == "smtp.gmail.com" and server.port == 465
    assert server.logged_in == ("bot@gmail.com", "app-pw")
    msg = server.sent
    assert msg["To"] == "me@example.com" and msg["Subject"] == "Daily"
    assert msg["From"] == "bot@gmail.com"
    # the PDF attachment is present
    attachments = [p for p in msg.iter_attachments()]
    assert len(attachments) == 1
    assert attachments[0].get_filename() == "digest.pdf"


def test_credentials_fall_back_to_env(tmp_path, monkeypatch):
    _FakeSMTP.instances.clear()
    monkeypatch.setattr(smtplib, "SMTP_SSL", _FakeSMTP)
    monkeypatch.setenv("GMAIL_ADDRESS", "env@gmail.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "env-pw")
    smtp.send_email(to="x@example.com", subject="S", text="t")
    assert _FakeSMTP.instances[0].logged_in == ("env@gmail.com", "env-pw")
