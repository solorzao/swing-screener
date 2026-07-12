"""Coach narrative author: firewall behaviour. Injected fake client -> its prose is
returned; a failing client -> the deterministic template with usage None; the system
prompt forbids altering numbers. No real API is ever hit."""

from datetime import date

from swing_screener.db.models import Trade
from swing_screener.journal.coach_author import _COACH_SYSTEM, draft_review
from swing_screener.journal.coach_grade import equity_review_facts


class _Block:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _Resp:
    def __init__(self, text):
        self.content = [_Block(text)]
        self.usage = None  # capture path returns None; we assert the None-usage fallback separately


class _FakeClient:
    def __init__(self, text=None, raises=False):
        self._text = text
        self._raises = raises
        self.messages = self

    def create(self, **kwargs):
        if self._raises:
            raise RuntimeError("api down")
        return _Resp(self._text)


def _facts():
    t = Trade(ticker="AMD", timeframe="1d", horizon="medium", entry_date=date(2026, 7, 1),
              entry_price=100.0, size=1.0, stop=95.0, target=110.0, status="closed",
              exit_price=110.0, exit_date=date(2026, 7, 5), exit_reason="target")
    return equity_review_facts(t)


def test_injected_client_prose_is_returned():
    out = draft_review(_facts(), client=_FakeClient(text="Nice target hit; size up next time."))
    assert out.text == "Nice target hit; size up next time."


def test_failing_client_degrades_to_template_with_no_usage():
    out = draft_review(_facts(), client=_FakeClient(raises=True))
    assert out.usage is None
    assert "AMD" in out.text and "target" in out.text          # deterministic template


def test_empty_reply_degrades_to_template():
    out = draft_review(_facts(), client=_FakeClient(text="   "))
    assert out.usage is None
    assert out.text.endswith(".")


def test_system_prompt_forbids_altering_numbers():
    assert "MUST NOT" in _COACH_SYSTEM
    assert "immutable ground truth" in _COACH_SYSTEM
