"""Digest orchestrator: selection -> Claude analysis -> PDF -> email.

Wires the Phase 4 pieces into one idempotent run. For a given ``(kind,
run_date)`` it selects the picks, has Claude narrate each one (with a
deterministic fallback baked into ``analyze_signal``), builds the attachment
PDF, and sends the digest email plus a standalone exit-alert email when there
are pending exit events.

Idempotency is enforced via an ``EmailLog`` row per ``(kind, run_date)``: a
second run for the same day is a no-op. PDF rendering is best-effort and must
never block the email. The Anthropic client and the SMTP send function are
injectable seams so tests never hit the network or send mail.
"""

import argparse
import hashlib
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from swing_screener.analytics.calibration import max_conviction_step
from swing_screener.config import StrategyConfig
from swing_screener.config_secrets import get_secret
from swing_screener.data.fetch import fetch_bars
from swing_screener.data.universe import names_by_ticker
from swing_screener.db import repo
from swing_screener.db.models import EmailLog, ExitEvent, Signal
from swing_screener.db.session import get_engine
from swing_screener.notify import market_context
from swing_screener.notify import select as sel
from swing_screener.notify.alerts import compose_exit_alert
from swing_screener.notify.analysis import (
    ConvictionResult,
    SignalAnalysis,
    SignalFacts,
    analyze_conviction,
    analyze_signal,
    analyze_signal_deep,
)
from swing_screener.notify.body import (
    AlertLine,
    DigestPick,
    OrderIntentLine,
    OrderTicketLine,
    compose_digest_body,
)
from swing_screener.notify.pdf import PdfPick, build_digest_pdf
from swing_screener.notify.proposals import (
    ProposedOrder,
    build_proposals,
    proposals_html,
    proposals_text,
    write_proposals_artifact,
)
from swing_screener.notify.transport import resolve_sender
from swing_screener.pipeline.autonomy import autonomy_gate, gate_status_line
from swing_screener.pipeline.health import health_line
from swing_screener.pipeline.broker import BrokerClient
from swing_screener.pipeline.broker_alpaca import build_broker
from swing_screener.pipeline.disarm import ensure_stop_protection, pull_entry_orders
from swing_screener.pipeline.execution import (
    ExecutionAdapter,
    LiveAdapter,
    ManualAdapter,
    NoOpAdapter,
    OrderResult,
    PaperAdapter,
)
from swing_screener.pipeline.exitcheck import ExitCheckResult, LatestBarsFn, run_exit_check
from swing_screener.pipeline.insight import (
    OrderIntent,
    build_order_intent,
    conviction_baseline,
    record_analyst_call,
)
from swing_screener.pipeline.regime import MARKET_PROXY, classify_regime
from swing_screener.pipeline.reflect import load_verdicts
from swing_screener.settings import (
    load_settings,
    resolve_edge_dir,
    resolve_execution,
    resolve_risk_unit,
)
from swing_screener.signals.actionability import classify
from swing_screener.storage.blob import blob_enabled, download_bytes

log = logging.getLogger(__name__)

SmtpSend = Callable[..., None]

_PICKERS = {"daily": sel.daily_picks, "weekly": sel.weekly_picks, "monthly": sel.monthly_picks}


@dataclass(frozen=True)
class DigestResult:
    n_picks: int
    pdf_attached: bool
    sent: bool
    n_reversals: int = 0  # reversal-play picks included (daily digest)


def _exit_alert_key(alerts: list[ExitEvent]) -> str:
    """Deterministic key over the SET of exit-event ids (sha1, 40 chars).

    Idempotency is keyed on the *set of events*, not the calendar day, so a later
    hour with a NEW exit yields a different key (and thus a new alert) while a
    re-run over the same events maps to the same key (a no-op). Sorting by id
    makes the key order-independent; it distinguishes {1,2} from {1,2,3} from
    {1,3}. The 40-char sha1 hexdigest fits ``EmailLog.alert_key`` (String(64)).
    """
    ids = ",".join(str(a.id) for a in sorted(alerts, key=lambda a: a.id))
    return hashlib.sha1(ids.encode()).hexdigest()


def _exit_already_sent(session: Session, run_date: date, alert_key: str) -> bool:
    """True if an exit alert for this exact event set already logged on this date."""
    stmt = select(EmailLog).where(
        EmailLog.kind == "exit",
        EmailLog.run_date == run_date,
        EmailLog.alert_key == alert_key,
    )
    return session.scalars(stmt).first() is not None


def _emit_pending_exit_alert(session: Session, run_date: date, recipient: str,
                             smtp_send: SmtpSend) -> bool:
    """Send a standalone exit-alert email if real exit events are pending today.

    Exit alerts are urgent and tracked independently of the digest, keyed on the
    SET of pending exit events (see ``_exit_alert_key``) rather than the calendar
    day. That makes the hourly cadence work: a NEW exit firing later in the
    session produces a fresh key and a new alert, while a re-run over the same
    events is a no-op. Returns True iff an email was sent. Shared by
    ``send_digest`` and the ``exit`` run path.

    Ordering is deliberate: we SEND then LOG (not log-then-send). An exit alert
    can be an urgent hard stop, so we prioritize never LOSING it over strictly
    preventing a rare duplicate -- if the SMTP send fails we leave no log row, so
    the next hourly run retries. The unique constraint plus the
    ``_exit_already_sent`` pre-check make the common sequential re-run a clean
    no-op; the ``IntegrityError`` catch only guards the rare concurrent-replica
    race (which may double-send -- accepted).
    """
    alerts = sel.pending_exit_alerts(session, run_date)
    if not alerts:
        return False
    key = _exit_alert_key(alerts)
    if _exit_already_sent(session, run_date, key):
        return False
    alert_email = compose_exit_alert(alerts, run_date)
    smtp_send(to=recipient, subject=alert_email.subject, text=alert_email.text,
              html=alert_email.html, attachments=[])  # SEND FIRST (see docstring)
    session.add(EmailLog(sent_at=datetime.now(UTC), kind="exit",
                         subject=alert_email.subject, run_date=run_date, alert_key=key))
    try:
        session.commit()
    except IntegrityError:  # lost the concurrent-replica race; the row already exists
        session.rollback()
    return True


def _load_chart_bytes(chart_path: str | None) -> bytes | None:
    """Read a chart PNG's bytes for the deep-analysis image input.

    Mirrors pdf.py: in Azure chart_path is a blob KEY (fetch by key); locally it's
    a filesystem path. Best-effort -- any failure (missing blob/file) yields None,
    and the deep path runs chartless rather than crashing.
    """
    if not chart_path:
        return None
    try:
        if blob_enabled():
            return download_bytes(chart_path)
        p = Path(chart_path)
        return p.read_bytes() if p.exists() else None
    except Exception:  # noqa: BLE001 -- never let a missing chart block the digest
        log.warning("chart load failed for %s; deep analysis runs chartless", chart_path)
        return None


def _load_playbook(edge_dir: Path, play_type: str) -> tuple[str, list] | None:
    """Load ``(playbook_text, verdicts)`` for a play type, or None if either is missing.

    The insight engine needs BOTH the human/LLM-authored playbook prose (``<pt>.md``,
    fed to the analyst) AND the code-owned verdicts sidecar (``<pt>.verdicts.json``,
    which the deterministic baseline keys on). A missing/unparseable sidecar -> None,
    and the caller falls back to the old deep path (today's behavior). Best-effort: any
    read/parse error degrades to None rather than blocking the digest.
    """
    md = edge_dir / f"{play_type}.md"
    sidecar = edge_dir / f"{play_type}.verdicts.json"
    if not (md.exists() and sidecar.exists()):
        return None
    try:
        return md.read_text(encoding="utf-8"), load_verdicts(sidecar.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 -- a broken sidecar must not block the digest
        log.warning("playbook load failed for %s; falling back to deep path", play_type)
        return None


def _default_market_trend() -> str | None:
    """The live SPY trend for the conviction baseline, or None if SPY is unavailable.

    Fetches SPY daily through the same offline-able fetch seam the pipeline uses and
    classifies the regime. Any failure (no data, fetch error) yields None -- the baseline
    simply won't match its market_trend dimension, never raising."""
    try:
        cfg = StrategyConfig()
        cache_dir = load_settings().cache_dir
        spy_daily = fetch_bars(MARKET_PROXY, "1d", cache_dir=cache_dir)
        return classify_regime(spy_daily, cfg).trend
    except Exception:  # noqa: BLE001 -- SPY unavailable -> unknown trend, fail safe
        log.warning("SPY regime unavailable for the digest; baseline trend dim unmatched")
        return None


def _facts(sig: Signal) -> SignalFacts:
    return SignalFacts(
        ticker=sig.ticker, timeframe=sig.timeframe, trade_type=sig.horizon, score=sig.score,
        mtf_aligned=sig.mtf_aligned, quality_tier=sig.quality_tier,
        volatility_tier=sig.volatility_tier, oversold=sig.oversold,
        trigger_close=sig.trigger_close, atr=sig.atr, rsi=sig.rsi, entry_floor=sig.entry_floor,
        entry_ceiling=sig.entry_ceiling, stop=sig.stop, target=sig.target,
    )


def _drop_already_ran(
    signals: list[Signal],
    latest_closes_fn: Callable[[list[str]], dict[str, float]],
) -> list[Signal]:
    """Drop picks whose entry is no longer live at digest time -- with PLAY-TYPE-AWARE
    semantics.

    CONTINUATION picks drop on ``extended`` (ran past the ceiling: the chase the freshness
    gate exists to prevent) and on ``broken`` (stop violated). A REVERSAL pick is a RESTING
    LIMIT with a multi-bar fill window: sitting above its ceiling at digest time is its
    NORMAL state (a confirmed reversal closes above the flip high by definition), so
    ``extended`` is kept and only ``broken`` drops it -- the old drop-on-extended rule
    silently deleted every confirmed reversal during the 2026-07 rotation. Fail-open: a
    pick with no live quote -- or ANY fetch error -- is KEPT, so a quote outage never
    silences the digest. Caller passes ``latest_closes_fn=None`` to skip entirely."""
    if not signals:
        return signals
    try:
        prices = latest_closes_fn([s.ticker for s in signals])
    except Exception:  # noqa: BLE001 -- a quote outage must never block/empty the digest
        log.warning("live-quote fetch failed; keeping all picks (already-ran filter skipped)")
        return signals
    out: list[Signal] = []
    for s in signals:
        status = classify(entry_floor=s.entry_floor, entry_ceiling=s.entry_ceiling,
                          stop=s.stop, price=prices.get(s.ticker)).status
        keep = ("actionable", "unknown", "extended") if s.play_type == "reversal" else (
            "actionable", "unknown")
        if status in keep:
            out.append(s)
    return out


def _warn_chartless(kind: str, signals: list[Signal]) -> None:
    """Loudly flag surfaced picks with no rendered chart (they reach the PDF as a
    chartless section). The evening render selects charts BEFORE digest-time filters
    run, so a selection-set gap between the two shows up exactly here -- the 2026-07-03
    regression (most confirmed reversal picks chartless) was invisible for weeks
    because nothing checked."""
    missing = [s.ticker for s in signals if not s.chart_path]
    if missing:
        log.warning("digest %s: %d surfaced pick(s) have NO chart "
                    "(render/selection gap): %s", kind, len(missing), ", ".join(missing))


def _already_sent(session: Session, kind: str, run_date: date) -> bool:
    stmt = select(EmailLog).where(EmailLog.kind == kind, EmailLog.run_date == run_date)
    return session.scalars(stmt).first() is not None


def _adapter_for_mode(mode: str, *, broker: BrokerClient | None = None) -> ExecutionAdapter:
    """Resolve the configured execution mode to its adapter (prod path; tests inject one).

    ``"manual"`` RECORDS an order ticket for the human to place by hand (no broker, no
    position -- money never moves); ``"paper"`` opens simulated fills; ``"live"`` submits one
    order to a real broker through the injected ``broker`` -- but ONLY when a broker is
    configured: a stray ``live`` config with NO broker can never place an order, so it falls
    back to the NoOp with a loud warning. Everything else -- ``"off"`` (the default) --
    resolves to the NoOp adapter, which writes nothing (exactly today's behavior).
    """
    if mode == "manual":
        return ManualAdapter()  # needs no broker -- it only records a ticket
    if mode == "paper":
        return PaperAdapter()
    if mode == "live":
        if broker is not None:
            return LiveAdapter(broker)
        log.warning("execution_mode=live but no broker configured; not executing")
    return NoOpAdapter()


def _execution_halted(adapter: ExecutionAdapter, mode_reader: Callable[[], str]) -> bool:
    """The per-submit KILL SWITCH: has the live arming been pulled mid-dispatch?

    For a LIVE adapter ONLY, RE-READ the execution mode (``mode_reader``, default the live
    env via ``load_settings``) before each submit: if it is no longer ``"live"``, the operator
    has disarmed mid-loop, so we HALT -- the caller stops submitting the rest and pulls the
    resting orders. For every other adapter (paper / off / a test fake) this is a no-op: only
    the live path arms a real venue, so only it needs an in-loop abort.
    """
    if not isinstance(adapter, LiveAdapter):
        return False
    return mode_reader() != "live"


def _ticket_line(intent: OrderIntent, result: OrderResult) -> OrderTicketLine:
    """The renderer-facing ticket: the intent's deterministic order spec + the result."""
    return OrderTicketLine(
        side=intent.side, shares=intent.shares, ticker=intent.ticker,
        limit_price=intent.limit_price, stop=intent.stop, target=intent.target,
        status=result.status, detail=result.detail)


def _attach_digest_tickets(
    picks: list[DigestPick], tickets: dict[tuple[str, str], OrderTicketLine], play_type: str
) -> list[DigestPick]:
    """Re-stamp the dispatched picks with their order ticket (frozen -> ``replace``)."""
    return [replace(p, order_ticket=tickets[(p.ticker, play_type)])
            if (p.ticker, play_type) in tickets else p for p in picks]


def _attach_pdf_tickets(
    picks: list[PdfPick], tickets: dict[tuple[str, str], OrderTicketLine], play_type: str
) -> list[PdfPick]:
    """Re-stamp the dispatched PDF picks with their order ticket (frozen -> ``replace``)."""
    out: list[PdfPick] = []
    for p in picks:
        t = tickets.get((p.ticker, play_type))
        out.append(p if t is None else replace(
            p, ticket_status=t.status, ticket_detail=t.detail,
            ticket_side=t.side, ticket_limit_price=t.limit_price))
    return out


def send_digest(*, kind: str, db_url: str, run_date: date | None = None, to: str | None = None,
                pdf_dir: Path = Path(".digests"), anthropic_client: object | None = None,
                smtp_send: SmtpSend | None = None, force: bool = False,
                deep_analyze_fn: Callable[..., SignalAnalysis] | None = None,
                chart_bytes_loader: Callable[[str | None], bytes | None] | None = None,
                fundamentals_fn: Callable[[str], market_context.Fundamentals] | None = None,
                news_fn: Callable[[str], list[market_context.NewsItem]] | None = None,
                analyze_conviction_fn: Callable[..., ConvictionResult] | None = None,
                edge_dir: Path | None = None,
                market_trend_fn: Callable[[], str | None] | None = None,
                execution_adapter: ExecutionAdapter | None = None,
                broker: BrokerClient | None = None,
                mode_reader: Callable[[], str] | None = None,
                latest_closes_fn: Callable[[list[str]], dict[str, float]] | None = None,
                ) -> DigestResult:
    send = smtp_send or resolve_sender()  # env-driven transport (ACS or SMTP)
    recipient = to or get_secret("DIGEST_TO")
    if not recipient:  # fail fast, before any billable Claude calls
        raise RuntimeError("no recipient: set DIGEST_TO or pass to=")
    # Deep-analysis seams (default to the real impls; tests inject fakes). Whether
    # the deep path actually runs is gated by settings below, NOT by these.
    cfg = load_settings()
    # Env-first (SWING_EDGE_DIR) and ABSOLUTE: the old cwd-relative Path("edge")
    # default resolved to a nonexistent dir in the container, silently disabling
    # the insight engine in prod. Tests still inject an explicit edge_dir.
    edge_dir = resolve_edge_dir(edge_dir)
    deep_analyze = deep_analyze_fn or analyze_signal_deep
    analyze_conv = analyze_conviction_fn or analyze_conviction
    load_chart = chart_bytes_loader or _load_chart_bytes
    get_fundamentals = fundamentals_fn or market_context.get_fundamentals
    get_news = news_fn or market_context.get_recent_news
    deep_on = cfg.deep_analysis_enabled and kind in cfg.deep_analysis_kinds
    risk_unit, max_sh = resolve_risk_unit(cfg)  # per-trade sizing (0.0 -> R-multiples)
    # Execution seam: tests inject a fake adapter; prod resolves it from the configured
    # mode. The default mode is "off" -> NoOpAdapter (writes nothing, dispatches nothing),
    # so the digest stays byte-for-byte today's behavior until execution is armed. For the
    # LIVE mode the broker is built from settings (tests inject a FakeBroker via `broker=`);
    # with no broker the live adapter is NOT armed (NoOp + warn).
    exec_mode, limits = resolve_execution(cfg)
    # Resolve the live broker only when we own the adapter (no injected one) and the mode is
    # live: the passed `broker` (a test FakeBroker) wins, else build it from settings. None
    # otherwise -- so off/paper never builds a broker and the kill switch never cancels.
    live_broker = None
    if exec_mode == "live" and execution_adapter is None:
        live_broker = broker or build_broker(cfg)
    adapter = execution_adapter or _adapter_for_mode(exec_mode, broker=live_broker)

    engine = get_engine(db_url)
    with Session(engine) as session:
        # The digest summarizes the LATEST screen run -- the morning digest reflects
        # the prior evening's screen (they run on different days), so defaulting to
        # date.today() would query a run_date with no signals. An explicit run_date
        # (tests / backfill) overrides.
        if run_date is None:
            run_date = repo.latest_run_date(session) or date.today()
        alerts = sel.pending_exit_alerts(session, run_date)
        _emit_pending_exit_alert(session, run_date, recipient, send)

        # Staleness cooldown: drop picks whose setup has been on the list too long so the
        # same play isn't re-pitched daily (legacy NULL-first_seen rows always pass).
        cooldown = StrategyConfig().digest_repeat_cooldown_days
        # Sector-diversity cap on the DAILY list only (weekly/monthly stay pure rank), so one
        # hot sector can't fill every slot. Fail-open on unknown sectors; None disables it.
        if kind == "daily":
            picks = sel.daily_picks(session, run_date, max_age_days=cooldown,
                                    max_per_sector=StrategyConfig().daily_max_per_sector)
        else:
            picks = _PICKERS[kind](session, run_date, max_age_days=cooldown)
        # Already-ran filter: re-check live actionability so the email never pitches a pick
        # that ran past its entry (or broke its stop) overnight. Done BEFORE the (billable)
        # deep analysis so stale picks never cost an Opus call. No-op when the seam is off.
        if latest_closes_fn is not None:
            picks = _drop_already_ran(picks, latest_closes_fn)
        _warn_chartless(kind, picks)
        already = _already_sent(session, kind, run_date)
        if already and not force:  # don't re-send the same digest (force overrides for ad-hoc resends)
            return DigestResult(n_picks=len(picks), pdf_attached=False, sent=False)

        names = names_by_ticker()  # ticker -> company name, loaded once

        # The live SPY trend feeds the deterministic conviction baseline; computed ONCE
        # per run, lazily (only when the deep path is on, so the off path never fetches
        # SPY). None when SPY is unavailable -> the baseline just won't match its trend dim.
        market_trend: str | None = None
        if deep_on:
            market_trend = (market_trend_fn or _default_market_trend)()
        # Playbook + verdicts per play type, loaded once. Present -> the insight engine runs
        # for that play type's deep picks; absent -> they fall back to the old deep path.
        playbooks = {pt: _load_playbook(edge_dir, pt) for pt in ("continuation", "reversal")}
        # A missing playbook on the deep path must be LOUD: the fallback still bills Opus
        # but records no analyst call, so a silent miss freezes the whole learning loop
        # (calibration, autonomy gate) with every job green -- exactly the 2026-07 outage.
        # Logged here AND surfaced in the digest footer below: a container log line alone
        # is the unread channel that hid the original outage.
        missing_playbooks = [pt for pt, pb in playbooks.items() if pb is None]
        if deep_on:
            for pt in missing_playbooks:
                log.warning(
                    "insight engine disabled for %s: playbook or verdicts sidecar "
                    "missing under %s -- falling back to the legacy deep path "
                    "(no conviction baseline, no analyst call recorded)", pt, edge_dir)

        # The EARNED conviction-nudge bound per play type. The advisory autonomy gate already
        # runs ``conviction_calibrated`` PER play type over the scored book, so we reuse its
        # ``CalibrationVerdict`` to derive ``max_step`` -- no separate calibration query. We
        # read the gate ONCE here, BEFORE the picks are built/scored, so the bound is the track
        # record EARNED prior to grading this run's picks. ``max_conviction_step`` returns 2 for
        # a CALIBRATED play type (capped at the ``_NUDGE_CEILING`` constant) and 1 otherwise.
        # RECOMPUTED every run -> reversible: a play type that stops calibrating drops back to
        # the hard ±1. Off path -> no gate, no map, bound defaults to 1 (byte-identical today).
        nudge_steps: dict[str, int] = {}
        if deep_on:
            gate = autonomy_gate(session, edge_dir=edge_dir)
            nudge_steps = {
                pt: max_conviction_step(v["calibration"])
                for pt, v in gate.per_play_type.items()
            }

        def _deep_one(facts: SignalFacts, sig: Signal, play_type: str) -> tuple[
                SignalAnalysis, OrderIntentLine | None, OrderIntent | None, float]:
            """Run ONE deep Opus call for a top-N pick; return (analysis, line, intent, cost).

            When the pick's play type has a playbook + verdicts sidecar, run the INSIGHT
            ENGINE: a deterministic conviction baseline, one Opus conviction call (which
            NUDGES it, clamped +-1), a sized order intent, and a persisted AnalystCall. The
            analyst's insight becomes the rationale; a short core reason names the
            conviction + edge. No playbook -> fall back to the old ``deep_analyze`` (one
            call either way -- never both, so no double-billing). The 4th return is the
            call's estimated spend (``usage.est_cost_usd``, 0.0 when usage is absent) so the
            caller can accumulate it against the per-run ceiling."""
            context_text = market_context.context_block(
                get_fundamentals(sig.ticker), get_news(sig.ticker))
            pb = playbooks.get(play_type)
            if pb is None:  # no playbook/verdicts -> the existing deep path, unchanged
                analysis = deep_analyze(
                    facts, chart_bytes=load_chart(sig.chart_path), context_text=context_text,
                    client=anthropic_client,  # type: ignore[arg-type]  # test seam may be a fake
                    model=cfg.analysis_model, reasoning=cfg.analysis_reasoning,
                    max_searches=cfg.analysis_max_searches)
                cost = analysis.usage.est_cost_usd if analysis.usage is not None else 0.0
                return analysis, None, None, cost
            playbook_text, verdicts = pb
            baseline, edge_label = conviction_baseline(
                score=sig.score, volatility_tier=sig.volatility_tier,
                market_trend=market_trend, verdicts=verdicts)
            cr = analyze_conv(
                facts, baseline=baseline, playbook_text=playbook_text,
                context_text=context_text, chart_bytes=load_chart(sig.chart_path),
                client=anthropic_client,  # type: ignore[arg-type]  # test seam may be a fake
                model=cfg.analysis_model, reasoning=cfg.analysis_reasoning,
                max_searches=cfg.analysis_max_searches,
                max_step=nudge_steps.get(play_type, 1))  # earned ±2 ONLY if THIS play type calibrates
            intent = build_order_intent(
                facts, cr, play_type=play_type, edge_played=edge_label,
                risk_unit_dollars=risk_unit, max_shares=max_sh)
            record_analyst_call(
                session, facts=facts, run_date=run_date, created_date=run_date,
                baseline_conviction=baseline, conviction_result=cr,
                model=cfg.analysis_model, play_type=play_type)
            analysis = SignalAnalysis(
                core_reason=f"{cr.conviction.upper()} conviction — {edge_label}",
                rationale=cr.insight, is_deep=cr.is_deep)
            order_intent = OrderIntentLine(
                conviction=cr.conviction, shares=intent.shares,
                risk_dollars=intent.risk_dollars, edge_played=edge_label,
                entry_floor=intent.entry_floor, entry_ceiling=intent.entry_ceiling,
                stop=intent.stop, target=intent.target)
            cost = cr.usage.est_cost_usd if cr.usage is not None else 0.0
            return analysis, order_intent, intent, cost

        # Per-RUN deep-analysis spend ceiling. ``spend[0]`` accumulates every deep insight
        # call's est_cost across BOTH _build_picks calls (continuation + reversal share one
        # budget), and ``ceiling_logged`` ensures the cutoff is warned ONCE per run. None
        # ceiling -> the check never fires, so the loop is byte-identical to today.
        max_usd = cfg.deep_analysis_max_usd
        spend = [0.0]
        ceiling_logged = [False]

        def _build_picks(sigs: list[Signal], *, play_type: str,
                         collect_intents: list[OrderIntent]) -> tuple[
                list[DigestPick], list[PdfPick]]:
            """Build the (DigestPick, PdfPick) lists for a set of signals. The top-N get the
            deep path (the insight engine when a playbook exists for ``play_type``, else the
            legacy deep analyst); the rest get the cheap deterministic narration. Each built
            ``OrderIntent`` is appended to ``collect_intents`` for the post-build dispatch --
            rendering is unchanged here; nothing is dispatched inline.

            SPEND CEILING: a pick that WOULD get the deep path skips it for the deterministic
            narrator once the per-run accumulator (``spend``) has reached ``max_usd`` -- the
            pick still renders, just without the Opus insight/conviction nudge. Else it runs
            deep and adds its est_cost to the accumulator. ``max_usd is None`` -> no ceiling."""
            dps: list[DigestPick] = []
            pps: list[PdfPick] = []
            for i, sig in enumerate(sigs):
                facts = _facts(sig)
                order_intent: OrderIntentLine | None = None
                want_deep = deep_on and i < cfg.deep_analysis_top_n
                over_ceiling = max_usd is not None and spend[0] >= max_usd
                if want_deep and over_ceiling and not ceiling_logged[0]:
                    log.warning("deep-analysis spend ceiling $%.2f reached; remaining picks "
                                "use deterministic text", max_usd)
                    ceiling_logged[0] = True
                if want_deep and not over_ceiling:
                    analysis, order_intent, built_intent, cost = _deep_one(facts, sig, play_type)
                    spend[0] += cost
                    if built_intent is not None:
                        collect_intents.append(built_intent)
                else:
                    analysis = analyze_signal(facts, client=anthropic_client)  # type: ignore[arg-type]
                name = names.get(sig.ticker, "")
                dps.append(DigestPick(sig.ticker, name, sig.horizon, analysis.core_reason,
                                      score=sig.score, strength=sig.strength,
                                      is_deep=analysis.is_deep, order_intent=order_intent))
                pps.append(PdfPick(
                    ticker=sig.ticker, name=name, trade_type=sig.horizon, score=sig.score,
                    chart_path=sig.chart_path, entry_floor=sig.entry_floor,
                    entry_ceiling=sig.entry_ceiling, stop=sig.stop, target=sig.target,
                    risk_reward=facts.risk_reward, quality_tier=sig.quality_tier,
                    volatility_tier=sig.volatility_tier, oversold=sig.oversold,
                    mtf_aligned=sig.mtf_aligned, atr_pct=facts.atr_pct,
                    rationale=analysis.rationale, is_deep=analysis.is_deep, strength=sig.strength,
                    conviction=(order_intent.conviction if order_intent else None),
                    shares=(order_intent.shares if order_intent else 0),
                    risk_dollars=(order_intent.risk_dollars if order_intent else 0.0),
                    edge_played=(order_intent.edge_played if order_intent else "")))
            return dps, pps

        collected_intents: list[OrderIntent] = []  # the run's built intents, for dispatch
        digest_picks, pdf_picks = _build_picks(
            picks, play_type="continuation", collect_intents=collected_intents)
        # Reversal "Top 5" -- daily digest only for now (weekly/monthly stay continuation).
        reversal_digest: list[DigestPick] | None = None
        reversal_pdf: list[PdfPick] = []
        rev_funnel: tuple[int, ...] | None = None
        reversal_overflow: list[str] = []
        if kind == "daily":
            # Stage-attributed funnel, rendered under the reversal section so a
            # surfacing-bar wipeout is visibly different from a quiet market AND says
            # WHICH bar did the filtering (2026-07-02: 31 confirmations, 0 software
            # names surfaced, undiagnosable from the old two-count line).
            detected, confirmed_n = sel.reversal_funnel(session, run_date)
            scfg = StrategyConfig()
            # Over-fetch so the actionability drop and sector cap BACKFILL from below
            # the top-N instead of shrinking the list (top-5-then-filter left the Jul-2
            # digest with no room for the rotation names ranked 6th+).
            pool = sel.reversal_picks(
                session, run_date, top_n=sel.REVERSAL_POOL_N, max_age_days=cooldown,
                premium_only=scfg.reversal_surface_premium_only,
                confirmed_only=scfg.reversal_surface_confirmed_only)
            n_fresh = len(pool)
            if latest_closes_fn is not None:
                pool = _drop_already_ran(pool, latest_closes_fn)
            n_actionable = len(pool)
            reversal_sigs = sel.cap_signals_by_sector(
                session, pool, max_per_sector=scfg.reversal_max_per_sector, limit=5)
            _warn_chartless("daily-reversal", reversal_sigs)
            rev_funnel = (detected, confirmed_n, n_fresh, n_actionable)
            # Overflow: names that cleared every bar but lost the top-5/sector race. On a
            # broad rotation day these ARE the story (2026-07-02: CRM/WDAY/PTC at rank
            # 9-12 -- present, invisible); one compact line keeps them visible.
            surfaced = {s.ticker for s in reversal_sigs}
            reversal_overflow = [s.ticker for s in pool if s.ticker not in surfaced]
            reversal_digest, reversal_pdf = _build_picks(
                reversal_sigs, play_type="reversal", collect_intents=collected_intents)

        # Close the learning loop: score any now-resolved prior analyst calls. NOT gated
        # on deep_on -- scoring is a cheap idempotent DB join (no model call, no-op on an
        # empty table), and gating it meant pausing the analyst would silently freeze the
        # grading of calls already made (2026-07 audit).
        n_scored = repo.score_analyst_calls(session)
        log.info("scored %d resolved analyst call(s)", n_scored)

        # Batch-dispatch the run's order intents through the adapter in ONE try/except.
        # GATED: the "off" NoOp adapter writes nothing and produces no ticket, so the
        # digest stays byte-for-byte today's behavior; we skip the loop entirely for it.
        # GRACEFUL: ANY adapter failure is swallowed + logged and NEVER blocks the email
        # (mirrors the PDF/deep-analysis seam pattern) -- the run still sends.
        # KILL SWITCH: for a LIVE adapter we RE-READ the execution mode before each submit
        # (`_mode_reader`, default the live env); if it is no longer "live" the operator has
        # disarmed mid-loop, so we STOP submitting the rest AND pull the ENTRY-side resting
        # orders -- never a blanket cancel, which would strip the bracket stop legs off open
        # positions (the 2026-07-04 disarm fix) -- all inside the try/except, so it never
        # blocks.
        _mode_reader = mode_reader or (lambda: load_settings().execution_mode)
        tickets: dict[tuple[str, str], OrderTicketLine] = {}
        if collected_intents and not isinstance(adapter, NoOpAdapter):
            try:
                for intent in collected_intents:
                    if _execution_halted(adapter, _mode_reader):
                        log.warning("execution kill switch: halting dispatch for %s %s "
                                    "and pulling entry-side resting orders", kind, run_date)
                        if live_broker is not None:
                            pull_entry_orders(live_broker)
                            ensure_stop_protection(
                                live_broker,
                                lambda sym: repo.latest_recorded_stop(session, sym),
                                key_suffix=f"kill-{run_date:%Y%m%d}")
                        break
                    result = adapter.submit(
                        intent, session=session, run_date=run_date, limits=limits)
                    tickets[(intent.ticker, intent.play_type)] = _ticket_line(intent, result)
            except Exception:  # execution must never block the digest
                log.warning("execution dispatch failed for %s %s", kind, run_date,
                            exc_info=True)
        if tickets:  # attach each ticket to its pick (only when execution is armed)
            digest_picks = _attach_digest_tickets(digest_picks, tickets, "continuation")
            pdf_picks = _attach_pdf_tickets(pdf_picks, tickets, "continuation")
            if reversal_digest is not None:
                reversal_digest = _attach_digest_tickets(reversal_digest, tickets, "reversal")
                reversal_pdf = _attach_pdf_tickets(reversal_pdf, tickets, "reversal")

        # The consolidated "Proposed orders — place on Robinhood" shopping list: the human's
        # actionable copy-list for the manual (approval) posture ONLY. Levels + conviction are
        # COPIED from the built intents (never recomputed); a skipped/limit-blocked ticket is
        # dropped from the placeable list. Every other mode -> no proposals, so the body/PDF/
        # artifact are byte-for-byte unchanged. The JSON artifact write is wrapped so a failure
        # logs + returns None and NEVER blocks the digest (mirrors the PDF seam).
        proposals: list[ProposedOrder] = []
        if exec_mode == "manual" and tickets:
            proposals = build_proposals(collected_intents, tickets)
            write_proposals_artifact(proposals, Path(pdf_dir), run_date)

        pdf_path: Path | None = None
        if digest_picks or reversal_pdf:
            try:
                pdf_path = build_digest_pdf(
                    pdf_picks, Path(pdf_dir) / f"{kind}_{run_date:%Y%m%d}.pdf",
                    reversal_picks=reversal_pdf or None,
                    header=f"Swing Screener - {kind.capitalize()} Picks ({run_date:%b %d, %Y})",
                    proposals=proposals or None,
                )
            except Exception:  # PDF must never block the email
                log.warning("PDF build failed for %s %s", kind, run_date, exc_info=True)
                pdf_path = None
        pdf_attached = pdf_path is not None

        alert_lines = [
            AlertLine(ticker=(a.message.split(" ", 1)[0] if a.message else ""),
                      tier=a.tier, reason=a.reason, message=a.message)
            for a in alerts
        ]
        # Autonomy-gate countdown footer: surfaced ONLY on the deep path (the digest already
        # ran the analyst, so the gate's read-only SELECTs over scored calls + the verdicts
        # sidecars are free). It NEVER writes -- the gate is a pure SELECT (North Star #1) --
        # and a non-deep digest passes None, so the body is byte-for-byte unchanged there.
        # Recomputed here (AFTER scoring) so the countdown reflects this run's freshly-scored
        # calls -- distinct from the pre-build snapshot that sets the earned nudge bound, which
        # is the track record EARNED before this run's picks were graded.
        autonomy_status = (
            gate_status_line(autonomy_gate(session, edge_dir=edge_dir)) if deep_on else None
        )
        # The insight-OFF state rides the footer into the EMAIL (deep path only -- the
        # note is meaningless when the analyst isn't running): the reader sees "insight
        # engine OFF" instead of inferring it, weeks later, from an empty calibration.
        if deep_on and missing_playbooks:
            autonomy_status = (
                f"{autonomy_status} · insight engine OFF "
                f"({', '.join(missing_playbooks)}: playbook/verdicts missing)")
        # The always-on health footer: the "is the cron alive" push. Unlike the gate
        # countdown it is NOT gated on deep -- a silently-dead screen/digest cron must show on
        # EVERY digest. READ-ONLY: the latest run_date + the gate's pure SELECTs (the gate
        # never writes). ``latest_run_date`` is re-read here rather than reusing the local
        # ``run_date`` (which may be a backfill/explicit date) so the freshness reflects the
        # store's true newest screen.
        health_status = health_line(
            latest_run_date=repo.latest_run_date(session),
            today=date.today(),
            execution_mode=exec_mode,
            gate_ready=autonomy_gate(session, edge_dir=edge_dir).ready,
        )
        body = compose_digest_body(kind, run_date, digest_picks, alert_lines,
                                   has_pdf=pdf_attached, reversal_picks=reversal_digest,
                                   reversal_funnel=rev_funnel,
                                   reversal_overflow=reversal_overflow,
                                   proposals_text=proposals_text(proposals),
                                   proposals_html=proposals_html(proposals),
                                   autonomy_status=autonomy_status,
                                   health_status=health_status)
        send(to=recipient, subject=body.subject, text=body.text, html=body.html,
             attachments=([pdf_path] if pdf_path is not None else []))

        if not already:  # a forced resend reuses the existing day marker (no duplicate row)
            session.add(EmailLog(sent_at=datetime.now(UTC), kind=kind, subject=body.subject,
                                 run_date=run_date))
            session.commit()
        return DigestResult(n_picks=len(picks), pdf_attached=pdf_attached, sent=True,
                            n_reversals=len(reversal_digest or []))


def run_exit_check_and_alert(*, db_url: str, run_date: date | None = None, to: str | None = None,
                             smtp_send: SmtpSend | None = None,
                             latest_bars_fn: LatestBarsFn | None = None) -> ExitCheckResult:
    """Intraday exit path: PRODUCE today's real exit events, then SEND the alert.

    Unlike the digest kinds there is no ``_PICKERS["exit"]``; this is a thin
    two-step path. (a) ``run_exit_check`` records ``is_paper=False`` ExitEvents
    for any open real trade whose latest bar trips an exit, then (b) the shared
    ``_emit_pending_exit_alert`` helper emails them (subject "Exit", no PDF),
    deduped per exit-event-SET so a later hour with a NEW exit still alerts.
    """
    send = smtp_send or resolve_sender()  # env-driven transport (ACS or SMTP)
    run_date = run_date or date.today()
    recipient = to or get_secret("DIGEST_TO")
    if not recipient:
        raise RuntimeError("no recipient: set DIGEST_TO or pass to=")

    kwargs: dict[str, object] = {"db_url": db_url, "today": run_date}
    if latest_bars_fn is not None:
        kwargs["latest_bars_fn"] = latest_bars_fn
    result = run_exit_check(**kwargs)  # type: ignore[arg-type]

    with Session(get_engine(db_url)) as session:
        _emit_pending_exit_alert(session, run_date, recipient, send)
    return result


def main() -> None:
    settings = load_settings()  # absolute paths + env-resolved DB URL (container-safe)
    parser = argparse.ArgumentParser(description="Send a swing-screener email digest.")
    parser.add_argument("--kind", choices=["daily", "weekly", "monthly", "exit"], default="daily")
    parser.add_argument("--db", default=settings.db_url)
    parser.add_argument("--pdf-dir", type=Path, default=settings.pdf_dir)
    parser.add_argument("--force", action="store_true",
                        help="resend even if a digest for this (kind, day) already went out")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    if args.kind == "exit":
        exit_result = run_exit_check_and_alert(db_url=args.db)
        log.info("exit check: open=%d exited=%d", exit_result.n_open, exit_result.n_exited)
        return
    # SWING_FORCE_RESEND lets a scheduled container force a resend without changing
    # its args -- toggle the env, run once, untoggle (used for ad-hoc verification).
    force = args.force or os.environ.get("SWING_FORCE_RESEND", "").lower() in {"1", "true", "yes"}
    # Already-ran filter: re-check each pick against the latest close at send time and drop
    # the ones that ran past entry / broke the stop overnight (mirrors the dashboard).
    # Config-gated + fail-open; off -> None, so the digest is byte-for-byte today's behavior.
    latest_closes_fn: Callable[[list[str]], dict[str, float]] | None = None
    if StrategyConfig().digest_drop_already_ran:
        from swing_screener.dashboard.quotes import latest_closes

        def latest_closes_fn(tickers: list[str]) -> dict[str, float]:  # noqa: E731
            return latest_closes(tickers, cache_dir=settings.cache_dir)
    result = send_digest(kind=args.kind, db_url=args.db, pdf_dir=args.pdf_dir, force=force,
                         latest_closes_fn=latest_closes_fn)
    log.info("digest %s: picks=%d reversals=%d pdf=%s sent=%s",
             args.kind, result.n_picks, result.n_reversals, result.pdf_attached, result.sent)


if __name__ == "__main__":
    main()
