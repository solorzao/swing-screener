"""Task 3 contract: the cross-book scoreboard aggregation (``cockpit.scoreboard``).

Pure, session-taking read. The one sanctioned cross-book aggregate is the COMBINED
real-money pool (``manual_equity`` + ``live``, both R); robinhood is ``$``-only and
never enters an R pool; the ``paper`` card is the curated gold/baseline/default slice.
"""

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from swing_screener.cockpit.scoreboard import build_scoreboard
from swing_screener.db.models import OptionPaperTrade, PaperTrade, Trade
from swing_screener.db.session import get_engine


@pytest.fixture
def session():
    """A fresh in-memory DB session (StaticPool shares it across the engine)."""
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        yield s


def _card(board: dict, book: str) -> dict:
    return next(c for c in board["cards"] if c["book"] == book)


def _add_manual_trade(
    session: Session,
    ticker: str,
    *,
    entry: float,
    stop: float,
    exit: float,
    size: float,
    exit_date: date = date(2026, 1, 10),
) -> None:
    """One closed manual-equity ``Trade`` (the whole table IS the manual book)."""
    target = entry + 2 * (entry - stop) if entry > stop else entry + 1.0
    session.add(Trade(
        ticker=ticker, timeframe="1d", horizon="medium",
        entry_date=exit_date - timedelta(days=5), entry_price=entry, size=size,
        stop=stop, target=target, status="closed", exit_date=exit_date, exit_price=exit,
    ))
    session.commit()


def _add_robinhood_episode(
    session: Session,
    underlying: str,
    *,
    premium_pnl: float,
    closed_at: datetime = datetime(2026, 1, 10, 15, 0, tzinfo=UTC),
) -> None:
    """One imported flat-to-flat Robinhood option episode (``$``, never R)."""
    session.add(OptionPaperTrade(
        account="robinhood", underlying=underlying, direction="long",
        opened_at=closed_at - timedelta(days=1), closed_at=closed_at,
        status="closed", premium_pnl=premium_pnl,
    ))
    session.commit()


def _add_paper_trade(
    session: Session,
    ticker: str,
    r: float,
    *,
    account: str = "paper",
    would_surface: bool | None = None,  # PROD-FAITHFUL: the paper adapter never stamps it
    arm: str = "baseline",
    variant: str = "default",
    exit_date: date = date(2026, 1, 10),
) -> None:
    session.add(PaperTrade(
        ticker=ticker, timeframe="1d", horizon="medium", signal_score=0.8, rank=1,
        account=account, fill_status="filled", stop=95.0, target=110.0, risk=5.0,
        status="closed", realized_r=r, exit_date=exit_date, opened_date=exit_date,
        would_surface=would_surface, arm=arm, variant=variant,
    ))
    session.commit()


def test_combined_equals_manual_when_live_empty(session):
    _add_manual_trade(session, "AAA", entry=10, stop=9, exit=12, size=100)  # +2R, +$200
    board = build_scoreboard(session, window="all")
    manual = _card(board, "manual_equity")
    assert board["combined"]["n_closed"] == manual["n_closed"]
    assert board["combined"]["expectancy"]["value"] == manual["expectancy"]["value"]
    assert board["combined"]["realized_usd"] == manual["realized_usd"]


def test_robinhood_card_is_dollars_never_r(session):
    _add_robinhood_episode(session, "SPY", premium_pnl=130.0)   # win
    _add_robinhood_episode(session, "QQQ", premium_pnl=-40.0)   # loss
    card = _card(build_scoreboard(session, window="all"), "robinhood")
    assert card["unit"] == "$"
    assert card["expectancy"] is None
    assert card["n_wins"] == 1 and card["n_losses"] == 1
    assert card["realized_usd"] == 90.0


def test_degenerate_r_drops_from_expectancy_keeps_dollars(session):
    _add_manual_trade(session, "AAA", entry=10, stop=10, exit=12, size=100)  # risk=0 -> R None
    card = _card(build_scoreboard(session, window="all"), "manual_equity")
    assert card["n_closed"] == 0            # no R result
    assert card["realized_usd"] == 200.0    # dollars still count


def test_combined_merges_live_into_manual(session):
    _add_manual_trade(session, "AAA", entry=10, stop=9, exit=12, size=100)  # +2R
    _add_paper_trade(session, "BBB", 1.0, account="live")                   # +1R live
    board = build_scoreboard(session, window="all")
    manual = _card(board, "manual_equity")
    live = _card(board, "live")
    assert manual["n_closed"] == 1 and live["n_closed"] == 1
    # the one sanctioned cross-book pool: manual R + live R
    assert board["combined"]["n_closed"] == 2
    assert board["combined"]["expectancy"]["value"] == pytest.approx(1.5)
    assert board["combined"]["books"] == ["manual_equity", "live"]


def test_combined_concatenates_same_ticker_into_one_cluster(session):
    # SAME ticker in both books must CONCATENATE into one cluster, not overwrite:
    # AAA [2R (manual), 1R (live)] -> n_closed=2, mean 1.5, a single ticker-cluster.
    _add_manual_trade(session, "AAA", entry=10, stop=9, exit=12, size=100)  # AAA +2R
    _add_paper_trade(session, "AAA", 1.0, account="live")                   # AAA +1R
    combined = build_scoreboard(session, window="all")["combined"]
    assert combined["n_closed"] == 2
    assert combined["expectancy"]["value"] == pytest.approx(1.5)
    assert combined["expectancy"]["n_clusters"] == 1   # one ticker, concatenated


def test_profit_factor_all_winners_is_none(session):
    # No losses -> PF is +inf; JSON has no Infinity, so a served PF nulls (named rule).
    _add_manual_trade(session, "AAA", entry=10, stop=9, exit=12, size=100)  # +2R win
    _add_manual_trade(session, "BBB", entry=10, stop=9, exit=13, size=100)  # +3R win
    card = _card(build_scoreboard(session, window="all"), "manual_equity")
    assert card["n_closed"] == 2
    assert card["profit_factor"] is None


def test_combined_equity_curve_merges_and_resorts(session):
    _add_manual_trade(session, "AAA", entry=10, stop=9, exit=12, size=100,
                      exit_date=date(2026, 1, 5))                    # +2R, closes 1/5
    _add_paper_trade(session, "BBB", 1.0, account="live",
                     exit_date=date(2026, 1, 3))                     # +1R, closes 1/3
    combined = build_scoreboard(session, window="all")["combined"]
    assert set(combined) == {
        "books", "unit", "expectancy", "win_rate", "n_wins", "n_losses",
        "n_closed", "profit_factor", "realized_usd", "equity_r",
    }
    # merged across books, re-sorted by close date: live (1/3) before manual (1/5)
    assert combined["equity_r"] == [["2026-01-03", 1.0], ["2026-01-05", 3.0]]


def test_live_realized_usd_is_deferred_none(session):
    _add_paper_trade(session, "BBB", 1.0, account="live")
    card = _card(build_scoreboard(session, window="all"), "live")
    assert card["realized_usd"] is None       # ExecutionLog share-join deferred
    assert card["n_closed"] == 1


def test_paper_card_populates_without_would_surface(session):
    # Regression guard: the paper adapter (pipeline/execution.py PaperAdapter._open)
    # never stamps would_surface, so every prod paper row is would_surface=None. There
    # is NO gold gate on the intent book -- it must still populate. Only arm/variant scope.
    _add_paper_trade(session, "AAA", 2.0)                             # would_surface=None, counts
    _add_paper_trade(session, "DDD", 3.0, arm="partial33_cond")       # wrong arm, excluded
    _add_paper_trade(session, "EEE", 3.0, variant="tighter")        # wrong variant, excluded
    card = _card(build_scoreboard(session, window="all"), "paper")
    assert card["n_closed"] == 1
    assert card["expectancy"]["value"] == pytest.approx(2.0)


def test_window_cuts_by_close_date(session):
    old = datetime.now(UTC).date() - timedelta(days=400)
    recent = datetime.now(UTC).date() - timedelta(days=10)
    _add_manual_trade(session, "AAA", entry=10, stop=9, exit=12, size=100, exit_date=old)
    _add_manual_trade(session, "BBB", entry=10, stop=9, exit=11, size=100, exit_date=recent)
    all_card = _card(build_scoreboard(session, window="all"), "manual_equity")
    win_card = _card(build_scoreboard(session, window="90"), "manual_equity")
    assert all_card["n_closed"] == 2
    assert win_card["n_closed"] == 1       # the 400-day-old close drops


def test_shape_and_order_with_empty_books(session):
    board = build_scoreboard(session, window="all")
    assert [c["book"] for c in board["cards"]] == [
        "manual_equity", "robinhood", "live", "paper"
    ]
    card_keys = {"book", "unit", "expectancy", "win_rate", "n_wins", "n_losses",
                 "n_closed", "profit_factor", "realized_usd", "equity_r"}
    for c in board["cards"]:
        assert set(c) == card_keys
        assert c["n_closed"] == 0
        assert c["expectancy"] is None          # empty book -> honest-empty
        assert c["equity_r"] is None
    combined = board["combined"]
    assert combined["books"] == ["manual_equity", "live"]
    assert combined["unit"] == "R"
    assert combined["expectancy"] is None
    assert combined["realized_usd"] == 0.0
