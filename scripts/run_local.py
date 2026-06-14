"""Convenience entry point to run the nightly screener locally.

Thin wrapper over the package CLI with local-friendly defaults; equivalent to:

    python -m swing_screener.pipeline.run --db sqlite:///local.db \
        --cache-dir .cache --chart-dir .charts

Pass any of the package CLI flags through, e.g.:

    python scripts/run_local.py --max-tickers 50
"""
import sys

from swing_screener.pipeline.run import main

if __name__ == "__main__":
    # default to local sqlite + local cache/chart dirs unless overridden
    defaults = ["--db", "sqlite:///local.db", "--cache-dir", ".cache", "--chart-dir", ".charts"]
    if len(sys.argv) == 1:
        sys.argv += defaults
    main()
