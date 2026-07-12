"""The read-only preflight GO/NO-GO check -- run before a human flips to Alpaca-live.

Before anyone arms a REAL-money endpoint they run ``preflight``: is the broker reachable +
funded, are all the hard-limit caps set, does ``is_real_money`` match the configured host, is
the autonomy gate ready? It answers ONE question -- *should a human flip the switch?* -- and it
answers it WITHOUT touching anything. It is strictly READ-ONLY (North Star #1/#3): it performs
NO writes (no DB rows, no settings/env mutation, no edge-file rewrite) and **NEVER arms**. It
only reads the broker, the settings snapshot, and the advisory autonomy gate.

The checks split into SAFETY-CRITICAL and ADVISORY:

* ``config`` / ``reachable`` / ``funded`` / ``caps`` are CRITICAL -- ``go`` is True iff EVERY
  critical check passes. These are the can-this-safely-run questions.
* ``is_real_money`` / ``autonomy_gate`` are ADVISORY (warn, not critical) -- a heads-up the
  human weighs, never a hard gate. (The autonomy gate is itself advisory: the human decides
  whether the edge is proven enough; preflight just surfaces its verdict.)

A BROKER error is NEVER allowed to propagate: the reachability check wraps ``get_account`` in
try/except and records a NO-GO line, so a down broker reads as NO-GO rather than a crash. A
MISSING broker (``broker=None`` -- the cockpit's default local setup) is likewise a report,
not a crash: the CLI's early-exit wording rendered as checklist lines (see ``preflight``).
"""

import argparse
import logging
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from swing_screener.db.session import get_engine
from swing_screener.pipeline.autonomy import autonomy_gate
from swing_screener.pipeline.broker import BrokerAccount, BrokerClient
from swing_screener.pipeline.broker_alpaca import build_broker
from swing_screener.settings import (
    Settings,
    load_settings,
    real_money_limits_ok,
    resolve_edge_dir,
    resolve_execution,
)

log = logging.getLogger(__name__)

_EDGE_DIR = Path("edge")


@dataclass(frozen=True)
class PreflightCheck:
    """One line on the preflight checklist. ``critical`` marks the safety-critical checks that
    drive the GO/NO-GO verdict; advisory checks (``critical=False``) are surfaced as a heads-up
    but never flip ``go``."""

    name: str
    ok: bool
    detail: str
    critical: bool


@dataclass(frozen=True)
class PreflightReport:
    """The preflight verdict. ``go`` is True iff EVERY safety-critical check passed. ``checks``
    is the full checklist (critical + advisory), in evaluation order, for rendering."""

    go: bool
    checks: list[PreflightCheck]


def _check_config(settings: Settings) -> PreflightCheck:
    """CRITICAL: ``execution_mode`` + ``broker`` are coherent -- a broker must be selected, and
    the mode must be one that actually trades (``paper`` / ``live``)."""
    if not settings.broker:
        return PreflightCheck(
            "config", False, "no broker configured (SWING_BROKER is unset)", True)
    if settings.execution_mode not in ("paper", "live"):
        return PreflightCheck(
            "config", False,
            f"execution_mode is {settings.execution_mode!r} (not paper/live)", True)
    return PreflightCheck(
        "config", True,
        f"broker={settings.broker}, execution_mode={settings.execution_mode}", True)


def _check_reachable(
    broker: BrokerClient,
) -> tuple[PreflightCheck, BrokerAccount | None]:
    """CRITICAL: ``broker.get_account()`` succeeds. A broker error is CAUGHT -> a NO-GO line
    (never raised). Returns the check + the account (None when unreachable) so the funding check
    can reuse it without a second call."""
    try:
        account = broker.get_account()
    except Exception as exc:  # noqa: BLE001 -- a down broker is a NO-GO, never a crash
        return PreflightCheck("reachable", False, str(exc), True), None
    return PreflightCheck("reachable", True, f"account status {account.status}", True), account


def _check_funded(account: BrokerAccount | None) -> PreflightCheck:
    """CRITICAL: the account is ``ACTIVE`` AND ``buying_power > 0``. With no account (the broker
    was unreachable) this can't be evaluated -> not ok (and not a crash)."""
    if account is None:
        return PreflightCheck(
            "funded", False, "no account (broker unreachable)", True)
    if account.status != "ACTIVE":
        return PreflightCheck(
            "funded", False, f"account status is {account.status!r} (not ACTIVE)", True)
    if account.buying_power <= 0:
        return PreflightCheck(
            "funded", False, f"buying_power is {account.buying_power} (not > 0)", True)
    return PreflightCheck(
        "funded", True,
        f"ACTIVE, buying_power={account.buying_power}, cash={account.cash}", True)


def _check_caps(settings: Settings) -> PreflightCheck:
    """CRITICAL: every hard-limit cap is set -- real money may never run uncapped
    (``real_money_limits_ok``)."""
    _mode, limits = resolve_execution(settings)
    ok, reason = real_money_limits_ok(limits)
    detail = "all caps set" if ok else reason
    return PreflightCheck("caps", ok, detail, True)


def _check_is_real_money(broker: BrokerClient) -> PreflightCheck:
    """ADVISORY (warn): report whether this broker trades real money (a heads-up the human
    weighs, never a hard gate). Always ``ok`` -- it's informational, not pass/fail."""
    real = broker.is_real_money()
    detail = (
        "live host -> REAL money" if real else "paper host -> fake money (paper)")
    return PreflightCheck("is_real_money", True, detail, False)


def _check_gate(session: Session, *, edge_dir: Path) -> PreflightCheck:
    """ADVISORY (warn): surface the autonomy gate's ``ready`` verdict. The gate is itself
    advisory (the human decides), so this never flips GO -- it's a readiness heads-up. READ-ONLY
    (``autonomy_gate`` only reads the verdicts sidecars + the scored-call book)."""
    report = autonomy_gate(session, edge_dir=edge_dir)
    detail = "autonomy gate READY" if report.ready else "autonomy gate NOT ready"
    return PreflightCheck("autonomy_gate", report.ready, detail, False)


#: The explicit not-applicable detail every broker-shaped check wears when there is
#: no broker to ask (``preflight(broker=None)``) -- mirrors ``main()``'s early exit.
_NO_BROKER_DETAIL = "not applicable -- no broker"


def preflight(
    session: Session,
    settings: Settings,
    *,
    broker: BrokerClient | None,
    edge_dir: Path = _EDGE_DIR,
) -> PreflightReport:
    """Run the read-only GO/NO-GO preflight check. NO writes, NO arming.

    Evaluates six checks in order -- config / reachable / funded / caps (CRITICAL) +
    is_real_money / autonomy_gate (ADVISORY) -- and returns a ``PreflightReport`` whose ``go``
    is True iff every CRITICAL check passed. A broker error never propagates (the reachability
    check catches it). ``broker=None`` (the cockpit's default local setup) is the CLI's
    early-exit wording as a report, never a crash: config reads the NO-GO 'no broker
    configured' line; reachable / funded / is_real_money read as explicit not-applicable
    lines; caps and the autonomy gate stay REAL (neither needs the broker); ``go`` is False.
    This function performs NO writes: it only reads the broker, the settings
    snapshot, and the advisory gate (a SELECT over the scored-call book + the verdicts sidecars).
    It NEVER mutates ``execution_mode``, settings, env, or any edge file -- arming stays a human
    act, performed elsewhere."""
    if broker is None:
        checks = [
            PreflightCheck(
                "config", False, "no broker configured (SWING_BROKER is unset)", True),
            PreflightCheck("reachable", False, _NO_BROKER_DETAIL, True),
            PreflightCheck("funded", False, _NO_BROKER_DETAIL, True),
            _check_caps(settings),
            PreflightCheck("is_real_money", False, _NO_BROKER_DETAIL, False),
            _check_gate(session, edge_dir=edge_dir),
        ]
    else:
        config = _check_config(settings)
        reachable, account = _check_reachable(broker)
        funded = _check_funded(account)
        caps = _check_caps(settings)
        is_real = _check_is_real_money(broker)
        gate = _check_gate(session, edge_dir=edge_dir)
        checks = [config, reachable, funded, caps, is_real, gate]

    go = all(c.ok for c in checks if c.critical)
    return PreflightReport(go=go, checks=checks)


def render_preflight(report: PreflightReport) -> str:
    """Render the preflight report as a human-readable checklist (for the CLI). PURE. Carries
    the GO/NO-GO headline + a ✓/✗ line per check + the disclaimer that preflight arms NOTHING."""
    headline = "GO" if report.go else "NO-GO"
    lines = [
        "# Preflight -- live arming readiness (READ-ONLY)",
        "",
        f"Verdict: {headline}",
        "",
        "This is a READ-ONLY check. It does NOT arm anything and moves no money; "
        "flipping to live stays a deliberate human act, done elsewhere.",
        "",
    ]
    for c in report.checks:
        mark = "✓" if c.ok else "✗"
        tag = "" if c.critical else " (advisory)"
        lines.append(f"- {mark} {c.name}{tag}: {c.detail}")
    lines.append("")
    if report.go:
        lines.append("All safety-critical checks passed: a human MAY proceed to arm.")
    else:
        failed = [c.name for c in report.checks if c.critical and not c.ok]
        lines.append(f"NO-GO -- failing critical check(s): {', '.join(failed)}.")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Read-only preflight GO/NO-GO check: is the broker reachable + funded, "
                    "are all caps set, does is_real_money match the host, is the autonomy gate "
                    "ready? READ-ONLY -- it arms NOTHING and moves no money.")
    # None -> the shared env-first resolution (SWING_EDGE_DIR), so the GO/NO-GO check
    # reads the SAME directory as the digest instead of a second cwd-relative default.
    parser.add_argument("--edge-dir", type=Path, default=None)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)

    settings = load_settings()
    broker = build_broker(settings)
    if broker is None:
        print(  # noqa: T201 -- the CLI's whole job is to print the verdict
            "NO-GO: no broker configured (set SWING_BROKER, e.g. 'alpaca').")
        return
    engine = get_engine(settings.db_url)
    with Session(engine) as session:
        report = preflight(session, settings, broker=broker,
                           edge_dir=resolve_edge_dir(args.edge_dir))
    print(render_preflight(report))  # noqa: T201 -- the CLI prints the checklist


if __name__ == "__main__":
    main()
