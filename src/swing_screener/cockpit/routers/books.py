"""Health, heartbeats, the Stats reads (cohorts / performance), forward books, the
funnel snapshot, and the Azure sign-in action -- the Phase-2 surface. Moved verbatim
out of ``cockpit/api.py``; the contracts in that module's docstring (full Stat dicts,
no URL/credentials on the wire, friendly 503s) hold unchanged here."""

import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from swing_screener.analytics.performance import (
    SCORE_EDGES,
    PerformanceSummary,
    _bucket_trades_by_rank,
    _bucket_trades_by_score,
    breakdown,
    cost_level_for,
    equity_curve,
    leaderboard_flag,
    leaderboard_order,
    paired_arm_delta,
    score_stamped,
    summarize,
)
from swing_screener.cockpit.common import (
    _down_summary,
    _is_azure,
    _LoginFlight,
    _LoginProc,
    _require_cockpit,
    connection_label,
)
from swing_screener.cockpit.heartbeats import Heartbeat, collect_heartbeats
from swing_screener.cockpit.settlement import (
    STATES,
    SettlementCard,
    build_cards,
    facet_filter,
)
from swing_screener.cockpit.stats import stat_from_paired_delta, stat_from_summary
from swing_screener.db.models import PaperTrade
from swing_screener.db.repo import (
    latest_reversal_funnel,
    load_closed_paper_trades,
    load_research_paper_trades,
)
from swing_screener.pipeline.arms import BASELINE
from swing_screener.pipeline.registry import load_experiments
from swing_screener.pipeline.variants import DEFAULT_VARIANT
from swing_screener.settings import resolve_edge_dir


def build_books_router(
    *,
    db_url: str,
    _engine: Callable[[], Engine],
    _session: Callable[[], Iterator[Session]],
    edge_dir: Path | None,
    gh_latest: Callable[[str], tuple[datetime, str] | None] | None,
    spawner: Callable[[], _LoginProc | None],
) -> APIRouter:
    """The Phase-2 read endpoints plus ``/api/azure-login``, closed over the app's
    seams: the shared engine accessor / session dependency, the heartbeats edge
    dir, the optional GH poller, and the (already resolved) ``az login`` spawner."""
    router = APIRouter()

    @router.get("/api/health")
    def health() -> dict[str, object]:
        """Connectivity + safe label. Always 200; ``connected`` carries the truth."""
        label = connection_label(db_url)
        try:
            with _engine().connect() as conn:
                conn.execute(text("SELECT 1"))
        except Exception as exc:
            return {"connected": False, "label": label, "error": _down_summary(exc),
                    "azure": _is_azure(db_url)}
        return {"connected": True, "label": label, "error": None, "azure": _is_azure(db_url)}

    @router.get("/api/heartbeats")
    def heartbeats(session: Session = Depends(_session)) -> list[dict[str, object]]:
        """Every job's pulse, evaluated against the wall clock at request time."""
        beats = collect_heartbeats(
            session, now=datetime.now(UTC), edge_dir=edge_dir, gh_latest=gh_latest
        )
        return [_beat_dict(b) for b in beats]

    @router.get("/api/stats/cohorts")
    def cohort_stats(
        facet: Literal["research", "gold"] = "research",
        session: Session = Depends(_session),
    ) -> dict[str, object]:
        """Research-grid cohort expectancies; every number is a full Stat dict (rule 1).

        Shape (stable contract): ``{"cohorts": [{"key": <play_type>, "strength":
        <str | null>, "stat": {<10-key Stat>}}, ...]}``, play_type-major: each
        play_type's aggregate row first (``strength: null``), then its per-strength
        split, both alphabetically. Strength-split keys come from ``breakdown``'s
        ``str()`` coercion, so a null-strength cohort appears as the string ``"None"``
        -- distinct from the aggregate row's ``null``. ``cost_level`` is derived per
        cohort SUBSET by ``cost_level_for``'s cutoff rule: ``"0.05"`` iff every closed
        trade provably exited on/after the cost epoch, else an honest null (one
        pre-cutoff exit poisons its whole cohort). ``corpus_id`` stays an explicit
        null (not persisted yet). ``facet`` selects the book -- ``gold`` keeps only
        would_surface-truthy rows -- and is echoed on every Stat; anything else is
        FastAPI's 422 via the ``Literal``.
        """
        trades = facet_filter(load_research_paper_trades(session), facet)
        by_play = breakdown(trades, "play_type")
        cohorts: list[dict[str, object]] = []
        for play_type in sorted(by_play):
            subset = [t for t in trades if str(t.play_type) == play_type]
            cohorts.append(_cohort(play_type, None, by_play[play_type], subset, facet))
            by_strength = breakdown(subset, "strength")
            cohorts.extend(
                _cohort(play_type, strength, by_strength[strength],
                        [t for t in subset if str(t.strength) == strength], facet)
                for strength in sorted(by_strength)
            )
        return {"cohorts": cohorts}

    @router.get("/api/forward-books")
    def forward_books(
        facet: Literal["research", "gold"] = "research",
        session: Session = Depends(_session),
    ) -> dict[str, object]:
        """One settlement card per registered experiment (edge/experiments.json).

        Cards order decision-forcing first: awaiting-decision (settled or futile),
        then accruing, then retired -- stable by name within each group. The ordering
        is a presentation concern, so it lives here, not in settlement's math. A
        missing or empty registry is a normal setup state: ``{"cards": []}``, never
        an error. ``facet`` threads through to ``build_cards`` (it filters each
        loaded book internally, before any math). Budget: ~1.1s of bootstrap math per
        request at the real 9-experiment registry shape -- ~2% duty cycle at the
        frontend's 60s poll; revisit (cache across requests) if wall time approaches
        the poll interval or a second polling client appears.
        """
        experiments = load_experiments(resolve_edge_dir(edge_dir))

        # Per-request memo: the registry shares books heavily (every arm card hydrates
        # the identical default-variant pool; variant cards share the default control),
        # so the distinct loads are roughly half the raw count. Copied on the way out
        # so no consumer can mutate a list another card is about to read.
        books: dict[tuple[str | None, str | None, str], list[PaperTrade]] = {}

        def _load(
            *, play_type: str | None, arm: str | None, variant: str
        ) -> list[PaperTrade]:
            key = (play_type, arm, variant)
            if key not in books:
                books[key] = load_closed_paper_trades(
                    session, play_type=play_type, arm=arm, variant=variant
                )
            return list(books[key])

        cards = build_cards(
            experiments, book_loader=_load, now=datetime.now(UTC), facet=facet
        )
        cards.sort(key=lambda c: (_STATE_RANK[c.state], c.name))
        return {"cards": [_card_dict(c) for c in cards]}

    @router.get("/api/funnel")
    def funnel(session: Session = Depends(_session)) -> dict[str, object]:
        """The latest daily digest's reversal funnel snapshot -- the row with the
        newest ``run_date``; ``{"funnel": null}`` before the first digest records
        one. ``overflow`` is the comma-joined column split back into a ticker list,
        empties dropped (the empty string means "no overflow", never ``[""]``)."""
        row = latest_reversal_funnel(session)
        if row is None:
            return {"funnel": None}
        return {"funnel": {
            "run_date": row.run_date.isoformat(),
            "detected": row.detected,
            "confirmed": row.confirmed,
            "fresh": row.fresh,
            "actionable": row.actionable,
            "surfaced": row.surfaced,
            "overflow": [t for t in row.overflow_tickers.split(",") if t],
            "pool_n": row.pool_n,
            "confirmed_only": row.confirmed_only,
            "premium_only": row.premium_only,
            "already_ran_checked": row.already_ran_checked,
        }}

    @router.get("/api/stats/performance")
    def performance_stats(
        play_type: Literal["all", "continuation", "reversal"] = "all",
        window: Literal["all", "90", "180", "365"] = "all",
        facet: Literal["research", "gold"] = "research",
        session: Session = Depends(_session),
    ) -> dict[str, object]:
        """The Streamlit Screener Performance page as data -- every aggregate a Stat.

        Replicates the retired Streamlit Screener Performance page's load-bearing order
        EXACTLY: (1) ``facet`` (``facet_filter``) then the ``play_type`` filter over
        the research grid; (2) the strategy leaderboard over the ``arm == BASELINE``
        subset, with the trailing ``window`` cut (days back from today, on
        ``opened_date`` -- an undated row drops from windowed views) applied to THAT
        subset; the window scopes ONLY the leaderboard, like the page's segmented
        control; (3) everything downstream -- arms, KPIs, breakdowns, equity curve --
        scopes to ``variant == DEFAULT_VARIANT`` (the exit-arm A/B is only honest
        within one screen variant), with the page's arm-detail radio pinned at its
        default (the BASELINE arm when several arms exist).

        Where the page HID degenerate sections (single variant / single arm / < 2
        populated score bands), the API always returns every key with whatever data
        exists (empty lists when there is none) -- the FRONTEND decides rendering.
        The one data-shaping exception: score bands with no closes are omitted so
        they don't read as spurious zeros (rank buckets keep their fixed labels,
        page parity). The score cut goes through ``score_stamped`` (forward book
        only); regime cuts skip unknown-regime rows. ``profit_factor`` serializes an
        all-winner book as null -- JSON has no Infinity (the page's ∞ glyph).

        Budget: ~1.25s per request at a realistic 6k-row book (~25 clustered
        bootstraps across the five breakdown families plus the per-arm paired
        deltas; measured 0.6s median on the dev box -- the budget leaves headroom
        for slower boxes and Azure round-trips). Combined with /api/forward-books
        the 60s poll cycle carries ~2.4s (~4% duty); revisit (cache across requests)
        if wall time approaches the poll interval or a second polling client appears.
        """
        trades = facet_filter(load_research_paper_trades(session), facet)
        if play_type != "all":
            trades = [t for t in trades if t.play_type == play_type]

        # (2) Variant leaderboard: judged on the BASELINE exit arm, window cut applied
        # AFTER the arm filter. The `t.opened_date and ...` truthiness is deliberate
        # page parity: an undated row drops from windowed views, stays in "all".
        baseline = [t for t in trades if t.arm == BASELINE]
        if window != "all":
            cutoff = date.today() - timedelta(days=int(window))
            baseline = [t for t in baseline if t.opened_date and t.opened_date >= cutoff]
        by_variant = breakdown(baseline, "variant")
        leaderboard = [
            _leaderboard_row(
                v, by_variant[v], [t for t in baseline if str(t.variant) == v], facet
            )
            for v in leaderboard_order(by_variant)
        ]

        # (3) Everything downstream is ONE screen variant: the arms share fills only
        # within a single screen config, so a second variant would pollute the A/B.
        scoped = [t for t in trades if t.variant == DEFAULT_VARIANT]
        by_arm = breakdown(scoped, "arm")
        arm_names = sorted(by_arm)
        arms = [_arm_row(a, by_arm[a], scoped, facet) for a in arm_names]

        # KPI/breakdown subset: the page's arm-detail radio pinned at its default --
        # the BASELINE arm when several arms exist (else the first alphabetically;
        # with a single arm the book passes through unchanged, exactly like the page).
        if len(arm_names) > 1:
            detail = BASELINE if BASELINE in arm_names else arm_names[0]
            detail_trades = [t for t in scoped if str(t.arm) == detail]
        else:
            detail_trades = scoped

        summary = summarize(detail_trades)
        pf = summary.profit_factor
        kpis: dict[str, object] = {
            "expectancy": _stat_dict(summary, detail_trades, facet),
            "win_rate": summary.win_rate,
            "fill_rate": summary.fill_rate,
            "profit_factor": None if pf == float("inf") else pf,
            "n_closed": summary.n_closed,
        }

        by_tf = breakdown(detail_trades, "timeframe")
        score_rows: list[dict[str, object]] = []
        for label, group in _bucket_trades_by_score(
            score_stamped(detail_trades), SCORE_EDGES
        ).items():
            band = summarize(group)
            if band.n_closed > 0:  # an empty band would read as a spurious zero
                score_rows.append(_breakdown_row(label, band, group, facet))
        trend_pool = [t for t in detail_trades if t.market_trend is not None]
        vol_pool = [t for t in detail_trades if t.market_vol is not None]
        by_trend = breakdown(trend_pool, "market_trend")
        by_vol = breakdown(vol_pool, "market_vol")
        breakdowns: dict[str, object] = {
            "timeframe": [
                _breakdown_row(
                    k, by_tf[k],
                    [t for t in detail_trades if str(t.timeframe) == k], facet,
                )
                for k in sorted(by_tf)
            ],
            "rank": [
                _breakdown_row(label, summarize(group), group, facet)
                for label, group in _bucket_trades_by_rank(
                    detail_trades, _RANK_EDGES
                ).items()
            ],
            "score": score_rows,
            "market_trend": [
                _breakdown_row(
                    k, by_trend[k],
                    [t for t in trend_pool if str(t.market_trend) == k], facet,
                )
                for k in sorted(by_trend)
            ],
            "market_vol": [
                _breakdown_row(
                    k, by_vol[k],
                    [t for t in vol_pool if str(t.market_vol) == k], facet,
                )
                for k in sorted(by_vol)
            ],
        }

        return {
            "kpis": kpis,
            "leaderboard": leaderboard,
            "arms": arms,
            "breakdowns": breakdowns,
            "equity_curve": [[d.isoformat(), r] for d, r in equity_curve(detail_trades)],
        }

    login_lock = threading.Lock()
    login_flight = _LoginFlight()

    @router.post("/api/azure-login", dependencies=[Depends(_require_cockpit)])
    def azure_login() -> dict[str, object]:
        """Spawn ``az login`` to refresh the AAD credential (design doc 2026-07-06).

        Contract: header-guarded (``_require_cockpit``), Azure-mode only (a login
        cannot fix a sqlite file), single-flight, and NO success signal --
        ``/api/health`` is the sole recovery oracle; the per-connection token fetch
        picks up the new credential on the next poll. Never touches the engine."""
        if not _is_azure(db_url):
            raise HTTPException(status_code=409, detail="not an Azure database")
        with login_lock:
            if login_flight.active(time.monotonic()):
                return {"started": False, "already_running": True}
            proc = spawner()
            if proc is None:
                return {"started": False, "error": "az-not-found"}
            login_flight.proc = proc
            login_flight.started = time.monotonic()
        return {"started": True}

    return router


def _stat_dict(
    summary: PerformanceSummary, subset: list[PaperTrade], facet: str
) -> dict[str, object]:
    """The one Stat-building call every aggregate row shares: the summary's expectancy
    wrapped with the SUBSET's provable cost level (the cutoff rule in
    ``cost_level_for`` -- ``subset`` must be exactly the rows ``summary`` was computed
    from), an explicit null ``corpus_id`` (not persisted yet), and the facet the row
    was computed under."""
    return stat_from_summary(
        summary, cost_level=cost_level_for(subset), corpus_id=None, facet=facet
    ).as_dict()


def _cohort(
    key: str,
    strength: str | None,
    summary: PerformanceSummary,
    trades: list[PaperTrade],
    facet: str,
) -> dict[str, object]:
    """One cohort row -- see ``_stat_dict`` for the Stat posture."""
    return {"key": key, "strength": strength, "stat": _stat_dict(summary, trades, facet)}


# The Streamlit performance page's fixed rank-bucket edges (1-5 / 6-10 / 11+),
# ported as-is -- they are the parity contract. The score-band edges are the shared
# SCORE_EDGES constant (analytics.performance): the reflection's verdict buckets and
# this breakdown must band identically.
_RANK_EDGES: tuple[int, ...] = (5, 10)


def _leaderboard_row(
    variant: str, summary: PerformanceSummary, subset: list[PaperTrade], facet: str
) -> dict[str, object]:
    """One strategy-leaderboard row. The Stat guards the EDGE claim (expectancy + CI);
    win/fill rates and the signal count ride as plain context fields -- same posture
    as the cohort rows' ``key``/``strength``. ``fill_rate``/``n_total`` are the fill
    visibility rule (2026-07-01 audit): a variant that 'wins' by rarely filling must
    show it where the ranking is read. ``flag`` is ``leaderboard_flag``'s shared
    trust label (iid beats thin/ok: an unclustered bound is the first thing to see)."""
    return {
        "variant": variant,
        "stat": _stat_dict(summary, subset, facet),
        "win_rate": summary.win_rate,
        "fill_rate": summary.fill_rate,
        "n_total": summary.n_total,
        "flag": leaderboard_flag(summary),
    }


def _arm_row(
    arm: str, summary: PerformanceSummary, pooled: list[PaperTrade], facet: str
) -> dict[str, object]:
    """One experiment-arm row. ``pooled`` is the whole DEFAULT_VARIANT book (every
    arm): the paired delta needs both legs of each fill. The delta Stat is built by
    ``stat_from_paired_delta`` -- the one home for the mapping, shared with
    settlement.py's arm branch -- with the cost level stamped over the POOLED book
    (both delta sides). The baseline row carries ``delta: null`` (there is no
    self-delta) with ``n_pairs: 0`` -- no pairs back a delta claim there."""
    subset = [t for t in pooled if str(t.arm) == arm]
    row: dict[str, object] = {"arm": arm, "stat": _stat_dict(summary, subset, facet)}
    if arm == BASELINE:
        row["n_pairs"] = 0
        row["delta"] = None
        return row
    pad = paired_arm_delta(pooled, arm=arm)
    row["n_pairs"] = pad.n_pairs
    row["delta"] = stat_from_paired_delta(
        pad, cost_level=cost_level_for(pooled), facet=facet
    ).as_dict()
    return row


def _breakdown_row(
    key: str, summary: PerformanceSummary, subset: list[PaperTrade], facet: str
) -> dict[str, object]:
    """One breakdown row (timeframe / rank / score / regime): the Stat guards the
    expectancy claim; win rate and the closed count ride as plain context fields."""
    return {
        "key": key,
        "stat": _stat_dict(summary, subset, facet),
        "win_rate": summary.win_rate,
        "n_closed": summary.n_closed,
    }


# Forward Books wall order: decision-forcing cards first (settled and futile both
# await a human), then still-accruing books, then retired history. Built FROM
# settlement's STATES tuple so the two modules cannot drift: a fifth state breaks
# this unpacking at import time -- loudly, in tests -- never as a request-time 500.
_RETIRED, _FUTILE, _SETTLED, _ACCRUING = STATES
_STATE_RANK = {_SETTLED: 0, _FUTILE: 0, _ACCRUING: 1, _RETIRED: 2}


def _card_dict(c: SettlementCard) -> dict[str, object]:
    """Explicit wire form for a settlement card -- hand-rolled like ``_beat_dict``
    (never ``dataclasses.asdict`` on the wire): Stats serialize via their own
    ``as_dict`` and the spark's (date, value) tuples become JSON pairs here, visibly."""
    return {
        "name": c.name,
        "kind": c.kind,
        "play_type": c.play_type,
        "state": c.state,
        "n_accrued": c.n_accrued,
        "n_needed": c.n_needed,
        "eta": c.eta,
        "stopping_rule": c.stopping_rule,
        "registered_sha": c.registered_sha,
        "registered_at": c.registered_at,
        "mde_r": c.mde_r,
        "book": c.book.as_dict(),
        "control": c.control.as_dict(),
        "delta": c.delta.as_dict(),
        "upper_bound_type": c.upper_bound_type,
        "spark": [[d, v] for d, v in c.spark],
        "decision": c.decision,
    }


def _beat_dict(b: Heartbeat) -> dict[str, object]:
    """Explicit wire form -- ``Heartbeat`` has no ``as_dict`` by design, so the
    serialization contract (ISO-8601-or-null ``last``) lives here, visibly."""
    return {
        "name": b.name,
        "state": b.state,
        "last": b.last.isoformat() if b.last is not None else None,
        "period_s": b.period_s,
        "grace_s": b.grace_s,
        "detail": b.detail,
    }
