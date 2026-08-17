> **Status:** shipped · **Verified-against:** 7304330 (main) · **Last-audited:** 2026-08-17

# ADR-0006: Environment-Only NCE_MASTER_KEY (_ENV_ONLY_SECRETS)

## Status

Shipped

## Context

`NCE_MASTER_KEY` is the AES-256 master key used to wrap all signing keys stored in the `signing_keys` table. If it were ever read from a database table, a settings file, or a SettingsStore record, a database compromise would expose both the signing keys (ciphertext) and the master key (plaintext) in the same breach — defeating the entire key-wrapping scheme.

NCE has a general-purpose `SettingsStore` and a `SecretsProvider` abstraction that allows operators to plug in alternative secret backends (Vault, AWS Secrets Manager, etc.). These backends may eventually write to or cache in PostgreSQL. `NCE_MASTER_KEY` must never flow through any of these paths.

## Decision

`NCE_MASTER_KEY` is declared in `_ENV_ONLY_SECRETS` — a `frozenset` in `nce/config.py` — which marks it as permanently off-limits for any non-environment provider.

The `resolve_secret()` function checks membership in `_ENV_ONLY_SECRETS` before consulting the active `SecretsProvider`. If the name is in the set, it reads directly from `os.environ` regardless of which provider is configured:

```python
if name in _ENV_ONLY_SECRETS:
    raw = os.environ.get(name)
    value = raw.strip() if raw is not None else None
    return value or default
```

At startup, `NCEEngine.connect()` calls `cfg.validate()` which calls `_fail_unless_nce_master_key_ok()`. This raises `RuntimeError` with the message `"CRITICAL SECURITY FAILURE: NCE_MASTER_KEY is missing or too short."` if the key is absent or shorter than 32 characters, preventing the engine from serving requests without it.

The admin settings endpoint (`/api/admin/settings`) explicitly handles `NCE_MASTER_KEY` to report only whether the key is sourced from `env` and redact its value — the key value is never returned over the API.

**Source citations** (verified via `git show main:<path>`):
- `nce/config.py:186` — comment: "Invariant (R3): NCE_MASTER_KEY is secret-manager / environment only."
- `nce/config.py:226` — `_ENV_ONLY_SECRETS: frozenset[str] = frozenset({"NCE_MASTER_KEY"})`
- `nce/config.py:251-255` — `resolve_secret()`: env-only check before provider dispatch
- `nce/config.py:102` — `_fail_unless_nce_master_key_ok()` — raises `RuntimeError` with `"CRITICAL SECURITY FAILURE: NCE_MASTER_KEY is missing or too short."` if key is absent or < 32 bytes
- `nce/config.py:509` — `NCE_MASTER_KEY: str = os.getenv("NCE_MASTER_KEY", "")` — class field reads only from env
- `nce/config.py:894` — `_fail_unless_nce_master_key_ok(cls.NCE_MASTER_KEY)` — called in `validate()`
- `nce/admin_handlers/settings.py:51-53` — `if key == "NCE_MASTER_KEY": source = "env" if "NCE_MASTER_KEY" in os.environ` — admin endpoint redacts value

## Consequences

### Positive

- A database compromise (PostgreSQL or SettingsStore) cannot yield `NCE_MASTER_KEY` through any application code path; the environment must be separately compromised.
- The startup check ensures the key is provisioned correctly before any signing operation is attempted.
- The `_ENV_ONLY_SECRETS` mechanism is extensible: future secrets that must never transit a database can be added to the frozenset.

### Negative / Trade-offs

- Operators must inject `NCE_MASTER_KEY` via environment variable, container secret, or equivalent; it cannot be bootstrapped via the SettingsStore or admin API.
- Key rotation requires restarting the process with a new environment variable; there is no live rotation path for the master key itself (as distinct from signing key rotation, which does have a live path).
- If `NCE_MASTER_KEY` is absent or short, the entire engine refuses to start — which is the correct behaviour but means a misconfigured deployment fails completely rather than degrading gracefully.
