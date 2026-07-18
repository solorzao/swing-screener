"""/api/stats/performance (books router; split from test_api.py): Streamlit
Screener Performance parity -- KPIs, leaderboard, arms A/B, breakdowns."""

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient
import pytest
from sqlalchemy.orm import Session

from swing_screener.analytics.performance import COST_STAMPED_FROM, SCORE_STAMPED_FROM
from swing_screener.cockpit.api import create_app
from swing_screener.db.models import (
    PaperTrade,
)
from swing_screener.db.session import get_engine
from tests.cockpit.conftest import (
    _client,
    _db_url,
    STAT_KEYS,
)


# --- /api/stats/performance (Streamlit Screener Performance parity) -----------------

PERF_KEYS = {"kpis", "leaderboard", "arms", "breakdowns", "equity_curve"}
BREAKDOWN_KEYS = {"timeframe", "rank", "score", "market_trend", "market_vol"}
KPI_KEYS = {"expectancy", "win_rate", "fill_rate", "profit_factor", "n_closed"}
LEADERBOARD_ROW_KEYS = {"variant", "stat", "win_rate", "fill_rate", "n_total", "flag"}
ARM_ROW_KEYS = {"arm", "stat", "n_pairs", "delta"}
BREAKDOWN_ROW_KEYS = {"key", "stat", "win_rate", "n_closed"}


def _perf_trade(
    ticker: str, r: float | None = None, *, play_type: str = "continuation",
    arm: str = "baseline", variant: str = "default", timeframe: str = "1d",
    rank: int = 1, score: float = 0.8, fill_status: str = "filled",
    status: str = "closed", opened_date: date | None = None,
    exit_date: date | None = None, trigger_ts: datetime | None = None,
    market_trend: str | None = None, market_vol: str | None = None,
    would_surface: bool | None = None,
) -> PaperTrade:
    """A research-grid row shaped for the performance endpoint: variant/arm/opened_date
    are the leaderboard's and window's filter keys, trigger_ts the arm-pair identity."""
    return PaperTrade(
        ticker=ticker, timeframe=timeframe, horizon="medium", signal_score=score,
        rank=rank, account="research", play_type=play_type, arm=arm, variant=variant,
        trigger_ts=trigger_ts, would_surface=would_surface, market_trend=market_trend,
        market_vol=market_vol, fill_status=fill_status, stop=95.0, target=110.0,
        risk=5.0, status=status, realized_r=r, opened_date=opened_date,
        exit_date=exit_date, hold_bars=3 if status == "closed" else None,
    )


def _seed(url: str, trades: list[PaperTrade]) -> None:
    engine = get_engine(url)
    with Session(engine) as s:
        s.add_all(trades)
        s.commit()


def test_performance_kpis_breakdowns_and_equity_curve(tmp_path: Path) -> None:
    """Port of dashboard test_performance_renders_kpis_and_altair_charts: the KPI strip,
    every breakdown, and the equity curve as one JSON shape -- every aggregate a 10-key
    Stat, and every key present even when its section is degenerate (single variant /
    single arm / regime unknown everywhere)."""
    url = _db_url(tmp_path)
    _seed(url, [
        _perf_trade("AMD", 2.0, rank=1, score=0.9, exit_date=date(2026, 1, 5)),
        _perf_trade("NVDA", -1.0, rank=7, score=0.8, exit_date=date(2026, 1, 6)),
        _perf_trade("MSFT", 1.5, rank=12, score=0.7, timeframe="1w",
                    exit_date=date(2026, 1, 7)),
    ])
    r = TestClient(create_app(url, edge_dir=tmp_path)).get("/api/stats/performance")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == PERF_KEYS

    kpis = body["kpis"]
    assert set(kpis) == KPI_KEYS
    assert set(kpis["expectancy"]) == STAT_KEYS
    assert kpis["expectancy"]["value"] == pytest.approx(2.5 / 3)
    assert kpis["expectancy"]["facet"] == "research"
    assert kpis["expectancy"]["corpus_id"] is None
    assert kpis["win_rate"] == pytest.approx(2 / 3)
    assert kpis["fill_rate"] == 1.0
    assert kpis["profit_factor"] == pytest.approx(3.5)
    assert kpis["n_closed"] == 3

    assert set(body["breakdowns"]) == BREAKDOWN_KEYS
    by_tf = {row["key"]: row for row in body["breakdowns"]["timeframe"]}
    assert set(by_tf) == {"1d", "1w"}
    assert set(by_tf["1d"]) == BREAKDOWN_ROW_KEYS
    assert set(by_tf["1d"]["stat"]) == STAT_KEYS
    assert by_tf["1d"]["n_closed"] == 2 and by_tf["1d"]["win_rate"] == 0.5
    # Rank buckets keep their natural order and INCLUDE empty buckets (page parity:
    # rank_bucket emits every label) -- only score omits empties.
    assert [row["key"] for row in body["breakdowns"]["rank"]] == ["1-5", "6-10", "11+"]
    assert [row["key"] for row in body["breakdowns"]["score"]] == ["0.70-0.80", "0.80-1.00"]
    assert body["breakdowns"]["market_trend"] == []  # regime unknown on every row

    assert body["equity_curve"] == [
        ["2026-01-05", 2.0], ["2026-01-06", 1.0], ["2026-01-07", 2.5]]
    # Degenerate sections still return their data -- the frontend decides rendering.
    assert [row["variant"] for row in body["leaderboard"]] == ["default"]
    assert [row["arm"] for row in body["arms"]] == ["baseline"]


def test_performance_leaderboard_ranks_trusted_above_thin(tmp_path: Path) -> None:
    """Port of dashboard test_leaderboard_ranks_trusted_above_thin_lucky_sample: the
    deep +0.5R variant (n=25) outranks the lone lucky +3R trade despite the lower
    expectancy, and the 1-ticker variant is flagged 'iid' (no clustered bound)."""
    url = _db_url(tmp_path)
    trades = [_perf_trade(f"T{i}", 0.5, variant="deep", exit_date=date(2026, 1, 5))
              for i in range(25)]
    trades.append(_perf_trade("LUCK", 3.0, variant="thinlucky",
                              exit_date=date(2026, 1, 5)))
    _seed(url, trades)
    board = TestClient(create_app(url, edge_dir=tmp_path)).get(
        "/api/stats/performance").json()["leaderboard"]
    assert all(set(row) == LEADERBOARD_ROW_KEYS for row in board)
    order = [row["variant"] for row in board]
    assert order.index("deep") < order.index("thinlucky")
    flags = {row["variant"]: row["flag"] for row in board}
    assert flags == {"deep": "ok", "thinlucky": "iid"}


def test_performance_leaderboard_flags_iid_fallback_for_thin_clusters(
    tmp_path: Path,
) -> None:
    """Port of dashboard test_leaderboard_flags_iid_fallback_for_thin_clusters: a
    single-ticker variant's bound is the IID fallback ('iid'); an 8-ticker variant
    clusters fine and reads 'thin' (n<20) -- the cluster counts ride on the Stat."""
    url = _db_url(tmp_path)
    trades = [_perf_trade(f"T{i}", 0.5, variant="broad", exit_date=date(2026, 1, 5))
              for i in range(8)]
    trades += [_perf_trade("SOLO", r, variant="narrow", exit_date=date(2026, 1, 6))
               for r in (1.0, 1.2)]
    _seed(url, trades)
    board = TestClient(create_app(url, edge_dir=tmp_path)).get(
        "/api/stats/performance").json()["leaderboard"]
    rows = {row["variant"]: row for row in board}
    assert rows["broad"]["stat"]["n_clusters"] == 8
    assert rows["narrow"]["stat"]["n_clusters"] == 1
    assert rows["narrow"]["flag"] == "iid" and rows["broad"]["flag"] == "thin"


def test_performance_leaderboard_surfaces_fill_rate_and_total(tmp_path: Path) -> None:
    """Port of dashboard test_variant_leaderboard_surfaces_fill_rate_and_total: a
    variant that 'wins' by rarely filling must show it where the ranking is read."""
    url = _db_url(tmp_path)
    trades = [_perf_trade("AMD", 2.0, variant="picky", exit_date=date(2026, 1, 5))]
    trades += [_perf_trade(f"N{i}", None, variant="picky", fill_status="pending",
                           status="open") for i in range(3)]
    trades += [_perf_trade("MSFT", r, exit_date=date(2026, 1, 6)) for r in (1.0, 0.5)]
    _seed(url, trades)
    board = TestClient(create_app(url, edge_dir=tmp_path)).get(
        "/api/stats/performance").json()["leaderboard"]
    rows = {row["variant"]: row for row in board}
    assert rows["picky"]["fill_rate"] == 0.25 and rows["picky"]["n_total"] == 4
    assert rows["default"]["fill_rate"] == 1.0 and rows["default"]["n_total"] == 2


def test_performance_arms_ab_with_paired_delta(tmp_path: Path) -> None:
    """Port of dashboard test_performance_shows_per_arm_ab_when_multiple_arms (+ the
    arm-table scenario): per-arm rows with the spec'd wire shape, KPIs pinned to the
    page's default arm-detail selection (BASELINE), and -- the deviation that IS the
    spec -- each non-baseline arm carries a paired delta Stat built like settlement's
    arm branch. The baseline row's delta is null (no self-delta), n_pairs 0."""
    url = _db_url(tmp_path)
    t0 = datetime(2026, 1, 2, 15, 0, tzinfo=UTC)
    exit_d = COST_STAMPED_FROM + timedelta(days=1)  # post-cutoff: both sides net @0.05
    _seed(url, [
        _perf_trade("AMD", 1.0, trigger_ts=t0, exit_date=exit_d),
        _perf_trade("AMD", 1.6, arm="partial33_cond", trigger_ts=t0, exit_date=exit_d),
    ])
    body = TestClient(create_app(url, edge_dir=tmp_path)).get(
        "/api/stats/performance").json()
    arms = {row["arm"]: row for row in body["arms"]}
    assert set(arms) == {"baseline", "partial33_cond"}
    for row in arms.values():
        assert set(row) == ARM_ROW_KEYS
        assert set(row["stat"]) == STAT_KEYS
    assert arms["baseline"]["stat"]["value"] == pytest.approx(1.0)
    assert arms["baseline"]["delta"] is None and arms["baseline"]["n_pairs"] == 0
    partial = arms["partial33_cond"]
    assert partial["stat"]["value"] == pytest.approx(1.6)
    assert partial["n_pairs"] == 1
    delta = partial["delta"]
    assert set(delta) == STAT_KEYS
    assert delta["value"] == pytest.approx(0.6)
    assert delta["n"] == 1
    assert delta["ci_low"] == pytest.approx(0.6)  # 1 pair: interval collapses to point
    assert delta["ci_high"] == pytest.approx(0.6)
    assert delta["thin_clusters"] is True and delta["unit"] == "R"
    assert delta["cost_level"] == "0.05"  # cost_level_for over the POOLED book (both sides)
    # KPIs read the page's default arm-detail selection: the BASELINE arm.
    assert body["kpis"]["expectancy"]["value"] == pytest.approx(1.0)
    assert body["kpis"]["n_closed"] == 1


def test_performance_arm_pin_falls_back_alphabetically_without_baseline(
    tmp_path: Path,
) -> None:
    """A multi-arm book WITHOUT a baseline arm pins the KPI/breakdown subset to the
    first arm alphabetically -- the page's radio default (index 0 when BASELINE is
    absent). Deltas vs the missing baseline honestly carry zero pairs."""
    url = _db_url(tmp_path)
    _seed(url, [
        _perf_trade("AMD", 1.6, arm="partial33_cond", exit_date=date(2026, 1, 5)),
        _perf_trade("AMD", 0.9, arm="flip_only", exit_date=date(2026, 1, 5)),
    ])
    body = TestClient(create_app(url, edge_dir=tmp_path)).get(
        "/api/stats/performance").json()
    assert [row["arm"] for row in body["arms"]] == ["flip_only", "partial33_cond"]
    assert body["kpis"]["expectancy"]["value"] == pytest.approx(0.9)  # flip_only pins
    assert body["kpis"]["n_closed"] == 1
    assert all(row["n_pairs"] == 0 for row in body["arms"])  # no baseline: no pairs


def test_performance_play_type_filter_scopes_the_arm_ab(tmp_path: Path) -> None:
    """Port of dashboard test_performance_play_type_filter_scopes_the_arm_ab: the
    play_type param scopes EVERYTHING (the arms span both engines); unknown enum
    values are FastAPI's 422, never a silent 'all'."""
    url = _db_url(tmp_path)
    rows = [("continuation", "baseline", 1.0), ("continuation", "partial33_cond", 1.5),
            ("reversal", "baseline", -0.5), ("reversal", "partial33_cond", 0.5)]
    _seed(url, [_perf_trade("AMD", r, play_type=p, arm=a, exit_date=date(2026, 1, 5))
                for p, a, r in rows])
    client = TestClient(create_app(url, edge_dir=tmp_path))

    def _expectancy(query: str) -> float:
        body = client.get(f"/api/stats/performance{query}").json()
        value = body["kpis"]["expectancy"]["value"]
        assert isinstance(value, float)
        return value

    assert _expectancy("") == pytest.approx(0.25)  # baseline arm across both engines
    assert _expectancy("?play_type=reversal") == pytest.approx(-0.5)
    assert _expectancy("?play_type=continuation") == pytest.approx(1.0)
    assert client.get("/api/stats/performance?play_type=bogus").status_code == 422
    assert client.get("/api/stats/performance?window=45").status_code == 422


def test_performance_scopes_arms_to_default_variant(tmp_path: Path) -> None:
    """The arm A/B and every downstream KPI/breakdown slice to variant ==
    DEFAULT_VARIANT -- the A/B is only honest within one screen variant. The
    leaderboard (computed upstream of the scoping) still sees every variant."""
    url = _db_url(tmp_path)
    t0 = datetime(2026, 1, 2, tzinfo=UTC)
    t1 = datetime(2026, 1, 3, tzinfo=UTC)
    _seed(url, [
        _perf_trade("AMD", 1.0, trigger_ts=t0, exit_date=date(2026, 1, 5)),
        _perf_trade("AMD", 1.6, arm="partial33_cond", trigger_ts=t0,
                    exit_date=date(2026, 1, 5)),
        _perf_trade("NVDA", 5.0, variant="aggressive", trigger_ts=t1,
                    exit_date=date(2026, 1, 6)),
        _perf_trade("NVDA", 9.0, arm="partial33_cond", variant="aggressive",
                    trigger_ts=t1, exit_date=date(2026, 1, 6)),
    ])
    body = TestClient(create_app(url, edge_dir=tmp_path)).get(
        "/api/stats/performance").json()
    assert {row["variant"] for row in body["leaderboard"]} == {"default", "aggressive"}
    arms = {row["arm"]: row for row in body["arms"]}
    assert arms["baseline"]["stat"]["n"] == 1
    assert arms["baseline"]["stat"]["value"] == pytest.approx(1.0)  # never blends the 5.0
    assert arms["partial33_cond"]["n_pairs"] == 1  # the aggressive pair must not count
    assert arms["partial33_cond"]["delta"]["value"] == pytest.approx(0.6)  # not 2.3
    assert body["kpis"]["expectancy"]["value"] == pytest.approx(1.0)
    tf = {row["key"]: row for row in body["breakdowns"]["timeframe"]}
    assert tf["1d"]["n_closed"] == 1


def test_performance_window_cuts_after_arm_filter(tmp_path: Path) -> None:
    """The trailing window cuts the arm==BASELINE subset on opened_date: undated rows
    drop from windowed views but stay in 'all', non-baseline arms never enter the
    leaderboard at any window, and the window scopes ONLY the leaderboard (KPIs,
    breakdowns, and the equity curve are windowless, mirroring the page)."""
    url = _db_url(tmp_path)
    today = date.today()
    _seed(url, [
        _perf_trade("AAA", 1.0, opened_date=today - timedelta(days=10),
                    exit_date=today - timedelta(days=5)),
        _perf_trade("BBB", -1.0, opened_date=today - timedelta(days=200),
                    exit_date=today - timedelta(days=195)),
        _perf_trade("CCC", 0.5, exit_date=today - timedelta(days=3)),  # opened_date None
        _perf_trade("DDD", 2.0, arm="partial33_cond",
                    opened_date=today - timedelta(days=5),
                    exit_date=today - timedelta(days=2)),
    ])
    client = TestClient(create_app(url, edge_dir=tmp_path))
    everything = client.get("/api/stats/performance").json()
    board = {row["variant"]: row for row in everything["leaderboard"]}
    assert board["default"]["n_total"] == 3  # AAA+BBB+CCC; DDD is the wrong arm
    assert board["default"]["stat"]["n"] == 3

    windowed = client.get("/api/stats/performance?window=90").json()
    board90 = {row["variant"]: row for row in windowed["leaderboard"]}
    # BBB is beyond the window; CCC has no opened_date -> drops from windowed views.
    assert board90["default"]["n_total"] == 1
    assert board90["default"]["stat"]["value"] == pytest.approx(1.0)
    assert windowed["kpis"] == everything["kpis"]
    assert windowed["arms"] == everything["arms"]
    assert windowed["breakdowns"] == everything["breakdowns"]
    assert windowed["equity_curve"] == everything["equity_curve"]


def test_profit_factor_inf_is_null(tmp_path: Path) -> None:
    """An all-winner book's profit factor is float('inf') in summarize; JSON has no
    Infinity, so the wire form is null (the page rendered the same case as ∞)."""
    url = _db_url(tmp_path)
    _seed(url, [_perf_trade("AAA", 1.0, exit_date=date(2026, 1, 5)),
                _perf_trade("BBB", 0.5, exit_date=date(2026, 1, 6))])
    kpis = TestClient(create_app(url, edge_dir=tmp_path)).get(
        "/api/stats/performance").json()["kpis"]
    assert kpis["profit_factor"] is None
    assert kpis["win_rate"] == 1.0 and kpis["n_closed"] == 2


def test_performance_score_calibration_bands(tmp_path: Path) -> None:
    """Port of dashboard test_performance_renders_score_calibration, plus the two
    load-bearing shaping rules: bands with no closes are omitted (never spurious
    zeros), and the score cut goes through score_stamped -- a reversal row scored
    under the OLD definition (opened before the score-v2 cutoff) is excluded from
    the score breakdown while still counting everywhere else."""
    url = _db_url(tmp_path)
    stale = SCORE_STAMPED_FROM["reversal"] - timedelta(days=1)
    trades: list[PaperTrade] = []
    for i in range(4):
        trades.append(_perf_trade(f"L{i}", -1.0, score=0.45, exit_date=date(2026, 1, 5)))
        trades.append(_perf_trade(f"H{i}", 2.0, score=0.85, exit_date=date(2026, 1, 6)))
    trades.append(_perf_trade("OLD", 10.0, play_type="reversal", score=0.85,
                              opened_date=stale, exit_date=date(2026, 1, 7)))
    _seed(url, trades)
    body = TestClient(create_app(url, edge_dir=tmp_path)).get(
        "/api/stats/performance").json()
    score = {row["key"]: row for row in body["breakdowns"]["score"]}
    assert set(score) == {"0.00-0.50", "0.80-1.00"}  # empty bands omitted
    assert score["0.00-0.50"]["win_rate"] == 0.0 and score["0.00-0.50"]["n_closed"] == 4
    assert score["0.80-1.00"]["n_closed"] == 4  # OLD is excluded from the score cut...
    assert score["0.80-1.00"]["stat"]["value"] == pytest.approx(2.0)
    assert body["kpis"]["n_closed"] == 9  # ...but counts in every other aggregate


def test_performance_regime_breakdown_skips_unknown(tmp_path: Path) -> None:
    """Port of dashboard test_performance_renders_regime_breakdown: expectancy by SPY
    trend and vol regime; unknown-regime rows (None) never become a 'None' bucket."""
    url = _db_url(tmp_path)
    trades: list[PaperTrade] = []
    for i in range(3):
        trades.append(_perf_trade(f"B{i}", 1.5, market_trend="bull", market_vol="calm",
                                  exit_date=date(2026, 1, 5)))
        trades.append(_perf_trade(f"R{i}", -0.8, market_trend="bear", market_vol="high",
                                  exit_date=date(2026, 1, 6)))
    trades.append(_perf_trade("UNK", 0.1, exit_date=date(2026, 1, 7)))
    _seed(url, trades)
    body = TestClient(create_app(url, edge_dir=tmp_path)).get(
        "/api/stats/performance").json()
    trend = {row["key"]: row for row in body["breakdowns"]["market_trend"]}
    assert set(trend) == {"bull", "bear"}
    assert trend["bull"]["stat"]["value"] == pytest.approx(1.5)
    assert trend["bear"]["stat"]["value"] == pytest.approx(-0.8)
    assert trend["bull"]["n_closed"] == 3
    vol = {row["key"]: row for row in body["breakdowns"]["market_vol"]}
    assert set(vol) == {"calm", "high"}


def test_performance_facet_gold_filters_before_everything(tmp_path: Path) -> None:
    """?facet=gold applies facet_filter to the research trades BEFORE the play-type
    filter, leaderboard, and every downstream aggregate; every Stat echoes the facet
    it was computed under; an unknown facet is a 422."""
    url = _db_url(tmp_path)
    _seed(url, [
        _perf_trade("AAA", 1.0, would_surface=True, exit_date=date(2026, 1, 5)),
        _perf_trade("BBB", 0.5, would_surface=True, exit_date=date(2026, 1, 6)),
        _perf_trade("CCC", -1.0, would_surface=False, exit_date=date(2026, 1, 7)),
        _perf_trade("DDD", -0.5, exit_date=date(2026, 1, 8)),  # None: never gold
    ])
    client = TestClient(create_app(url, edge_dir=tmp_path))
    research = client.get("/api/stats/performance").json()
    assert research["kpis"]["n_closed"] == 4

    gold = client.get("/api/stats/performance?facet=gold").json()
    assert gold["kpis"]["n_closed"] == 2
    assert gold["kpis"]["expectancy"]["value"] == pytest.approx(0.75)
    assert gold["kpis"]["expectancy"]["facet"] == "gold"
    assert all(row["stat"]["facet"] == "gold" for row in gold["leaderboard"])
    assert all(row["stat"]["facet"] == "gold"
               for rows in gold["breakdowns"].values() for row in rows)
    assert len(gold["equity_curve"]) == 2
    assert client.get("/api/stats/performance?facet=bogus").status_code == 422


def test_performance_empty_book_returns_all_keys(tmp_path: Path) -> None:
    """Port of dashboard test_performance_empty_state_has_no_chart, reshaped: the API
    never hides sections -- every key is present with empty lists (or zero KPIs), and
    the frontend decides rendering. Rank keeps its fixed labels (page parity)."""
    body = _client(tmp_path).get("/api/stats/performance").json()
    assert set(body) == PERF_KEYS
    assert body["kpis"]["n_closed"] == 0
    assert body["kpis"]["profit_factor"] == 0.0  # zeros, not null: only inf -> null
    assert body["leaderboard"] == [] and body["arms"] == []
    assert body["equity_curve"] == []
    assert body["breakdowns"]["timeframe"] == []
    assert body["breakdowns"]["score"] == []  # every band empty -> all omitted
    assert body["breakdowns"]["market_trend"] == []
    assert body["breakdowns"]["market_vol"] == []
    assert [row["key"] for row in body["breakdowns"]["rank"]] == ["1-5", "6-10", "11+"]
    assert all(row["n_closed"] == 0 for row in body["breakdowns"]["rank"])


