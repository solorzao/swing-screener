"""The volume profile is measured where the OHLCV frame lives (pipeline.analyze) and
carried to the row the digest later reads.

The digest has no frames -- it reads persisted ``Signal`` rows -- so the setup's volume
footprint has to be computed at screen time and stored, or the conviction analyst can
only ever see volume as pixels in the chart image.

Both play types describe the run-up into the trigger the same way: continuation uses the
pullback length, reversal uses the decline window. That is what makes "trigger 2.0x vs
pullback 0.4x" a comparable pair rather than two differently-normalized numbers.
"""

from datetime import date

from swing_screener.config import StrategyConfig
from swing_screener.pipeline.analyze import analyze_frames
from swing_screener.pipeline.run import _to_signal
from swing_screener.signals.frame import build_frame

RUN = date(2026, 6, 15)

# The anti-chase freshness gate is ORTHOGONAL to volume and would reject these
# deliberately decisive trigger bars before a profile could be observed. 0 disables it.
CFG = StrategyConfig(max_extension_atr=0.0)


def _dryup_then_thrust(bars, *, pullback_vol=400_000.0, trigger_vol=2_000_000.0):
    """A valid continuation trigger whose volume tells the constructive story: an uptrend
    on 1M, a pullback drying up to ``pullback_vol``, then a ``trigger_vol`` resumption.

    Four pullback-shaped bars are drawn but the detector counts the last THREE as the
    pullback (it stops one short of the trend). The first one therefore belongs to the
    BASELINE window, so it is drawn at the trend's 1M -- keeping the denominator a clean
    1M and the expected ratios exact.
    """
    rows = []
    p = 10.0
    for _ in range(60):
        rows.append({"open": p, "high": p + 1.2, "low": p, "close": p + 1.0,
                     "volume": 1_000_000.0})
        p += 1.0
    for i in range(4):
        rows.append({"open": p, "high": p + 0.05, "low": p - 1.5, "close": p - 1.2,
                     "volume": 1_000_000.0 if i == 0 else pullback_vol})
        p -= 1.2
    rows.append({"open": p, "high": p + 3.4, "low": p, "close": p + 3.0,
                 "volume": trigger_vol})
    return bars(rows)


def test_analyze_frames_measures_the_setups_volume_footprint(bars):
    frames = {"1d": build_frame(_dryup_then_thrust(bars), CFG)}

    [res] = analyze_frames("AMD", frames, CFG)

    # trigger 2.0M over a 1.0M pre-pullback baseline; the pullback ran at 0.4x of it.
    assert res.rvol_trigger == 2.0
    assert res.rvol_pullback == 0.4
    assert res.pocket_pivot is True   # up bar out-trading every recent down day


def test_the_baseline_excludes_the_pullbacks_own_dried_up_volume(bars):
    """A denominator that included the pullback would be dragged DOWN by the very
    contraction being measured, inflating the thrust ratio. Changing the dry-up depth
    must move ONLY the dry-up reading, never the trigger's."""
    quiet = analyze_frames("AMD", {"1d": build_frame(
        _dryup_then_thrust(bars, pullback_vol=100_000.0), CFG)}, CFG)[0]
    louder = analyze_frames("AMD", {"1d": build_frame(
        _dryup_then_thrust(bars, pullback_vol=800_000.0), CFG)}, CFG)[0]

    assert quiet.rvol_trigger == louder.rvol_trigger == 2.0
    assert quiet.rvol_pullback == 0.1
    assert louder.rvol_pullback == 0.8


def test_a_pullback_on_heavy_volume_reads_as_distribution(bars):
    """The inverse story must be visible too, or the fact carries no information: a
    pullback trading ABOVE its baseline is supply, not exhaustion."""
    [res] = analyze_frames("AMD", {"1d": build_frame(
        _dryup_then_thrust(bars, pullback_vol=1_500_000.0), CFG)}, CFG)

    assert res.rvol_pullback == 1.5


def test_the_profile_is_persisted_onto_the_signal_row(bars):
    """_to_signal must carry the profile through: the digest reads rows, not frames."""
    [res] = analyze_frames("AMD", {"1d": build_frame(_dryup_then_thrust(bars), CFG)}, CFG)

    sig = _to_signal(res, rank=1, run_date=RUN, first_seen=RUN)

    assert sig.rvol_trigger == 2.0
    assert sig.rvol_pullback == 0.4
    assert sig.pocket_pivot is True
