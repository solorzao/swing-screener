"""Account isolation: the curated intent book is fenced off from the research grid.

Phase 3 adds an ``account`` dimension to ``PaperTrade``. The research grid (the shadow
book that auto-books every screened signal x arm x variant) defaults to
``account == "research"``; a future curated "intent" book paper-executes OrderIntents
under ``account == "paper"``. Without isolation a paper book would inflate the
leaderboards and the analyst calibration.

These tests pin the CLOSED-trade research aggregates to ``account == "research"`` while
keeping OPEN-trade stepping inclusive (advance_open must keep advancing paper trades):
  * ``load_closed_paper_trades`` -> research only
  * ``load_research_paper_trades`` (the performance/leaderboard loader) -> research only
  * ``score_analyst_calls`` attribution -> research only
  * ``load_open_paper_trades`` -> BOTH books (open paper trades still get stepped)
The default ``research`` preserves all current behavior (covered by the existing suites).
"""

from datetime import date

from sqlalchemy.orm import Session

from swing_screener.db import repo
from swing_screener.db.models import AnalystCall, PaperTrade
from swing_screener.db.session import get_engine
from swing_screener.pipeline.arms import BASELINE
from swing_screener.pipeline.variants import DEFAULT_VARIANT


def _pt(*, ticker="AAPL", account="research", play_type="continuation", arm=BASELINE,
        variant=DEFAULT_VARIANT, status="closed", fill_status="filled", realized_r=1.0,
        opened_date=date(2026, 6, 20), exit_date=date(2026, 6, 25)) -> PaperTrade:
    return PaperTrade(
        ticker=ticker, timeframe="1d", horizon="medium", signal_score=0.8, rank=1,
        play_type=play_type, account=account, arm=arm, variant=variant,
        fill_status=fill_status, stop=95.0, target=110.0, risk=5.0, status=status,
        realized_r=realized_r, opened_date=opened_date, exit_date=exit_date,
    )


def test_account_defaults_to_research() -> None:
    """An unspecified account backfills to 'research' (additive, behavior-preserving)."""
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        s.add(PaperTrade(
            ticker="AAPL", timeframe="1d", horizon="medium", signal_score=0.8, rank=1,
            fill_status="filled", stop=95.0, target=110.0, risk=5.0, status="closed",
            realized_r=1.0,
        ))
        s.commit()
        assert s.query(PaperTrade).one().account == "research"


def test_load_closed_excludes_paper_account() -> None:
    """An identical closed trade under account='paper' is excluded; 'research' is kept."""
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        repo.save_paper_trades(s, [
            _pt(ticker="RESEARCH", account="research"),
            _pt(ticker="PAPER", account="paper"),
        ])
        got = repo.load_closed_paper_trades(s)
        assert [t.ticker for t in got] == ["RESEARCH"]
        # the facet slice (live forward book) is also research-only
        faceted = repo.load_closed_paper_trades(
            s, play_type="continuation", arm=BASELINE, variant=DEFAULT_VARIANT)
        assert [t.ticker for t in faceted] == ["RESEARCH"]


def test_research_loader_excludes_paper_account() -> None:
    """The performance/leaderboard loader returns the research book only."""
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        repo.save_paper_trades(s, [
            _pt(ticker="RESEARCH", account="research"),
            _pt(ticker="PAPER", account="paper"),
            # open trades belong to the research book here too; still research-only
            _pt(ticker="OPENRES", account="research", status="open", realized_r=None,
                exit_date=None),
            _pt(ticker="OPENPAPER", account="paper", status="open", realized_r=None,
                exit_date=None),
        ])
        got = {t.ticker for t in repo.load_research_paper_trades(s)}
        assert got == {"RESEARCH", "OPENRES"}


def test_score_analyst_calls_attributes_research_only() -> None:
    """A call is graded by the RESEARCH baseline/default fill, never a paper one."""
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        s.add(AnalystCall(
            created_date=date(2026, 6, 19), ticker="AMD", timeframe="1d",
            play_type="continuation", run_date=date(2026, 6, 19),
            baseline_conviction="medium", final_conviction="high", nudge_reason="x",
            model="claude-opus-4-8",
        ))
        # a paper-account fill that opened EARLIER must NOT be picked as the attribution
        s.add(_pt(ticker="AMD", account="paper", realized_r=9.9,
                  opened_date=date(2026, 6, 20), exit_date=date(2026, 6, 22)))
        s.add(_pt(ticker="AMD", account="research", realized_r=1.5,
                  opened_date=date(2026, 6, 21), exit_date=date(2026, 6, 25)))
        s.commit()

        assert repo.score_analyst_calls(s) == 1
        call = s.query(AnalystCall).one()
        assert call.realized_r == 1.5  # the research fill, not the paper 9.9
        assert call.scored_at == date(2026, 6, 25)


def test_score_analyst_calls_no_research_fill_stays_unscored() -> None:
    """A pick whose only post-call fill is a paper trade stays unscored."""
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        s.add(AnalystCall(
            created_date=date(2026, 6, 19), ticker="AMD", timeframe="1d",
            play_type="continuation", run_date=date(2026, 6, 19),
            baseline_conviction="medium", final_conviction="high", nudge_reason="x",
            model="claude-opus-4-8",
        ))
        s.add(_pt(ticker="AMD", account="paper", realized_r=2.0))
        s.commit()
        assert repo.score_analyst_calls(s) == 0
        assert s.query(AnalystCall).one().realized_r is None


def test_load_open_includes_both_accounts() -> None:
    """advance_open must keep stepping OPEN paper trades -> both accounts come back."""
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        repo.save_paper_trades(s, [
            _pt(ticker="OPENRES", account="research", status="open", realized_r=None,
                exit_date=None),
            _pt(ticker="OPENPAPER", account="paper", status="open", realized_r=None,
                exit_date=None),
        ])
        got = {t.ticker for t in repo.load_open_paper_trades(s)}
        assert got == {"OPENRES", "OPENPAPER"}


def test_load_open_default_includes_live() -> None:
    """The default (inclusive) loader returns the live row too -- reconcile uses it."""
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        repo.save_paper_trades(s, [
            _pt(ticker="OPENRES", account="research", status="open", realized_r=None,
                exit_date=None),
            _pt(ticker="OPENLIVE", account="live", status="open", realized_r=None,
                exit_date=None),
        ])
        got = {t.ticker for t in repo.load_open_paper_trades(s)}
        assert got == {"OPENRES", "OPENLIVE"}


def test_load_open_exclude_live_drops_the_live_row() -> None:
    """The STEPPING loader (exclude_live=True) fences off the broker-owned live book:
    research + paper still step; the live row -- reconcile's exclusively -- is dropped."""
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        repo.save_paper_trades(s, [
            _pt(ticker="OPENRES", account="research", status="open", realized_r=None,
                exit_date=None),
            _pt(ticker="OPENPAPER", account="paper", status="open", realized_r=None,
                exit_date=None),
            _pt(ticker="OPENLIVE", account="live", status="open", realized_r=None,
                exit_date=None),
        ])
        got = {t.ticker for t in repo.load_open_paper_trades(s, exclude_live=True)}
        assert got == {"OPENRES", "OPENPAPER"}


def test_load_open_live_trades_returns_live_only() -> None:
    """reconcile_live's own loader: OPEN live rows only (research/paper excluded)."""
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        repo.save_paper_trades(s, [
            _pt(ticker="OPENRES", account="research", status="open", realized_r=None,
                exit_date=None),
            _pt(ticker="OPENLIVE", account="live", status="open", realized_r=None,
                exit_date=None),
            # a CLOSED live row must NOT come back (only OPEN live trades are reconciled)
            _pt(ticker="CLOSEDLIVE", account="live", status="closed"),
        ])
        got = {t.ticker for t in repo.load_open_live_trades(s)}
        assert got == {"OPENLIVE"}
