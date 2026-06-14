"""One-off: build the screening universe seed CSV from S&P 500 constituents.

Requires lxml (used by pandas.read_html). Run once, then commit
src/swing_screener/data/universe_seed.csv. The loader is source-agnostic, so
the seed can later be swapped/extended (e.g. Russell 1000) without code changes.

Run:  python scripts/make_universe_seed.py
"""
import io
from pathlib import Path

import pandas as pd
import requests

OUT = Path("src/swing_screener/data/universe_seed.csv")
URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
HEADERS = {"User-Agent": "Mozilla/5.0 (swing-screener universe seed builder)"}


def main() -> None:
    resp = requests.get(URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    tables = pd.read_html(io.StringIO(resp.text))
    df = tables[0]  # first table is the constituents list
    out = pd.DataFrame(
        {
            # yfinance uses '-' for share classes (BRK.B -> BRK-B)
            "ticker": df["Symbol"].astype(str).str.replace(".", "-", regex=False).str.upper(),
            "name": df["Security"].astype(str),
            "exchange": "",  # not provided by the source; left blank
        }
    ).drop_duplicates(subset="ticker")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(OUT, index=False)
    print(f"wrote {len(out)} tickers to {OUT}")


if __name__ == "__main__":
    main()
