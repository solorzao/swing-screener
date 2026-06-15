"""Tests for the env-first / Key-Vault secrets resolver.

These tests NEVER hit azure or the network: azure is an optional extra that is
not installed in CI, and the KV branch is only taken when ``KEY_VAULT_URL`` is
set. The env path and the default path are exercised here; KV is verified only
by its name-mapping helper and its no-secret-value-logging guarantee.
"""

import logging
import sys

import pytest

from swing_screener import config_secrets


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    # ensure a clean KV cache and no KEY_VAULT_URL leaking in from the real env
    config_secrets.reset_cache()
    monkeypatch.delenv("KEY_VAULT_URL", raising=False)
    yield
    config_secrets.reset_cache()


def test_env_present_returns_env_value_without_azure(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-from-env")
    assert config_secrets.get_secret("ANTHROPIC_API_KEY") == "sk-from-env"
    # the KV branch was not taken -> azure was never imported
    assert "azure" not in sys.modules


def test_env_wins_even_when_key_vault_url_set(monkeypatch):
    # env precedence beats KV: with both set, value comes from env, no azure import
    monkeypatch.setenv("KEY_VAULT_URL", "https://vault.vault.azure.net")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-env-wins")
    assert config_secrets.get_secret("ANTHROPIC_API_KEY") == "sk-env-wins"
    assert "azure" not in sys.modules


def test_default_returned_when_unset_and_no_vault(monkeypatch):
    monkeypatch.delenv("SOME_UNSET_SECRET", raising=False)
    assert config_secrets.get_secret("SOME_UNSET_SECRET", default="fallback") == "fallback"
    assert config_secrets.get_secret("SOME_UNSET_SECRET") is None


def test_secret_name_mapping():
    assert config_secrets._secret_name("ANTHROPIC_API_KEY") == "anthropic-api-key"
    assert config_secrets._secret_name("GMAIL_APP_PASSWORD") == "gmail-app-password"
    assert config_secrets._secret_name("DIGEST_TO") == "digest-to"


def test_require_secret_raises_when_unresolved(monkeypatch):
    monkeypatch.delenv("MISSING_SECRET", raising=False)
    with pytest.raises(RuntimeError, match="missing required secret: MISSING_SECRET"):
        config_secrets.require_secret("MISSING_SECRET")


def test_require_secret_returns_value(monkeypatch):
    monkeypatch.setenv("PRESENT_SECRET", "value")
    assert config_secrets.require_secret("PRESENT_SECRET") == "value"


def test_require_secret_treats_empty_as_missing(monkeypatch):
    monkeypatch.setenv("EMPTY_SECRET", "")
    with pytest.raises(RuntimeError, match="missing required secret: EMPTY_SECRET"):
        config_secrets.require_secret("EMPTY_SECRET")


def test_kv_failure_logs_name_only_never_value(monkeypatch, caplog):
    # force the KV branch with a fake SecretClient whose value would leak if logged.
    secret_value = "super-secret-value-do-not-log"
    monkeypatch.setenv("KEY_VAULT_URL", "https://vault.vault.azure.net")
    monkeypatch.delenv("SOME_SECRET", raising=False)

    class _BoomClient:
        def __init__(self, *a, **k):
            pass

        def get_secret(self, name):
            raise RuntimeError(f"boom: {secret_value}")

    # patch the lazy builders so no real azure import / network happens
    monkeypatch.setattr(config_secrets, "_build_secret_client", lambda url: _BoomClient())

    with caplog.at_level(logging.WARNING):
        out = config_secrets.get_secret("SOME_SECRET", default="dflt")
    assert out == "dflt"  # fell through to default on KV failure
    # the secret VALUE must never appear in any log record
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert secret_value not in joined
    assert secret_value not in caplog.text
    # the NAME is allowed (and present) in the warning
    assert "SOME_SECRET" in joined


def test_kv_success_is_cached(monkeypatch):
    monkeypatch.setenv("KEY_VAULT_URL", "https://vault.vault.azure.net")
    monkeypatch.delenv("CACHED_SECRET", raising=False)
    calls = {"n": 0}

    class _Secret:
        value = "kv-value"

    class _Client:
        def __init__(self, *a, **k):
            pass

        def get_secret(self, name):
            calls["n"] += 1
            return _Secret()

    monkeypatch.setattr(config_secrets, "_build_secret_client", lambda url: _Client())

    assert config_secrets.get_secret("CACHED_SECRET") == "kv-value"
    assert config_secrets.get_secret("CACHED_SECRET") == "kv-value"
    assert calls["n"] == 1  # second call served from cache, not a fresh KV fetch
