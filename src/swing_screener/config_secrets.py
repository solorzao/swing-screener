"""Env-first secret resolution with an optional Azure Key Vault fallback.

Precedence for every lookup:

1. ``os.environ[name]`` -- read FRESH on every call (never cached), so the local
   workflow (export the var) wins and tests can monkeypatch it.
2. Key Vault -- only if ``KEY_VAULT_URL`` is set. The env NAME maps to a secret
   name by lower-casing and replacing ``_`` -> ``-`` (``ANTHROPIC_API_KEY`` ->
   ``anthropic-api-key``). The azure SDKs are imported LAZILY inside this branch
   because they are an optional extra not installed in CI. Successful KV results
   are cached; on ANY failure we log the secret NAME ONLY (never the value) and
   fall through to the default.
3. The supplied ``default``.

No secret VALUE is ever logged on any path in this module.
"""

import logging
import os
from typing import Any

log = logging.getLogger(__name__)

# Cache of SUCCESSFUL Key Vault lookups only (keyed by env-var name). The env
# path is never cached -- it is re-read on every call. Tests clear this via
# reset_cache().
_kv_cache: dict[str, str] = {}


def reset_cache() -> None:
    """Clear the Key Vault success cache (test seam)."""
    _kv_cache.clear()


def _secret_name(name: str) -> str:
    """Map an env-var name to a Key Vault secret name (``A_B`` -> ``a-b``)."""
    return name.lower().replace("_", "-")


def _build_secret_client(vault_url: str) -> Any:
    """Construct an Azure ``SecretClient`` for ``vault_url``.

    Imports ``azure-identity`` / ``azure-keyvault-secrets`` LAZILY: they are an
    optional extra, so importing them at module top would break CI. Isolated in
    its own function so tests can monkeypatch it without touching azure.
    """
    from azure.identity import DefaultAzureCredential
    from azure.keyvault.secrets import SecretClient

    return SecretClient(vault_url=vault_url, credential=DefaultAzureCredential())


def _fetch_from_key_vault(name: str, vault_url: str) -> str | None:
    """Fetch a secret from Key Vault, caching success; None on any failure.

    NEVER logs the secret value -- only the env-var NAME on the warning path.
    """
    if name in _kv_cache:
        return _kv_cache[name]
    try:
        client = _build_secret_client(vault_url)
        value = client.get_secret(_secret_name(name)).value
    except Exception:  # noqa: BLE001 -- best-effort key vault lookup, falls back to default
        # NAME only -- never the value, and never the raw exception text (which
        # could echo the value back). exc_info is omitted for the same reason.
        log.warning("key vault lookup failed for secret %s; falling back to default", name)
        return None
    if value is None:
        return None
    _kv_cache[name] = value
    return value


def get_secret(name: str, *, default: str | None = None) -> str | None:
    """Resolve a secret: env (fresh) -> Key Vault (if configured) -> default."""
    env_value = os.environ.get(name)
    if env_value is not None:
        return env_value

    vault_url = os.environ.get("KEY_VAULT_URL")
    if vault_url:
        value = _fetch_from_key_vault(name, vault_url)
        if value is not None:
            return value

    return default


def require_secret(name: str) -> str:
    """Like :func:`get_secret` but raise if the secret is missing/empty."""
    value = get_secret(name)
    if not value:
        raise RuntimeError(f"missing required secret: {name}")
    return value
