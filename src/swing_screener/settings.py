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
    # Deep-analysis (Opus web-search analyst) -- all default to the cheap/off path.
    deep_analysis_enabled: bool
    analysis_model: str
    analysis_reasoning: str  # one of none/low/medium/high (-> extended-thinking budget)
    deep_analysis_top_n: int
    deep_analysis_kinds: frozenset[str]
    analysis_max_searches: int


_TRUE = {"1", "true", "yes", "on"}
_REASONING = {"none", "low", "medium", "high"}


def _abs(value: str) -> Path:
    """Resolve a (possibly relative) path string to an absolute path."""
    return Path(value).resolve()


def _int(value: str | None, default: int) -> int:
    """Parse an int env var, falling back to ``default`` on missing/garbage."""
    try:
        return int(value) if value is not None else default
    except ValueError:
        return default


def load_settings() -> Settings:
    """Build a :class:`Settings` from the CURRENT environment.

    Reads ``os.environ`` fresh on every call so tests can set env and observe
    the change. Path defaults are repo-relative for local parity but always
    resolved absolute so a container with a different cwd still behaves.
    """
    env = os.environ
    reasoning = env.get("SWING_ANALYSIS_REASONING", "high").strip().lower()
    if reasoning not in _REASONING:  # invalid -> max reasoning rather than silently weaker
        reasoning = "high"
    kinds_raw = env.get("SWING_DEEP_ANALYSIS_KINDS", "daily,weekly,monthly")
    kinds = frozenset(k.strip().lower() for k in kinds_raw.split(",") if k.strip())
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
        deep_analysis_enabled=(env.get("SWING_DEEP_ANALYSIS", "").strip().lower() in _TRUE),
        analysis_model=env.get("SWING_ANALYSIS_MODEL", "claude-opus-4-8"),
        analysis_reasoning=reasoning,
        deep_analysis_top_n=_int(env.get("SWING_DEEP_ANALYSIS_TOP_N"), 5),
        deep_analysis_kinds=kinds,
        analysis_max_searches=_int(env.get("SWING_ANALYSIS_MAX_SEARCHES"), 4),
    )
