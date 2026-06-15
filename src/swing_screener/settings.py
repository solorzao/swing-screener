"""Centralized, env-first configuration.

The same code runs locally (env vars / SQLite / relative dirs) and in an Azure
container (Key Vault secrets / mssql / absolute dirs). To make that work,
``os.environ`` is read at CALL time inside :func:`load_settings`, never captured
into module-level constants at import time -- tests monkeypatch env then call.

Every directory is resolved to an ABSOLUTE path: a repo-relative default like
``.charts`` is correct locally but wrong in a container with a different working
directory, so we ``Path(value).resolve()`` regardless of source. This module is
intentionally import-light: NO azure imports live here.
"""

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    """Immutable snapshot of configuration resolved from the current env."""

    db_url: str
    chart_dir: Path
    cache_dir: Path
    pdf_dir: Path
    blob_account_url: str | None
    blob_container: str
    key_vault_url: str | None
    azure_client_id: str | None
    acs_endpoint: str | None
    acs_sender: str | None


def _abs(value: str) -> Path:
    """Resolve a (possibly relative) path string to an absolute path."""
    return Path(value).resolve()


def load_settings() -> Settings:
    """Build a :class:`Settings` from the CURRENT environment.

    Reads ``os.environ`` fresh on every call so tests can set env and observe
    the change. Path defaults are repo-relative for local parity but always
    resolved absolute so a container with a different cwd still behaves.
    """
    env = os.environ
    return Settings(
        db_url=env.get("SWING_DB_URL", "sqlite:///local.db"),
        chart_dir=_abs(env.get("SWING_CHART_DIR", ".charts")),
        cache_dir=_abs(env.get("SWING_CACHE_DIR", ".cache")),
        pdf_dir=_abs(env.get("SWING_PDF_DIR", ".digests")),
        blob_account_url=env.get("SWING_BLOB_ACCOUNT_URL"),
        blob_container=env.get("SWING_BLOB_CONTAINER", "charts"),
        key_vault_url=env.get("KEY_VAULT_URL"),
        azure_client_id=env.get("AZURE_CLIENT_ID"),
        acs_endpoint=env.get("SWING_ACS_ENDPOINT"),
        acs_sender=env.get("SWING_ACS_SENDER"),
    )
