"""Robinhood activity-CSV import: parse -> pair episodes -> commit tagged trades.

Three stages, all pinned to the real Robinhood "Activity" export format:

  1. ``parse_activity_csv``: raw CSV text -> ``list[FillRecord]``. The header is
     validated and any drift fails loudly; noise rows (ACH/RTP transfers, the
     blank line, the 10-column disclaimer trailer) are skipped; option
     descriptions are parsed into OCC fields; money is unwound from Robinhood's
     ``$``/comma/paren formatting. Each fill carries a stable ``import_hash``
     salted with a PER-CONTENT OCCURRENCE ORDINAL (not the line number) so that
     byte-identical duplicate fills stay distinct AND overlapping re-exports
     (which prepend newer rows) keep every existing hash unchanged.
  2. ``pair_episodes``: flat-to-flat FIFO walk per OCC contract -> ``Episode``.
  3. ``store_fills`` / ``commit_episodes``: idempotent persistence keyed on the
     unique ``import_hash`` / ``import_key`` columns (the ``add_execution_log``
     rollback-on-``IntegrityError`` pattern).
"""

import csv
import hashlib
import io
import json
import logging
import re
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from swing_screener.db.models import BrokerFill, OptionPaperTrade

log = logging.getLogger(__name__)

_EXPECTED_HEADER = [
    "Activity Date", "Process Date", "Settle Date", "Instrument",
    "Description", "Trans Code", "Quantity", "Price", "Amount",
]
_TRADE_CODES = {"BTO", "STC", "STO", "BTC", "OEXP"}
_ADD_CODES = {"BTO", "BTC"}              # increase (long open / short cover)
_REDUCE_CODES = {"STC", "STO", "OEXP"}   # decrease (close / short open / expire)

_DESC_RE = re.compile(
    r"^(?:Option Expiration for )?(?P<u>[A-Z][A-Z0-9.]*) "
    r"(?P<m>\d{1,2})/(?P<d>\d{1,2})/(?P<y>\d{4}) "
    r"(?P<right>Call|Put) \$(?P<strike>[\d,.]+)$"
)


@dataclass(frozen=True)
class FillRecord:
    import_hash: str
    activity_date: date
    underlying: str
    occ_symbol: str
    expiry: date
    right: str
    strike: float
    trans_code: str
    quantity: int
    price: float | None
    amount: float | None
    raw: str  # JSON of the original {header: value} row dict


def _money(raw: str) -> float | None:
    """Robinhood money: empty -> None; strip ``$``/commas; ``(x)`` -> ``-x``."""
    s = raw.strip()
    if not s:
        return None
    negative = s.startswith("(") and s.endswith(")")
    s = s.strip("()").replace("$", "").replace(",", "").strip()
    if not s:
        return None
    value = float(s)
    return -value if negative else value


def _qty(raw: str) -> int:
    """Quantity, dropping the OEXP ``S`` suffix (e.g. ``30S`` -> ``30``)."""
    s = raw.strip()
    if s.endswith("S"):
        s = s[:-1]
    return int(s)


def _parse_date(raw: str) -> date:
    month, day, year = (int(part) for part in raw.strip().split("/"))
    return date(year, month, day)


def _occ_symbol(underlying: str, expiry: date, right: str, strike: float) -> str:
    return f"{underlying:<6}{expiry:%y%m%d}{right}{int(round(strike * 1000)):08d}"


def parse_activity_csv(text: str) -> list[FillRecord]:
    """Parse a Robinhood activity CSV into option ``FillRecord`` rows.

    Raises ``ValueError`` if the header doesn't match the pinned export format.
    Non-option / transfer / trailer rows are skipped; a row whose *description*
    doesn't parse is warned-and-skipped, but a matching row with unparseable
    money propagates the ``float`` error (that is real data corruption).
    """
    rows = list(csv.reader(io.StringIO(text)))
    if not rows or rows[0] != _EXPECTED_HEADER:
        header = rows[0] if rows else []
        raise ValueError(f"unrecognized Robinhood CSV header: {header!r}")

    seen: Counter[tuple[str, ...]] = Counter()
    fills: list[FillRecord] = []
    for row in rows[1:]:
        if len(row) != 9:
            continue  # blank line (1 field) or the 10-column disclaimer trailer
        if not any(cell.strip() for cell in row):
            continue  # fully empty row
        instrument, description, code = row[3], row[4], row[5]
        if not instrument.strip():
            continue  # ACH/RTP transfer noise carries an empty Instrument
        if code not in _TRADE_CODES:
            continue
        match = _DESC_RE.match(description.strip())
        if match is None:
            log.warning("unrecognized option description, skipping: %r", description)
            continue

        # Per-content occurrence ordinal: count byte-identical prior rows so
        # duplicate fills get distinct hashes WITHOUT tying the hash to a line
        # number (a prepended re-export must not shift existing hashes).
        key = tuple(row)
        ordinal = seen[key]
        seen[key] += 1

        underlying = match.group("u")
        expiry = date(int(match.group("y")), int(match.group("m")), int(match.group("d")))
        right = "C" if match.group("right") == "Call" else "P"
        strike = float(match.group("strike").replace(",", ""))
        import_hash = hashlib.sha256(
            "|".join([*row, str(ordinal)]).encode("utf-8")
        ).hexdigest()

        fills.append(
            FillRecord(
                import_hash=import_hash,
                activity_date=_parse_date(row[0]),
                underlying=underlying,
                occ_symbol=_occ_symbol(underlying, expiry, right, strike),
                expiry=expiry,
                right=right,
                strike=strike,
                trans_code=code,
                quantity=_qty(row[6]),
                price=_money(row[7]),
                amount=_money(row[8]),
                raw=json.dumps(dict(zip(_EXPECTED_HEADER, row))),
            )
        )
    return fills


# --- Task 11: episode pairing + fill persistence ---------------------------


@dataclass(frozen=True)
class Episode:
    occ_symbol: str
    underlying: str
    expiry: date
    right: str
    strike: float
    opened_on: date
    closed_on: date | None
    status: str              # open | closed
    contracts: int           # max cumulative absolute position
    entry_premium: float | None  # VWAP of opening fills
    exit_premium: float | None   # VWAP of closing fills (OEXP contributes 0.0)
    pnl: float | None        # sum of fill amounts; None while open
    exit_reason: str | None  # sold | expired | None
    needs_review: bool
    import_key: str          # first fill's import_hash
    fill_hashes: tuple[str, ...]


@dataclass
class ImportStats:
    added: int
    skipped: int


def _code_rank(code: str) -> int:
    """Sort opens (BTO/BTC) before reduces within a day (see ``pair_episodes``)."""
    return 0 if code in _ADD_CODES else 1


def _vwap(side: list[tuple[int, float]]) -> float | None:
    total_qty = sum(qty for qty, _ in side)
    if total_qty == 0:
        return None
    return round(sum(qty * price for qty, price in side) / total_qty, 4)


class _EpisodeAcc:
    """Mutable accumulator for one flat-to-flat episode; frozen at ``finalize``."""

    def __init__(self, occ: str, first: FillRecord) -> None:
        self.occ = occ
        self.first = first
        self.opened_on = first.activity_date
        self.closed_on: date | None = None
        self.status = "open"
        self.max_position = 0
        self.opens: list[tuple[int, float]] = []
        self.closes: list[tuple[int, float]] = []
        self.amounts: list[float] = []
        self.exit_reason: str | None = None
        self.fill_hashes: list[str] = []

    def add_fill(self, fill: FillRecord, delta: int) -> None:
        self.fill_hashes.append(fill.import_hash)
        if fill.amount is not None:
            self.amounts.append(fill.amount)
        if delta > 0:
            self.opens.append((fill.quantity, fill.price if fill.price is not None else 0.0))
        else:
            self.closes.append((fill.quantity, fill.price if fill.price is not None else 0.0))

    def finalize(self, needs_review: bool) -> Episode:
        return Episode(
            occ_symbol=self.occ,
            underlying=self.first.underlying,
            expiry=self.first.expiry,
            right=self.first.right,
            strike=self.first.strike,
            opened_on=self.opened_on,
            closed_on=self.closed_on,
            status=self.status,
            contracts=self.max_position,
            entry_premium=_vwap(self.opens),
            exit_premium=_vwap(self.closes),
            pnl=sum(self.amounts) if self.status == "closed" else None,
            exit_reason=self.exit_reason,
            needs_review=needs_review,
            import_key=self.first.import_hash,
            fill_hashes=tuple(self.fill_hashes),
        )


def _walk_symbol(occ: str, fills: list[FillRecord]) -> list[Episode]:
    accs: list[_EpisodeAcc] = []
    position = 0
    acc: _EpisodeAcc | None = None
    needs_review = False

    for fill in fills:
        delta = fill.quantity if fill.trans_code in _ADD_CODES else -fill.quantity
        if position == 0:
            acc = _EpisodeAcc(occ, fill)
            if delta < 0:
                # Opening from flat with a reduce = short open (STO) / orphan.
                needs_review = True
        assert acc is not None
        acc.add_fill(fill, delta)
        position += delta
        acc.max_position = max(acc.max_position, abs(position))
        if position == 0:
            acc.status = "closed"
            acc.closed_on = fill.activity_date
            acc.exit_reason = "expired" if fill.trans_code == "OEXP" else "sold"
            accs.append(acc)
            acc = None

    if acc is not None:
        acc.status = "open"
        accs.append(acc)

    return [a.finalize(needs_review) for a in accs]


def pair_episodes(fills: list[FillRecord]) -> list[Episode]:
    """Group fills per OCC contract and walk each flat-to-flat, FIFO.

    The export is date-only and lists same-day fills newest-first, so a same-day
    round trip can show its close before its open; sorting opens (BTO/BTC) ahead
    of reduces (STC/STO/OEXP) within a day repairs the phantom-short the naive
    walker would otherwise see. Accepted Phase-1 limitation: multiple *separate*
    same-day round trips in one contract collapse into a single episode. Any
    contract that opens short (STO from flat) has all its episodes flagged
    ``needs_review`` and no P&L semantics beyond the raw amount sum.
    """
    by_symbol: dict[str, list[tuple[int, FillRecord]]] = {}
    for idx, fill in enumerate(fills):
        by_symbol.setdefault(fill.occ_symbol, []).append((idx, fill))

    def _sort_key(item: tuple[int, FillRecord]) -> tuple[date, int, int]:
        idx, fill = item
        return (fill.activity_date, _code_rank(fill.trans_code), idx)

    episodes: list[Episode] = []
    for occ, indexed in by_symbol.items():
        ordered = [fill for _, fill in sorted(indexed, key=_sort_key)]
        episodes.extend(_walk_symbol(occ, ordered))
    return episodes


def store_fills(session: Session, fills: list[FillRecord]) -> ImportStats:
    """Persist fills idempotently; the unique ``import_hash`` dedups re-imports."""
    added = 0
    skipped = 0
    for fill in fills:
        session.add(
            BrokerFill(
                import_hash=fill.import_hash,
                activity_date=fill.activity_date,
                underlying=fill.underlying,
                occ_symbol=fill.occ_symbol,
                trans_code=fill.trans_code,
                quantity=fill.quantity,
                price=fill.price,
                amount=fill.amount,
                raw=fill.raw,
                source="robinhood",
            )
        )
        try:
            session.commit()
            added += 1
        except IntegrityError:
            session.rollback()
            skipped += 1
    return ImportStats(added=added, skipped=skipped)


# --- Task 12: tagged episode commit ----------------------------------------


def commit_episodes(
    session: Session, episodes: list[Episode], tags: dict[str, str]
) -> int:
    """Commit episodes whose ``import_key`` is tagged ``gex`` or ``other``.

    ``skip`` / missing tags are not committed. The unique ``import_key`` makes a
    re-commit of an already-stored episode a no-op. Returns fresh-insert count.
    """
    committed = 0
    for episode in episodes:
        tag = tags.get(episode.import_key)
        if tag not in ("gex", "other"):
            continue
        opened_at = datetime.combine(episode.opened_on, datetime.min.time())
        closed_at = (
            datetime.combine(episode.closed_on, datetime.min.time())
            if episode.closed_on is not None
            else None
        )
        # direction is provisionally long even for needs_review STO-origin
        # episodes; the flag carries the doubt into human review (Phase 1).
        session.add(
            OptionPaperTrade(
                account="robinhood",
                strategy=tag,
                underlying=episode.underlying,
                direction="long",
                occ_symbol=episode.occ_symbol,
                strike=episode.strike,
                expiry=episode.expiry,
                right=episode.right,
                contracts=episode.contracts,
                entry_premium=episode.entry_premium,
                exit_premium=episode.exit_premium,
                premium_pnl=episode.pnl,
                opened_at=opened_at,
                closed_at=closed_at,
                status=episode.status,
                exit_reason=episode.exit_reason,
                import_key=episode.import_key,
                needs_review=episode.needs_review,
            )
        )
        try:
            session.commit()
            committed += 1
        except IntegrityError:
            session.rollback()
    return committed
