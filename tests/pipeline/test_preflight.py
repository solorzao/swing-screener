"""The read-only preflight GO/NO-GO check (``pipeline.preflight``).

Before a human flips to Alpaca-live REAL money they run ``preflight``: is the broker
reachable + funded, are all caps set, does ``is_real_money`` match the host, is the autonomy
gate ready? It is strictly READ-ONLY -- it performs NO writes and NEVER arms anything.

The load-bearing properties pinned here:

* ``go`` is True iff every SAFETY-CRITICAL check passes (config / reachable / funded / caps,
  plus ``guardrails`` on a REAL-money host). ``is_real_money`` and the autonomy gate are
  ADVISORY (warn, not critical) -- they never flip the GO/NO-GO verdict.
* the ``guardrails`` check mirrors execution's posture EXACTLY: the brake mandate binds a
  real-money endpoint (critical) and paper hosts stay exempt (advisory) -- but the line is
  rendered honestly either way, so the gap is visible BEFORE the flip.
* a BROKER error never raises out of ``preflight`` -- the reachability check catches it and
  records a NO-GO line (so a down broker reads as NO-GO, not a crash).
* preflight BOOKS NOTHING: no calls/tickets, nothing queued on the session, no settings/env
  mutation, no edge-file rewrite. Its one write is the idempotent guardrails get-or-create
  seed (the default all-unset brake row), which is never a state change.

No live network: every test drives a ``FakeBroker`` (or a raising stub); the autonomy gate is
driven from a verdicts sidecar + a scored-call book, exactly like the gate's own tests.
"""

from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from swing_screener.db import guardrails_repo as gr
from swing_screener.db.models import (
    AgentGuardrailEvent,
    AgentGuardrails,
    AnalystCall,
    ExecutionLog,
)
from swing_screener.db.session import get_engine
from swing_screener.pipeline.broker import BrokerAccount, FakeBroker
from swing_screener.pipeline.preflight import (
    PreflightCheck,
    PreflightReport,
    preflight,
    render_preflight,
)
from swing_screener.pipeline.reflect import Verdict, verdicts_to_json
from swing_screener.settings import Settings


# --- fixtures ----------------------------------------------------------------
def _session() -> Session:
    return Session(get_engine("sqlite:///:memory:"))


def _settings(
    *,
    execution_mode: str = "live",
    broker: str = "alpaca",
    max_daily_notional: float | None = 10_000.0,
    max_daily_loss: float | None = 500.0,
    max_concurrent: int | None = 3,
) -> Settings:
    """A live-intent Settings with all caps set -- the happy path. The keyword params peel a
    piece off (a missing cap, no broker, the wrong mode) to exercise a NO-GO. Built directly
    (not via env) so the test controls the fields without monkeypatching the environment."""
    return Settings(
        db_url="sqlite:///:memory:", chart_dir=Path("."), cache_dir=Path("."),
        pdf_dir=Path("."), edge_dir=Path("."), blob_account_url=None, blob_container="charts",
        key_vault_url=None, azure_client_id=None, acs_endpoint=None, acs_sender=None,
        deep_analysis_enabled=False, analysis_model="claude-opus-4-8",
        analysis_reasoning="high", deep_analysis_top_n=5,
        deep_analysis_kinds=frozenset({"daily"}), analysis_max_searches=4,
        deep_analysis_max_usd=None,
        coach_enabled=False, coach_max_usd=None,
        audit_enabled=False, audit_max_usd=None,
        account_equity=None, risk_per_trade_dollars=None, risk_pct=0.01, max_shares=None,
        execution_mode=execution_mode, max_daily_notional=max_daily_notional,
        max_daily_loss=max_daily_loss, max_concurrent=max_concurrent,
        broker=broker, allow_real_money=True,
    )


def _verdict(tier: str, *, dimension: str = "market_trend", bucket: str = "bull") -> Verdict:
    return Verdict(
        play_type="continuation", dimension=dimension, bucket=bucket, tier=tier,
        n=30, expectancy_r=0.6, ci_low=0.2, n_clusters=10, source="forward",
    )


def _write_verdicts(edge_dir, play_type: str, verdicts: list[Verdict]) -> None:
    (edge_dir / f"{play_type}.verdicts.json").write_text(
        verdicts_to_json(verdicts), encoding="utf-8"
    )


def _call(ticker: str, conviction: str, r: float, *, play_type: str = "continuation") -> AnalystCall:
    return AnalystCall(
        created_date=date(2026, 6, 1), ticker=ticker, timeframe="1d",
        play_type=play_type, run_date=date(2026, 6, 1),
        baseline_conviction="medium", final_conviction=conviction,
        nudge_reason="x", model="claude-opus-4-8",
        realized_r=r, scored_at=date(2026, 6, 5),
    )


def _calibrated_calls(play_type: str = "continuation") -> list[AnalystCall]:
    calls: list[AnalystCall] = []
    for i in range(8):
        for r in (1.2, 1.4, 1.3, 1.2, 1.4):
            calls.append(_call(f"H{play_type}{i}", "high", r, play_type=play_type))
        for r in (-0.1, 0.0, -0.2, 0.1, -0.1):
            calls.append(_call(f"L{play_type}{i}", "low", r, play_type=play_type))
    return calls


def _ready_edge_dir(tmp_path):
    """An edge dir with a forward-confirmed continuation verdict (gate-ready when paired with
    the calibrated book)."""
    edge_dir = tmp_path / "edge"
    edge_dir.mkdir()
    _write_verdicts(edge_dir, "continuation", [_verdict("forward_confirmed")])
    return edge_dir


def _check(report: PreflightReport, name: str) -> PreflightCheck:
    return next(c for c in report.checks if c.name == name)


class _RaisingBroker(FakeBroker):
    """A FakeBroker whose get_account raises -- to drive the reachability NO-GO path."""

    def get_account(self) -> BrokerAccount:
        raise RuntimeError("broker unreachable: connection refused")


# --- the happy path: everything green -> GO ----------------------------------
def test_go_when_reachable_funded_capped_and_gate_ready(tmp_path) -> None:
    edge_dir = _ready_edge_dir(tmp_path)
    broker = FakeBroker(buying_power=50_000.0, status="ACTIVE")
    with _session() as s:
        s.add_all(_calibrated_calls("continuation"))
        s.commit()
        report = preflight(s, _settings(), broker=broker, edge_dir=edge_dir)

    assert isinstance(report, PreflightReport)
    assert report.go is True
    # every safety-critical check is ok.
    assert all(c.ok for c in report.checks if c.critical)
    assert _check(report, "config").ok
    assert _check(report, "reachable").ok
    assert _check(report, "funded").ok
    assert _check(report, "caps").ok


# --- funding: an unfunded / non-ACTIVE broker -> NO-GO on the funding line ----
def test_no_go_when_buying_power_is_zero(tmp_path) -> None:
    edge_dir = _ready_edge_dir(tmp_path)
    broker = FakeBroker(buying_power=0.0, status="ACTIVE")
    with _session() as s:
        s.add_all(_calibrated_calls("continuation"))
        s.commit()
        report = preflight(s, _settings(), broker=broker, edge_dir=edge_dir)

    assert report.go is False
    funded = _check(report, "funded")
    assert funded.ok is False
    assert funded.critical is True
    # reachability still passed (get_account succeeded); the failure is funding.
    assert _check(report, "reachable").ok is True


def test_no_go_when_status_not_active(tmp_path) -> None:
    edge_dir = _ready_edge_dir(tmp_path)
    broker = FakeBroker(buying_power=50_000.0, status="HALTED")
    with _session() as s:
        s.add_all(_calibrated_calls("continuation"))
        s.commit()
        report = preflight(s, _settings(), broker=broker, edge_dir=edge_dir)

    assert report.go is False
    assert _check(report, "funded").ok is False


# --- caps: a missing cap -> NO-GO on the caps line ---------------------------
def test_no_go_when_a_cap_is_missing(tmp_path) -> None:
    edge_dir = _ready_edge_dir(tmp_path)
    broker = FakeBroker()
    with _session() as s:
        s.add_all(_calibrated_calls("continuation"))
        s.commit()
        report = preflight(
            s, _settings(max_concurrent=None), broker=broker, edge_dir=edge_dir)

    assert report.go is False
    caps = _check(report, "caps")
    assert caps.ok is False
    assert caps.critical is True
    assert "max_concurrent" in caps.detail


# --- reachability: a raising broker -> NO-GO, NEVER raises --------------------
def test_no_go_when_broker_raises_and_never_propagates(tmp_path) -> None:
    edge_dir = _ready_edge_dir(tmp_path)
    broker = _RaisingBroker()
    with _session() as s:
        s.add_all(_calibrated_calls("continuation"))
        s.commit()
        # The call MUST NOT raise -- a down broker is a NO-GO line, not a crash.
        report = preflight(s, _settings(), broker=broker, edge_dir=edge_dir)

    assert report.go is False
    reachable = _check(report, "reachable")
    assert reachable.ok is False
    assert reachable.critical is True
    # Leak posture: the detail carries the exception CLASS only -- broker messages
    # embed venue hosts/URLs/credentials and this detail reaches the cockpit wire.
    assert reachable.detail == "broker error (RuntimeError)"
    assert "connection refused" not in reachable.detail
    # funding can't be evaluated without an account -> it is also not ok (not a crash).
    assert _check(report, "funded").ok is False


# --- is_real_money + the autonomy gate are ADVISORY (never flip GO) -----------
def test_is_real_money_and_gate_are_advisory_not_critical(tmp_path) -> None:
    edge_dir = _ready_edge_dir(tmp_path)
    broker = FakeBroker(real_money=True)
    with _session() as s:
        s.add_all(_calibrated_calls("continuation"))
        s.commit()
        report = preflight(s, _settings(), broker=broker, edge_dir=edge_dir)

    real = _check(report, "is_real_money")
    gate = _check(report, "autonomy_gate")
    assert real.critical is False
    assert gate.critical is False


def test_go_stays_true_even_when_gate_not_ready(tmp_path) -> None:
    # No edge files -> the autonomy gate is NOT ready, but it is advisory, so GO still holds
    # when every safety-critical check passes.
    edge_dir = tmp_path / "edge"
    edge_dir.mkdir()
    broker = FakeBroker(buying_power=50_000.0, status="ACTIVE")
    with _session() as s:
        report = preflight(s, _settings(), broker=broker, edge_dir=edge_dir)

    assert _check(report, "autonomy_gate").ok is False  # advisory: not ready
    assert report.go is True  # ...but GO holds: every CRITICAL check passed


# --- the guardrails brake: critical on REAL money, advisory on paper ----------
def _set_breakers(session: Session) -> None:
    """Set the three MANDATORY breakers through guardrails_repo's own API (never raw
    SQL -- every brake write is event-audited and the state machine owns the columns)."""
    gr.edit_limits(session, source="test", max_daily_loss_usd=500.0,
                   max_trades_per_day=3, max_drawdown_usd=1_000.0)


def test_preflight_guardrails_check_fails_real_money_when_mandate_unset(tmp_path) -> None:
    """A REAL-money host with a mandatory breaker unset is a NO-GO: the brake mandate
    (``guardrails_mandate_ok``) is the same one execution enforces at submit time, so
    preflight must not hand out a GO the first live order would be refused under. The
    detail is the mandate's own reason string, naming the FIRST missing breaker."""
    edge_dir = _ready_edge_dir(tmp_path)
    broker = FakeBroker(buying_power=50_000.0, status="ACTIVE", real_money=True)
    with _session() as s:
        s.add_all(_calibrated_calls("continuation"))
        s.commit()
        report = preflight(s, _settings(), broker=broker, edge_dir=edge_dir)

    check = _check(report, "guardrails")
    assert check.ok is False
    assert check.critical is True
    assert check.detail == "max_daily_loss_usd is not set"
    assert report.go is False
    # nothing else failed: the brake alone carries this NO-GO.
    assert [c.name for c in report.checks if c.critical and not c.ok] == ["guardrails"]


def test_preflight_guardrails_check_fails_real_money_when_tripped(tmp_path) -> None:
    """Every breaker SET but the brake TRIPPED is still a NO-GO on real money -- a
    tripped brake blocks dispatch, so arming into one would only produce rejections."""
    edge_dir = _ready_edge_dir(tmp_path)
    broker = FakeBroker(buying_power=50_000.0, status="ACTIVE", real_money=True)
    with _session() as s:
        s.add_all(_calibrated_calls("continuation"))
        s.commit()
        _set_breakers(s)
        assert gr.trip(s, breaker="max_daily_loss_usd", reason="daily loss breach",
                       source="test") is not None
        report = preflight(s, _settings(), broker=broker, edge_dir=edge_dir)

    check = _check(report, "guardrails")
    assert check.ok is False
    assert check.critical is True
    assert check.detail == "guardrails state is tripped"
    assert report.go is False


def test_preflight_guardrails_green_on_real_money_when_breakers_set(tmp_path) -> None:
    """The arming floor a human actually needs: real money, all three breakers set and
    the brake 'ok' -> the check passes AS a critical line and GO holds."""
    edge_dir = _ready_edge_dir(tmp_path)
    broker = FakeBroker(buying_power=50_000.0, status="ACTIVE", real_money=True)
    with _session() as s:
        s.add_all(_calibrated_calls("continuation"))
        s.commit()
        _set_breakers(s)
        report = preflight(s, _settings(), broker=broker, edge_dir=edge_dir)

    check = _check(report, "guardrails")
    assert check.ok is True
    assert check.critical is True
    # internally formatted only -- this detail reaches the cockpit wire.
    assert check.detail == "all mandatory breakers set, brake state ok"
    assert report.go is True


def test_preflight_guardrails_advisory_on_paper_host(tmp_path) -> None:
    """A PAPER host with the brake unset: the check is present and HONEST (ok False --
    the breakers really are unset, and the operator should see that before a flip), but
    ADVISORY -- fake money never blocks on the brake, exactly as execution's mandate
    block sits inside ``is_real_money()``. GO is unaffected."""
    edge_dir = _ready_edge_dir(tmp_path)
    broker = FakeBroker(buying_power=50_000.0, status="ACTIVE")  # paper host
    with _session() as s:
        s.add_all(_calibrated_calls("continuation"))
        s.commit()
        report = preflight(s, _settings(), broker=broker, edge_dir=edge_dir)

    check = _check(report, "guardrails")
    assert check.critical is False
    assert check.ok is False              # honest: the breakers ARE unset
    assert check.detail == "max_daily_loss_usd is not set"
    assert report.go is True              # ...but paper money never gates on the brake


# --- broker=None: the cockpit's default local setup is a REPORT, not a crash --
def test_none_broker_is_a_no_go_report_never_a_crash(tmp_path) -> None:
    """``broker=None`` with SWING_BROKER unset (the cockpit's default local setup):
    config is the NO-GO 'no broker configured' line -- the REAL ``_check_config``
    output, not a hardcoded string; reachable/funded/is_real_money read as
    explicit not-applicable lines (never evaluated against a missing client, never
    an AttributeError); caps + the autonomy gate stay REAL -- neither needs the
    broker. ``go`` is False."""
    edge_dir = _ready_edge_dir(tmp_path)
    with _session() as s:
        s.add_all(_calibrated_calls("continuation"))
        s.commit()
        report = preflight(s, _settings(broker=""), broker=None, edge_dir=edge_dir)

    assert report.go is False
    config = _check(report, "config")
    assert config.ok is False and config.critical is True
    assert "no broker configured" in config.detail
    for name in ("reachable", "funded"):
        check = _check(report, name)
        assert check.ok is False and check.critical is True
        assert check.detail == "not applicable -- no broker"
    real = _check(report, "is_real_money")
    assert real.ok is False and real.critical is False
    assert real.detail == "not applicable -- no broker"
    # caps + gate never needed the broker: both evaluated for real.
    assert _check(report, "caps").ok is True
    assert _check(report, "autonomy_gate").ok is True
    # guardrails evaluates for REAL too (it reads the DB, not the venue) but stays
    # ADVISORY: with no client to ask, real-vs-paper is UNKNOWN -- and ``go`` is
    # already False here on reachable/funded, so a critical line would add noise,
    # never safety.
    guardrails = _check(report, "guardrails")
    assert guardrails.ok is False and guardrails.critical is False


def test_none_broker_with_configured_settings_keeps_config_honest(tmp_path) -> None:
    """``broker=None`` while SWING_BROKER IS set -- the cockpit's raising-factory
    degrade path: the config line evaluates the settings for real and must NOT
    claim 'SWING_BROKER is unset' (the factory failed; the configuration did not).
    ``go`` stays False -- reachable/funded are still critical failures."""
    edge_dir = _ready_edge_dir(tmp_path)
    with _session() as s:
        s.add_all(_calibrated_calls("continuation"))
        s.commit()
        report = preflight(s, _settings(), broker=None, edge_dir=edge_dir)

    assert report.go is False
    config = _check(report, "config")
    assert config.ok is True
    assert "broker=alpaca" in config.detail
    assert "SWING_BROKER is unset" not in config.detail
    assert _check(report, "reachable").detail == "not applicable -- no broker"


def test_none_broker_keeps_the_caps_check_real(tmp_path) -> None:
    """A missing cap must still read as the caps NO-GO even without a broker --
    the cockpit's safety screen shows the caps mandate on the default local setup."""
    edge_dir = tmp_path / "edge"
    edge_dir.mkdir()
    with _session() as s:
        report = preflight(
            s, _settings(max_concurrent=None), broker=None, edge_dir=edge_dir)

    assert report.go is False
    caps = _check(report, "caps")
    assert caps.ok is False
    assert "max_concurrent" in caps.detail


# --- config coherence (critical) ---------------------------------------------
def test_no_go_when_broker_not_configured(tmp_path) -> None:
    edge_dir = _ready_edge_dir(tmp_path)
    broker = FakeBroker()
    with _session() as s:
        report = preflight(
            s, _settings(broker=""), broker=broker, edge_dir=edge_dir)
    assert report.go is False
    config = _check(report, "config")
    assert config.ok is False
    assert config.critical is True


# --- READ-ONLY: preflight decides NOTHING (load-bearing) ---------------------
def test_preflight_writes_nothing_but_the_guardrails_row_seed(tmp_path) -> None:
    """Preflight books no work of its own: no calls, no tickets, nothing queued.

    The ONE row it can create is the guardrails get-or-create seed -- reading the
    brake through ``guardrails_mandate_ok`` materialises the single default
    ``agent_guardrails`` row (state 'ok', every breaker unset) exactly as every other
    brake reader does. It is idempotent (a second preflight adds no second row), it
    is never a STATE change, and the row it seeds is the most restrictive answer the
    mandate has -- so it can only ever make preflight say NO-GO, never GO."""
    edge_dir = _ready_edge_dir(tmp_path)
    broker = FakeBroker()
    with _session() as s:
        s.add_all(_calibrated_calls("continuation"))
        s.commit()
        before_calls = s.query(AnalystCall).count()
        before_logs = s.query(ExecutionLog).count()

        preflight(s, _settings(), broker=broker, edge_dir=edge_dir)
        preflight(s, _settings(), broker=broker, edge_dir=edge_dir)

        # No new rows of any kind; the call book is unchanged; nothing queued on the session.
        assert s.query(AnalystCall).count() == before_calls
        assert s.query(ExecutionLog).count() == before_logs
        assert before_logs == 0
        assert not s.new and not s.dirty and not s.deleted
        # the seed, and ONLY the seed: one row, default state, no audit event.
        assert s.query(AgentGuardrails).count() == 1
        assert gr.load_guardrails(s).state == "ok"
        assert s.query(AgentGuardrailEvent).count() == 0


def test_preflight_never_changes_the_brake_state(tmp_path) -> None:
    """A TRIPPED brake is still tripped after preflight: the check reads the state and
    reports it -- it never clears, never halts, never re-trips (arming and releasing
    both stay human acts, performed elsewhere)."""
    edge_dir = _ready_edge_dir(tmp_path)
    broker = FakeBroker(real_money=True)
    with _session() as s:
        _set_breakers(s)
        assert gr.trip(s, breaker="loss_streak_halt", reason="streak", source="test")
        before = gr.load_guardrails(s)

        preflight(s, _settings(), broker=broker, edge_dir=edge_dir)

        assert gr.load_guardrails(s) == before  # state, trip_id, sweep_state: untouched


def test_preflight_does_not_mutate_settings_or_env(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SWING_EXECUTION_MODE", "off")
    edge_dir = _ready_edge_dir(tmp_path)
    settings = _settings()
    broker = FakeBroker()
    with _session() as s:
        preflight(s, settings, broker=broker, edge_dir=edge_dir)
    import os

    # The settings snapshot is frozen + unchanged; the env the gate would read is untouched.
    assert settings == replace(settings)
    assert settings.execution_mode == "live"
    assert os.environ["SWING_EXECUTION_MODE"] == "off"


def test_preflight_does_not_touch_edge_files(tmp_path) -> None:
    edge_dir = _ready_edge_dir(tmp_path)
    before = {p.name: p.read_bytes() for p in edge_dir.iterdir()}
    broker = FakeBroker()
    with _session() as s:
        preflight(s, _settings(), broker=broker, edge_dir=edge_dir)
    after = {p.name: p.read_bytes() for p in edge_dir.iterdir()}
    assert before == after


# --- render ------------------------------------------------------------------
def test_render_preflight_is_human_readable_with_disclaimer(tmp_path) -> None:
    edge_dir = _ready_edge_dir(tmp_path)
    broker = FakeBroker(buying_power=50_000.0, status="ACTIVE")
    with _session() as s:
        s.add_all(_calibrated_calls("continuation"))
        s.commit()
        report = preflight(s, _settings(), broker=broker, edge_dir=edge_dir)
    text = render_preflight(report)

    # the GO/NO-GO headline + a per-check checklist + the "does not arm" disclaimer.
    assert "GO" in text.upper()
    assert "reachable" in text
    assert "funded" in text
    # the disclaimer that preflight arms nothing.
    assert "arm" in text.lower()
    # a ✓ or ✗ marker per check.
    assert "✓" in text or "✗" in text


def test_render_preflight_shows_no_go_for_a_failed_report(tmp_path) -> None:
    edge_dir = _ready_edge_dir(tmp_path)
    broker = FakeBroker(buying_power=0.0)
    with _session() as s:
        s.add_all(_calibrated_calls("continuation"))
        s.commit()
        report = preflight(s, _settings(), broker=broker, edge_dir=edge_dir)
    text = render_preflight(report)
    assert "NO-GO" in text.upper()


def test_preflight_report_is_frozen() -> None:
    report = PreflightReport(go=True, checks=[])
    with pytest.raises((AttributeError, TypeError)):
        report.go = False  # type: ignore[misc]
