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
