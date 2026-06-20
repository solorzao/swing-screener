"""Validity controls for replay/A-B output: an out-of-sample era split and a
label-shuffle placebo. Pure, no I/O. Both operate on a list of PaperTrade rows.

These exist because a replay/A-B harness is trivially fooled by its own freedom:
any arm or threshold chosen on the same data it was measured on is in-sample, and a
comparison that finds a "winner" might just be reading noise the harness leaked.
The two helpers here are the cheap insurance against both failure modes --
``partition_by_era`` forces an out-of-sample re-confirmation, and
``shuffle_realized_r`` is a negative control that a sound harness must pass.
"""

import random
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date

from swing_screener.db.models import PaperTrade


def _copy_except_id(t: PaperTrade, **overrides: object) -> PaperTrade:
    """A fresh, session-free PaperTrade carrying every column of ``t`` except the
    primary key, with optional column overrides. Same copy-except-id idiom as
    ``pipeline.replay._detach`` -- kept inline here to keep this module I/O- and
    pipeline-free (it imports only the model)."""
    cols: dict[str, object] = {
        c.name: getattr(t, c.name)
        for c in PaperTrade.__table__.columns
        if c.name != "id"
    }
    cols.update(overrides)
    return PaperTrade(**cols)


@dataclass(frozen=True)
class EraSplit:
    dev: list[PaperTrade]          # opened on/before cutoff
    confirm: list[PaperTrade]      # opened after cutoff


def partition_by_era(trades: Iterable[PaperTrade], cutoff: date) -> EraSplit:
    """Split trades into a development era (opened_date <= cutoff) and a frozen
    confirmation era (opened_date > cutoff). Rows with no opened_date are dropped
    (they were never filled). Any arm/threshold chosen on ``dev`` must be re-confirmed
    on ``confirm`` -- without an out-of-sample era, every replay-derived decision is
    in-sample."""
    dev: list[PaperTrade] = []
    confirm: list[PaperTrade] = []
    for t in trades:
        if t.opened_date is None:
            continue
        (dev if t.opened_date <= cutoff else confirm).append(t)
    return EraSplit(dev=dev, confirm=confirm)


def shuffle_realized_r(trades: Iterable[PaperTrade], *, seed: int) -> list[PaperTrade]:
    """Return COPIES of the trades with the closed-filled rows' realized_r permuted
    across rows (seeded). A placebo control: re-running the A/B guard on shuffled
    labels must find NO winner. If a 'winner' survives the shuffle, the harness is
    leaking -- the apparent edge is an artifact, not signal. Open/unfilled rows are
    passed through unchanged (their realized_r is None and not part of the
    comparison)."""
    trades = list(trades)
    closed_idx = [
        i for i, t in enumerate(trades)
        if t.status == "closed" and t.fill_status == "filled" and t.realized_r is not None
    ]
    values = [trades[i].realized_r for i in closed_idx]
    rng = random.Random(seed)
    rng.shuffle(values)
    shuffled = dict(zip(closed_idx, values, strict=True))
    return [
        _copy_except_id(t, realized_r=shuffled[i]) if i in shuffled else _copy_except_id(t)
        for i, t in enumerate(trades)
    ]
