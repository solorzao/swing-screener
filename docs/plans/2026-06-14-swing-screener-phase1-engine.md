# Swing Screener — Phase 1 (Engine Core) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Each component is built test-first per superpowers:test-driven-development.

**Goal:** Build the deterministic, fully-tested engine that turns OHLCV bars into Heiken Ashi pullback-continuation long signals — with entry zones, tiered exits, and a ranking score — and prove it reproduces the user's AMD daily setup via a golden test.

**Architecture:** Pure functions over pandas DataFrames, no I/O, no network, no DB. Indicators → HA classification → signal detection → entry-zone/stop/target → exits → score. Everything is config-driven via one `StrategyConfig` dataclass. This is the kernel that later phases (data fetch, shadow book, dashboard, email, Azure) wrap.

**Tech Stack:** Python 3.12, pandas, numpy, pytest, ruff, mypy. No external services in this phase.

**Design reference:** `docs/plans/2026-06-14-swing-screener-design.md`

---

## Conventions

- All bar DataFrames use lowercase columns `open, high, low, close, volume` and a sorted `DatetimeIndex`.
- All engine functions are **pure** (no mutation of inputs; return new objects).
- Money/price comparisons that test "no wick" use a fractional tolerance (`wick_frac`), never `==`.
- Each task: write the failing test → run it (confirm failure) → minimal implementation → run (confirm pass) → commit.
- Commit messages: `feat:`, `test:`, `chore:` prefixes; end with the Co-Authored-By trailer.

---

## Task 0: Project scaffolding

**Files:**
- Create: `pyproject.toml`
- Create: `src/swing_screener/__init__.py`
- Create: `tests/__init__.py`
- Create: `tests/conftest.py`

**Step 1: Write `pyproject.toml`**

```toml
[project]
name = "swing-screener"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = ["pandas>=2.2", "numpy>=1.26"]

[project.optional-dependencies]
dev = ["pytest>=8", "ruff>=0.6", "mypy>=1.11", "pandas-stubs"]

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
pythonpath = ["src"]
testpaths = ["tests"]

[tool.ruff]
line-length = 100
target-version = "py312"

[tool.mypy]
python_version = "3.12"
packages = ["swing_screener"]
ignore_missing_imports = true
```

**Step 2: Create empty `src/swing_screener/__init__.py` and `tests/__init__.py`.**

**Step 3: Create `tests/conftest.py` with a reusable bar-builder fixture**

```python
import pandas as pd
import pytest


def make_bars(rows: list[dict], start: str = "2024-01-01", freq: str = "D") -> pd.DataFrame:
    """Build an OHLCV DataFrame from a list of dicts with keys open/high/low/close[/volume]."""
    idx = pd.date_range(start=start, periods=len(rows), freq=freq)
    df = pd.DataFrame(rows, index=idx)
    if "volume" not in df.columns:
        df["volume"] = 1_000_000
    return df[["open", "high", "low", "close", "volume"]].astype(float)


@pytest.fixture
def bars():
    return make_bars
```

**Step 4: Set up the environment and confirm pytest runs**

Run:
```
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\python -m pytest -q
```
Expected: `no tests ran` (collection succeeds, exit 0 or 5).

**Step 5: Commit**

```
git checkout -b phase1-engine
git add pyproject.toml src tests
git commit -m "chore: scaffold project (pyproject, pytest, package layout)"
```

---

## Task 1: Heiken Ashi transform

**Files:**
- Create: `src/swing_screener/indicators/__init__.py`
- Create: `src/swing_screener/indicators/heiken_ashi.py`
- Test: `tests/indicators/test_heiken_ashi.py`

**Step 1: Write the failing test**

```python
# tests/indicators/test_heiken_ashi.py
import pandas as pd
from swing_screener.indicators.heiken_ashi import heiken_ashi


def test_first_bar_seeds_ha_open_as_avg_of_open_close(bars):
    df = bars([{"open": 10, "high": 12, "low": 9, "close": 11}])
    ha = heiken_ashi(df)
    assert ha["ha_close"].iloc[0] == (10 + 12 + 9 + 11) / 4
    assert ha["ha_open"].iloc[0] == (10 + 11) / 2


def test_subsequent_ha_open_is_avg_of_prev_ha_open_and_close(bars):
    df = bars([
        {"open": 10, "high": 12, "low": 9, "close": 11},
        {"open": 11, "high": 13, "low": 10, "close": 12},
    ])
    ha = heiken_ashi(df)
    expected = (ha["ha_open"].iloc[0] + ha["ha_close"].iloc[0]) / 2
    assert ha["ha_open"].iloc[1] == expected


def test_ha_high_low_envelope(bars):
    df = bars([{"open": 10, "high": 12, "low": 9, "close": 11}])
    ha = heiken_ashi(df)
    assert ha["ha_high"].iloc[0] == max(12, ha["ha_open"].iloc[0], ha["ha_close"].iloc[0])
    assert ha["ha_low"].iloc[0] == min(9, ha["ha_open"].iloc[0], ha["ha_close"].iloc[0])
```

**Step 2: Run → expect FAIL** (`ModuleNotFoundError: ... heiken_ashi`).
Run: `.venv\Scripts\python -m pytest tests/indicators/test_heiken_ashi.py -v`

**Step 3: Minimal implementation**

```python
# src/swing_screener/indicators/heiken_ashi.py
import pandas as pd


def heiken_ashi(df: pd.DataFrame) -> pd.DataFrame:
    """Return ha_open/ha_high/ha_low/ha_close from regular OHLC bars."""
    ha_close = (df["open"] + df["high"] + df["low"] + df["close"]) / 4.0

    ha_open = pd.Series(index=df.index, dtype="float64")
    ha_open.iloc[0] = (df["open"].iloc[0] + df["close"].iloc[0]) / 2.0
    for i in range(1, len(df)):
        ha_open.iloc[i] = (ha_open.iloc[i - 1] + ha_close.iloc[i - 1]) / 2.0

    ha_high = pd.concat([df["high"], ha_open, ha_close], axis=1).max(axis=1)
    ha_low = pd.concat([df["low"], ha_open, ha_close], axis=1).min(axis=1)
    return pd.DataFrame(
        {"ha_open": ha_open, "ha_high": ha_high, "ha_low": ha_low, "ha_close": ha_close}
    )
```

Also create empty `src/swing_screener/indicators/__init__.py` and `tests/indicators/__init__.py`.

**Step 4: Run → expect PASS.**

**Step 5: Commit** — `git commit -m "feat: heiken ashi transform"`

---

## Task 2: EMA, ATR, RSI helpers

**Files:**
- Create: `src/swing_screener/indicators/trend.py`
- Test: `tests/indicators/test_trend.py`

**Step 1: Failing test**

```python
# tests/indicators/test_trend.py
import numpy as np
import pandas as pd
from swing_screener.indicators.trend import ema, atr, rsi


def test_ema_matches_pandas_ewm(bars):
    df = bars([{"open": i, "high": i + 1, "low": i - 1, "close": i} for i in range(1, 11)])
    got = ema(df["close"], span=3)
    want = df["close"].ewm(span=3, adjust=False).mean()
    pd.testing.assert_series_equal(got, want)


def test_atr_is_positive_and_wilder_smoothed(bars):
    df = bars([{"open": 10, "high": 11 + (i % 3), "low": 9 - (i % 2), "close": 10 + (i % 2)}
               for i in range(20)])
    a = atr(df, period=14)
    assert (a.dropna() > 0).all()
    assert a.notna().iloc[-1]


def test_rsi_bounds_0_100(bars):
    df = bars([{"open": 10, "high": 11, "low": 9, "close": 10 + np.sin(i)} for i in range(40)])
    r = rsi(df["close"], period=14).dropna()
    assert ((r >= 0) & (r <= 100)).all()
```

**Step 2: Run → FAIL.**

**Step 3: Implementation**

```python
# src/swing_screener/indicators/trend.py
import pandas as pd


def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [df["high"] - df["low"],
         (df["high"] - prev_close).abs(),
         (df["low"] - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))
```

**Step 4: Run → PASS. Step 5: Commit** — `feat: ema/atr/rsi indicators`

---

## Task 3: Strategy config

**Files:**
- Create: `src/swing_screener/config.py`
- Test: `tests/test_config.py`

**Step 1: Failing test**

```python
# tests/test_config.py
from swing_screener.config import StrategyConfig


def test_defaults_present():
    cfg = StrategyConfig()
    assert cfg.ema_fast == 20
    assert cfg.ema_slow == 50
    assert cfg.max_pullback_bars >= 1
    assert 0 < cfg.zone_body_frac < 1
    assert cfg.max_hold_bars["4h"] > 0


def test_override():
    cfg = StrategyConfig(ema_fast=10)
    assert cfg.ema_fast == 10
```

**Step 2: Run → FAIL. Step 3: Implementation**

```python
# src/swing_screener/config.py
from dataclasses import dataclass, field


@dataclass(frozen=True)
class StrategyConfig:
    ema_fast: int = 20
    ema_slow: int = 50
    atr_period: int = 14
    rsi_period: int = 14

    # HA classification
    wick_frac: float = 0.05        # "no wick" tolerance as fraction of range
    zone_body_frac: float = 0.30   # doji/zone: body <= frac * range

    # pullback
    min_pullback_bars: int = 1
    max_pullback_bars: int = 4

    # entry zone / risk
    ceiling_atr_mult: float = 0.35   # ceiling = trigger_close + mult * ATR
    floor_buffer_atr: float = 0.10   # floor = swing_low + buffer * ATR
    stop_buffer_atr: float = 0.25    # stop  = swing_low - buffer * ATR
    target_r_multiple: float = 2.0   # target = entry + R * risk

    # exits
    time_stop_factor: float = 1.0    # time stop = factor * max_hold_bars[tf]
    max_hold_bars: dict = field(default_factory=lambda: {
        "4h": 18,   # ~3 trading days of 4h bars
        "1d": 10,
        "1wk": 8,
        "1mo": 6,
    })
```

**Step 4: Run → PASS. Step 5: Commit** — `feat: strategy config dataclass`

---

## Task 4: Heiken Ashi candle classification

**Files:**
- Create: `src/swing_screener/signals/__init__.py`
- Create: `src/swing_screener/signals/classify.py`
- Test: `tests/signals/test_classify.py`

**Step 1: Failing test**

```python
# tests/signals/test_classify.py
from swing_screener.config import StrategyConfig
from swing_screener.indicators.heiken_ashi import heiken_ashi
from swing_screener.signals.classify import classify_ha


def test_bullish_shaved_bottom(bars):
    # green candle, open == low (no lower wick)
    df = bars([{"open": 10, "high": 13, "low": 10, "close": 12}])
    c = classify_ha(heiken_ashi(df), StrategyConfig())
    assert bool(c["bullish"].iloc[0])
    assert bool(c["shaved_bottom"].iloc[0])


def test_bearish_shaved_head(bars):
    df = bars([
        {"open": 12, "high": 13, "low": 11, "close": 12},   # seed
        {"open": 12, "high": 12, "low": 8, "close": 9},      # strong down, high near open
    ])
    c = classify_ha(heiken_ashi(df), StrategyConfig())
    assert bool(c["bearish"].iloc[1])
    assert bool(c["shaved_head"].iloc[1])


def test_zone_doji_small_body(bars):
    df = bars([{"open": 10, "high": 11, "low": 9, "close": 10.05}])
    c = classify_ha(heiken_ashi(df), StrategyConfig())
    assert bool(c["zone"].iloc[0])
```

**Step 2: Run → FAIL. Step 3: Implementation**

```python
# src/swing_screener/signals/classify.py
import pandas as pd
from swing_screener.config import StrategyConfig


def classify_ha(ha: pd.DataFrame, cfg: StrategyConfig) -> pd.DataFrame:
    rng = (ha["ha_high"] - ha["ha_low"]).clip(lower=1e-12)
    body = (ha["ha_close"] - ha["ha_open"]).abs()
    top = ha[["ha_open", "ha_close"]].max(axis=1)
    bot = ha[["ha_open", "ha_close"]].min(axis=1)
    upper_wick = ha["ha_high"] - top
    lower_wick = bot - ha["ha_low"]

    bullish = ha["ha_close"] > ha["ha_open"]
    bearish = ha["ha_close"] < ha["ha_open"]
    tol = cfg.wick_frac * rng

    return pd.DataFrame({
        "bullish": bullish,
        "bearish": bearish,
        "shaved_bottom": bullish & (lower_wick <= tol),
        "shaved_head": bearish & (upper_wick <= tol),
        "zone": body <= cfg.zone_body_frac * rng,
        "body_frac": body / rng,
    }, index=ha.index)
```

**Step 4: Run → PASS. Step 5: Commit** — `feat: heiken ashi candle classification`

---

## Task 5: Indicator assembly helper

Combine raw bars → a single enriched frame (HA + EMA + ATR + RSI + classification) that the detector consumes.

**Files:**
- Create: `src/swing_screener/signals/frame.py`
- Test: `tests/signals/test_frame.py`

**Step 1: Failing test**

```python
# tests/signals/test_frame.py
from swing_screener.config import StrategyConfig
from swing_screener.signals.frame import build_frame


def test_build_frame_has_all_columns(bars):
    df = bars([{"open": 10 + i, "high": 11 + i, "low": 9 + i, "close": 10 + i}
               for i in range(60)])
    f = build_frame(df, StrategyConfig())
    for col in ["ha_open", "ha_close", "ema_fast", "ema_slow", "atr", "rsi",
                "bullish", "shaved_bottom", "shaved_head", "zone"]:
        assert col in f.columns
    assert len(f) == len(df)
```

**Step 2: Run → FAIL. Step 3: Implementation**

```python
# src/swing_screener/signals/frame.py
import pandas as pd
from swing_screener.config import StrategyConfig
from swing_screener.indicators.heiken_ashi import heiken_ashi
from swing_screener.indicators.trend import ema, atr, rsi
from swing_screener.signals.classify import classify_ha


def build_frame(df: pd.DataFrame, cfg: StrategyConfig) -> pd.DataFrame:
    ha = heiken_ashi(df)
    cls = classify_ha(ha, cfg)
    out = df.join(ha)
    out["ema_fast"] = ema(df["close"], cfg.ema_fast)
    out["ema_slow"] = ema(df["close"], cfg.ema_slow)
    out["atr"] = atr(df, cfg.atr_period)
    out["rsi"] = rsi(df["close"], cfg.rsi_period)
    return out.join(cls)
```

**Step 4: Run → PASS. Step 5: Commit** — `feat: enriched indicator frame`

---

## Task 6: Pullback + trigger detection

Detect whether the **last** bar of the frame is a valid pullback-continuation trigger. Returns a `PullbackContext` (with the pullback window bounds) or `None`.

**Files:**
- Create: `src/swing_screener/signals/detect.py`
- Test: `tests/signals/test_detect.py`

**Step 1: Failing test** — construct a synthetic uptrend → shaved-head pullback → green trigger.

```python
# tests/signals/test_detect.py
from swing_screener.config import StrategyConfig
from swing_screener.signals.frame import build_frame
from swing_screener.signals.detect import detect_last_bar


def _uptrend_then_pullback_then_trigger(bars):
    rows = []
    # 1) long, clean uptrend (rising closes) to push ema_fast > ema_slow
    p = 10.0
    for _ in range(60):
        rows.append({"open": p, "high": p + 1.2, "low": p, "close": p + 1.0})
        p += 1.0
    # 2) shaved-head pullback: 3 down bars, high near open, holding above ema_slow
    for _ in range(3):
        rows.append({"open": p, "high": p + 0.05, "low": p - 1.5, "close": p - 1.2})
        p -= 1.2
    # 3) trigger: strong green, open == low (shaved bottom)
    rows.append({"open": p, "high": p + 2.0, "low": p, "close": p + 1.8})
    return bars(rows)


def test_detects_valid_trigger(bars):
    df = _uptrend_then_pullback_then_trigger(bars)
    ctx = detect_last_bar(build_frame(df, StrategyConfig()), StrategyConfig())
    assert ctx is not None
    assert ctx.pullback_bars >= 1
    assert ctx.swing_low < ctx.trigger_close


def test_no_signal_during_uptrend_run(bars):
    rows = [{"open": 10 + i, "high": 11 + i, "low": 10 + i, "close": 11 + i} for i in range(60)]
    ctx = detect_last_bar(build_frame(bars(rows), StrategyConfig()), StrategyConfig())
    assert ctx is None  # no pullback preceding the last green bar


def test_no_signal_when_below_ema_slow(bars):
    rows = [{"open": 50 - i, "high": 51 - i, "low": 49 - i, "close": 50 - i} for i in range(60)]
    rows.append({"open": rows[-1]["close"], "high": rows[-1]["close"] + 2,
                 "low": rows[-1]["close"], "close": rows[-1]["close"] + 1.8})
    ctx = detect_last_bar(build_frame(bars(rows), StrategyConfig()), StrategyConfig())
    assert ctx is None  # downtrend: close not above ema_slow
```

**Step 2: Run → FAIL. Step 3: Implementation**

```python
# src/swing_screener/signals/detect.py
from dataclasses import dataclass
import pandas as pd
from swing_screener.config import StrategyConfig


@dataclass(frozen=True)
class PullbackContext:
    trigger_ts: pd.Timestamp
    trigger_close: float
    atr: float
    swing_low: float          # lowest low across the pullback window
    pullback_bars: int
    shaved_bottom: bool       # trigger quality flag
    rsi: float


def detect_last_bar(f: pd.DataFrame, cfg: StrategyConfig) -> PullbackContext | None:
    if len(f) < cfg.ema_slow + cfg.max_pullback_bars + 2:
        return None

    last = f.iloc[-1]
    # 1) uptrend context at the trigger bar
    if not (last["ema_fast"] > last["ema_slow"] and last["close"] > last["ema_slow"]):
        return None
    # trigger must be a bullish HA candle
    if not bool(last["bullish"]):
        return None

    # 2) walk back over the immediately preceding bars looking for the pullback
    #    (contiguous bearish/zone bars), and require a bearish shaved head within it.
    pullback = []
    saw_shaved_head = False
    for k in range(2, cfg.max_pullback_bars + 2):
        bar = f.iloc[-k]
        if bool(bar["bearish"]) or bool(bar["zone"]):
            pullback.append(bar)
            if bool(bar["shaved_head"]):
                saw_shaved_head = True
        else:
            break

    if len(pullback) < cfg.min_pullback_bars or not saw_shaved_head:
        return None

    swing_low = min(b["ha_low"] for b in pullback)
    # 3) shallow pullback: stayed above ema_slow (continuation, not reversal)
    if swing_low <= last["ema_slow"]:
        return None

    return PullbackContext(
        trigger_ts=f.index[-1],
        trigger_close=float(last["close"]),
        atr=float(last["atr"]),
        swing_low=float(swing_low),
        pullback_bars=len(pullback),
        shaved_bottom=bool(last["shaved_bottom"]),
        rsi=float(last["rsi"]),
    )
```

**Step 4: Run → PASS.** Adjust the synthetic fixture if thresholds need nudging — the *test* encodes the intended behavior; tune `StrategyConfig` defaults, not the assertions. **Step 5: Commit** — `feat: pullback-continuation trigger detection`

---

## Task 7: Entry zone, stop, target

**Files:**
- Create: `src/swing_screener/signals/entry_zone.py`
- Test: `tests/signals/test_entry_zone.py`

**Step 1: Failing test**

```python
# tests/signals/test_entry_zone.py
from swing_screener.config import StrategyConfig
from swing_screener.signals.entry_zone import compute_zone


def test_zone_ordering_and_risk():
    cfg = StrategyConfig()
    z = compute_zone(trigger_close=100.0, atr=4.0, swing_low=96.0, cfg=cfg)
    assert z.floor < z.ceiling
    assert z.stop < z.floor
    assert z.ceiling == 100.0 + cfg.ceiling_atr_mult * 4.0
    assert z.target > z.ceiling
    # target sits target_r_multiple * risk above the reference entry
    assert z.risk > 0
```

**Step 2: Run → FAIL. Step 3: Implementation**

```python
# src/swing_screener/signals/entry_zone.py
from dataclasses import dataclass
from swing_screener.config import StrategyConfig


@dataclass(frozen=True)
class EntryZone:
    floor: float
    ceiling: float
    stop: float
    target: float
    risk: float       # reference_entry - stop (per share)
    reference: float  # midpoint used for R math


def compute_zone(trigger_close: float, atr: float, swing_low: float,
                 cfg: StrategyConfig) -> EntryZone:
    floor = swing_low + cfg.floor_buffer_atr * atr
    ceiling = trigger_close + cfg.ceiling_atr_mult * atr
    stop = swing_low - cfg.stop_buffer_atr * atr
    reference = (floor + ceiling) / 2.0
    risk = reference - stop
    target = reference + cfg.target_r_multiple * risk
    return EntryZone(floor=floor, ceiling=ceiling, stop=stop,
                     target=target, risk=risk, reference=reference)
```

**Step 4: Run → PASS. Step 5: Commit** — `feat: entry zone / stop / target`

---

## Task 8: Shadow-book fill resolution (worst-case in-zone)

Given a signal's zone and the **next** bar, decide: filled / missed / invalidated, and the fill price (worst-case in-zone for a long).

**Files:**
- Create: `src/swing_screener/signals/fill.py`
- Test: `tests/signals/test_fill.py`

**Step 1: Failing test**

```python
# tests/signals/test_fill.py
from swing_screener.signals.entry_zone import EntryZone
from swing_screener.signals.fill import resolve_fill

ZONE = EntryZone(floor=96.0, ceiling=101.0, stop=94.0, target=110.0, risk=4.0, reference=98.5)


def test_filled_at_worst_case_in_zone():
    # bar trades through the whole zone -> worst-case long fill is the ceiling
    res = resolve_fill(ZONE, bar_high=105.0, bar_low=97.0)
    assert res.status == "filled"
    assert res.price == 101.0  # min(bar_high, ceiling)


def test_filled_partial_overlap_uses_bar_high():
    res = resolve_fill(ZONE, bar_high=99.0, bar_low=95.0)
    assert res.status == "filled"
    assert res.price == 99.0


def test_missed_when_gap_above_ceiling():
    res = resolve_fill(ZONE, bar_high=120.0, bar_low=102.0)
    assert res.status == "missed"


def test_invalidated_when_gap_below_stop():
    res = resolve_fill(ZONE, bar_high=93.0, bar_low=90.0)
    assert res.status == "invalidated"
```

**Step 2: Run → FAIL. Step 3: Implementation**

```python
# src/swing_screener/signals/fill.py
from dataclasses import dataclass
from swing_screener.signals.entry_zone import EntryZone


@dataclass(frozen=True)
class FillResult:
    status: str           # "filled" | "missed" | "invalidated"
    price: float | None   # worst-case in-zone long fill, or None


def resolve_fill(zone: EntryZone, bar_high: float, bar_low: float) -> FillResult:
    # invalidated: the bar gapped/traded below the stop before we could enter in-zone
    if bar_low < zone.stop and bar_high < zone.floor:
        return FillResult("invalidated", None)
    # missed: the bar never came down into the zone (gapped above the ceiling)
    if bar_low > zone.ceiling:
        return FillResult("missed", None)
    # filled: some overlap with [floor, ceiling]; worst case for a long = highest in-zone price
    if bar_high < zone.floor:
        return FillResult("invalidated", None)
    price = min(bar_high, zone.ceiling)
    return FillResult("filled", price)
```

**Step 4: Run → PASS. Step 5: Commit** — `feat: worst-case shadow-book fill resolution`

---

## Task 9: Tiered exit logic

Given an open trade (entry, stop, target, timeframe, bars_held) and the latest enriched bar, return an `ExitDecision` with action + tier + reason. Hard stop overrides everything.

**Files:**
- Create: `src/swing_screener/signals/exits.py`
- Test: `tests/signals/test_exits.py`

**Step 1: Failing test**

```python
# tests/signals/test_exits.py
from swing_screener.config import StrategyConfig
from swing_screener.signals.exits import evaluate_exit, OpenTrade

CFG = StrategyConfig()
TRADE = OpenTrade(entry=100.0, stop=95.0, target=110.0, timeframe="1d", bars_held=3)


def _bar(close, high, low, bearish=False, shaved_head=False):
    return {"close": close, "ha_high": high, "ha_low": low,
            "low": low, "high": high, "bearish": bearish, "shaved_head": shaved_head}


def test_hard_stop_overrides_even_with_target_hit():
    bar = _bar(close=112, high=112, low=94)  # spiked to target but also broke stop
    d = evaluate_exit(TRADE, bar, CFG)
    assert d.action == "EXIT"
    assert d.tier == "hard"
    assert d.reason == "stop"


def test_momentum_flip_strong():
    bar = _bar(close=104, high=105, low=103, bearish=True, shaved_head=True)
    d = evaluate_exit(TRADE, bar, CFG)
    assert d.action == "EXIT" and d.tier == "strong" and d.reason == "momentum_flip"


def test_target_advisory():
    bar = _bar(close=111, high=111, low=108)
    d = evaluate_exit(TRADE, bar, CFG)
    assert d.action == "EXIT" and d.tier == "advisory" and d.reason == "target"


def test_time_stop_advisory():
    late = OpenTrade(entry=100, stop=95, target=110, timeframe="1d",
                     bars_held=CFG.max_hold_bars["1d"] + 1)
    d = evaluate_exit(late, _bar(close=101, high=102, low=100), CFG)
    assert d.action == "EXIT" and d.tier == "advisory" and d.reason == "time_stop"


def test_hold_when_nothing_triggers():
    d = evaluate_exit(TRADE, _bar(close=102, high=103, low=99), CFG)
    assert d.action == "HOLD"
```

**Step 2: Run → FAIL. Step 3: Implementation**

```python
# src/swing_screener/signals/exits.py
from dataclasses import dataclass
from typing import Mapping
from swing_screener.config import StrategyConfig


@dataclass(frozen=True)
class OpenTrade:
    entry: float
    stop: float
    target: float
    timeframe: str
    bars_held: int


@dataclass(frozen=True)
class ExitDecision:
    action: str    # "EXIT" | "HOLD"
    tier: str | None      # "hard" | "strong" | "advisory"
    reason: str | None    # "stop" | "momentum_flip" | "target" | "time_stop"


def evaluate_exit(trade: OpenTrade, bar: Mapping, cfg: StrategyConfig) -> ExitDecision:
    # 1) hard stop: absolute override
    if bar["low"] <= trade.stop:
        return ExitDecision("EXIT", "hard", "stop")
    # 2) strong: HA momentum flip
    if bool(bar.get("shaved_head")) or bool(bar.get("bearish")):
        # require the stronger flip (shaved head) for the strong tier;
        # plain bearish alone is weaker and handled as advisory below
        if bool(bar.get("shaved_head")):
            return ExitDecision("EXIT", "strong", "momentum_flip")
    # 3) advisory: target reached
    if bar["high"] >= trade.target:
        return ExitDecision("EXIT", "advisory", "target")
    # 4) advisory: time stop
    limit = cfg.max_hold_bars.get(trade.timeframe, 0) * cfg.time_stop_factor
    if limit and trade.bars_held >= limit:
        return ExitDecision("EXIT", "advisory", "time_stop")
    return ExitDecision("HOLD", None, None)
```

**Step 4: Run → PASS. Step 5: Commit** — `feat: tiered exit logic with hard-stop override`

---

## Task 10: Composite ranking score

Blend signal strength, MTF alignment, trend slope, and volatility-fit into one score for ranking. MTF alignment is passed in (computed by the orchestrator that runs all timeframes).

**Files:**
- Create: `src/swing_screener/signals/score.py`
- Test: `tests/signals/test_score.py`

**Step 1: Failing test**

```python
# tests/signals/test_score.py
from swing_screener.signals.score import score_signal, ScoreInputs


def test_mtf_alignment_increases_score():
    base = ScoreInputs(shaved_bottom=True, body_frac=0.9, trend_slope=0.5,
                       atr_pct=0.03, mtf_aligned=False)
    aligned = ScoreInputs(shaved_bottom=True, body_frac=0.9, trend_slope=0.5,
                          atr_pct=0.03, mtf_aligned=True)
    assert score_signal(aligned) > score_signal(base)


def test_score_bounded_0_1():
    s = score_signal(ScoreInputs(True, 1.0, 1.0, 0.05, True))
    assert 0.0 <= s <= 1.0
```

**Step 2: Run → FAIL. Step 3: Implementation**

```python
# src/swing_screener/signals/score.py
from dataclasses import dataclass


@dataclass(frozen=True)
class ScoreInputs:
    shaved_bottom: bool
    body_frac: float       # trigger body / range, 0..1
    trend_slope: float     # normalized (ema_fast - ema_slow) / price, clipped
    atr_pct: float         # ATR / price
    mtf_aligned: bool


def _clip01(x: float) -> float:
    return max(0.0, min(1.0, x))


def score_signal(s: ScoreInputs) -> float:
    strength = 0.6 * _clip01(s.body_frac) + 0.4 * (1.0 if s.shaved_bottom else 0.0)
    slope = _clip01(s.trend_slope * 20.0)           # ~0.05 slope -> 1.0
    vol_fit = _clip01(s.atr_pct / 0.04)             # reward some volatility, saturate at 4%
    mtf = 1.0 if s.mtf_aligned else 0.0
    score = 0.40 * strength + 0.25 * mtf + 0.20 * slope + 0.15 * vol_fit
    return _clip01(score)
```

**Step 4: Run → PASS. Step 5: Commit** — `feat: composite ranking score`

---

## Task 11: Golden test — reproduce the AMD daily setup

Freeze real AMD daily data covering the user's example (the May–Aug 2018 pullback zone) as a CSV fixture, then assert the engine flags a long trigger inside the pullback zone and **not** during the preceding uptrend run.

**Files:**
- Create: `scripts/make_amd_fixture.py` (one-off; run once, commit the CSV)
- Create: `tests/fixtures/amd_daily_2018.csv`
- Test: `tests/test_golden_amd.py`

**Step 1: Write the fixture generator**

```python
# scripts/make_amd_fixture.py
"""Run once: writes tests/fixtures/amd_daily_2018.csv. Requires yfinance."""
import yfinance as yf

df = yf.download("AMD", start="2018-03-01", end="2018-08-15", interval="1d", auto_adjust=False)
df = df.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]]
df.to_csv("tests/fixtures/amd_daily_2018.csv")
print(f"wrote {len(df)} rows")
```

Run: `.venv\Scripts\python scripts/make_amd_fixture.py` and commit the CSV.

**Step 2: Write the golden test**

```python
# tests/test_golden_amd.py
import pandas as pd
from swing_screener.config import StrategyConfig
from swing_screener.signals.frame import build_frame
from swing_screener.signals.detect import detect_last_bar


def _load():
    df = pd.read_csv("tests/fixtures/amd_daily_2018.csv", index_col=0, parse_dates=True)
    return df[["open", "high", "low", "close", "volume"]].astype(float)


def _signal_dates(df, cfg):
    """Sliding evaluation: a signal 'fires' on date D if detect_last_bar is truthy
    when the frame ends at D."""
    dates = []
    full = build_frame(df, cfg)
    for i in range(cfg.ema_slow + 6, len(full)):
        window = full.iloc[: i + 1]
        if detect_last_bar(window, cfg) is not None:
            dates.append(full.index[i])
    return dates


def test_amd_fires_in_pullback_zone_not_in_uptrend():
    cfg = StrategyConfig()
    df = _load()
    fired = _signal_dates(df, cfg)
    # The circled pullback/entry zone in the user's chart is ~mid-June to mid-July 2018.
    zone = [d for d in fired if pd.Timestamp("2018-06-08") <= d <= pd.Timestamp("2018-07-20")]
    uptrend_run = [d for d in fired if pd.Timestamp("2018-04-10") <= d <= pd.Timestamp("2018-05-20")]
    assert zone, "engine should flag a long trigger inside the AMD pullback zone"
    assert not uptrend_run, "engine should not fire mid-uptrend with no preceding pullback"
```

**Step 3: Run → likely FAIL first.** This is the tuning gate: adjust **`StrategyConfig` defaults** (pullback bars, wick/zone fractions, EMA lengths) until the golden test passes with realistic values. Do **not** weaken the assertion to pass. If it can't pass with sane config, the signal spec needs a design conversation — surface it.

**Step 4: Run → PASS.**

**Step 5: Commit** — `test: golden AMD daily setup reproduces strategy`

---

## Task 12: Lint, type-check, full suite, wrap up

**Step 1:** Run `.venv\Scripts\ruff check src tests` → fix issues.
**Step 2:** Run `.venv\Scripts\mypy` → fix type issues.
**Step 3:** Run `.venv\Scripts\python -m pytest -q` → all green.
**Step 4:** Per superpowers:requesting-code-review, request a review of the engine before declaring Phase 1 done.
**Step 5:** Commit — `chore: lint + types clean for phase 1 engine`. Open a PR or merge `phase1-engine` per superpowers:finishing-a-development-branch.

---

## Phases 2–6 roadmap (planned in detail after the engine is validated)

| Phase | Scope | Key tasks |
|---|---|---|
| **2. Data + pipeline + shadow book + charts (local, SQLite)** | Real bars flow through the engine nightly on your PC | universe loader; yfinance fetch + 1h→4h / 1d→1wk/1mo resampling with per-ticker isolation + cache; SQLAlchemy models (`universe`, `signals`, `trades`, `paper_trades`, `exit_events`, `email_log`); orchestrator that runs all timeframes, computes MTF alignment + score, writes signals, opens shadow trades (worst-case fill), advances open paper trades through exits; `mplfinance` annotated chart renderer (HA + EMA + shaded pullback/entry zone + stop/target). **Local dry-run for a couple weeks to validate signal quality.** |
| **3. Dashboard (local Streamlit)** | The 6 tabs over the local DB | Today's Candidates (filters, chart, "Take this trade"); Active Trades (live P/L, exit badges); Trade entry/management; Closed trades; Screener Performance (win rate / avg-R by rank bucket, fill rate, equity curve); Exit Log. Bind to `127.0.0.1`. |
| **4. Email + LLM analysis** | The digests + alerts | Claude API client (Sonnet 4.6) that narrates deterministic facts into a rationale; email composer (daily top-5, weekly, monthly, exit alerts) with inline CID charts; Gmail SMTP sender; graceful degradation (LLM/chart failures still send). |
| **5. Deploy to Azure** | Move the job to the cloud | Containerize the orchestrator; Azure Container Apps Jobs cron schedules (post-close, pre-open, 4h, weekly, monthly, ET/DST-aware); migrate store to Azure SQL serverless + Blob; Key Vault + managed identity; health heartbeat + App Insights. Dashboard stays local, points at Azure SQL/Blob via `az login`. |
| **6. Options module** | Phase 2 product feature | Revisit once on a paid data feed (~$30+/mo): options chains, basic greeks/IV, liquidity filters, call suggestions on bullish triggers. |

---

## Notes for the executor

- **Tune config, never the assertions.** Tests encode intended behavior; thresholds live in `StrategyConfig`.
- The synthetic fixtures in Tasks 6–10 may need numeric nudging to express the intended setup cleanly — that's expected; keep the *behavioral* assertion intact.
- Keep every engine function pure and I/O-free — that's what makes the golden test and later backtests trivial.
- Windows shell: use `.venv\Scripts\python` (PowerShell) or the Bash tool with forward slashes.
