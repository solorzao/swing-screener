"""Centralized, env-first configuration.

The same code runs locally (env vars / SQLite / relative dirs) and in an Azure
container (Key Vault secrets / mssql / absolute dirs). To make that work,
``os.environ`` is read at CALL time inside :func:`load_settings`, never captured
into module-level constants at import time -- tests monkeypatch env then call.

Every directory is resolved to an ABSOLUTE path: a repo-relative default like
``.charts`` is correct locally but wrong in a container with a different working
directory, so we ``Path(value).resolve()`` regardless of source. This module is
intentionally import-light: NO azure imports live here.
"""

import logging
import os
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Settings:
    """Immutable snapshot of configuration resolved from the current env."""

    db_url: str
    chart_dir: Path
    cache_dir: Path
    pdf_dir: Path
    # Playbook home: edge/<play_type>.md + <play_type>.verdicts.json (the insight engine's
    # inputs). Env-first + resolved ABSOLUTE like every other dir -- the old cwd-relative
    # Path("edge") default silently pointed nowhere in the container.
    edge_dir: Path
    blob_account_url: str | None
    blob_container: str
    key_vault_url: str | None
    azure_client_id: str | None
    acs_endpoint: str | None
    acs_sender: str | None
    # Deep-analysis (Opus web-search analyst) -- all default to the cheap/off path.
    deep_analysis_enabled: bool
    analysis_model: str
    analysis_reasoning: str  # one of none/low/medium/high/xhigh/max (-> adaptive-thinking effort)
    deep_analysis_top_n: int
    deep_analysis_kinds: frozenset[str]
    analysis_max_searches: int
    # Per-RUN deep-analysis spend ceiling (USD). None -> NO ceiling (today's behavior); once a
    # run's accumulated deep insight spend reaches it, the remaining picks fall back to the
    # deterministic narrator. Fail-safe: missing/garbage -> None, never an accidental cap.
    deep_analysis_max_usd: float | None
    # Journal v2 coaches (Personal Trade Coach + System Behavior Auditor). Both default
    # OFF; each has its own per-run USD ceiling (None -> uncapped, acceptable only
    # because the surface is default-off; missing/garbage -> None, never an accidental cap).
    coach_enabled: bool
    coach_max_usd: float | None
    audit_enabled: bool
    audit_max_usd: float | None
    # Per-trade risk sizing (for the insight engine's order intent). All optional:
    # with none set the resolver yields 0.0 -> the sizer renders R-multiples, never
    # a guessed dollar.
    account_equity: float | None
    risk_per_trade_dollars: float | None
    risk_pct: float
    max_shares: int | None
    # Execution master switch + hard-limit caps (for the execution adapter). The switch
    # FAILS SAFE: default "off" and any unknown value coerces back to "off" -- the screener
    # never accidentally arms. The caps are optional (None -> the adapter applies no cap).
    execution_mode: str  # one of off/manual/paper/live
    max_daily_notional: float | None
    max_daily_loss: float | None
    max_concurrent: int | None
    # Broker config (for the LiveAdapter). ``broker`` selects the impl ("alpaca" is the only
    # one); ``allow_real_money`` is the explicit, separate, LOUD real-money flag -- a real-money
    # endpoint arms only when execution_mode is "live" AND this is set AND the autonomy gate is
    # ready (see ``can_arm_real_money``). Both default safe: no broker, real money disallowed.
    broker: str
    allow_real_money: bool
    # TICKER LAB deep analysis: reasoning effort for the lab's user-triggered Opus
    # call. Defaults to "xhigh" -- near-max quality at ~25% less thinking budget
    # (thinking bills as OUTPUT at $25/MTok, the dominant per-call term), the
    # 2026-07-23 cost audit's low-risk trim; raise to "max" via SWING_LAB_REASONING
    # for a specific deep dive. Lives in the defaulted tail so direct Settings(...)
    # constructions stay valid.
    lab_reasoning: str = "xhigh"
    # Submit live entries as BRACKET orders (venue-held stop + target), so a filled
    # position stays protected even if the screener dies. Default ON; SWING_BRACKET_ORDERS
    # ="off" falls back to plain limit entries (reconcile-managed exits only).
    bracket_orders: bool = True
    # Weekly macro "Market Weather" report: the env off-switch for its once-a-week LLM
    # call (SWING_MARKET_REPORT). Default ON -- StrategyConfig.market_report_enabled is
    # the code-level switch and notify.market_run ANDs this env gate with it, so an
    # absent env preserves today's behavior. Only an EXPLICIT off value disables (a
    # garbage value must not silently kill a scheduled report -- bracket_orders parse).
    market_report_enabled: bool = True
    # Execution-scope CEILING (SWING_EXECUTE_PLAY_TYPES): the play types the dispatch
    # loop may submit, or None = unscoped (all -- today's behavior). Parsed like
    # deep_analysis_kinds then VALIDATED against the canonical PLAY_TYPES vocabulary:
    # an unknown member is dropped with a loud warning, and an all-garbage value
    # leaves the EMPTY set = allow-NONE (fail-closed, the mode-coercion posture --
    # garbage never widens scope). Task 22's cockpit subtraction sits BENEATH this
    # ceiling (see guardrails_repo.effective_execution_scope).
    execute_play_types: frozenset[str] | None = None
    # Route the digest's per-pick conviction calls through the Message Batches API (50%
    # off ALL tokens) instead of synchronous calls (SWING_DEEP_ANALYSIS_BATCH). Default
    # OFF -- opt-in only: it trades up-to-~1h added digest-email latency for the discount,
    # acceptable because the digest is a scheduled (non-interactive) job. See notify.batch
    # + notify.analysis.analyze_convictions_batched.
    deep_analysis_batch: bool = False


_TRUE = {"1", "true", "yes", "on"}
_REASONING = {"none", "low", "medium", "high", "xhigh", "max"}
_EXECUTION_MODES = {"off", "manual", "paper", "live"}


@dataclass(frozen=True)
class Limits:
    """The optional hard-limit caps the execution adapter enforces (None -> no cap)."""

    max_daily_notional: float | None
    max_daily_loss: float | None
    max_concurrent: int | None


def _abs(value: str) -> Path:
    """Resolve a (possibly relative) path string to an absolute path."""
    return Path(value).resolve()


def _int(value: str | None, default: int) -> int:
    """Parse an int env var, falling back to ``default`` on missing/garbage."""
    try:
        return int(value) if value is not None else default
    except ValueError:
        return default


def _opt_int(value: str | None) -> int | None:
    """Parse an optional int env var: missing/garbage -> None."""
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _opt_float(value: str | None) -> float | None:
    """Parse an optional float env var: missing/garbage -> None."""
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _float(value: str | None, default: float) -> float:
    """Parse a float env var, falling back to ``default`` on missing/garbage."""
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _parse_play_types(value: str | None) -> frozenset[str] | None:
    """Parse ``SWING_EXECUTE_PLAY_TYPES`` -- the execution-scope CEILING (fail-closed).

    Unset or blank -> ``None`` = unscoped (every play type may dispatch -- today's
    behavior, so the default changes nothing). Otherwise the value is split /
    stripped / lowered like ``deep_analysis_kinds``, then each member is validated
    against the canonical ``PLAY_TYPES`` vocabulary: an unknown member is DROPPED
    with a loud warning (the unknown-mode posture), and if NOTHING valid survives
    the result is the EMPTY set = allow-NONE (nothing dispatches). Garbage never
    widens scope.
    """
    if value is None or not value.strip():
        return None
    # Function-level import: a LAYERING guard, not a cycle break. ``pipeline.proposed``
    # imports only ``config`` + ``pipeline.variants``, so a module-level import here
    # would not cycle today -- but this module is the import-light LEAF every layer
    # pulls in, and a module-level pipeline edge would hand every settings importer
    # (db, notify, cockpit, the CLIs) the pipeline layer, and would close a real loop
    # the day anything in proposed's chain imports settings. Kept function-level so
    # neither can happen.
    from swing_screener.pipeline.proposed import PLAY_TYPES  # noqa: PLC0415
    members = {m.strip().lower() for m in value.split(",") if m.strip()}
    scope = frozenset(m for m in members if m in PLAY_TYPES)
    for unknown in sorted(members - scope):
        log.warning(
            "Unknown play type %r in SWING_EXECUTE_PLAY_TYPES; dropping it "
            "(valid: %s).", unknown, ", ".join(PLAY_TYPES))
    if not scope:
        # Unconditional whenever a NON-BLANK value leaves nothing valid (an
        # all-garbage list, or a member-free value like ","): the operator set
        # the knob, so the allow-NONE outcome must be said out loud.
        log.warning(
            "SWING_EXECUTE_PLAY_TYPES=%r contains no valid play types; execution "
            "scope is EMPTY -- nothing will dispatch.", value)
    return scope


def load_settings() -> Settings:
    """Build a :class:`Settings` from the CURRENT environment.

    Reads ``os.environ`` fresh on every call so tests can set env and observe
    the change. Path defaults are repo-relative for local parity but always
    resolved absolute so a container with a different cwd still behaves.
    """
    env = os.environ
    reasoning = env.get("SWING_ANALYSIS_REASONING", "high").strip().lower()
    if reasoning not in _REASONING:  # invalid -> strong reasoning rather than silently weaker
        reasoning = "high"
    lab_reasoning = env.get("SWING_LAB_REASONING", "xhigh").strip().lower()
    if lab_reasoning not in _REASONING:  # invalid -> the lab's near-max default
        lab_reasoning = "xhigh"
    kinds_raw = env.get("SWING_DEEP_ANALYSIS_KINDS", "daily,weekly,monthly")
    kinds = frozenset(k.strip().lower() for k in kinds_raw.split(",") if k.strip())
    execution_mode = env.get("SWING_EXECUTION_MODE", "off").strip().lower()
    if execution_mode not in _EXECUTION_MODES:  # fail safe: unknown -> off, never armed
        log.warning("Unknown SWING_EXECUTION_MODE %r; falling back to 'off'.", execution_mode)
        execution_mode = "off"
    return Settings(
        db_url=env.get("SWING_DB_URL", "sqlite:///local.db"),
        chart_dir=_abs(env.get("SWING_CHART_DIR", ".charts")),
        cache_dir=_abs(env.get("SWING_CACHE_DIR", ".cache")),
        pdf_dir=_abs(env.get("SWING_PDF_DIR", ".digests")),
        edge_dir=_abs(env.get("SWING_EDGE_DIR", "edge")),
        blob_account_url=env.get("SWING_BLOB_ACCOUNT_URL"),
        blob_container=env.get("SWING_BLOB_CONTAINER", "charts"),
        key_vault_url=env.get("KEY_VAULT_URL"),
        azure_client_id=env.get("AZURE_CLIENT_ID"),
        acs_endpoint=env.get("SWING_ACS_ENDPOINT"),
        acs_sender=env.get("SWING_ACS_SENDER"),
        deep_analysis_enabled=(env.get("SWING_DEEP_ANALYSIS", "").strip().lower() in _TRUE),
        analysis_model=env.get("SWING_ANALYSIS_MODEL", "claude-opus-4-8"),
        analysis_reasoning=reasoning,
        deep_analysis_top_n=_int(env.get("SWING_DEEP_ANALYSIS_TOP_N"), 5),
        deep_analysis_kinds=kinds,
        analysis_max_searches=_int(env.get("SWING_ANALYSIS_MAX_SEARCHES"), 4),
        deep_analysis_batch=(env.get("SWING_DEEP_ANALYSIS_BATCH", "").strip().lower() in _TRUE),
        lab_reasoning=lab_reasoning,
        deep_analysis_max_usd=_opt_float(env.get("SWING_DEEP_ANALYSIS_MAX_USD")),
        coach_enabled=(env.get("SWING_COACH_ENABLED", "").strip().lower() in _TRUE),
        coach_max_usd=_opt_float(env.get("SWING_COACH_MAX_USD")),
        audit_enabled=(env.get("SWING_AUDIT_ENABLED", "").strip().lower() in _TRUE),
        audit_max_usd=_opt_float(env.get("SWING_AUDIT_MAX_USD")),
        account_equity=_opt_float(env.get("SWING_ACCOUNT_EQUITY")),
        risk_per_trade_dollars=_opt_float(env.get("SWING_RISK_PER_TRADE_DOLLARS")),
        risk_pct=_float(env.get("SWING_RISK_PCT"), 0.01),
        max_shares=_opt_int(env.get("SWING_MAX_SHARES")),
        execution_mode=execution_mode,
        max_daily_notional=_opt_float(env.get("SWING_MAX_DAILY_NOTIONAL")),
        max_daily_loss=_opt_float(env.get("SWING_MAX_DAILY_LOSS")),
        max_concurrent=_opt_int(env.get("SWING_MAX_CONCURRENT")),
        broker=env.get("SWING_BROKER", "").strip().lower(),
        allow_real_money=(
            env.get("SWING_BROKER_ALLOW_REAL_MONEY", "").strip().lower() in _TRUE
        ),
        bracket_orders=(
            env.get("SWING_BRACKET_ORDERS", "on").strip().lower()
            not in {"off", "0", "false", "no"}
        ),
        market_report_enabled=(
            env.get("SWING_MARKET_REPORT", "on").strip().lower()
            not in {"off", "0", "false", "no"}
        ),
        execute_play_types=_parse_play_types(env.get("SWING_EXECUTE_PLAY_TYPES")),
    )


def resolve_edge_dir(explicit: Path | None) -> Path:
    """The ONE edge-dir resolution, shared by the digest and the autonomy / preflight /
    reflect CLIs: an explicit path (CLI flag / test seam) wins untouched; ``None`` falls
    back to the env-first settings value (``SWING_EDGE_DIR``, resolved absolute). Split
    resolution is how the digest and the gate CLI ended up reading DIFFERENT directories
    for the same verdicts files (2026-07-01 audit).
    """
    return explicit if explicit is not None else load_settings().edge_dir


def resolve_risk_unit(settings: Settings) -> tuple[float, int | None]:
    """Resolve the per-trade risk unit (1R, in dollars) + the optional share cap.

    Precedence: an explicit ``risk_per_trade_dollars`` wins; else ``account_equity *
    risk_pct`` when equity is set; else ``0.0``. A 0.0 risk unit is the deliberate
    "unconfigured" signal -- ``insight.size_order`` reads it as R-multiples, never a
    guessed dollar. Pure: depends only on the passed ``settings`` snapshot.
    """
    if settings.risk_per_trade_dollars is not None:
        risk_unit = settings.risk_per_trade_dollars
    elif settings.account_equity is not None:
        risk_unit = settings.account_equity * settings.risk_pct
    else:
        risk_unit = 0.0
    return risk_unit, settings.max_shares


def resolve_execution(settings: Settings) -> tuple[str, Limits]:
    """Resolve the execution mode + the hard-limit caps the adapter enforces.

    ``settings.execution_mode`` is already validated/coerced in ``load_settings`` (an
    unknown value is "off" by the time it reaches here), so this is a thin, pure projection:
    it pairs the mode with a ``Limits`` bundle of the caps. Pure: depends only on the passed
    ``settings`` snapshot, no env read.
    """
    return settings.execution_mode, Limits(
        max_daily_notional=settings.max_daily_notional,
        max_daily_loss=settings.max_daily_loss,
        max_concurrent=settings.max_concurrent,
    )


def can_arm_real_money(settings: Settings, *, gate_ready: bool) -> tuple[bool, str]:
    """Decide whether a REAL-money endpoint may arm -- three independent locks.

    Returns ``(True, "")`` ONLY when ALL three hold: ``execution_mode == "live"`` AND the
    explicit ``allow_real_money`` flag AND a ready autonomy ``gate_ready``. Any single lock
    missing refuses, naming the FIRST failing lock so a misconfig reads as one concrete cause.
    This is the load-bearing safety property: no single misconfig can move real money. It
    guards a real-money endpoint only -- a paper broker bypasses it (the adapter checks
    ``broker.is_real_money()`` first). Pure: depends only on the args, no env/DB/IO.
    """
    if settings.execution_mode != "live":
        return False, "execution_mode is not live"
    if not settings.allow_real_money:
        return False, "SWING_BROKER_ALLOW_REAL_MONEY is not set"
    if not gate_ready:
        return False, "autonomy gate is not ready"
    return True, ""


def real_money_limits_ok(limits: Limits) -> tuple[bool, str]:
    """The mandate that a real-money endpoint may not run with an unbounded cap.

    Returns ``(True, "")`` only if ``max_daily_notional``, ``max_daily_loss`` AND
    ``max_concurrent`` are ALL set -- real money must never run uncapped. Otherwise refuses,
    naming the FIRST unset cap. Paper is exempt (the adapter calls this for a real-money
    endpoint only). Pure: depends only on the passed ``Limits``, no env/DB/IO.
    """
    if limits.max_daily_notional is None:
        return False, "max_daily_notional is not set"
    if limits.max_daily_loss is None:
        return False, "max_daily_loss is not set"
    if limits.max_concurrent is None:
        return False, "max_concurrent is not set"
    return True, ""
