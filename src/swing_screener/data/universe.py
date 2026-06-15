import csv
from dataclasses import dataclass
from pathlib import Path

DEFAULT_SEED = Path(__file__).parent / "universe_seed.csv"


@dataclass(frozen=True)
class UniverseEntry:
    ticker: str
    name: str
    exchange: str


def load_universe(path: Path = DEFAULT_SEED) -> list[UniverseEntry]:
    """Load the screening universe from a CSV (columns: ticker,name,exchange).

    Tickers are upper-cased and de-duplicated (first occurrence wins, order
    preserved); blank/incomplete rows are skipped.
    """
    seen: set[str] = set()
    entries: list[UniverseEntry] = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            raw = (row.get("ticker") or "").strip()
            if not raw:
                continue
            ticker = raw.upper()
            if ticker in seen:
                continue
            seen.add(ticker)
            entries.append(
                UniverseEntry(
                    ticker=ticker,
                    name=(row.get("name") or "").strip(),
                    exchange=(row.get("exchange") or "").strip(),
                )
            )
    return entries


def names_by_ticker(path: Path = DEFAULT_SEED) -> dict[str, str]:
    """Map upper-cased ticker -> company name from the universe CSV.

    Built from :func:`load_universe`, so the same upper-casing and first-wins
    de-duplication apply.
    """
    return {e.ticker: e.name for e in load_universe(path)}
