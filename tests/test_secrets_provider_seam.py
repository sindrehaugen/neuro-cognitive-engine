"""Secrets-provider seam + production secret-handling guardrails (Batch 55 / VI.1 / R3).

Asserts:
  (a) the secrets seam resolves named secrets via the provider abstraction
      (an installed provider is consulted; env is the fallback); and
  (b) production config rejects the dev dotenv-persist path AND never sources
      ``NCE_MASTER_KEY`` through a non-environment provider.

The (a) tests run in-process against ``nce.config`` (importable under the dev
test env). The (b) tests boot a fresh interpreter with ``NCE_ENV=prod`` —
mirroring tests/test_config_prod_hardening.py — because the prod guardrails are
import-time / ``validate()``-time and must not mutate the in-process singleton.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import nce.config as config
from nce.config import (
    EnvSecretsProvider,
    SecretsProvider,
    get_secrets_provider,
    resolve_secret,
    set_secrets_provider,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# (a) The seam resolves via the provider abstraction
# ---------------------------------------------------------------------------


class _RecordingProvider(SecretsProvider):
    """A non-env provider used to prove resolution flows through the seam."""

    name = "recording"

    def __init__(self, values: dict[str, str]) -> None:
        self._values = values
        self.seen: list[str] = []

    def get_secret(self, name: str) -> str | None:
        self.seen.append(name)
        return self._values.get(name)


def test_default_provider_is_env_backed() -> None:
    assert isinstance(get_secrets_provider(), EnvSecretsProvider)
    assert get_secrets_provider().name == "env"


def test_seam_resolves_through_installed_provider(monkeypatch) -> None:
    monkeypatch.delenv("NCE_DEMO_SECRET", raising=False)
    provider = _RecordingProvider({"NCE_DEMO_SECRET": "from-manager"})
    try:
        set_secrets_provider(provider)
        # Resolution must consult the installed provider, not the environment.
        assert resolve_secret("NCE_DEMO_SECRET") == "from-manager"
        assert "NCE_DEMO_SECRET" in provider.seen
    finally:
        set_secrets_provider(None)
    # Restored to the env-backed default.
    assert isinstance(get_secrets_provider(), EnvSecretsProvider)


def test_seam_falls_back_to_default_when_provider_misses(monkeypatch) -> None:
    monkeypatch.delenv("NCE_ABSENT_SECRET", raising=False)
    provider = _RecordingProvider({})  # returns None for everything
    try:
        set_secrets_provider(provider)
        assert resolve_secret("NCE_ABSENT_SECRET", default="fallback") == "fallback"
    finally:
        set_secrets_provider(None)


def test_env_provider_reads_environment(monkeypatch) -> None:
    monkeypatch.setenv("NCE_ENV_BACKED_SECRET", "  value-with-space  ")
    assert resolve_secret("NCE_ENV_BACKED_SECRET") == "value-with-space"


def test_master_key_never_sourced_from_a_store(monkeypatch) -> None:
    """R3: even with a non-env provider installed, NCE_MASTER_KEY bypasses it."""
    monkeypatch.setenv("NCE_MASTER_KEY", "env-master-key-32-characters-min!!")
    provider = _RecordingProvider({"NCE_MASTER_KEY": "store-supplied-DANGER"})
    try:
        set_secrets_provider(provider)
        resolved = resolve_secret("NCE_MASTER_KEY")
        # Must be the environment value, and the provider must NOT have been
        # consulted for the master key at all.
        assert resolved == "env-master-key-32-characters-min!!"
        assert "NCE_MASTER_KEY" not in provider.seen
    finally:
        set_secrets_provider(None)
    assert "NCE_MASTER_KEY" in config._ENV_ONLY_SECRETS


# ---------------------------------------------------------------------------
# (b) Production config rejects dotenv-persist (fresh interpreter)
# ---------------------------------------------------------------------------


def _prod_env() -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "NCE_ENV": "prod",
            "NCE_LOAD_DOTENV": "false",
            "NCE_MASTER_KEY": "prod-master-key-32-characters-min!!",
            "NCE_API_KEY": "prod-api-key-for-ci-tests-only",
            "NCE_MCP_API_KEY": "prod-mcp-key-for-config-tests-only!!",
            "NCE_MCP_NAMESPACE_ID": "00000000-0000-4000-8000-000000000001",
            "NCE_ADMIN_API_KEY": "prod-admin-key-for-config-tests",
            "NCE_ADMIN_USERNAME": "admin",
            "NCE_ADMIN_PASSWORD": "$pbkdf2$sha256$600000$testsalt$notarealhashbutformatok",
            "PG_DSN": "postgresql://mcp_user:secret@db.internal.example:5432/memory_meta",
            "MONGO_URI": "mongodb://mongo.internal.example:27017",
            "REDIS_URL": "redis://redis.internal.example:6379/0",
            "MINIO_ACCESS_KEY": "minio-access-key",
            "MINIO_SECRET_KEY": "minio-secret-key-value",
            "NCE_JWT_SECRET": "jwt-secret-for-prod-config-tests!!",
            "NCE_A2A_JWT_AUDIENCE": "prod-jwt-audience-for-tests!!",
        }
    )
    # Strip anything that would trip an unrelated guard.
    for k in ("NCE_ADMIN_OVERRIDE", "NCE_BYPASS_WORM", "NCE_BYPASS_RLS"):
        env.pop(k, None)
    return env


def _run(code: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_prod_import_rejects_dotenv_persist() -> None:
    """Importing config under prod with dotenv-persist enabled must fail fast."""
    env = _prod_env()
    env["NCE_ALLOW_ADMIN_DOTENV_PERSIST"] = "true"
    result = _run("import nce.config  # noqa: F401", env)
    assert result.returncode != 0
    assert "NCE_ALLOW_ADMIN_DOTENV_PERSIST" in (result.stderr + result.stdout)


def test_prod_validate_rejects_dotenv_persist() -> None:
    """cfg.validate() must reject dotenv-persist in production.

    The default in prod is already false, so we force it true via env. Because
    the module-import guard also fires, validate() is the path under test here
    only when the import guard is satisfied; we assert the dedicated method
    rejects it directly to keep the contract explicit.
    """
    env = _prod_env()
    env["NCE_ALLOW_ADMIN_DOTENV_PERSIST"] = "true"
    code = "from nce.config import _Config; _Config.validate_secrets_provider()"
    result = _run(code, env)
    assert result.returncode != 0
    assert "NCE_ALLOW_ADMIN_DOTENV_PERSIST" in (result.stderr + result.stdout)


def test_prod_validate_passes_with_secret_manager_posture() -> None:
    """A correct prod posture (no dotenv-persist, env-only master key) validates."""
    env = _prod_env()
    env["NCE_ALLOW_ADMIN_DOTENV_PERSIST"] = "false"
    code = (
        "from nce.config import _Config, _ENV_ONLY_SECRETS; "
        "assert _Config.IS_PROD; "
        "_Config.validate(); "
        "assert 'NCE_MASTER_KEY' in _ENV_ONLY_SECRETS"
    )
    result = _run(code, env)
    assert result.returncode == 0, result.stderr + result.stdout


def test_prod_master_key_resolves_env_only_not_store() -> None:
    """In a prod interpreter, NCE_MASTER_KEY resolves env-only even with a provider."""
    env = _prod_env()
    env["NCE_ALLOW_ADMIN_DOTENV_PERSIST"] = "false"
    code = "\n".join(
        [
            "import nce.config as c",
            "",
            "class P(c.SecretsProvider):",
            "    name = 'store'",
            "    def get_secret(self, name):",
            "        return 'store-DANGER'",
            "",
            "c.set_secrets_provider(P())",
            "got = c.resolve_secret('NCE_MASTER_KEY')",
            "assert got == 'prod-master-key-32-characters-min!!', got",
        ]
    )
    result = _run(code, env)
    assert result.returncode == 0, result.stderr + result.stdout
