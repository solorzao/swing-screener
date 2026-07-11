"""Re-export shim -- moved to :mod:`swing_screener.data.quotes`; dashboard/ dies in Phase 3."""

from swing_screener.data.quotes import latest_close, latest_closes

__all__ = ["latest_close", "latest_closes"]
