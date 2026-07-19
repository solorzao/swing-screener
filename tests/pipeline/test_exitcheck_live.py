"""The hourly live-book sync inside ``run_exit_check`` (Task 11) + the shared
``live_sync.maybe_reconcile_live`` helper it and the evening screen both use.

The load-bearing properties proven here:

* SHARED RECONCILE POSTURE: reconcile with mode off when open live exposure
  exists (even disarmed -- the screen-run posture the helper extracts); build a
  broker ON DEMAND for that path; a broker-build failure degrades to a loud
  warning and a 0 count, never a raise; no exposure + mode off = no-op.
* SAME-HOUR FRESHNESS: the hourly job materializes a broker fill / books a
  venue stop-out within the hour -- and the guardrails consult right after the
  reconcile trips the daily-loss breaker on that realized $ the SAME hour
  (state 'tripped', sweep 'complete', trip source 'exitcheck').
* SWEEP RESUME HOIST: a tripped book with a pending sweep is finished by the
  hourly job even with the mode off (broker built on demand), and the state
  check comes BEFORE any broker build (no venue client is ever constructed for
  an ok book).
* AT-LEAST-ONCE REJECTION ALERTS: the hourly pass is QUERY-based (uncovered
  rejected/canceled rows joined against per-row EmailLog coverage), so a digest
  whose send FAILED is retried here -- and the per-row 'xlog-{id}' keys make a
  partial overlap (digest covered {a,b}; new row c appears) alert ONLY the
  uncovered row, never re-alert covered ones (the Task-11 dedup pin).
* GUARDRAIL-TRIP RETRY: an unmailed trip is mailed within the hour, on the
  shared ``alert_key=str(trip_id)`` so every other emitter then no-ops.
* IMPORT CANARY: ``pipeline.exitcheck`` imports clean in a fresh interpreter
  and never drags ``swing_screener.notify.*`` onto its module-import surface
  (notify.run imports exitcheck at module level -- the edge must stay one-way).

All tmp-file sqlite + FakeBroker + injected send spies -- no venue, no SMTP.
"""

import os
import subprocess
import sys
from datetime import UTC, date, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from swing_screener.db import guardrails_repo as gr
from swing_screener.db import repo
from swing_screener.db.models import AgentGuardrailEvent, EmailLog, ExecutionLog, PaperTrade
from swing_screener.db.session import get_engine
from swing_screener.pipeline import exitcheck, live_sync
from swing_screener.pipeline.broker import BrokerOrderSpec, FakeBroker
from swing_screener.pipeline.execution import LiveAdapter
from swing_screener.pipeline.insight import OrderIntent
from swing_screener.settings import Limits

RUN = date(2026, 6, 15)
NO_LIMITS = Limits(max_daily_notional=None, max_daily_loss=None, max_concurrent=None)


def _no_bars(tickers, timeframe):  # noqa: ARG001 -- mirror the live seam signature
    return {}


def _hermetic_env(monkeypatch, **env):
    """Default-off env: no execution mode, no broker, no recipient, no transport
    creds -- each test then opts back in explicitly. Belt-and-braces so a
    developer's exported DIGEST_TO can never make a test touch SMTP/ACS."""
    for var in ("SWING_EXECUTION_MODE", "SWING_BROKER", "DIGEST_TO",
                "GMAIL_ADDRESS", "GMAIL_APP_PASSWORD",
                "SWING_ACS_ENDPOINT", "SWING_ACS_SENDER"):
        monkeypatch.delenv(var, raising=False)
    for var, value in env.items():
        monkeypatch.setenv(var, value)


def _open_live_trade(*, ticker: str = "AMD") -> PaperTrade:
    """One OPEN live trade (entry 50, stop 45, qty 10) the reconciler can close."""
    return PaperTrade(
        ticker=ticker, timeframe="1d", horizon="medium", signal_score=0.8, rank=1,
        account="live", fill_status="filled", entry_date=date(2026, 6, 10),
        entry_price=50.0, stop=45.0, target=60.0, risk=5.0, status="open", qty=10,
    )


def _entry_spec(key: str, symbol: str) -> BrokerOrderSpec:
    return BrokerOrderSpec(client_order_id=key, symbol=symbol, side="buy", qty=10,
                           order_type="limit", limit_price=100.0, time_in_force="day")


def _submit_live_order(url: str, broker: FakeBroker) -> str:
    """One submitted_live ExecutionLog via the LiveAdapter (the reconcile's input)."""
    intent = OrderIntent(
        ticker="AMD", timeframe="1d", play_type="continuation",
        entry_floor=99.0, entry_ceiling=101.0, stop=94.0, target=110.0,
        conviction="high", shares=10, risk_dollars=70.0,
        edge_played="e", key_risk="", insight="i", side="long", limit_price=101.0)
    with Session(get_engine(url)) as s:
        adapter = LiveAdapter(broker, gate_ready_fn=lambda _s: True)
        result = adapter.submit(intent, session=s, run_date=date(2026, 6, 14),
                                limits=NO_LIMITS)
        s.commit()
    assert result.broker_order_id is not None
    return result.broker_order_id


def _add_rejected_log(s: Session, *, ticker: str, status: str = "rejected_live",
                      detail: str = "insufficient buying power") -> int:
    row = repo.add_execution_log(
        s, created_date=RUN, ticker=ticker, timeframe="1d",
        play_type="continuation", run_date=RUN, account="live", mode="live",
        side="buy", limit_price=100.0, shares=10, stop=95.0, target=110.0,
        risk_dollars=50.0, notional=1000.0, status=status,
        detail=detail, idempotency_key=f"k-{ticker}")
    return row.id


def _spy_transport(monkeypatch):
    """Patch the lazily-resolved transport with a spy; returns the sent list."""
    sent: list[dict] = []
    monkeypatch.setattr("swing_screener.notify.transport.resolve_sender",
                        lambda: (lambda **kw: sent.append(kw)))
    return sent


# --- import canaries ----------------------------------------------------------


def test_pipeline_exitcheck_imports_clean_in_a_fresh_interpreter():
    """The exitcheck canary (the run.py canary's sibling): a fresh interpreter
    importing exitcheck FIRST must succeed, and the module-import surface must
    never reach ``swing_screener.notify.*`` -- notify.run imports exitcheck at
    module level, so that edge must stay one-way (every notify import in
    exitcheck is call-time-lazy). pytest collection order hides both failure
    modes; the subprocess is the honest probe."""
    root = Path(__file__).resolve().parents[2]
    env = dict(os.environ, PYTHONPATH=str(root / "src"))
    probe = ("import sys; import swing_screener.pipeline.exitcheck; "
             "bad = [m for m in sys.modules if m.startswith('swing_screener.notify')]; "
             "assert not bad, f'exitcheck dragged notify onto its import surface: {bad}'")
    proc = subprocess.run(
        [sys.executable, "-c", probe],
        env=env, cwd=root, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


# --- live_sync.maybe_reconcile_live -------------------------------------------


def test_reconciles_open_exposure_with_mode_off(tmp_path, monkeypatch):
    """Mode off + open live exposure + a broker in hand: the venue close is
    reconciled anyway (the disarm-safety posture), and the injected broker
    rides back for the caller's guardrails consult."""
    _hermetic_env(monkeypatch)
    url = f"sqlite:///{tmp_path / 'exposure.sqlite'}"
    broker = FakeBroker()
    broker.close_position("AMD", price=44.0)
    with Session(get_engine(url)) as s:
        s.add(_open_live_trade())
        s.commit()

        n, out = live_sync.maybe_reconcile_live(s, today=RUN, broker=broker)

        assert n == 1
        assert out is broker
        pt = s.scalars(select(PaperTrade).where(PaperTrade.account == "live")).one()
        assert pt.status == "closed"
        assert pt.exit_price == 44.0
        assert pt.exit_reason == "broker_close"


def test_builds_broker_on_demand_for_disarmed_exposure(tmp_path, monkeypatch):
    """Mode off, no broker handed in, but a broker CONFIGURED and open exposure:
    the helper builds one on demand and reconciles -- and returns it."""
    _hermetic_env(monkeypatch, SWING_BROKER="alpaca")
    broker = FakeBroker()
    broker.close_position("AMD", price=44.0)
    monkeypatch.setattr(live_sync, "build_broker", lambda settings: broker)
    url = f"sqlite:///{tmp_path / 'ondemand.sqlite'}"
    with Session(get_engine(url)) as s:
        s.add(_open_live_trade())
        s.commit()

        n, out = live_sync.maybe_reconcile_live(s, today=RUN)

        assert n == 1
        assert out is broker
        pt = s.scalars(select(PaperTrade).where(PaperTrade.account == "live")).one()
        assert pt.status == "closed"


def test_broker_build_failure_degrades_to_zero_never_raises(tmp_path, monkeypatch):
    """The secrets-gap path: open exposure wants a reconcile but the broker
    cannot be built -> loud warning, (0, None) back, the caller lives on."""
    _hermetic_env(monkeypatch, SWING_BROKER="alpaca")

    def _boom(settings):
        raise RuntimeError("no secrets on this box")
    monkeypatch.setattr(live_sync, "build_broker", _boom)
    url = f"sqlite:///{tmp_path / 'buildfail.sqlite'}"
    with Session(get_engine(url)) as s:
        s.add(_open_live_trade())
        s.commit()

        n, out = live_sync.maybe_reconcile_live(s, today=RUN)

        assert (n, out) == (0, None)
        pt = s.scalars(select(PaperTrade).where(PaperTrade.account == "live")).one()
        assert pt.status == "open"                # left for a luckier cycle


def test_no_exposure_and_mode_off_is_a_noop(tmp_path, monkeypatch):
    """Nothing to sync: mode off, no open live rows -> (0, broker) untouched,
    even with a broker in hand and a working order at the venue (mirrors the
    screen's off-mode gate: the live path stays dark)."""
    _hermetic_env(monkeypatch)
    url = f"sqlite:///{tmp_path / 'noop.sqlite'}"
    broker = FakeBroker()
    oid = _submit_live_order(url, broker)
    broker.fill(oid, price=99.5)                  # a fill the gate must NOT materialize
    with Session(get_engine(url)) as s:
        n, out = live_sync.maybe_reconcile_live(s, today=RUN, broker=broker)
        assert n == 0
        assert out is broker
        assert s.scalars(select(ExecutionLog)).one().status == "submitted_live"


# --- the hourly job: reconcile + consult ---------------------------------------


def test_hourly_reconcile_materializes_fill_same_hour(tmp_path, monkeypatch):
    """Live mode + a broker fill: the hourly run materializes the live position
    (entry from BROKER truth) instead of waiting for the evening screen."""
    _hermetic_env(monkeypatch, SWING_EXECUTION_MODE="live")
    url = f"sqlite:///{tmp_path / 'hourlyfill.sqlite'}"
    broker = FakeBroker()
    oid = _submit_live_order(url, broker)
    broker.fill(oid, price=99.5)

    result = exitcheck.run_exit_check(db_url=url, today=RUN,
                                      latest_bars_fn=_no_bars, broker=broker)

    assert result.n_exited == 0                   # the exit walk itself saw nothing
    with Session(get_engine(url)) as s:
        pt = s.scalars(select(PaperTrade).where(PaperTrade.account == "live")).one()
        assert pt.status == "open"
        assert pt.entry_price == 99.5             # the BROKER fill, not the limit
        assert s.scalars(select(ExecutionLog)).one().status == "filled_live"


def test_hourly_stop_out_trips_daily_loss_same_hour(tmp_path, monkeypatch):
    """The same-HOUR freshness loop: the hourly reconcile books a venue
    stop-out's realized loss and the consult right after it trips the
    daily-loss breaker -- broker already in hand, so the sweep completes, and
    the trip event is stamped source='exitcheck'."""
    _hermetic_env(monkeypatch)                    # mode OFF: exposure drives it
    url = f"sqlite:///{tmp_path / 'hourlytrip.sqlite'}"
    broker = FakeBroker()
    broker.close_position("AMD", price=44.0)      # (44 - 50) * 10 = -$60 realized
    with Session(get_engine(url)) as s:
        s.add(_open_live_trade())
        s.commit()
        gr.edit_limits(s, source="test", max_daily_loss_usd=50.0)

    exitcheck.run_exit_check(db_url=url, today=RUN,
                             latest_bars_fn=_no_bars, broker=broker)

    with Session(get_engine(url)) as s:
        pt = s.scalars(select(PaperTrade).where(PaperTrade.account == "live")).one()
        assert pt.status == "closed"              # the reconcile booked the loss...
        g = gr.load_guardrails(s)
        assert g.state == "tripped"               # ...and the consult tripped on it
        assert g.trip_reason == "max daily loss: $-60.00 <= -$50.00"
        assert g.sweep_state == "complete"        # broker in scope -> the sweep ran
        trip = s.query(AgentGuardrailEvent).filter_by(kind="trip").one()
        assert trip.source == "exitcheck"


# --- the hourly job: sweep resume hoist ----------------------------------------


def test_hourly_resume_completes_pending_sweep_mode_off(tmp_path, monkeypatch):
    """A tripped book with a pending sweep and the mode OFF: the hourly hoist
    builds a broker on demand (mode-independent, the digest hoist mirrored) and
    finishes the sweep within the hour."""
    _hermetic_env(monkeypatch)
    url = f"sqlite:///{tmp_path / 'hourlyresume.sqlite'}"
    with Session(get_engine(url)) as s:
        assert gr.trip(s, breaker="max_daily_loss_usd",
                       reason="max daily loss: $-60.00 <= -$50.00",
                       source="screen") is not None
        assert gr.load_guardrails(s).sweep_state == "pending"
    broker = FakeBroker()
    broker.submit_order(_entry_spec("rest-1", "TSLA"))  # a resting DAY entry to pull
    monkeypatch.setattr(exitcheck, "build_broker", lambda settings: broker)

    exitcheck.run_exit_check(db_url=url, today=RUN, latest_bars_fn=_no_bars)

    with Session(get_engine(url)) as s:
        g = gr.load_guardrails(s)
        assert g.state == "tripped"
        assert g.sweep_state == "complete"        # the hoist finished it
        # tripped -> the consult skipped evaluation -> no second trip event.
        assert s.query(AgentGuardrailEvent).filter_by(kind="trip").count() == 1
    assert broker.list_open_orders() == []        # the resting entry was pulled


def test_hourly_resume_state_check_precedes_broker_build(tmp_path, monkeypatch):
    """The digest hoist's ordering, mirrored: an ok book must never construct a
    venue client -- the state check gates the on-demand build."""
    _hermetic_env(monkeypatch)
    built: list[object] = []
    monkeypatch.setattr(exitcheck, "build_broker",
                        lambda settings: built.append(settings))
    url = f"sqlite:///{tmp_path / 'hourlynobuild.sqlite'}"

    exitcheck.run_exit_check(db_url=url, today=RUN, latest_bars_fn=_no_bars)

    assert built == []                            # ok state -> no broker was built


# --- the hourly job: at-least-once alert retries --------------------------------


def test_hourly_alerts_rejections_the_digest_lost(tmp_path, monkeypatch):
    """The digest's rejection send FAILED (rows flipped, no coverage written):
    the hourly QUERY-based pass finds the uncovered rows and sends ONE email
    naming both -- then a re-run no-ops on the per-row coverage."""
    _hermetic_env(monkeypatch, DIGEST_TO="op@example.com")
    sent = _spy_transport(monkeypatch)
    url = f"sqlite:///{tmp_path / 'hourlyreject.sqlite'}"
    with Session(get_engine(url)) as s:
        id_a = _add_rejected_log(s, ticker="AMD")
        id_b = _add_rejected_log(s, ticker="NVDA", status="canceled",
                                 detail="order canceled by venue")

    exitcheck.run_exit_check(db_url=url, today=RUN, latest_bars_fn=_no_bars)

    rejections = [m for m in sent if "Rejected/Canceled" in m["subject"]]
    assert len(rejections) == 1                   # ONE email for BOTH rows
    assert rejections[0]["to"] == "op@example.com"
    assert "AMD" in rejections[0]["text"] and "NVDA" in rejections[0]["text"]
    with Session(get_engine(url)) as s:
        keys = {r.alert_key for r in
                s.scalars(select(EmailLog).where(EmailLog.kind == "execution"))}
        assert keys == {f"xlog-{id_a}", f"xlog-{id_b}"}

    sent.clear()
    exitcheck.run_exit_check(db_url=url, today=RUN, latest_bars_fn=_no_bars)
    assert sent == []                             # covered -> the re-run no-ops


def test_hourly_partial_overlap_alerts_only_the_uncovered_row(tmp_path, monkeypatch):
    """THE per-row dedup pin (Task 11): the digest already alerted {a, b}; a new
    rejection c lands before the next hour. The old sha1-of-the-set key would
    hash {a, b, c} to a FRESH key and re-alert a and b; the per-row coverage
    join alerts ONLY c."""
    _hermetic_env(monkeypatch, DIGEST_TO="op@example.com")
    sent = _spy_transport(monkeypatch)
    url = f"sqlite:///{tmp_path / 'hourlyoverlap.sqlite'}"
    with Session(get_engine(url)) as s:
        id_a = _add_rejected_log(s, ticker="AAA")
        id_b = _add_rejected_log(s, ticker="BBB")
        # the digest's (successful) earlier alert: per-row coverage for a and b.
        for log_id in (id_a, id_b):
            s.add(EmailLog(sent_at=datetime.now(UTC), kind="execution",
                           subject="covered earlier", run_date=RUN,
                           alert_key=f"xlog-{log_id}"))
        s.commit()
        id_c = _add_rejected_log(s, ticker="CCC")

    exitcheck.run_exit_check(db_url=url, today=RUN, latest_bars_fn=_no_bars)

    rejections = [m for m in sent if "Rejected/Canceled" in m["subject"]]
    assert len(rejections) == 1
    body = rejections[0]["text"]
    assert "CCC" in body                          # the uncovered row is alerted...
    assert "AAA" not in body and "BBB" not in body  # ...the covered ones are NOT
    with Session(get_engine(url)) as s:
        keys = {r.alert_key for r in
                s.scalars(select(EmailLog).where(EmailLog.kind == "execution"))}
        assert keys == {f"xlog-{id_a}", f"xlog-{id_b}", f"xlog-{id_c}"}


def test_hourly_mails_an_unmailed_trip(tmp_path, monkeypatch):
    """An evening trip whose alert never landed (transport-less screen, dead
    SMTP): the hourly retry mails it on the shared trip-id key, and a re-run
    dedups on the EmailLog row."""
    _hermetic_env(monkeypatch, DIGEST_TO="op@example.com")
    sent = _spy_transport(monkeypatch)
    url = f"sqlite:///{tmp_path / 'hourlytripretry.sqlite'}"
    with Session(get_engine(url)) as s:
        trip_id = gr.trip(s, breaker="max_daily_loss_usd",
                          reason="max daily loss: $-60.00 <= -$50.00",
                          source="screen")
        assert trip_id is not None

    exitcheck.run_exit_check(db_url=url, today=RUN, latest_bars_fn=_no_bars)

    trips = [m for m in sent if "GUARDRAIL TRIPPED" in m["subject"]]
    assert len(trips) == 1
    assert "max daily loss: $-60.00 <= -$50.00" in trips[0]["text"]
    with Session(get_engine(url)) as s:
        row = s.scalars(select(EmailLog).where(EmailLog.kind == "guardrail")).one()
        assert row.alert_key == str(trip_id)

    sent.clear()
    exitcheck.run_exit_check(db_url=url, today=RUN, latest_bars_fn=_no_bars)
    assert [m for m in sent if "GUARDRAIL TRIPPED" in m["subject"]] == []
