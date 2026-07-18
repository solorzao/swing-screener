from dataclasses import dataclass


@dataclass(frozen=True)
class GexConfig:
    # Morning-plan universe. Ad-hoc analysis accepts any ticker; only these get
    # an automatic plan. Tuple, not list: the config is frozen all the way down.
    watchlist: tuple[str, ...] = ("SPY", "QQQ")

    # EMA stack (the strategy's 9/21/50; independent from StrategyConfig's 20/50
    # by design -- lab knobs never live in the equity config).
    ema_spans: tuple[int, int, int] = (9, 21, 50)
    # A stack only counts as directional when the fast EMA has moved in the stack
    # direction over this many bars (slope confirmation, endpoint-to-endpoint so a
    # 1-bar pullback inside a trend does not flip the bias to chop).
    slope_lookback: int = 3
    # A directional stack must be meaningfully SEPARATED, not just momentarily
    # ordered: fast-to-slow spacing below this (% of price) reads as chop/tangled,
    # per the guide ("clustered EMAs = no trend"). This is the real chop guard --
    # a tight oscillation seats the 9/21/50 within ~0.1% of each other, while a
    # clean daily trend runs several %. A lab knob (tune later with real data).
    min_stack_spacing_pct: float = 0.3

    # GEX computation
    max_expiries: int = 4          # nearest N expiries in the map (0DTE + weeklies)
    risk_free_rate: float = 0.04   # BS r; precision is irrelevant for wall RANKING
    # Strikes further than this fraction from spot are noise for wall detection.
    strike_window_pct: float = 0.15

    # Chain-liquidity guard (warn, never block -- see docs/modules/gex-lab.md).
    min_total_oi: int = 10_000       # sum of OI across the windowed chain
    min_populated_strikes: int = 10  # strikes with OI > 0 inside the window

    # Checklist auto-grader (options/autograde.py). Thresholds are versioned into
    # the setup's provenance, so a later re-grade reads the values in force at
    # decision time. Lab knobs -- tune with real data, never the equity config.
    # A confirming 5m bar must trade at least this multiple of its recent volume
    # baseline: a break on average volume is not confirmation.
    vol_confirm_mult: float = 1.5
    # Bars of 5m volume that form that baseline (the completed bars BEFORE the
    # one being graded). ~100 minutes -- enough to level out a single quiet bar.
    vol_lookback: int = 20
    # Reward-to-risk floor for chk_rr_at_least_2 (the strategy's 2R rule).
    rr_min: float = 2.0
    # How close price must sit to a GEX level to count as "at the pivot", as a %
    # of price. 0.25% is ~$1.1 on a 450 index product -- roughly one strike and a
    # couple of 5m bars' range near a wall: tight enough that "at the pivot" means
    # at the level (not mid-range), loose enough to absorb the spot-vs-strike gap.
    pivot_tolerance_pct: float = 0.25
    # Completed 5m bars whose extreme (low for longs, high for shorts) the stop
    # hint measures against -- the recent swing the stop should sit behind.
    swing_lookback: int = 12
