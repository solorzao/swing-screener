"""One-off: fetch AMD daily bars for the 2018 example and freeze them as a CSV
fixture for the golden test. Requires yfinance (not a project dependency).

Run once:  python scripts/make_amd_fixture.py
Then commit tests/fixtures/amd_daily_2018.csv.
"""
from pathlib import Path

import pandas as pd
import yfinance as yf

OUT = Path("tests/fixtures/amd_daily_2018.csv")


def main() -> None:
    df = yf.download(
        "AMD",
        start="2018-03-01",
        end="2018-08-15",
        interval="1d",
        auto_adjust=False,
        progress=False,
    )
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]]
    df.index.name = "date"
    OUT.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(OUT)
    print(f"wrote {len(df)} rows to {OUT}")


if __name__ == "__main__":
    main()
