"""The execution-safety surface: the advisory gate, the DISARM runbook action, and
the Execution Safety report. Moved verbatim out of ``cockpit/api.py``; lock
semantics (single-flight, non-blocking acquire, release in the outer ``finally``)
and every status code are unchanged."""

import logging
import threading
from collections.abc import Callable, Iterator
from dataclasses import asdict, replace
from datetime import UTC, date, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from swing_screener.cockpit.common import (
    ActionNonce,
    _armed_symbols,
    _bracket,
    _require_cockpit,
)
from swing_screener.cockpit.livedata import BrokerSnapshot, Snapshot
from swing_screener.cockpit.spend import spend_rows_since
from swing_screener.db.models import DisarmEvent
from swing_screener.db.repo import latest_recorded_stop
from swing_screener.pipeline.autonomy import autonomy_gate, gate_countdown
from swing_screener.pipeline.broker import BrokerClient, BrokerOrder
from swing_screener.pipeline.disarm import ensure_stop_protection, pull_entry_orders
from swing_screener.pipeline.preflight import (
    PreflightReport,
    broker_error_detail,
    preflight,
)
from swing_screener.config import StrategyConfig
from swing_screener.options.config import GexConfig
from swing_screener.settings import (
    load_settings,
    real_money_limits_ok,
    resolve_edge_dir,
    resolve_execution,
    resolve_risk_unit,
)


log = logging.getLogger(__name__)


def _record_disarm(session: Session, *, reason: str, orders_cancelled: int) -> None:
    """Persist a DisarmEvent so the System Behavior Auditor can see an unexpected
    disarm. Best-effort: a disarm (complete OR partial) has moved venue state, so
    a failure to log it must not change the response -- a completed run's 200
    stays a 200, and a failed run's 503 must carry the ORIGINAL broker error,
    never a masking DB one. The rollback-first clears any failed transaction the
    request may have left (the SQLAlchemyError path arrives here with a poisoned
    session; add+commit on it would always lose the event) -- safe because this
    endpoint only READS before recording, so there is nothing pending to lose."""
    try:
        session.rollback()
        session.add(DisarmEvent(
            created_at=datetime.now(UTC), reason=reason, orders_cancelled=orders_cancelled))
        session.commit()
    except Exception:  # noqa: BLE001 -- audit logging is best-effort; the disarm stands
        log.warning("failed to persist DisarmEvent", exc_info=True)
        try:
            session.rollback()
        except Exception:  # noqa: BLE001 -- a dead session must not mask the response either
            log.warning("DisarmEvent rollback also failed", exc_info=True)


def _cfg_row(
    key: str, env: str | None, value: object, note: str = ""
) -> dict[str, object]:
    """One CONFIG panel row: display key, the env var that sets it (None for a
    code constant), the CURRENT resolved value, and a short meaning note."""
    return {"key": key, "env": env, "value": value, "note": note}


def build_safety_router(
    *,
    _session: Callable[[], Iterator[Session]],
    edge_dir: Path | None,
    resolved_broker_factory: Callable[[], BrokerClient | None],
    broker_snapshot: BrokerSnapshot,
    disarm_lock: threading.Lock,
    action_nonce: ActionNonce,
) -> APIRouter:
    """The gate/DISARM/safety endpoints, closed over the app's seams: the session
    dependency, the edge dir, the resolved broker factory (DISARM needs a LIVE
    client), the cached venue snapshot (also parked on ``app.state``), the DISARM
    single-flight lock (also parked on ``app.state`` for tests), and the
    post-action wake nonce (bumped by a REAL disarm run)."""
    router = APIRouter()

    @router.get("/api/gate")
    def gate(session: Session = Depends(_session)) -> dict[str, object]:
        """The advisory autonomy gate + today's analyst spend, as one status object.

        ``ready`` and ``countdown`` come from ``pipeline.autonomy`` VERBATIM -- the
        countdown's line format is pinned by tests/pipeline/test_autonomy_countdown.py,
        so the arithmetic is never reimplemented here. A missing verdicts sidecar
        reads as not-ready (the gate's own missing-file posture), never an error.
        ``execution_mode`` reads the env-backed settings at request time;
        ``analyst_spend_today_usd`` sums ``est_cost_usd`` over TODAY's LLM spend
        UNIONED across analyst calls + Journal v2 coach/audit rows (NULL costs --
        the deterministic/fallback path -- count 0.0).
        ``broker_configured`` is settings TRUTHINESS (is ``SWING_BROKER`` set),
        NEVER connectivity: DISARM's enablement keys on it, and it rides this
        already-polled endpoint so the always-visible masthead needs no extra poll.
        """
        report = autonomy_gate(session, edge_dir=resolve_edge_dir(edge_dir))
        today = date.today()
        # union across analyst calls + Journal v2 coach/audit spend (NULL costs -> 0.0)
        spend_today = sum(
            (c or 0.0) for d, c in spend_rows_since(session, today) if d == today
        )
        settings = load_settings()
        return {
            "ready": report.ready,
            "countdown": gate_countdown(report),
            "execution_mode": settings.execution_mode,
            "broker_configured": bool(settings.broker),
            "analyst_spend_today_usd": spend_today,
        }

    @router.post("/api/disarm", dependencies=[Depends(_require_cockpit)])
    def disarm_book(
        dry_run: bool = Query(default=False),
        session: Session = Depends(_session),
    ) -> dict[str, object]:
        """The DISARM runbook step over HTTP: pull entry-side orders, keep the stops.

        Semantics are ``pipeline.disarm``'s, exactly: ONLY ``side == 'buy'`` open
        orders are cancelled (a blanket cancel would strip the bracket stop legs off
        the very positions disarm deliberately does NOT close -- the 2026-07-04
        bug); every remaining position must end stop-protected, a dead stop leg
        re-submitted as a plain GTC stop at the ExecutionLog ticket's RECORDED
        level (COPIED, never computed -- North Star #4); no recorded level -> the
        position is named in ``unprotected`` and LEFT ALONE (never auto-closed --
        North Star #3). Header-guarded (``_require_cockpit``). SINGLE-FLIGHT:
        two concurrent real runs could both read the open-order list before
        either cancels, and the per-second ``key_suffix`` only collapses stop
        re-submits landing within the same second -- overlapping runs risk
        DUPLICATE live GTC sell stops (2x the position: triggered, the second
        sells short). The lock is acquired NON-blocking; the loser answers 409
        'disarm already in flight' without touching the venue. The broker comes
        from the FACTORY seam, live per request -- the cached snapshot is a READ
        and nothing to cancel through. A None factory answer (no broker
        configured) is a 409 STATE, not a crash; a broker/factory error is a 503
        carrying the exception CLASS only (leak posture). ``dry_run=1`` previews
        -- ``cancelled`` / ``stops_restored`` say what a real run WOULD do, the
        venue is untouched. After a REAL run the broker snapshot cache is
        invalidated, the post-action nonce bumps, AND a DisarmEvent is persisted
        for the System Behavior Auditor (all three in ``finally`` -- a partial
        disarm has still moved venue state), so the UI never renders pre-disarm
        orders for up to a TTL, other windows wake immediately, and the Auditor's
        breach scan sees the attempt even when the run 503s partway (reason
        ``cockpit`` on completion, ``cockpit-partial`` on the failure path).
        """
        if not disarm_lock.acquire(blocking=False):
            raise HTTPException(status_code=409, detail="disarm already in flight")
        try:
            try:
                broker = resolved_broker_factory()
            except Exception as exc:
                raise HTTPException(
                    status_code=503, detail=broker_error_detail(exc)
                ) from exc
            if broker is None:
                raise HTTPException(status_code=409, detail="no broker configured")
            # Pre-bound so the FAILURE path can report how many entries were pulled
            # before the raise: pull_entry_orders raising mid-cancel leaves the name
            # unbound, and 0 ("we don't know that any cancel landed") is the honest
            # floor -- never an invented count.
            entries: list[BrokerOrder] = []
            completed = False
            try:
                entries, sells = pull_entry_orders(broker, dry_run=dry_run)
                restored, unprotected = ensure_stop_protection(
                    broker, lambda sym: latest_recorded_stop(session, sym),
                    key_suffix=f"cockpit-{datetime.now(UTC):%Y%m%d%H%M%S}",
                    dry_run=dry_run)
                completed = True
            except SQLAlchemyError:
                raise  # the app-level handler's 503: a DB failure is not a broker error
            except Exception as exc:
                raise HTTPException(
                    status_code=503, detail=broker_error_detail(exc)
                ) from exc
            finally:
                if not dry_run:
                    # A PARTIAL disarm has still moved venue state, so ALL THREE run
                    # on the failure path too: invalidate FIRST (a woken fetch must
                    # never hit the stale cache), then the post-action wake -- the
                    # venue calls that returned before a raise are durable, and
                    # other windows must refetch NOW, not at the 60s poll floor;
                    # staleness is scariest on exactly this action. A dry run does
                    # none of them: it changed nothing, and Task 15's hold-to-confirm
                    # fires a preview on EVERY hold-start -- waking all windows
                    # per hold would be noise.
                    broker_snapshot.invalidate()
                    action_nonce.bump()
                    # Every REAL attempt is recorded for the Auditor -- the breach
                    # scan exists to flag disarms, and a disarm that cancelled the
                    # entries then died restoring stops has moved MORE alarming
                    # venue state than a clean one, not less. 'cockpit-partial'
                    # names the failure path; best-effort (_record_disarm never
                    # raises), so a failed event write cannot mask the 503.
                    _record_disarm(
                        session,
                        reason="cockpit" if completed else "cockpit-partial",
                        orders_cancelled=len(entries))
            return {
                "dry_run": dry_run,
                "cancelled": [{"symbol": o.symbol,
                               "broker_order_id": o.broker_order_id}
                              for o in entries],
                "sells_kept": len(sells),
                "stops_restored": restored,
                "unprotected": unprotected,
            }
        finally:
            disarm_lock.release()

    @router.get("/api/execution/safety")
    def execution_safety(session: Session = Depends(_session)) -> dict[str, object]:
        """The Execution Safety screen in one read: is real money possible, and why not.

        ``broker_configured`` is settings TRUTHINESS, never connectivity (same rule
        as ``/api/gate``). ``preflight`` is ``pipeline.preflight``'s report over a
        FRESH client from the factory seam; a None factory answer takes the report's
        None-broker shape (the config line evaluated from settings for REAL, explicit
        not-applicable broker lines -- the default local setup is never a 500), and a
        RAISING factory degrades to that same shape -- the config line stays honest
        (the settings ARE configured; the factory failed) and the reachable line
        carries the exception CLASS only (leak posture).
        ``locks`` renders ``can_arm_real_money``'s three components
        individually (``gate_ready`` reuses the report's advisory gate line -- one
        evaluation per request); ``caps_mandate`` is ``real_money_limits_ok`` over
        the resolved limits. ``env_scope`` is the honesty label: everything here
        reads THIS process's env -- the Azure jobs run under their own.
        ``bracket_shield`` reads the CACHED broker snapshot (the venue-truth table:
        see ``_bracket_shield`` -- UNKNOWN is never rendered green).
        """
        settings = load_settings()
        broker: BrokerClient | None
        broker_error: str | None = None
        try:
            broker = resolved_broker_factory()
        except Exception as exc:
            broker = None
            broker_error = broker_error_detail(exc)
        report = preflight(session, settings, broker=broker,
                           edge_dir=resolve_edge_dir(edge_dir))
        if broker_error is not None:
            report = PreflightReport(go=report.go, checks=[
                replace(c, detail=broker_error) if c.name == "reachable" else c
                for c in report.checks])
        # Default False: a renamed/absent gate check degrades to not-green
        # (UNKNOWN is never green), instead of a StopIteration 500.
        gate_ready = next(
            (c.ok for c in report.checks if c.name == "autonomy_gate"), False)
        mode, limits = resolve_execution(settings)
        caps_ok, caps_reason = real_money_limits_ok(limits)
        return {
            "broker_configured": bool(settings.broker),
            "mode": mode,
            "env_scope": "this process",
            "locks": {
                "mode_is_live": mode == "live",
                "allow_real_money": settings.allow_real_money,
                "gate_ready": gate_ready,
            },
            "caps_mandate": {"ok": caps_ok, "reason": caps_reason},
            "preflight": {
                "go": report.go,
                "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail,
                            "critical": c.critical} for c in report.checks],
            },
            "bracket_shield": _bracket_shield(session, broker_snapshot.get()),
        }

    @router.get("/api/config")
    def config() -> dict[str, object]:
        """The live configuration, READ-ONLY BY DESIGN -- the instrument panel
        shows every knob; git remains the control column (North Star #1/#3: no
        config write ever originates in the cockpit, and env is per-process, so
        a cockpit 'edit' could not reach the Azure jobs anyway). Three sections,
        each row ``{key, env, value, note}`` with a per-section ``change_via``
        stating exactly where the real edit lives. Leak posture: secrets are
        never echoed (the one secret-adjacent value here is the broker's
        real-money switch, a boolean); the DB URL is deliberately absent (the
        masthead chip already carries its safe label). ``env_scope`` mirrors
        the safety report's honesty label: these are THIS process's values --
        the Azure jobs run under their own env (infra/main.bicepparam)."""
        s = load_settings()
        risk_unit, max_shares = resolve_risk_unit(s)
        mode, limits = resolve_execution(s)
        strategy = StrategyConfig()
        gex = GexConfig()
        sizing = [
            _cfg_row("account equity", "SWING_ACCOUNT_EQUITY", s.account_equity,
                     "dollars; 1R = equity × risk pct"),
            _cfg_row("risk pct", "SWING_RISK_PCT", s.risk_pct,
                     "fraction of equity risked per trade (default 0.01)"),
            _cfg_row("risk per trade $", "SWING_RISK_PER_TRADE_DOLLARS",
                     s.risk_per_trade_dollars,
                     "explicit 1R override — wins over equity × pct when set"),
            _cfg_row("resolved 1R", None, risk_unit,
                     "the computed risk unit; 0 = sizing unconfigured (R-multiples only)"),
            _cfg_row("max shares", "SWING_MAX_SHARES", max_shares,
                     "hard share cap per order (null = uncapped)"),
        ]
        execution = [
            _cfg_row("execution mode", "SWING_EXECUTION_MODE", mode,
                     "off | manual | paper | live — unknown coerces to off (fail-safe)"),
            _cfg_row("broker", "SWING_BROKER", s.broker or None,
                     "venue for brackets/DISARM; unset = lamps UNKNOWN, DISARM disabled"),
            _cfg_row("allow real money", "SWING_BROKER_ALLOW_REAL_MONEY",
                     s.allow_real_money,
                     "one of the three live locks — false blocks live arming"),
            _cfg_row("max daily notional $", "SWING_MAX_DAILY_NOTIONAL",
                     limits.max_daily_notional,
                     "per-account-day recorded order notional (null = unbounded)"),
            _cfg_row("max daily loss (R)", "SWING_MAX_DAILY_LOSS",
                     limits.max_daily_loss,
                     "R threshold: new orders blocked once the day sums ≤ −this"),
            _cfg_row("max concurrent", "SWING_MAX_CONCURRENT", limits.max_concurrent,
                     "open positions per account (null = unbounded)"),
        ]
        analyst = [
            _cfg_row("deep analysis", "SWING_DEEP_ANALYSIS", s.deep_analysis_enabled,
                     "the Opus web-search analyst (default off)"),
            _cfg_row("analysis model", "SWING_ANALYSIS_MODEL", s.analysis_model, ""),
            _cfg_row("deep analysis max $/run", "SWING_DEEP_ANALYSIS_MAX_USD",
                     s.deep_analysis_max_usd, "per-run spend ceiling (null = uncapped)"),
            _cfg_row("coach", "SWING_COACH_ENABLED", s.coach_enabled,
                     "Personal Trade Coach (Journal)"),
            _cfg_row("auditor", "SWING_AUDIT_ENABLED", s.audit_enabled,
                     "System Behavior Auditor (System Audit)"),
        ]
        return {
            "env_scope": "this process — the Azure jobs run under their own env",
            "sections": [
                {
                    "title": "sizing",
                    "change_via": "user-level env on this box · infra/main.bicepparam in prod (bicep deploy applies)",
                    "rows": sizing,
                },
                {
                    "title": "execution",
                    "change_via": "user-level env on this box · infra/main.bicepparam in prod (bicep deploy applies)",
                    "rows": execution,
                },
                {
                    "title": "analyst & coaches",
                    "change_via": "user-level env on this box · infra/main.bicepparam or jobs.bicep in prod",
                    "rows": analyst,
                },
                {
                    "title": "engine (StrategyConfig — the incumbent)",
                    "change_via": "config.py via PR — the optimizer proposes, evidence gates, a human merges (North Star #1)",
                    "rows": [
                        _cfg_row(k, None, v, "")
                        for k, v in asdict(strategy).items()
                    ],
                },
                {
                    "title": "GEX lab (GexConfig)",
                    "change_via": "options/config.py via PR — lab knobs never live in the equity config",
                    "rows": [
                        _cfg_row(
                            k, None,
                            list(v) if isinstance(v, tuple) else v, "",
                        )
                        for k, v in asdict(gex).items()
                    ],
                },
            ],
        }

    return router


def _bracket_shield(session: Session, snapshot: Snapshot | None) -> dict[str, object]:
    """The safety screen's venue-truth table: one row per VENUE position, each with
    its ``_bracket`` state -- ``armed`` (a live protective sell stop at the venue),
    ``db-only`` (only a recorded ExecutionLog level -- ``latest_recorded_stop``,
    the same lookup disarm restores from), or ``unprotected`` (no level anywhere,
    loud). No snapshot (no broker configured, or the venue read failed/degraded)
    -> ``known: False`` with an empty table: absence of evidence is never a claim,
    so UNKNOWN can never render green."""
    if snapshot is None:
        return {"known": False, "as_of": None, "positions": []}
    armed = _armed_symbols(snapshot)
    return {
        "known": True,
        "as_of": snapshot.as_of.isoformat(),
        "positions": [
            {"symbol": pos.symbol, "qty": pos.qty,
             "state": _bracket(pos.symbol, latest_recorded_stop(session, pos.symbol),
                               snapshot=snapshot, armed=armed)}
            for pos in snapshot.positions
        ],
    }
