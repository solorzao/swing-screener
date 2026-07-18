"""Machine pre-grade of the 12-point A+ checklist: verify what a computer can.

Eight of the twelve checklist items reduce to deterministic facts over the same
inputs the lab already computes -- the daily/5m EMA stacks (``bias.py``), a
same-day GEX snapshot (``gex.py`` levels persisted by ``plan.py``), and the typed
entry/stop/target. This module grades exactly those eight and renders advisory
hints for two of the human items; it never fetches, never calls a model, and
never writes (``reading.py``'s boundary -- North Star #4: the rules engine sets
facts, this only reports them). The remaining human items -- the self-honesty
pattern check and position sizing -- stay the trader's call, and the journaled
grade still comes from the full 12 via ``checklist.grade``.

Verdict rule: any machine item FAILS -> "no" (a tangled stack is a no even when
5m bars are missing); all eight PASS -> "yes"; otherwise (a gap, no fail) ->
"incomplete". Never fabricate a yes/no over a missing input.

Same-day / completed-bar clocks are naive US/Eastern (``chain._now_eastern``, the
lab-wide convention) and injectable so tests can freeze them.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Literal, Protocol

import pandas as pd

from swing_screener.indicators.trend import ema
from swing_screener.options.bias import StackState, stack_state
from swing_screener.options.chain import _now_eastern
from swing_screener.options.checklist import CHECKLIST_ITEMS
from swing_screener.options.config import GexConfig

State = Literal["pass", "fail", "needs_input", "unavailable"]
Verdict = Literal["yes", "no", "incomplete"]

# The eight keys this module machine-verifies, in checklist order. The other four
# stay human: chk_pattern_clean / chk_risk_sized fully, chk_price_at_pivot /
# chk_stop_structural get the advisory hints below.
MACHINE_KEYS: tuple[str, ...] = (
    "chk_daily_bias_clear", "chk_daily_stack_ordered", "chk_m5_agrees",
    "chk_gex_levels_marked", "chk_regime_match", "chk_volume_confirming",
    "chk_rr_at_least_2", "chk_confirmation_candle",
)
HINT_KEYS: tuple[str, ...] = ("chk_price_at_pivot", "chk_stop_structural")

# Guard against key drift: every key above must exist on the canonical checklist,
# or a verdict would be attributed to a key the journal rejects (checklist.grade
# raises on unknowns). Fail loud at import, not silently at decision time.
_CANON = frozenset(item.key for item in CHECKLIST_ITEMS)
_UNKNOWN = (set(MACHINE_KEYS) | set(HINT_KEYS)) - _CANON
if _UNKNOWN:
    raise ValueError(f"autograde keys absent from checklist.py: {sorted(_UNKNOWN)}")

# A 5m bar opening at T covers [T, T+5m); it is COMPLETE only once the clock has
# passed T+5m. The frame's last row may be the still-forming bar -- grading it
# would read a partial candle.
_BAR = timedelta(minutes=5)


class _Snapshot(Protocol):
    """Duck-typed GEX snapshot -- the ORM ``GexSnapshot`` row or any object with
    these attributes (routers/gex.py reads snapshots the same structural way)."""

    ts: datetime
    spot: float
    call_wall: float | None
    put_wall: float | None
    gamma_flip: float | None
    regime: str


@dataclass(frozen=True)
class ItemVerdict:
    """One machine-graded checklist item. ``fact`` is a single line the trader
    reads -- the number that decided it, never a restatement of the rule."""

    key: str
    state: State
    fact: str


@dataclass(frozen=True)
class AutoGrade:
    items: tuple[ItemVerdict, ...]   # exactly the eight MACHINE_KEYS, in order
    hints: tuple[str, ...]           # advisory: pivot, then stop
    machine_verdict: Verdict


# --- formatting -------------------------------------------------------------

def _compact(v: float) -> str:
    """Share volume as a compact magnitude ('2.1M', '780K') -- raw 5m volume runs
    to seven digits and is unreadable inline."""
    a = abs(v)
    if a >= 1e9:
        return f"{v / 1e9:.1f}B"
    if a >= 1e6:
        return f"{v / 1e6:.1f}M"
    if a >= 1e3:
        return f"{v / 1e3:.0f}K"
    return f"{v:.0f}"


# --- shared seams -----------------------------------------------------------

def _daily_stack(daily_bars: pd.DataFrame | None, cfg: GexConfig) -> StackState | None:
    """The daily EMA-stack state, or None when the frame can't seat the stack
    (missing, no ``close``, or shorter than the slowest EMA needs twice over --
    below which ``stack_state`` can only ever return tangled)."""
    if daily_bars is None or "close" not in daily_bars.columns:
        return None
    if len(daily_bars) < max(cfg.ema_spans) * 2:
        return None
    return stack_state(daily_bars["close"], cfg)


def _prep_5m(bars_5m: pd.DataFrame | None) -> tuple[pd.DataFrame | None, str]:
    """Normalize a 5m OHLCV frame to a naive US/Eastern index (yfinance frames are
    tz-aware, fixtures naive; settle.py normalizes the same way). Returns
    ``(frame, "")``, or ``(None, reason)`` -- the honest per-item fact -- when the
    frame is missing, empty, not OHLCV, or not datetime-indexed (a RangeIndex
    would coerce to 1970 stamps and read every bar as "complete": fabrication).
    Copies only when a tz conversion is needed, so the caller's frame is never
    mutated."""
    if bars_5m is None or bars_5m.empty:
        return None, "5m bars unavailable — grade by eye"
    if not {"open", "high", "low", "close", "volume"}.issubset(bars_5m.columns):
        return None, "5m bars not OHLCV — grade by eye"
    idx = bars_5m.index
    if not isinstance(idx, pd.DatetimeIndex):
        return None, "5m index not datetimed — grade by eye"
    if idx.tz is not None:
        bars_5m = bars_5m.copy()
        bars_5m.index = idx.tz_convert("America/New_York").tz_localize(None)
    return bars_5m, ""


def _last_completed_pos(frame: pd.DataFrame, now: datetime) -> int | None:
    """Integer position of the last COMPLETED 5m bar, or None when none is. Walks
    back from the last row so an in-progress final bar (now < its open + 5m) is
    skipped rather than graded as a finished candle."""
    for pos in range(len(frame) - 1, -1, -1):
        open_ts = pd.Timestamp(frame.index[pos]).to_pydatetime()
        if now >= open_ts + _BAR:
            return pos
    return None


def _snap_date(snapshot: _Snapshot) -> date | None:
    ts = getattr(snapshot, "ts", None)
    return ts.date() if ts is not None else None


# --- items ------------------------------------------------------------------

def _item_bias(is_long: bool, stack: StackState | None) -> ItemVerdict:
    key = "chk_daily_bias_clear"
    if stack is None:
        return ItemVerdict(key, "unavailable", "daily bars unavailable — grade by eye")
    side = "long" if is_long else "short"
    want = "bullish" if is_long else "bearish"
    if stack.direction == "tangled":
        return ItemVerdict(key, "fail", f"daily bias tangled · spacing {stack.spacing_pct:.2f}%")
    if stack.direction == want:
        return ItemVerdict(key, "pass",
                           f"daily bias {stack.direction} matches {side} · "
                           f"spacing {stack.spacing_pct:.2f}%")
    return ItemVerdict(key, "fail", f"daily bias {stack.direction}, setup is {side}")


def _item_stack_ordered(is_long: bool, daily_bars: pd.DataFrame | None,
                        cfg: GexConfig) -> ItemVerdict:
    # Raw ordering only -- spacing/slope-free on purpose, so items 1 and 2 stay
    # non-redundant (a tight-but-ordered stack passes 2 while 1 reads tangled).
    key = "chk_daily_stack_ordered"
    if (daily_bars is None or "close" not in daily_bars.columns
            or len(daily_bars) < max(cfg.ema_spans) * 2):
        return ItemVerdict(key, "unavailable", "daily bars unavailable — grade by eye")
    fast_span, mid_span, slow_span = cfg.ema_spans
    close = daily_bars["close"]
    f = float(ema(close, fast_span).iloc[-1])
    m = float(ema(close, mid_span).iloc[-1])
    s = float(ema(close, slow_span).iloc[-1])
    ordered = f > m > s if is_long else f < m < s
    spans = f"{fast_span}/{mid_span}/{slow_span}"
    if ordered:
        return ItemVerdict(key, "pass", f"EMA {spans} ordered ({f:.2f}/{m:.2f}/{s:.2f})")
    side = "long" if is_long else "short"
    return ItemVerdict(key, "fail",
                       f"EMA {spans} not {side}-ordered ({f:.2f}/{m:.2f}/{s:.2f})")


def _item_m5(is_long: bool, stack: StackState | None, frame5: pd.DataFrame | None,
             no_5m: str, cfg: GexConfig) -> ItemVerdict:
    key = "chk_m5_agrees"
    if frame5 is None:
        return ItemVerdict(key, "unavailable", no_5m)
    floor = max(cfg.ema_spans) * 2  # below this, stack_state can only read tangled
    if len(frame5) < floor:
        return ItemVerdict(key, "unavailable",
                           f"only {len(frame5)} 5m bars (need ≥{floor}) — grade by eye")
    if stack is None:
        return ItemVerdict(key, "unavailable", "daily bias unavailable — can't confirm 5m agreement")
    m5 = stack_state(frame5["close"], cfg)
    side = "long" if is_long else "short"
    want = "bullish" if is_long else "bearish"
    if m5.direction == want and stack.direction == want:
        return ItemVerdict(key, "pass", f"5m {m5.direction} agrees with daily + {side}")
    return ItemVerdict(key, "fail",
                       f"5m {m5.direction}, daily {stack.direction}, setup {side} — disagree")


def _item_levels_marked(snapshot: _Snapshot | None, snap_today: bool) -> ItemVerdict:
    key = "chk_gex_levels_marked"
    if snapshot is None:
        return ItemVerdict(key, "unavailable", "no GEX snapshot — run analyze first")
    if not snap_today:
        return ItemVerdict(key, "unavailable", f"GEX snapshot stale ({_snap_date(snapshot)}) — grade by eye")
    cw, pw, flip = snapshot.call_wall, snapshot.put_wall, snapshot.gamma_flip
    if cw is None or pw is None:
        missing = ("call & put walls" if cw is None and pw is None
                   else "call wall" if cw is None else "put wall")
        return ItemVerdict(key, "fail", f"{missing} unmarked")
    # A null flip is a FACT, not a failure -- walls define the structure.
    flip_note = f"flip {flip:.2f}" if flip is not None else "flip null (ok)"
    return ItemVerdict(key, "pass", f"call wall {cw:.2f}, put wall {pw:.2f} marked · {flip_note}")


def _item_regime(play_type: str, snapshot: _Snapshot | None, snap_today: bool) -> ItemVerdict:
    key = "chk_regime_match"
    if play_type not in ("breakout", "range"):
        return ItemVerdict(key, "needs_input", "pick a play type (breakout or range)")
    if snapshot is None:
        return ItemVerdict(key, "unavailable", "no GEX snapshot — regime unknown")
    if not snap_today:
        return ItemVerdict(key, "unavailable", f"GEX snapshot stale ({_snap_date(snapshot)}) — grade by eye")
    regime = snapshot.regime
    if regime == "unknown":
        return ItemVerdict(key, "fail", "gamma regime unknown — no directional map")
    ok = ((play_type == "breakout" and regime == "negative")
          or (play_type == "range" and regime == "positive"))
    if ok:
        return ItemVerdict(key, "pass", f"{play_type} play · {regime} gamma")
    want = "negative" if play_type == "breakout" else "positive"
    return ItemVerdict(key, "fail", f"{play_type} play wants {want} gamma, regime is {regime}")


def _item_volume(is_long: bool, frame5: pd.DataFrame | None, pos: int | None,
                 no_5m: str, cfg: GexConfig) -> ItemVerdict:
    key = "chk_volume_confirming"
    if frame5 is None:
        return ItemVerdict(key, "unavailable", no_5m)
    if pos is None:
        return ItemVerdict(key, "unavailable", "no completed 5m bar yet — grade by eye")
    if pos < cfg.vol_lookback:
        return ItemVerdict(key, "unavailable",
                           f"need {cfg.vol_lookback + 1}+ completed 5m bars for the volume baseline")
    vol = float(frame5["volume"].iloc[pos])
    baseline = float(frame5["volume"].iloc[pos - cfg.vol_lookback:pos].astype(float).mean())
    o = float(frame5["open"].iloc[pos])
    c = float(frame5["close"].iloc[pos])
    ts = pd.Timestamp(frame5.index[pos])
    vol_ok = vol >= cfg.vol_confirm_mult * baseline
    body_ok = c > o if is_long else c < o
    span = f"{cfg.vol_confirm_mult}×{cfg.vol_lookback}-bar avg {_compact(baseline)}"
    if vol_ok and body_ok:
        return ItemVerdict(key, "pass",
                           f"{ts:%H:%M} 5m vol {_compact(vol)} ≥ {span}, body {'up' if is_long else 'down'}")
    if not vol_ok:
        return ItemVerdict(key, "fail", f"{ts:%H:%M} 5m vol {_compact(vol)} < {span}")
    return ItemVerdict(key, "fail",
                       f"{ts:%H:%M} 5m vol {_compact(vol)} ok but body against {'long' if is_long else 'short'}")


def _item_rr(is_long: bool, entry: float | None, stop: float | None,
             target: float | None, cfg: GexConfig) -> ItemVerdict:
    key = "chk_rr_at_least_2"
    if entry is None or stop is None or target is None:
        return ItemVerdict(key, "needs_input", "enter entry/stop/target for R:R")
    side = "long" if is_long else "short"
    ordered = stop < entry < target if is_long else target < entry < stop
    if not ordered:
        return ItemVerdict(key, "fail",
                           f"{side} levels not ordered (stop {stop:.2f}, entry {entry:.2f}, target {target:.2f})")
    risk = abs(entry - stop)
    reward = abs(target - entry)
    ratio = reward / risk  # ordered strictly -> risk > 0
    if ratio >= cfg.rr_min:
        return ItemVerdict(key, "pass", f"R:R {ratio:.2f} ≥ {cfg.rr_min} (risk {risk:.2f}, reward {reward:.2f})")
    return ItemVerdict(key, "fail", f"R:R {ratio:.2f} < {cfg.rr_min}")


def _item_confirmation(is_long: bool, frame5: pd.DataFrame | None,
                       pos: int | None, no_5m: str) -> ItemVerdict:
    # v1 is deliberately simple: the last completed 5m bar closed in the setup's
    # direction. This is the item that separates an A+ from a B.
    key = "chk_confirmation_candle"
    if frame5 is None:
        return ItemVerdict(key, "unavailable", no_5m)
    if pos is None:
        return ItemVerdict(key, "unavailable", "no completed 5m bar yet — grade by eye")
    o = float(frame5["open"].iloc[pos])
    c = float(frame5["close"].iloc[pos])
    ts = pd.Timestamp(frame5.index[pos])
    confirms = c > o if is_long else c < o
    side = "long" if is_long else "short"
    if confirms:
        return ItemVerdict(key, "pass",
                           f"{ts:%H:%M} 5m closed {c:.2f} ({'up' if is_long else 'down'}, confirms {side})")
    return ItemVerdict(key, "fail",
                       f"{ts:%H:%M} 5m closed {c:.2f} — no {side} confirmation (open {o:.2f})")


# --- hints (advisory: the box stays human) ----------------------------------

def _pivot_hint(entry: float | None, pivot_level: float | None,
                snapshot: _Snapshot | None, snap_today: bool, cfg: GexConfig) -> str:
    """Distance from entry (else spot) to the nearest marked level, named and as a
    % of price, with whether that clears the at-the-pivot tolerance."""
    if entry is not None:
        ref, ref_name = float(entry), "entry"
    elif snap_today and snapshot is not None and getattr(snapshot, "spot", None) is not None:
        ref, ref_name = float(snapshot.spot), "spot"
    else:
        return "no entry or spot price — can't measure pivot distance"

    levels: list[tuple[str, float]] = []
    if snap_today and snapshot is not None:
        if snapshot.call_wall is not None:
            levels.append(("call wall", float(snapshot.call_wall)))
        if snapshot.put_wall is not None:
            levels.append(("put wall", float(snapshot.put_wall)))
        if snapshot.gamma_flip is not None:
            levels.append(("gamma flip", float(snapshot.gamma_flip)))
    if pivot_level is not None:
        levels.append(("your pivot", float(pivot_level)))
    if not levels:
        return "no GEX levels or pivot marked to measure against"

    name, value = min(levels, key=lambda nv: abs(nv[1] - ref))
    pct = abs(value - ref) / ref * 100.0 if ref else 0.0
    tol = cfg.pivot_tolerance_pct
    verdict = f"at the pivot (≤{tol:.2f}%)" if pct <= tol else f"mid-range, not at a pivot (>{tol:.2f}%)"
    return f"{ref_name} {ref:.2f} is {pct:.2f}% from {name} {value:.2f} — {verdict}"


def _stop_hint(is_long: bool, stop: float | None, snapshot: _Snapshot | None,
               snap_today: bool, frame5: pd.DataFrame | None, pos: int | None,
               cfg: GexConfig) -> str:
    """Whether the stop sits beyond the nearest protective level for the direction
    and/or beyond the recent 5m swing extreme, both named."""
    if stop is None:
        return "no stop entered — nothing to check"

    protective: float | None = None
    pname = ""
    if snap_today and snapshot is not None:
        if is_long and snapshot.put_wall is not None:
            protective, pname = float(snapshot.put_wall), "put wall"
        elif not is_long and snapshot.call_wall is not None:
            protective, pname = float(snapshot.call_wall), "call wall"

    swing: float | None = None
    if frame5 is not None and pos is not None:
        lo = max(0, pos - cfg.swing_lookback + 1)
        window = frame5.iloc[lo:pos + 1]
        swing = float(window["low"].min()) if is_long else float(window["high"].max())

    if protective is None and swing is None:
        return "no protective level or 5m swing to measure the stop against"

    parts: list[str] = []
    beyond_any = False

    def _rel(level: float) -> tuple[bool, str]:
        # "beyond" = the stop sits further from entry than structure: below a long's
        # support, above a short's resistance. Exactly ON the level counts as
        # protected but reads "at" -- "below the wall" would be a false fact.
        beyond = stop <= level if is_long else stop >= level
        if stop == level:
            word = "at"
        elif is_long:
            word = "below" if beyond else "above"
        else:
            word = "above" if beyond else "below"
        return beyond, word

    if protective is not None:
        beyond, word = _rel(protective)
        beyond_any = beyond_any or beyond
        parts.append(f"{word} {pname} {protective:.2f}")
    if swing is not None:
        beyond, word = _rel(swing)
        beyond_any = beyond_any or beyond
        parts.append(f"{word} the {cfg.swing_lookback}-bar 5m {'low' if is_long else 'high'} {swing:.2f}")

    tag = "behind structure" if beyond_any else "inside structure — not protected"
    return f"stop {stop:.2f} sits {' and '.join(parts)} — {tag}"


# --- verdict ----------------------------------------------------------------

def _verdict(items: tuple[ItemVerdict, ...]) -> Verdict:
    # A fail always wins: a tangled stack is a NO even if 5m bars are missing.
    if any(v.state == "fail" for v in items):
        return "no"
    if all(v.state == "pass" for v in items):
        return "yes"
    return "incomplete"


def autograde(
    underlying: str, direction: str, play_type: str,
    entry: float | None, stop: float | None, target: float | None,
    pivot_level: float | None, *, cfg: GexConfig,
    daily_bars: pd.DataFrame | None, bars_5m: pd.DataFrame | None,
    snapshot: _Snapshot | None,
    now: Callable[[], datetime] | None = None,
) -> AutoGrade:
    """Machine-grade the eight computable checklist items plus the two hints.

    ``daily_bars`` (a ``close`` column) and ``bars_5m`` (OHLCV) are frames-or-None;
    ``snapshot`` is a same-day GEX snapshot-or-None. Nothing is fetched -- the
    caller injects what it has, and any missing input degrades that item to
    ``unavailable``/``needs_input`` rather than fabricating a verdict. ``now``
    defaults to ``chain._now_eastern`` (naive US/Eastern) and is injectable so the
    same-day snapshot gate and the completed-bar cutoff can be frozen in tests.

    ``direction`` outside {"long", "short"} raises ``ValueError`` -- anything else
    would silently grade the ticket as short (``checklist.grade``'s posture:
    raise rather than mis-grade; T2's router turns it into a 422).
    """
    if direction not in ("long", "short"):
        raise ValueError(f"direction must be 'long' or 'short', got {direction!r}")
    clock = (now or _now_eastern)()
    today = clock.date()
    is_long = direction == "long"

    stack = _daily_stack(daily_bars, cfg)
    frame5, no_5m = _prep_5m(bars_5m)
    pos = _last_completed_pos(frame5, clock) if frame5 is not None else None
    snap_today = snapshot is not None and _snap_date(snapshot) == today

    items = (
        _item_bias(is_long, stack),
        _item_stack_ordered(is_long, daily_bars, cfg),
        _item_m5(is_long, stack, frame5, no_5m, cfg),
        _item_levels_marked(snapshot, snap_today),
        _item_regime(play_type, snapshot, snap_today),
        _item_volume(is_long, frame5, pos, no_5m, cfg),
        _item_rr(is_long, entry, stop, target, cfg),
        _item_confirmation(is_long, frame5, pos, no_5m),
    )
    hints = (
        _pivot_hint(entry, pivot_level, snapshot, snap_today, cfg),
        _stop_hint(is_long, stop, snapshot, snap_today, frame5, pos, cfg),
    )
    return AutoGrade(items=items, hints=hints, machine_verdict=_verdict(items))
