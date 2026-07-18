"""Swing discipline metrics over the shadow book's EXISTING columns.

Pure, None-safe read-model aggregations that answer execution-discipline questions
from columns the swing module already records -- no new tables and, deliberately, no
checklist (the GEX module owns checklist / A+-rate / sat-on-hands discipline over its
own data; see the Journal v1 plan). Everything is R-native and per closed-filled
trade; an empty or all-unqualified cohort yields ``None`` metric floats and zero
counts, never a division error.

Metrics (longs):

- ``giveback_r`` -- mean ``mfe_r - realized_r`` ("left on the table": how much of the
  favorable excursion was handed back), averaged over closed-filled trades that have a
  computable excursion.
- ``stop_honored_rate`` -- share of closed-filled trades (with a known ``exit_reason``)
  that exited on the stop.
- ``avg_mae_before_win`` -- mean MAE-R on winning trades that have an excursion ("did
  winners dip first?").

The excursion math is reused from :mod:`swing_screener.journal.excursions`
(``mae_r = (entry - low_water)/risk``, ``mfe_r = (high_water - entry)/risk``; ``None``
when any of ``low_water``/``high_water``/``entry_price``/``risk`` is None or risk == 0).
"""

from collections.abc import Iterable
from statistics import mean

from swing_screener.analytics.performance import _is_closed_filled
from swing_screener.db.models import PaperTrade
from swing_screener.journal.excursions import excursion_r


def discipline_report(trades: Iterable[PaperTrade]) -> dict:
    """Aggregate swing discipline metrics over ``trades``. Pure and None-safe."""
    closed = [t for t in trades if _is_closed_filled(t)]

    givebacks: list[float] = []
    win_maes: list[float] = []
    for t in closed:
        exc = excursion_r(t)
        r = t.realized_r
        if exc is None or r is None:
            continue
        givebacks.append(exc.mfe_r - r)
        if r > 0:
            win_maes.append(exc.mae_r)

    with_reason = [t for t in closed if t.exit_reason is not None]
    n_stopped = sum(1 for t in with_reason if t.exit_reason == "stop")

    return {
        "giveback_r": mean(givebacks) if givebacks else None,
        "stop_honored_rate": (n_stopped / len(with_reason)) if with_reason else None,
        "avg_mae_before_win": mean(win_maes) if win_maes else None,
        "n_closed": len(closed),
        "n_with_excursion": len(givebacks),
        "n_wins": len(win_maes),
        "n_stopped": n_stopped,
        "n_with_exit_reason": len(with_reason),
    }
