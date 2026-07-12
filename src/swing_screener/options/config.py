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
    # direction over this many bars (slope confirmation).
    slope_lookback: int = 3

    # GEX computation
    max_expiries: int = 4          # nearest N expiries in the map (0DTE + weeklies)
    risk_free_rate: float = 0.04   # BS r; precision is irrelevant for wall RANKING
    # Strikes further than this fraction from spot are noise for wall detection.
    strike_window_pct: float = 0.15

    # Chain-liquidity guard (warn, never block -- see docs/modules/gex-lab.md).
    min_total_oi: int = 10_000       # sum of OI across the windowed chain
    min_populated_strikes: int = 10  # strikes with OI > 0 inside the window
