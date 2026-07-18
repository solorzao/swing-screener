"""Trades-router read endpoints (split from test_api.py): GET /api/positions
and GET /api/trade-defaults."""

from datetime import date, datetime
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from swing_screener.cockpit.routers.trades import (
    _ACCOUNT_FOR_MODE,
)
from swing_screener.db.models import (
    PaperTrade,
    Trade,
)
from swing_screener.settings import _EXECUTION_MODES
from swing_screener.pipeline.broker import BrokerOrderSpec, FakeBroker
from tests.cockpit.conftest import (
    _exec_log,
    _positions_client,
    _signal_row,
)


# ---- trade READ endpoints: GET /api/positions + GET /api/trade-defaults ----


POSITIONS_KEYS = {"open", "caps", "closed", "equity", "quotes_as_of", "broker_as_of"}
PL_KEYS = {"unrealized_pl", "unrealized_pct", "r_multiple", "dist_to_stop_pct",
           "dist_to_target_pct"}
ROW_KEYS_COMMON = {"kind", "ticker", "timeframe", "entry_price", "size", "stop",
                   "target", "last_close", "pl", "badge", "bracket", "override",
                   "signal_id", "unlinked"}
REAL_ROW_KEYS = ROW_KEYS_COMMON | {"trade_id"}
LIVE_ROW_KEYS = ROW_KEYS_COMMON | {"paper_id"}
CAPS_KEYS = {"mode", "account", "run_date", "notional", "loss_r", "concurrent"}
CLOSED_KEYS = {"trade_id", "ticker", "entry_date", "exit_date", "entry_price",
               "exit_price", "size", "realized_usd", "exit_reason"}

def _live_paper(**over: object) -> PaperTrade:
    """An OPEN account='live' PaperTrade shaped like reconcile's materialized fill."""
    row: dict[str, object] = dict(
        ticker="LIV", timeframe="1d", horizon="", signal_score=0.0, rank=0,
        account="live", play_type="continuation", fill_status="filled",
        entry_price=50.0, stop=45.0, target=60.0, risk=5.0, status="open",
    )
    row.update(over)
    return PaperTrade(**row)


def test_positions_per_row_degradation_and_badges(tmp_path: Path) -> None:
    """One malformed trade (risk <= 0) or one missing quote never 503s the zone:
    every open row is KEPT, the sick one degrades to pl null with the badge STILL
    computed from price/stop/target (a missing quote nulls last_close and reads
    badge 'unknown'), and healthy rows still carry full P/L -- with unrealized_pct
    a FRACTION on the wire, never a percent."""
    client, engine = _positions_client(
        tmp_path, {"GOOD": 104.0, "BADRISK": 50.0, "RED": 94.0, "YEL": 111.0})
    with Session(engine) as s:
        for ticker, stop in (("GOOD", 95.0), ("BADRISK", 100.0), ("NOQUOTE", 95.0),
                             ("RED", 95.0), ("YEL", 95.0)):
            s.add(Trade(ticker=ticker, timeframe="1d", horizon="medium",
                        entry_date=date(2026, 7, 1), entry_price=100.0, size=10.0,
                        stop=stop, target=110.0))
        s.commit()
    r = client.get("/api/positions")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == POSITIONS_KEYS
    rows = {row["ticker"]: row for row in body["open"]}
    assert set(rows) == {"GOOD", "BADRISK", "NOQUOTE", "RED", "YEL"}  # all KEPT
    for row in rows.values():
        assert set(row) == REAL_ROW_KEYS
        assert row["kind"] == "real"
        assert row["unlinked"] is True  # no signal_id on any seeded row

    good = rows["GOOD"]
    assert good["last_close"] == 104.0
    assert set(good["pl"]) == PL_KEYS
    assert good["pl"]["unrealized_pl"] == pytest.approx(40.0)   # (104-100)*10
    assert good["pl"]["unrealized_pct"] == pytest.approx(0.04)  # FRACTION, not 4.0
    assert good["pl"]["r_multiple"] == pytest.approx(0.8)       # (104-100)/(100-95)
    assert good["pl"]["dist_to_stop_pct"] == pytest.approx((104 - 95) / 104)
    assert good["pl"]["dist_to_target_pct"] == pytest.approx((110 - 104) / 104)
    assert good["badge"] == "green"

    bad = rows["BADRISK"]  # stop == entry: position_pl raises ValueError
    assert bad["last_close"] == 50.0  # the quote itself is fine and still shown
    assert bad["pl"] is None
    # The badge reads only price/stop/target, so it SURVIVES broken P/L math: the
    # price (50) breached the recorded stop (100) and that lamp's job is 'get out'.
    assert bad["badge"] == "red"

    noq = rows["NOQUOTE"]  # upstream miss: absent from the price dict
    assert noq["last_close"] is None
    assert noq["pl"] is None and noq["badge"] == "unknown"

    assert rows["RED"]["badge"] == "red"    # 94 <= stop 95
    assert rows["YEL"]["badge"] == "yellow"  # 111 >= target 110
    assert datetime.fromisoformat(body["quotes_as_of"])
    assert body["broker_as_of"] is None  # broker_factory answered None


def test_positions_live_rows_r_only_vs_shares_join(tmp_path: Path) -> None:
    """Live rows have NO size column. The join is spec'd, not improvised: the NEWEST
    ExecutionLog for the ticker with status submitted_live/filled_live supplies
    shares -> dollar P/L; no such row -> size and dollar P/L null with the
    R-multiple still rendered from the persisted per-share ``risk``. An unpriced
    (pending-entry) live row keeps pl null; its badge still reads off the quote."""
    client, engine = _positions_client(
        tmp_path, {"JOINED": 52.0, "LONE": 21.0, "PEND": 52.0})
    with Session(engine) as s:
        s.add(_live_paper(ticker="JOINED"))
        s.add(_live_paper(ticker="LONE", entry_price=20.0, stop=18.0, target=26.0,
                          risk=2.0))
        s.add(_live_paper(ticker="PEND", entry_price=None))
        # JOINED's log history: older filled_live(3) -> newer submitted_live(7) wins;
        # the newest-of-all canceled(99) never reserved shares and must not join,
        # and the even-newer SELL-side live ticket (42) is an exit, not position
        # size -- the join is buy-side only, like repo.latest_recorded_stop.
        s.add(_exec_log(ticker="JOINED", account="live", mode="live", shares=3,
                        status="filled_live"))
        s.add(_exec_log(ticker="JOINED", account="live", mode="live", shares=7,
                        status="submitted_live"))
        s.add(_exec_log(ticker="JOINED", account="live", mode="live", shares=99,
                        status="canceled"))
        s.add(_exec_log(ticker="JOINED", account="live", mode="live", shares=42,
                        side="sell", status="submitted_live"))
        s.commit()
    body = client.get("/api/positions").json()
    rows = {row["ticker"]: row for row in body["open"]}
    for row in rows.values():
        assert set(row) == LIVE_ROW_KEYS
        assert row["kind"] == "live"

    joined = rows["JOINED"]
    assert joined["size"] == 7.0  # the NEWEST counting log, not the older fill
    assert joined["pl"]["unrealized_pl"] == pytest.approx(14.0)  # (52-50)*7
    assert joined["pl"]["r_multiple"] == pytest.approx(0.4)      # (52-50)/risk 5
    assert joined["pl"]["unrealized_pct"] == pytest.approx(0.04)

    lone = rows["LONE"]  # no counting ExecutionLog anywhere
    assert lone["size"] is None
    assert lone["pl"]["unrealized_pl"] is None  # dollar P/L needs shares
    assert lone["pl"]["r_multiple"] == pytest.approx(0.5)  # (21-20)/risk 2
    assert lone["pl"]["unrealized_pct"] == pytest.approx(0.05)

    pend = rows["PEND"]  # entry pending: no P/L to speak of
    assert pend["entry_price"] is None and pend["pl"] is None
    assert pend["badge"] == "green"  # the quote vs stop/target still reads
    assert pend["last_close"] == 52.0


def test_positions_bracket_lamp_per_kind(tmp_path: Path) -> None:
    """Snapshot present: a venue sell stop/stop_limit for the symbol reads 'armed';
    otherwise BOTH kinds read 'db-only' (their stop is a NOT NULL DB column) --
    a REAL/manual row in particular is never 'unprotected': the venue does not
    know it exists, so its stop was only ever a DB number."""
    broker = FakeBroker()
    order = broker.submit_order(BrokerOrderSpec(
        client_order_id="c-1", symbol="LIV", side="buy", qty=7, order_type="limit",
        limit_price=50.0, time_in_force="day", stop_loss=45.0, take_profit=60.0))
    broker.fill(order.broker_order_id, 50.0)  # bracket legs spawn only after fill()
    client, engine = _positions_client(
        tmp_path, {"LIV": 52.0, "NAKED": 21.0, "AMD": 104.0}, broker=broker)
    with Session(engine) as s:
        s.add(_live_paper(ticker="LIV"))
        s.add(_live_paper(ticker="NAKED", entry_price=20.0, stop=18.0, target=26.0,
                          risk=2.0))
        s.add(Trade(ticker="AMD", timeframe="1d", horizon="medium",
                    entry_date=date(2026, 7, 1), entry_price=100.0, size=10.0,
                    stop=95.0, target=110.0))
        s.commit()
    body = client.get("/api/positions").json()
    rows = {row["ticker"]: row for row in body["open"]}
    assert rows["LIV"]["bracket"] == "armed"     # venue-held sell stop leg
    assert rows["NAKED"]["bracket"] == "db-only"  # live row, no venue stop
    assert rows["AMD"]["bracket"] == "db-only"    # manual row: NEVER 'unprotected'
    assert datetime.fromisoformat(body["broker_as_of"])


def test_positions_bracket_unknown_without_a_broker(tmp_path: Path) -> None:
    """No broker (factory answers None) -> every lamp 'unknown', broker_as_of null:
    absence of evidence is UNKNOWN, never a claim either way."""
    client, engine = _positions_client(tmp_path, {"AMD": 104.0, "LIV": 52.0})
    with Session(engine) as s:
        s.add(Trade(ticker="AMD", timeframe="1d", horizon="medium",
                    entry_date=date(2026, 7, 1), entry_price=100.0, size=10.0,
                    stop=95.0, target=110.0))
        s.add(_live_paper(ticker="LIV"))
        s.commit()
    body = client.get("/api/positions").json()
    assert all(row["bracket"] == "unknown" for row in body["open"])
    assert body["broker_as_of"] is None


def test_positions_caps_mirror_limit_block_semantics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The three gauges read EXACTLY what _limit_block reads: notional sums
    execution_logs_for_day (the repo filters to the counting statuses -- skipped/
    canceled never load), loss is realized_r_on (an R THRESHOLD, today's realized
    R, sign preserved), concurrent is count_open_positions -- all for the account
    the execution MODE maps to, on run_date = latest_run_date."""
    monkeypatch.setenv("SWING_EXECUTION_MODE", "manual")
    monkeypatch.setenv("SWING_MAX_DAILY_NOTIONAL", "5000")
    monkeypatch.setenv("SWING_MAX_DAILY_LOSS", "2")
    monkeypatch.setenv("SWING_MAX_CONCURRENT", "3")
    client, engine = _positions_client(tmp_path)
    latest = date(2026, 7, 8)
    with Session(engine) as s:
        s.add(_signal_row(run_date=date(2026, 7, 1)))
        s.add(_signal_row(run_date=latest, ticker="NVDA"))
        # notional: recorded 1000 + submitted_live 250 count; skipped never reserved;
        # wrong account and wrong run_date stay out of the sum.
        s.add(_exec_log(notional=1000.0, status="recorded"))
        s.add(_exec_log(notional=250.0, status="submitted_live"))
        s.add(_exec_log(notional=400.0, status="skipped"))
        s.add(_exec_log(notional=800.0, account="live"))
        s.add(_exec_log(notional=600.0, run_date=date(2026, 7, 1)))
        # loss: closed manual trades exiting ON the run date, sign preserved.
        s.add(_live_paper(ticker="L1", account="manual", status="closed",
                          exit_date=latest, realized_r=-0.7))
        s.add(_live_paper(ticker="L2", account="manual", status="closed",
                          exit_date=latest, realized_r=0.2))
        s.add(_live_paper(ticker="L3", account="manual", status="closed",
                          exit_date=date(2026, 7, 1), realized_r=-5.0))
        # concurrent: OPEN manual rows only; research rows belong to another book.
        s.add(_live_paper(ticker="O1", account="manual"))
        s.add(_live_paper(ticker="O2", account="manual"))
        s.add(_live_paper(ticker="O3", account="research"))
        s.commit()
    caps = client.get("/api/positions").json()["caps"]
    assert set(caps) == CAPS_KEYS
    assert caps["mode"] == "manual"  # the additive caption field, mode verbatim
    assert caps["account"] == "manual"
    assert caps["run_date"] == "2026-07-08"
    assert caps["notional"] == {"used": pytest.approx(1250.0), "limit": 5000.0}
    assert caps["loss_r"] == {"used": pytest.approx(-0.5), "limit": 2.0}
    assert caps["concurrent"] == {"used": 2, "limit": 3}


def test_positions_caps_unbounded_shape_and_no_run_date(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No cap configured -> limit null (NEVER 0: 0 would read as 'nothing allowed');
    mode off keeps the research account LABEL; an empty signals table -> run_date
    null with the day-scoped used values an honest 0 (there is no day to sum).
    Under mode off ``concurrent`` counts what the screen DISPLAYS -- so the open
    research shadow row contributes NOTHING (the caps-honesty rule, tested in
    depth below). Empty book: every zone empty, quotes_as_of still stamped."""
    for var in ("SWING_EXECUTION_MODE", "SWING_MAX_DAILY_NOTIONAL",
                "SWING_MAX_DAILY_LOSS", "SWING_MAX_CONCURRENT"):
        monkeypatch.delenv(var, raising=False)
    client, engine = _positions_client(tmp_path)
    with Session(engine) as s:
        s.add(_live_paper(ticker="R1", account="research"))  # open research row
        s.commit()
    body = client.get("/api/positions").json()
    caps = body["caps"]
    assert caps["mode"] == "off"          # the default mode, carried verbatim
    assert caps["account"] == "research"  # mode 'off' books nothing, reads research
    assert caps["run_date"] is None
    assert caps["notional"] == {"used": 0.0, "limit": None}
    assert caps["loss_r"] == {"used": 0.0, "limit": None}
    assert caps["concurrent"] == {"used": 0, "limit": None}
    assert body["open"] == [] and body["closed"] == [] and body["equity"] == []
    assert datetime.fromisoformat(body["quotes_as_of"])


def test_positions_caps_off_mode_counts_the_displayed_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The caps-honesty rule (2026-07 usability finding): under mode 'off' no
    adapter enforces a cap and OFF_ACCOUNT is merely the research LABEL, so the
    old per-account read counted the open research SHADOW GRID -- 'N concurrent
    positions used' with zero corresponding visible trades. ``concurrent.used``
    must count what the screen displays: open manual Trades + open live
    PaperTrades, and nothing else."""
    monkeypatch.delenv("SWING_EXECUTION_MODE", raising=False)  # default: off
    client, engine = _positions_client(tmp_path, {"AMD": 104.0, "LIV": 52.0})
    with Session(engine) as s:
        for i in range(5):  # the invisible shadow grid: must contribute NOTHING
            s.add(_live_paper(ticker=f"R{i}", account="research"))
        s.add(_live_paper(ticker="P1", account="paper"))  # curated book: also unseen
        s.add(Trade(ticker="AMD", timeframe="1d", horizon="medium",
                    entry_date=date(2026, 7, 1), entry_price=100.0, size=10.0,
                    stop=95.0, target=110.0))            # displayed: real row
        s.add(_live_paper(ticker="LIV"))                 # displayed: live row
        s.commit()
    body = client.get("/api/positions").json()
    assert {r["ticker"] for r in body["open"]} == {"AMD", "LIV"}  # the screen
    caps = body["caps"]
    assert set(caps) == CAPS_KEYS
    assert caps["mode"] == "off"
    assert caps["account"] == "research"  # the label field keeps its old meaning
    assert caps["concurrent"]["used"] == 2  # exactly the two rows rendered above


def test_account_for_mode_covers_every_settings_mode() -> None:
    """_ACCOUNT_FOR_MODE must stay total over the settings mode set: a mode added in
    settings becomes a failure HERE, not a KeyError-500 inside /api/positions."""
    assert set(_ACCOUNT_FOR_MODE) == _EXECUTION_MODES


def test_positions_closed_trades_and_realized_equity(tmp_path: Path) -> None:
    """Closed rows arrive newest-exit first (the repo's order); the equity series is
    the retired _render_closed math -- dated closes ascending, running sum of
    ((exit or entry) - entry) * size, an exit-less close falling back to entry
    (realized 0) and an UNDATED close listed but never plotted."""
    client, engine = _positions_client(tmp_path)

    def closed(ticker: str, exit_price: float | None, exit_date: date | None) -> Trade:
        return Trade(ticker=ticker, timeframe="1d", horizon="medium",
                     entry_date=date(2026, 6, 1), entry_price=100.0, size=10.0,
                     stop=95.0, target=110.0, status="closed", exit_date=exit_date,
                     exit_price=exit_price, exit_reason="target")

    with Session(engine) as s:
        s.add(closed("BBB", 108.0, date(2026, 7, 2)))   # +80
        s.add(closed("AAA", 97.0, date(2026, 7, 1)))    # -30
        s.add(closed("CCC", None, date(2026, 7, 3)))    # exit-less: realized 0
        s.add(closed("DDD", 120.0, None))               # undated: listed, not plotted
        s.commit()
    body = client.get("/api/positions").json()
    by_ticker = {row["ticker"]: row for row in body["closed"]}
    assert set(by_ticker) == {"AAA", "BBB", "CCC", "DDD"}
    for row in body["closed"]:
        assert set(row) == CLOSED_KEYS
    assert by_ticker["BBB"]["realized_usd"] == pytest.approx(80.0)
    assert by_ticker["AAA"]["realized_usd"] == pytest.approx(-30.0)
    assert by_ticker["CCC"]["realized_usd"] == pytest.approx(0.0)
    assert by_ticker["CCC"]["exit_price"] is None
    assert by_ticker["DDD"]["exit_date"] is None
    dated = [row["ticker"] for row in body["closed"] if row["exit_date"] is not None]
    assert dated == ["CCC", "BBB", "AAA"]  # newest exit first
    assert body["equity"] == [["2026-07-01", -30.0], ["2026-07-02", 50.0],
                              ["2026-07-03", 50.0]]


def test_trade_defaults_prefill(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The LOG-TRADE form's prefill: the signal's levels verbatim, actionability at
    the cached quote, suggested_entry = the quote CLAMPED into [floor, ceiling], and
    size_order at conviction 'medium' (risk unit 1200 -> medium budget 600 over a
    $6/share zone risk -> 100 shares)."""
    monkeypatch.setenv("SWING_RISK_PER_TRADE_DOLLARS", "1200")
    monkeypatch.delenv("SWING_MAX_SHARES", raising=False)
    monkeypatch.delenv("SWING_ACCOUNT_EQUITY", raising=False)
    # zone [96, 101], stop 95, target 110 (the _signal_row geometry); three quotes
    # exercise the three clamp regimes on three separate signals.
    client, engine = _positions_client(
        tmp_path, {"AMD": 104.0, "NVDA": 99.0, "MSFT": 95.5})
    with Session(engine) as s:
        signals = [_signal_row(), _signal_row(ticker="NVDA"),
                   _signal_row(ticker="MSFT")]
        s.add_all(signals)
        s.commit()
        ids = {sig.ticker: sig.id for sig in signals}

    body = client.get(f"/api/trade-defaults?signal_id={ids['AMD']}").json()
    assert set(body) == {"signal", "last_close", "actionability", "suggested_entry",
                         "sizing"}
    assert body["signal"] == {
        "ticker": "AMD", "timeframe": "1d", "horizon": "medium",
        "play_type": "continuation", "entry_floor": 96.0, "entry_ceiling": 101.0,
        "stop": 95.0, "target": 110.0, "conviction_tier": "base"}
    assert body["last_close"] == 104.0
    # 104 sits (104-101)/6 = 0.5R past the ceiling -- extended, and the prefill
    # refuses to chase: suggested entry clamps DOWN to the ceiling.
    assert body["actionability"] == {"status": "extended",
                                     "dist_r": pytest.approx(0.5)}
    assert body["suggested_entry"] == 101.0
    assert body["sizing"] == {"shares": 100, "risk_dollars": pytest.approx(600.0),
                              "unconfigured": False}

    inside = client.get(f"/api/trade-defaults?signal_id={ids['NVDA']}").json()
    assert inside["actionability"]["status"] == "actionable"
    assert inside["suggested_entry"] == 99.0  # in the zone: the quote itself

    below = client.get(f"/api/trade-defaults?signal_id={ids['MSFT']}").json()
    assert below["actionability"]["status"] == "actionable"  # above stop, room to enter
    assert below["suggested_entry"] == 96.0  # clamps UP to the floor


def test_trade_defaults_sizing_unconfigured_no_quote_and_404(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """shares == 0 is the deliberate 'sizing unconfigured' signal (no risk unit set:
    the UI renders R-multiples, never a guessed dollar); a missing quote nulls
    last_close and suggested_entry while actionability stays TOTAL in the
    /api/picks wire form ({"status": "unknown", "dist_r": null} -- one shape
    everywhere); an unknown signal_id is a 404 and a missing/garbage one
    FastAPI's 422."""
    for var in ("SWING_RISK_PER_TRADE_DOLLARS", "SWING_ACCOUNT_EQUITY"):
        monkeypatch.delenv(var, raising=False)
    client, engine = _positions_client(tmp_path)  # no quotes at all
    with Session(engine) as s:
        sig = _signal_row()
        s.add(sig)
        s.commit()
        sig_id = sig.id
    body = client.get(f"/api/trade-defaults?signal_id={sig_id}").json()
    assert body["sizing"] == {"shares": 0, "risk_dollars": 0.0, "unconfigured": True}
    assert body["last_close"] is None
    assert body["actionability"] == {"status": "unknown", "dist_r": None}
    assert body["suggested_entry"] is None
    assert client.get("/api/trade-defaults?signal_id=999").status_code == 404
    assert client.get("/api/trade-defaults").status_code == 422
    assert client.get("/api/trade-defaults?signal_id=abc").status_code == 422


