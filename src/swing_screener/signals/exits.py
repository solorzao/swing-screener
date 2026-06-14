from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from swing_screener.config import StrategyConfig

ExitAction = Literal["EXIT", "HOLD"]
ExitTier = Literal["hard", "strong", "advisory"]
ExitReason = Literal["stop", "momentum_flip", "target", "time_stop"]


@dataclass(frozen=True)
class OpenTrade:
    entry: float
    stop: float
    target: float
    timeframe: str
    bars_held: int


@dataclass(frozen=True)
class ExitDecision:
    action: ExitAction
    tier: ExitTier | None
    reason: ExitReason | None


def evaluate_exit(trade: OpenTrade, bar: Mapping, cfg: StrategyConfig) -> ExitDecision:
    # 1) hard stop: absolute override
    if bar["low"] <= trade.stop:
        return ExitDecision("EXIT", "hard", "stop")
    # 2) strong: HA momentum flip (bearish shaved head)
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
