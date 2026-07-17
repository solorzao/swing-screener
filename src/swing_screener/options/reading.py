"""Deterministic plain-language reading of a GEX map.

Turns one snapshot's level FACTS (spot, walls, flip, regime, net) into the
sentences a public GEX dashboard prints under its chart: what the regime means
mechanically (dealer hedging with or against moves), where the structure sits
relative to spot, and which side of the map is fragile. Pure templating over the
deterministic levels -- no model, no LLM, no invented numbers (North Star #4:
the rules engine sets levels; this only *annotates* them). Rendered under the
Day Plan profile chart and on the ad-hoc analyze result.
"""

from collections.abc import Sequence

__all__ = ["describe_gex", "fmt_gex_dollars"]


def fmt_gex_dollars(value: float) -> str:
    """Dollar gamma as a compact magnitude ('$1.2B', '$340M', '$8.5M', '$120K').

    Per-strike dollar GEX (gamma x OI x 100 x spot^2 x 1%) runs to eight-plus
    digits on index products; raw floats are unreadable on an axis or in prose.
    """
    sign = "-" if value < 0 else ""
    v = abs(value)
    if v >= 1e9:
        return f"{sign}${v / 1e9:.1f}B"
    if v >= 1e6:
        return f"{sign}${v / 1e6:.0f}M"
    if v >= 1e3:
        return f"{sign}${v / 1e3:.0f}K"
    return f"{sign}${v:.0f}"


def _pct_away(level: float, spot: float) -> str:
    """Signed distance of a level from spot, as '+1.1%' / '−2.3%'."""
    pct = (level - spot) / spot * 100.0
    return f"{'+' if pct >= 0 else '−'}{abs(pct):.1f}%"


def describe_gex(
    *,
    spot: float,
    call_wall: float | None,
    put_wall: float | None,
    gamma_flip: float | None,
    regime: str,
    net_gex: float | None = None,
    thin_chain: bool = False,
    thin_reasons: Sequence[str] = (),
) -> list[str]:
    """The map's meaning, one claim per line, worst news last.

    Only speaks about levels that exist -- a None wall or flip simply produces
    no sentence (never a fabricated number). The thin-chain warning, when
    present, is always the FIRST line: unreliable levels must be flagged before
    they are interpreted.
    """
    lines: list[str] = []

    if thin_chain:
        detail = f" ({'; '.join(thin_reasons)})" if thin_reasons else ""
        lines.append(
            f"THIN CHAIN{detail} — these levels are unreliable; "
            "cross-check a public GEX dashboard before trusting them."
        )

    if regime == "positive":
        flip_part = (
            f"spot {spot:,.0f} is above the gamma flip ({gamma_flip:,.0f})"
            if gamma_flip is not None
            else f"net dealer gamma is positive at spot {spot:,.0f}"
        )
        lines.append(
            f"Positive gamma: {flip_part} — dealers hedge AGAINST moves "
            "(sell rallies, buy dips), which dampens volatility and favors "
            "pinning / mean-reversion."
        )
    elif regime == "negative":
        flip_part = (
            f"spot {spot:,.0f} is below the gamma flip ({gamma_flip:,.0f})"
            if gamma_flip is not None
            else f"net dealer gamma is negative at spot {spot:,.0f}"
        )
        lines.append(
            f"Negative gamma: {flip_part} — dealers hedge WITH moves "
            "(sell weakness, buy strength), which amplifies volatility; "
            "moves can accelerate."
        )
    else:
        lines.append(
            "Regime unknown — no gamma flip could be located and net gamma "
            "is indeterminate; the map has no directional read (stand down)."
        )

    if call_wall is not None:
        lines.append(
            f"Call wall {call_wall:,.0f} ({_pct_away(call_wall, spot)}): the "
            "heaviest call-gamma strike above spot — hedging stiffens into "
            "it, so it acts as resistance and a magnet for pinning."
        )
    if put_wall is not None:
        lines.append(
            f"Put support {put_wall:,.0f} ({_pct_away(put_wall, spot)}): the "
            "heaviest put-gamma strike below spot — hedging cushions into "
            "it, so it acts as first support."
        )

    # The asymmetry callout: which side of the flip is fragile from HERE.
    if gamma_flip is not None and regime == "positive" and gamma_flip < spot:
        toward = (
            f" toward put support {put_wall:,.0f}" if put_wall is not None else ""
        )
        lines.append(
            f"Below {gamma_flip:,.0f} ({_pct_away(gamma_flip, spot)}) the regime "
            f"flips negative — a break of that level can accelerate{toward}: "
            "slow grind up, fast break down."
        )
    elif gamma_flip is not None and regime == "negative" and gamma_flip > spot:
        lines.append(
            f"Above {gamma_flip:,.0f} ({_pct_away(gamma_flip, spot)}) the regime "
            "flips positive — reclaiming it would restore dealer dampening."
        )

    if net_gex is not None and regime != "unknown":
        lines.append(
            f"Net dealer gamma across the strike window: "
            f"{fmt_gex_dollars(net_gex)} per 1% move."
        )

    lines.append(
        "Model: Black-Scholes gamma × open interest, standard "
        "public-dashboard convention — structure, not prophecy."
    )
    return lines
