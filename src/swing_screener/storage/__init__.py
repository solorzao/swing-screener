"""Pluggable, private blob chart store.

In Azure, chart PNGs live in a PRIVATE blob container because a container job's
filesystem is not shared across executions. Locally nothing changes: the store
is *enabled* iff a blob account URL is configured, and when disabled today's
exact local-file behavior is preserved.

Import from :mod:`swing_screener.storage.blob` directly (every caller does);
this package intentionally re-exports nothing.
"""
