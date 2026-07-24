"""Batch deep-analysis: the transport primitive (notify.batch.submit_and_poll) and
the batched conviction orchestrator (notify.analysis.analyze_convictions_batched).

All offline: a fake batches client drives the poll loop deterministically (injected
sleep/monotonic, no wall-clock), and a fake ``runner`` feeds canned result messages so
we assert the batch analog produces the SAME ConvictionResults -- and the SAME
deterministic fallback -- as the synchronous analyze_conviction.
"""

from types import SimpleNamespace

from swing_screener.notify.analysis import (
    ConvictionInput,
    SignalFacts,
    analyze_convictions_batched,
)
from swing_screener.notify.batch import submit_and_poll

# --- fakes -------------------------------------------------------------------

class _TextBlock:
    type = "text"

    def __init__(self, text):
        self.text = text


class _Resp:
    """A stand-in for a result message: text blocks, no usage (usage -> None)."""

    def __init__(self, text):
        self.content = [_TextBlock(text)]


def _result_item(custom_id, type_, message=None):
    return SimpleNamespace(custom_id=custom_id, result=SimpleNamespace(type=type_, message=message))


class _Batches:
    def __init__(self, statuses, results):
        self.statuses = list(statuses)
        self.results_data = results
        self.created = None
        self._i = 0

    def create(self, *, requests):
        self.created = requests
        return SimpleNamespace(id="batch_1")

    def retrieve(self, _bid):
        i = min(self._i, len(self.statuses) - 1)
        self._i += 1
        return SimpleNamespace(processing_status=self.statuses[i])

    def results(self, _bid):
        return iter(self.results_data)


class _FakeBatchClient:
    def __init__(self, statuses, results):
        self.messages = SimpleNamespace(batches=_Batches(statuses, results))


def _facts(ticker="AMD"):
    return SignalFacts(
        ticker=ticker, timeframe="1d", trade_type="medium", score=0.92, mtf_aligned=True,
        quality_tier="reputable", volatility_tier="high", oversold=False,
        trigger_close=100.0, atr=4.0, rsi=55.0, entry_floor=96.0, entry_ceiling=101.0,
        stop=95.0, target=110.0,
    )


# --- submit_and_poll ---------------------------------------------------------

def test_submit_and_poll_maps_succeeded_and_none_for_errored():
    client = _FakeBatchClient(
        statuses=["in_progress", "ended"],
        results=[
            _result_item("a", "succeeded", message=_Resp("ok")),
            _result_item("b", "errored"),
        ],
    )
    out = submit_and_poll(
        client, [("a", {"model": "m"}), ("b", {"model": "m"})],
        sleep=lambda _s: None, monotonic=lambda: 0.0,
    )
    assert isinstance(out["a"], _Resp)   # succeeded -> the message
    assert out["b"] is None              # errored -> None (caller falls back)
    # the batch was submitted with one request per (custom_id, kwargs) pair
    assert [r["custom_id"] for r in client.messages.batches.created] == ["a", "b"]
    assert client.messages.batches.created[0]["params"] == {"model": "m"}


def test_submit_and_poll_empty_makes_no_call():
    client = _FakeBatchClient(statuses=["ended"], results=[])
    assert submit_and_poll(client, []) == {}
    assert client.messages.batches.created is None


def test_submit_and_poll_times_out_to_all_none():
    client = _FakeBatchClient(statuses=["in_progress", "in_progress"], results=[])
    clock = iter([0.0, 9999.0])  # start, then past the deadline on the first check
    out = submit_and_poll(
        client, [("a", {"model": "m"})],
        max_wait_s=100.0, sleep=lambda _s: None, monotonic=lambda: next(clock),
    )
    assert out == {"a": None}   # never ended -> fall back for every id


# --- analyze_convictions_batched ---------------------------------------------

def test_batched_conviction_parses_hits_and_falls_back_on_none():
    captured = {}

    def fake_runner(_client, requests):
        captured["requests"] = requests
        return {
            "id-hit": _Resp("CONVICTION: high\nREASON: strong volume\nGreat continuation."),
            "id-miss": None,  # errored/expired/timed-out -> deterministic fallback
        }

    items = [
        ConvictionInput(custom_id="id-hit", facts=_facts("AMD"), baseline="medium",
                        playbook_text="pb", max_step=1),
        ConvictionInput(custom_id="id-miss", facts=_facts("NVDA"), baseline="low",
                        playbook_text="pb"),
    ]
    out = analyze_convictions_batched(
        items, client=object(), model="claude-sonnet-5", reasoning="medium",
        max_searches=2, runner=fake_runner,
    )

    hit = out["id-hit"]
    assert hit.is_deep is True
    assert hit.conviction == "high"          # medium +1 step, within the clamp
    assert "Great continuation." in hit.insight

    miss = out["id-miss"]
    assert miss.is_deep is False             # None response -> baseline fallback
    assert miss.conviction == "low"          # the untouched baseline
    assert "analyst unavailable" in miss.nudge_reason

    # each request carries the pick's custom_id and the web_search tool at max_uses=2
    reqs = dict(captured["requests"])
    assert set(reqs) == {"id-hit", "id-miss"}
    tool = reqs["id-hit"]["tools"][0]
    assert tool["name"] == "web_search" and tool["max_uses"] == 2
    assert reqs["id-hit"]["model"] == "claude-sonnet-5"


def test_batched_conviction_empty_items_no_call():
    called = []
    out = analyze_convictions_batched([], client=object(), runner=lambda *a: called.append(a) or {})
    assert out == {}
    assert called == []   # empty items -> no batch submitted


def test_capture_usage_applies_batch_discount():
    """A batched response is priced at the Batches API's 50% TOKEN rate; the per-call
    web-search server-tool fee is not discounted (here: zero searches)."""
    from swing_screener.notify.analysis import _capture_usage

    usage = SimpleNamespace(input_tokens=1_000_000, output_tokens=1_000_000)  # 0 web searches
    sync = _capture_usage(SimpleNamespace(usage=usage), "claude-sonnet-5")
    batch = _capture_usage(SimpleNamespace(usage=usage), "claude-sonnet-5", is_batch=True)
    assert sync is not None and batch is not None
    assert sync.est_cost_usd == 18.0    # $3/MTok in + $15/MTok out, 1 MTok each
    assert batch.est_cost_usd == 9.0    # tokens billed at half under the Batch API
