"""Pluggable, private blob chart store.

In Azure, chart PNGs live in a PRIVATE blob container because a container job's
filesystem is not shared across executions. Locally nothing changes: the store
is *enabled* iff a blob account URL is configured, and when disabled today's
exact local-file behavior is preserved.
"""

from swing_screener.storage.blob import (
    blob_enabled,
    download_bytes,
    upload_chart,
)

__all__ = ["blob_enabled", "download_bytes", "upload_chart"]
