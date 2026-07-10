"""Settlement: the Forward Books state machine.

Arms are same-sample (judged by paired_arm_delta -- pairing on the fill identity);
variants are separate books (judged by the clustered two-sample delta, called twice for
both bounds). Applying one primitive to both kinds is statistically dishonest by the
repo's own rules; the card also refuses to settle on an untrustworthy (thin) bound.
"""

import math
from datetime import UTC, date, datetime, timedelta

import pytest

from swing_screener.analytics.performance import (
    clustered_two_sample_delta_low,
    paired_arm_delta,
    summarize,
)
from swing_screener.cockpit.settlement import BookLoader, build_cards
from swing_screener.db.models import PaperTrade
from swing_screener.pipeline.arms import BASELINE
from swing_screener.pipeline.registry import Experiment
from swing_screener.pipeline.variants import DEFAULT_VARIANT

NOW = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)


def _trade(
    ticker: str,
    r: float | None,
    *,
    play_type: str = "reversal",
    arm: str = BASELINE,
    variant: str = DEFAULT_VARIANT,
    trigger_ts: datetime | None = None,
    exit_date: date | None = None,
    would_surface: bool | None = True,
    status: str = "closed",
) -> PaperTrade:
    return PaperTrade(
        ticker=ticker, timeframe="1d", horizon="medium", signal_score=0.8, rank=1,
        play_type=play_type, arm=arm, variant=variant, trigger_ts=trigger_ts,
        would_surface=would_surface, fill_status="filled", stop=95.0, target=110.0,
        risk=5.0, status=status, realized_r=r, exit_date=exit_date,
    )


def _exp(
    name: str,
    *,
    kind: str,
    play_type: str = "reversal",
    mde_r: float = 0.10,
    target: float = 0.15,
    status: str = "active",
    decided_at: str | None = None,
    decision: str | None = None,
) -> Experiment:
    return Experiment(
        name=name, kind=kind, play_type=play_type,
        control=BASELINE if kind == "arm" else DEFAULT_VARIANT,
        hypothesis="h", stopping_rule="rule text verbatim", mde_r=mde_r,
        target_ci_halfwidth_r=target, registered_at="2026-07-10",
        registered_sha="abc123", doc_ref="docs/x.md", provenance="test",
        status=status, decided_at=decided_at, decision=decision,
    )


def _loader(trades: list[PaperTrade]) -> BookLoader:
    """In-memory stand-in for the DB loader: None means 'no filter on this key'."""
    def load(
        *, play_type: str | None, arm: str | None, variant: str
    ) -> list[PaperTrade]:
        return [
            t for t in trades
            if (play_type is None or t.play_type == play_type)
            and (arm is None or t.arm == arm)
            and t.variant == variant
        ]
    return load


def _by_ticker(trades: list[PaperTrade]) -> dict[str, list[float]]:
    """Closed realized R keyed by ticker -- the two-sample bootstrap's input shape."""
    d: dict[str, list[float]] = {}
    for t in trades:
        if t.status == "closed" and t.realized_r is not None:
            d.setdefault(t.ticker, []).append(t.realized_r)
    return d


def _paired_trades(
    deltas: list[tuple[str, float]],
    *,
    arm: str,
    exit_date: date | None = None,
) -> list[PaperTrade]:
    """One baseline leg (r=0) + one arm leg (r=delta) per entry, sharing a trigger_ts."""
    out: list[PaperTrade] = []
    for i, (ticker, d) in enumerate(deltas):
        ts = datetime(2026, 6, 1, tzinfo=UTC) + timedelta(hours=i)
        out.append(_trade(ticker, 0.0, arm=BASELINE, trigger_ts=ts, exit_date=exit_date))
        out.append(_trade(ticker, d, arm=arm, trigger_ts=ts, exit_date=exit_date))
    return out


def test_variant_card_uses_two_sample_bounds() -> None:
    """Variant deltas are two separate books: both CI bounds must come from the clustered
    two-sample bootstrap (never the paired primitive), and the card says so."""
    book_r = [0.8, -0.2, 0.5]
    ctrl_r = [0.1, -0.3, 0.4]
    trades: list[PaperTrade] = []
    for k in range(10):  # 10 tickers x 3 closes each = 30 per book, well past the floors
        for j in range(3):
            # The k-dependent term varies the per-ticker means, so resampling TICKERS
            # produces a real distribution (identical pools would collapse the interval).
            trades.append(_trade(f"T{k:02d}", book_r[j] + k * 0.04, variant="rev_tight"))
            trades.append(_trade(f"T{k:02d}", ctrl_r[j] - k * 0.03, variant=DEFAULT_VARIANT))
    exp = _exp("rev_tight", kind="variant")

    [card] = build_cards([exp], book_loader=_loader(trades), now=NOW)

    book = [t for t in trades if t.variant == "rev_tight"]
    ctrl = [t for t in trades if t.variant == DEFAULT_VARIANT]
    expected_low = clustered_two_sample_delta_low(
        _by_ticker(book), _by_ticker(ctrl), lower_pct=2.5
    )
    expected_high = clustered_two_sample_delta_low(
        _by_ticker(book), _by_ticker(ctrl), lower_pct=97.5
    )
    assert expected_low < expected_high  # the bootstrap really produced an interval
    assert card.upper_bound_type == "clustered"
    assert card.delta.ci_low == pytest.approx(expected_low)
    assert card.delta.ci_high == pytest.approx(expected_high)
    assert card.delta.value == pytest.approx(
        summarize(book).expectancy_r - summarize(ctrl).expectancy_r
    )
    assert card.n_accrued == 30           # the treatment book's closed count
    assert card.delta.n == 30
    assert card.delta.n_clusters == 10    # min(book, control) distinct tickers
    assert card.delta.thin_clusters is False
    assert card.book.n == 30
    assert card.control.n == 30


def test_arm_card_uses_paired_delta() -> None:
    """Arm deltas are same-sample: the card's numbers must be paired_arm_delta's, and
    n_accrued counts PAIRS where both legs closed -- smaller than either arm's n."""
    deltas = [(f"T{i % 10:02d}", 0.3 + (i % 5 - 2) * 0.05) for i in range(20)]
    trades = _paired_trades(deltas, arm="tight_stop")
    # Two baseline closes with no arm twin + one pair whose arm leg is still open:
    # realized on one side only, so none of these three can count as a pair.
    trades.append(_trade("T00", 0.9, trigger_ts=datetime(2026, 6, 20, tzinfo=UTC)))
    trades.append(_trade("T01", -0.4, trigger_ts=datetime(2026, 6, 21, tzinfo=UTC)))
    open_ts = datetime(2026, 6, 22, tzinfo=UTC)
    trades.append(_trade("T02", 0.2, trigger_ts=open_ts))
    trades.append(_trade("T02", None, arm="tight_stop", trigger_ts=open_ts, status="open"))
    exp = _exp("tight_stop", kind="arm")

    [card] = build_cards([exp], book_loader=_loader(trades), now=NOW)

    expected = paired_arm_delta(trades, arm="tight_stop")
    assert expected.n_pairs == 20
    assert card.n_accrued == expected.n_pairs
    assert card.upper_bound_type == "iid"
    assert card.delta.value == pytest.approx(expected.mean_delta)
    assert card.delta.ci_low == pytest.approx(expected.delta_ci_low)
    assert card.delta.ci_high == pytest.approx(expected.delta_ci_high)
    assert card.delta.n_clusters == expected.n_clusters
    assert card.delta.thin_clusters == expected.thin_clusters
    assert card.control.n == 23           # 20 paired + 2 unpaired + 1 twin of the open leg
    assert card.n_accrued < card.control.n


def test_states() -> None:
    """The four states, plus the two honesty rules: thin clusters never settle, and
    futility beats settled-by-width (both hold -> the decision-forcing one wins)."""
    tickers10 = [f"T{i % 10:02d}" for i in range(24)]
    jitter = [((i % 3) - 1) * 0.01 for i in range(24)]

    # Tight interval well above the MDE: settled-awaiting-decision.
    settled = _paired_trades(
        [(t, 0.5 + j) for t, j in zip(tickers10, jitter)], arm="a_settle"
    )
    [card] = build_cards(
        [_exp("a_settle", kind="arm")], book_loader=_loader(settled), now=NOW
    )
    assert card.state == "settled-awaiting-decision"

    # Tight interval whose UPPER bound sits below the MDE: futile. This book is ALSO
    # settled-by-width, so this asserts the precedence choice (futile > settled).
    futile = _paired_trades(
        [(t, 0.0 + j) for t, j in zip(tickers10, jitter)], arm="a_futile"
    )
    [card] = build_cards(
        [_exp("a_futile", kind="arm")], book_loader=_loader(futile), now=NOW
    )
    assert card.delta.ci_high < 0.10
    assert card.state == "futile-awaiting-decision"

    # n >= 20 but a wide interval (alternating +/-2R deltas): still accruing.
    wide = _paired_trades(
        [(t, 2.0 if i % 2 == 0 else -2.0) for i, t in enumerate(tickers10)], arm="a_wide"
    )
    [card] = build_cards([_exp("a_wide", kind="arm")], book_loader=_loader(wide), now=NOW)
    assert (card.delta.ci_high - card.delta.ci_low) / 2 > 0.15
    assert card.state == "accruing"

    # Registry says retired: state is retired regardless of the math, and the card
    # still renders (falsified history stays legible) with its decision text.
    retired = _paired_trades(
        [(t, 0.5 + j) for t, j in zip(tickers10, jitter)], arm="a_retired"
    )
    [card] = build_cards(
        [_exp("a_retired", kind="arm", status="retired",
              decided_at="2026-07-01", decision="falsified: no edge over baseline")],
        book_loader=_loader(retired), now=NOW,
    )
    assert card.state == "retired"
    assert card.decision == "falsified: no edge over baseline"

    # Tight interval, n >= 20, but only 2 tickers: thin clusters NEVER settle.
    thin = _paired_trades(
        [(f"T{i % 2:02d}", 0.5 + ((i % 3) - 1) * 0.01) for i in range(20)], arm="a_thin"
    )
    [card] = build_cards([_exp("a_thin", kind="arm")], book_loader=_loader(thin), now=NOW)
    assert card.delta.thin_clusters is True
    assert (card.delta.ci_high - card.delta.ci_low) / 2 <= 0.15
    assert card.state == "accruing"


def test_n_needed_scales_inverse_square() -> None:
    """n_needed follows the CI-shrinkage law (halfwidth ~ 1/sqrt(n)); the eta projects
    it from the trailing-30-day close rate. Unmeasurable cases are None, never 0."""
    tickers10 = [f"T{i % 10:02d}" for i in range(24)]
    wide = _paired_trades(
        [(t, 2.0 if i % 2 == 0 else -2.0) for i, t in enumerate(tickers10)],
        arm="a_wide", exit_date=date(2026, 7, 1),
    )
    [card] = build_cards([_exp("a_wide", kind="arm")], book_loader=_loader(wide), now=NOW)
    halfwidth = (card.delta.ci_high - card.delta.ci_low) / 2
    expected_n = math.ceil(card.n_accrued * (halfwidth / 0.15) ** 2)
    assert card.n_needed == expected_n
    assert card.n_needed is not None and card.n_needed > card.n_accrued
    # 24 arm closes, all inside the trailing 30 days -> rate 0.8/day.
    days = math.ceil((expected_n - 24) / (24 / 30))
    assert card.eta == (NOW.date() + timedelta(days=days)).isoformat()
    # All 24 closes share one exit date -> the spark collapses to a single point.
    assert card.spark == [("2026-07-01", pytest.approx(0.0))]

    # Fewer than 5 accrued: the shrinkage law has nothing to extrapolate from.
    tiny = _paired_trades([("T00", 0.4), ("T01", 0.1), ("T02", 0.7)], arm="a_tiny")
    [card] = build_cards([_exp("a_tiny", kind="arm")], book_loader=_loader(tiny), now=NOW)
    assert card.n_accrued == 3
    assert card.n_needed is None
    assert card.eta is None

    # Zero halfwidth (identical deltas collapse the interval): also None.
    flat = _paired_trades(
        [(f"T{i:02d}", 0.25) for i in range(8)], arm="a_flat",
        exit_date=date(2026, 7, 1),
    )
    [card] = build_cards([_exp("a_flat", kind="arm")], book_loader=_loader(flat), now=NOW)
    assert card.delta.ci_low == card.delta.ci_high
    assert card.n_needed is None
    assert card.eta is None

    # Tight interval but only 10 pairs: the width projection alone says ~1 close, yet
    # the settlement rule cannot fire below n >= 20 -- n_needed floors at the n gate.
    tight = _paired_trades(
        [(f"T{i:02d}", 0.5 + ((i % 3) - 1) * 0.01) for i in range(10)], arm="a_tight"
    )
    [card] = build_cards([_exp("a_tight", kind="arm")], book_loader=_loader(tight), now=NOW)
    assert card.n_accrued == 10
    halfwidth = (card.delta.ci_high - card.delta.ci_low) / 2
    assert 0 < halfwidth <= 0.15  # the width condition is already met...
    assert card.n_needed == 20    # ...so the n >= MIN_LEADERBOARD_N gate is what binds


def test_futility_needs_a_real_interval() -> None:
    """A >= 20-close book vs an EMPTY control collapses the bounds to the point
    estimate -- an interval no bootstrap produced. However far below the MDE the
    numbers sit, no verdict may be issued from an untrusted bound: still accruing."""
    trades = [
        _trade(f"T{i % 10:02d}", -0.5 + (i % 3 - 1) * 0.01, variant="rev_orphan")
        for i in range(20)
    ]
    exp = _exp("rev_orphan", kind="variant")  # mde_r=0.10; no 'default' rows exist

    [card] = build_cards([exp], book_loader=_loader(trades), now=NOW)

    assert card.n_accrued == 20
    assert card.delta.ci_high < exp.mde_r        # the numbers alone would read futile
    assert card.delta.ci_low == card.delta.ci_high  # ...but the interval is collapsed
    assert card.state == "accruing"


def test_thin_control_side_blocks_settlement() -> None:
    """The weaker-side rules: a two-sample delta is only as trustworthy as its thinner
    book. Book on 10 tickers, control on 2 -> thin blocks settlement even with a tight
    interval, and the delta's n_clusters reports min(book, control)."""
    trades: list[PaperTrade] = []
    for k in range(10):  # book: 10 tickers x 2 closes, per-ticker means tightly spread
        for _ in range(2):
            trades.append(_trade(f"T{k:02d}", 0.5 + k * 0.01, variant="rev_thinctl"))
    for k in range(2):   # control: only 2 tickers -- below the cluster floor
        for _ in range(2):
            trades.append(_trade(f"T{k:02d}", 0.1 + k * 0.01, variant=DEFAULT_VARIANT))
    exp = _exp("rev_thinctl", kind="variant")

    [card] = build_cards([exp], book_loader=_loader(trades), now=NOW)

    assert card.n_accrued == 20
    assert (card.delta.ci_high - card.delta.ci_low) / 2 <= 0.15  # width alone would settle
    assert card.delta.ci_high > exp.mde_r                        # and futility is not in play
    assert card.delta.thin_clusters is True
    assert card.delta.n_clusters == 2
    assert card.state == "accruing"


def test_gold_facet_filters_would_surface_truthy() -> None:
    """The gold facet is entries a human could actually have taken: would_surface must
    be truthy -- None (legacy/replay) and False rows are both excluded BEFORE any math."""
    trades: list[PaperTrade] = []
    for i in range(4):
        trades.append(_trade(f"T{i % 2:02d}", 0.5, variant="rev_gold", would_surface=True))
    for i in range(3):
        trades.append(_trade(f"T{i % 2:02d}", -1.0, variant="rev_gold", would_surface=False))
        trades.append(_trade(f"T{i % 2:02d}", -1.0, variant="rev_gold", would_surface=None))
    for i in range(5):
        trades.append(_trade(f"T{i % 2:02d}", 0.1, variant=DEFAULT_VARIANT, would_surface=True))
    for i in range(2):
        trades.append(_trade(f"T{i % 2:02d}", 5.0, variant=DEFAULT_VARIANT, would_surface=None))
    exp = _exp("rev_gold", kind="variant")

    [gold] = build_cards([exp], book_loader=_loader(trades), now=NOW, facet="gold")
    assert gold.book.n == 4
    assert gold.control.n == 5
    assert gold.n_accrued == 4
    assert gold.delta.facet == "gold"
    assert gold.book.facet == "gold"
    assert gold.delta.value == pytest.approx(0.5 - 0.1)

    [research] = build_cards([exp], book_loader=_loader(trades), now=NOW)
    assert research.book.n == 10
    assert research.control.n == 7
    assert research.delta.facet == "research"


def test_empty_book_still_emits_a_finite_card() -> None:
    """An empty book must not poison the wire with the two-sample bootstrap's -inf:
    the card still renders, accruing at n=0, with the interval collapsed to the point."""
    trades = [_trade(f"T{i:02d}", 0.1, variant=DEFAULT_VARIANT) for i in range(10)]
    exp = _exp("rev_unborn", kind="variant")

    [card] = build_cards([exp], book_loader=_loader(trades), now=NOW)

    assert card.state == "accruing"
    assert card.n_accrued == 0
    assert card.delta.n == 0
    assert math.isfinite(card.delta.value)
    assert math.isfinite(card.delta.ci_low)
    assert math.isfinite(card.delta.ci_high)
    assert card.n_needed is None
    assert card.eta is None
    assert card.spark == []
