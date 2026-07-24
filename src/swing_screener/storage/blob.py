"""Private Azure Blob store for chart PNGs and report PDFs (optional, graceful, lazy).

The store is enabled iff ``load_settings().blob_account_url`` is set. When it is,
the pipeline uploads each rendered chart PNG to a PRIVATE container under a key,
and the pdf/dashboard consumers download it back by that same key. When it is
not set, the local filesystem is the store. The :func:`resolve_chart_bytes` /
:func:`resolve_pdf_bytes` pair owns that branching for consumers: blob key or
local path in, raw bytes out, ``None`` on any miss.

The ``azure-storage-blob`` / ``azure-identity`` packages are an optional extra
that is NOT installed in CI, so they are imported LAZILY inside the functions
that need them -- never at module import time. The container client is built
once and cached, keyed off the account URL, so a changed URL (e.g. in tests)
rebuilds it.
"""

from pathlib import Path
from typing import Any

from swing_screener.settings import load_settings

# Cache of the container client, keyed by (account_url, container). Built lazily
# on first use; rebuilt if the configured account URL changes.
_container_cache: dict[tuple[str, str], Any] = {}


def blob_enabled() -> bool:
    """True iff a blob account URL is configured (store is active)."""
    return bool(load_settings().blob_account_url)


def _get_container_client() -> Any:
    """Build (or reuse) the private container client for the configured account.

    Imports the azure SDKs LAZILY -- they are an optional extra. The client is
    cached per (account_url, container) so repeated calls do not reconstruct it,
    while a changed account URL still picks up a fresh client.
    """
    settings = load_settings()
    account_url = settings.blob_account_url
    if not account_url:
        raise RuntimeError("blob storage is not configured (no blob_account_url)")
    container = settings.blob_container
    cache_key = (account_url, container)
    cached = _container_cache.get(cache_key)
    if cached is not None:
        return cached

    from azure.identity import DefaultAzureCredential
    from azure.storage.blob import BlobServiceClient

    service = BlobServiceClient(
        account_url=account_url, credential=DefaultAzureCredential()
    )
    client = service.get_container_client(container)
    _container_cache[cache_key] = client
    return client


def upload_chart(local_path: Path, key: str) -> str:
    """Upload ``local_path`` (a PNG) to the private container under ``key``.

    Returns ``key`` so the caller can store it verbatim on the signal. Overwrites
    any existing blob at that key.
    """
    data = Path(local_path).read_bytes()
    client = _get_container_client()
    client.upload_blob(name=key, data=data, overwrite=True)
    return key


def upload_bytes(key: str, data: bytes) -> str:
    """Upload raw ``data`` to the private container under ``key``.

    Generic counterpart to :func:`upload_chart` for in-memory payloads that have
    no local file -- e.g. the on-demand PDF, which lives only as bytes. Returns
    ``key`` so the caller stores it verbatim; overwrites any existing blob.
    """
    client = _get_container_client()
    client.upload_blob(name=key, data=data, overwrite=True)
    return key


def download_bytes(key: str) -> bytes:
    """Download the blob at ``key`` from the private container as raw bytes."""
    client = _get_container_client()
    return client.get_blob_client(key).download_blob().readall()


def _resolve_bytes(key: str | None) -> bytes | None:
    """Blob-vs-local resolution to raw bytes, ``None`` on any miss.

    When the store is enabled, ``key`` is a blob KEY (the filesystem is not
    shared across Azure executions): download it, returning ``None`` on any
    failure so a missing/aged-out blob just skips the artifact. When disabled,
    ``key`` is a local path: read its bytes, with any failure (missing,
    unreadable, a directory) likewise resolving to ``None``.
    """
    if not key:
        return None
    if blob_enabled():
        try:
            return download_bytes(key)
        except Exception:  # noqa: BLE001 -- best-effort read; missing/unreadable blob -> None
            return None
    try:
        return Path(key).read_bytes()
    except Exception:  # noqa: BLE001 -- best-effort read; missing/unreadable file -> None
        return None


def resolve_chart_bytes(chart_path: str | None) -> bytes | None:
    """Resolve a signal's ``chart_path`` (blob key or local path) to PNG bytes.

    Consumed by the dashboard's candidate views and the cockpit's chart
    endpoint -- both render bytes, so the local branch reads the file rather
    than returning its path. Returns ``None`` when the chart is unset, missing,
    or the blob download fails, so callers just skip the image.
    """
    return _resolve_bytes(chart_path)


def resolve_pdf_bytes(key: str | None) -> bytes | None:
    """Resolve a report's ``pdf_blob_key`` (blob key or local path) to PDF bytes.

    Same blob-vs-local logic as :func:`resolve_chart_bytes`; consumed by the
    dashboard's download button and the cockpit's report endpoint. Returns
    ``None`` on any miss so callers just hide the download.
    """
    return _resolve_bytes(key)
