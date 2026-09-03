"""
NCE — centralised environment configuration.

All env-var reads for the entire package live here. No other module should
call os.getenv() directly. This makes the full configuration surface visible
in one place, easy to validate, and easy to override in tests.

Import pattern inside the package:
    from nce.config import cfg
"""

import logging
import os
import re
import unicodedata
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

from dotenv import load_dotenv

# Conditionally load .env — disabled in production by setting NCE_LOAD_DOTENV=false.
if os.environ.get("NCE_LOAD_DOTENV", "true").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}:
    load_dotenv()

# Hard cutover guard: raise early if any legacy TRIMCP_ keys are present so
# operators get an explicit, actionable error rather than silent misconfiguration.
_LEGACY_PREFIX = "TRIMCP_"
_NCE_PREFIX = "NCE_"
_legacy_keys = [k for k in os.environ if k.startswith(_LEGACY_PREFIX)]
if _legacy_keys:
    _mapping = "\n".join(
        f"  {k}  →  {_NCE_PREFIX}{k[len(_LEGACY_PREFIX) :]}" for k in sorted(_legacy_keys)
    )
    raise OSError(
        "Legacy TRIMCP_* environment variables detected. "
        "Rename them to NCE_* before starting the server:\n" + _mapping
    )

log = logging.getLogger("nce-config")

# (``*_FILE`` var, path) pairs already warned about for a UTF-8 BOM. secret_env
# is called per-operation (see require_master_key), so the warning is emitted
# once per offending file rather than on every read.
_BOM_WARNED: set[tuple[str, str]] = set()

# Byte-order marks that mean "this file is not UTF-8 at all". Ordered longest
# first: the UTF-32LE BOM (FF FE 00 00) starts with the UTF-16LE BOM (FF FE), so
# a shorter-first scan would misreport the encoding.
_NON_UTF8_BOMS: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xfe\x00\x00", "UTF-32LE"),
    (b"\x00\x00\xfe\xff", "UTF-32BE"),
    (b"\xff\xfe", "UTF-16LE"),
    (b"\xfe\xff", "UTF-16BE"),
)


def _strip_leading_boms(value: str, *, source: tuple[str, str] | str, is_file: bool) -> str:
    """Remove every leading U+FEFF from a resolved secret, warning once per source.

    A UTF-8 BOM decodes to U+FEFF under the "utf-8" codec and is KEPT (only
    "utf-8-sig" strips it). U+FEFF is not whitespace in Python, so no downstream
    .strip() removes it either: the secret would differ from its visible content
    by an invisible leading character, silently producing a WRONG key rather than
    an error. Windows PowerShell 5.1 writes such files by default, so this is a
    routine operator mistake.

    ALL leading marks are removed, not one: stripping a single BOM from a
    double-BOM'd file left a U+FEFF in the secret while logging that the BOM had
    been dealt with -- a wrong value and a misleading log line together.

    The warning is deduplicated per source because secret_env is on a hot path --
    ``require_master_key()`` re-reads on every envelope encrypt/decrypt, PII
    operation and settings read -- so warning unconditionally would emit
    thousands of identical lines. The strip itself always happens.
    """
    if not value.startswith("\ufeff"):
        return value
    if source not in _BOM_WARNED:
        _BOM_WARNED.add(source)
        if is_file:
            log.warning(
                "%s points at a secret file beginning with a UTF-8 BOM (%r); the "
                "BOM has been stripped. Rewrite the file without a BOM -- e.g. "
                "printf '%%s' \"$SECRET\" > file, or PowerShell 7 "
                "Set-Content -Encoding utf8NoBOM. WARNING: before this fix the "
                "BOM was part of the loaded secret, so anything encrypted under "
                "the old behaviour was wrapped with a DIFFERENT key.",
                source[0],
                source[1],
            )
        else:
            log.warning(
                "%s is set in the environment with a leading UTF-8 BOM; the BOM "
                "has been stripped. Check whatever wrote it (an env_file written "
                "by PowerShell is the usual cause). WARNING: before this fix the "
                "BOM was part of the loaded secret.",
                source,
            )
    return value.lstrip("\ufeff")


_MASTER_KEY_MIN_UTF8_BYTES: int = 32

# Match ``scheme://user:password@`` for common datastore / cache URI schemes (exception text scrubbing).
_RE_URI_CREDS = re.compile(
    r"(?P<prefix>(?:mongodb\+srv|mongodb|postgresql|postgres|redis|rediss)://)"
    r"(?P<user>[^:/?#\s]+):(?P<password>[^@/?#\s]+)@",
    re.IGNORECASE,
)
# Redis/Mongo ``scheme://:password@host`` (no username).
_RE_URI_PASS_ONLY = re.compile(
    r"(?P<prefix>(?:mongodb\+srv|mongodb|postgresql|postgres|redis|rediss)://)"
    r":(?P<password>[^@/?#\s]+)@",
    re.IGNORECASE,
)


def redact_dsn(dsn: str) -> str:
    """Mask the password component of a database/service URI.

    Handles the standard ``scheme://user:password@host/path`` format
    (including ``mongodb+srv``, ``redis://:password@host``).
    Returns the URI with the password replaced by ``***``.
    If parsing fails, returns ``<redacted>`` so the raw DSN is never
    accidentally surfaced in log or exception messages.
    """
    try:
        parsed = urlparse(dsn)
        if parsed.password:
            # Rebuild with masked password
            netloc = parsed.hostname or ""
            if parsed.port:
                netloc = f"{netloc}:{parsed.port}"
            if parsed.username:
                netloc = f"{parsed.username}:***@{netloc}"
            else:
                # Password-only auth (Redis format: redis://:pass@host)
                netloc = f":***@{netloc}"
            return urlunparse(parsed._replace(netloc=netloc))
        return dsn
    except Exception:
        return "<redacted>"


def redact_secrets_in_text(text: str) -> str:
    """Scrub ``user:password@`` fragments from arbitrary log/exception strings.

    Database clients sometimes echo the full DSN in connection errors. This
    regex pass catches embedded URIs that :func:`redact_dsn` would parse in
    isolation but appear inside longer messages.
    """
    if not text:
        return text
    scrubbed = _RE_URI_CREDS.sub(r"\g<prefix>\g<user>:***@", text)
    return _RE_URI_PASS_ONLY.sub(r"\g<prefix>:***@", scrubbed)


def _fail_unless_nce_master_key_ok(raw: str) -> None:
    """Raise RuntimeError if the master key is missing, too short, or malformed.

    "Malformed" means the key carries invisible Unicode control/format
    characters that survive ``.strip()`` — a file-encoding artifact, never real
    key material.  Such a key derives a DIFFERENT encryption key than its
    visible content implies, so it must fail closed here (import / startup)
    rather than later at decrypt time, where it is indistinguishable from data
    corruption.
    """
    v = (raw or "").strip()
    if not v or len(v.encode("utf-8")) < _MASTER_KEY_MIN_UTF8_BYTES:
        raise RuntimeError(
            "CRITICAL SECURITY FAILURE: NCE_MASTER_KEY is missing or too short. "
            f"A minimum of {_MASTER_KEY_MIN_UTF8_BYTES} UTF-8 bytes of random key material "
            "is required to import or start the server."
        )
    # The length floor above CANNOT catch an encoding artifact: a BOM-prefixed
    # 64-char key is 67 UTF-8 bytes and sails straight through it.  Reject the
    # invisible characters that are NOT whitespace — U+FEFF (UTF-8 BOM), U+200B,
    # U+200E, U+00AD, NUL — since those never occur in real key material.
    #
    # The ``not c.isspace()`` half is load-bearing, not decoration. ``.strip()``
    # removes only LEADING/TRAILING whitespace, so INTERNAL whitespace reaches
    # this scan, and LF / CR / TAB are all category Cc. Without the isspace()
    # exclusion this would reject ``openssl rand -base64 64``, whose output
    # base64-wraps at 64 columns and so legitimately contains a newline — and it
    # would reject it at *import*, refusing to start the server on a key that
    # ``main`` accepted. That is a worse failure than the BOM bug this guards.
    offenders = sorted(
        {
            f"U+{ord(c):04X}"
            for c in v
            if unicodedata.category(c) in ("Cc", "Cf") and not c.isspace()
        }
    )
    if offenders:
        raise RuntimeError(
            "CRITICAL SECURITY FAILURE: NCE_MASTER_KEY contains invisible control/"
            f"format character(s) {', '.join(offenders)}. This is almost always a "
            "file-encoding artifact — e.g. a UTF-8 BOM written by Windows "
            "PowerShell 5.1 (`Out-File -Encoding utf8`). A key carrying such a "
            "character derives a DIFFERENT encryption key than its visible content "
            "implies, so it is rejected at startup rather than failing later at "
            "decrypt time. Rewrite the secret without the BOM."
        )


# ---------------------------------------------------------------------------
# Env-var parsing helpers — used only by _Config below.
# ---------------------------------------------------------------------------


def _bool_env(name: str, default: bool = False) -> bool:
    """Parse a boolean environment variable.

    Accepts ``1``, ``true``, ``yes``, ``on`` (case-insensitive) as truthy.
    Returns *default* when the variable is unset.
    """
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def secret_env(name: str, default: str = "") -> str:
    """Resolve a secret env var with Docker/K8s ``*_FILE`` support.

    For a secret named ``NCE_X`` this checks ``NCE_X_FILE`` first: when set,
    the secret is read from that file path (a single trailing newline is
    stripped) so the secret never has to live in the process environment where
    it would be readable via ``/proc/<pid>/environ``.  When ``NCE_X_FILE`` is
    unset, the value falls back to ``os.getenv(NCE_X, default)`` — fully
    backward-compatible.

    Precedence: ``*_FILE`` always wins over the plain env var when both are set.

    Fail-closed: if ``*_FILE`` is set but the file is missing or unreadable, a
    :class:`RuntimeError` is raised that names the env var and the path but
    NEVER includes the (possibly partially read) secret contents.

    SECURITY: the returned value is the raw secret — callers must never log it.
    The file is read exactly once per call (at boot for ``cfg`` fields) and is
    never echoed back in any error or log message.
    """
    file_var = f"{name}_FILE"
    path = os.getenv(file_var)
    if path is not None and path.strip():
        path = path.strip()
        try:
            data = Path(path).read_bytes()
        except OSError as exc:
            # Surface the env var name + path + OS error class, but NEVER the
            # secret contents (the read failed, so there is nothing to leak —
            # we are careful not to include any partial buffer here).
            raise RuntimeError(
                f"{file_var} is set to {path!r} but the secret file could not be "
                f"read ({type(exc).__name__}: {exc.strerror}). Provide a readable "
                f"file containing the {name} secret."
            ) from None
        # A UTF-16/UTF-32 BOM means the file is not UTF-8 at all. This is the
        # SAME operator mistake as the UTF-8 BOM, and in fact the likelier one:
        # PowerShell 5.1 `>` redirection -- the most literal translation of the
        # documented `printf '%s' "$SECRET" > file` recipe -- writes UTF-16LE.
        # Decoding that as UTF-8 raises UnicodeDecodeError, a ValueError, which
        # escaped the OSError handler above and surfaced as a bare traceback
        # naming neither the env var nor the path. Fail closed, clearly, here.
        for _bom, _encoding_name in _NON_UTF8_BOMS:
            if data.startswith(_bom):
                raise RuntimeError(
                    f"{file_var} is set to {path!r} but that file begins with a "
                    f"{_encoding_name} byte-order mark, so it is {_encoding_name}-"
                    f"encoded, not UTF-8. Rewrite it as UTF-8 with no BOM -- e.g. "
                    f"on Windows [IO.File]::WriteAllText(path, secret). Note that "
                    f"PowerShell 5.1 `>` redirection produces {_encoding_name}."
                ) from None
        try:
            raw = data.decode("utf-8")
        except UnicodeDecodeError:
            # Never echo the offending bytes -- they are secret material.
            raise RuntimeError(
                f"{file_var} is set to {path!r} but that file is not valid UTF-8. "
                f"Provide the {name} secret as a UTF-8 text file with no BOM."
            ) from None
        raw = _strip_leading_boms(raw, source=(file_var, path), is_file=True)
        # Strip exactly one trailing newline (\n or \r\n) that editors / Docker
        # secret tooling commonly append; preserve any other whitespace.
        if raw.endswith("\r\n"):
            return raw[:-2]
        if raw.endswith("\n"):
            return raw[:-1]
        return raw
    # The env branch needs the same guard: only NCE_MASTER_KEY had a
    # backstop (the startup guard rejects U+FEFF), so a BOM in any of the
    # other ten secrets resolved here was silently part of the value.
    return _strip_leading_boms(os.getenv(name, default), source=name, is_file=False)


def _int_env(
    name: str, default: int, *, minimum: int | None = None, maximum: int | None = None
) -> int:
    """Parse an integer environment variable, optionally enforcing a minimum and/or maximum."""
    raw = os.getenv(name)
    value = default if raw is None or raw.strip() == "" else int(raw)
    if minimum is not None and value < minimum:
        raise RuntimeError(f"{name} must be >= {minimum}, got {value}")
    if maximum is not None and value > maximum:
        raise RuntimeError(f"{name} must be <= {maximum}, got {value}")
    return value


def live_env_str(name: str, *, default: str = "") -> str:
    """Read a string from the live process environment (honours runtime / pytest changes).

    Unlike ``cfg`` fields captured at import, this always reflects the current env.
    Auth scope checks use these helpers so ``monkeypatch.setenv`` / ``delenv`` behave correctly.
    """
    return (os.getenv(name, default) or "").strip()


def live_admin_override_enabled() -> bool:
    return live_env_str("NCE_ADMIN_OVERRIDE").lower() in {"1", "true", "yes", "on"}


def live_admin_api_key() -> str:
    """The admin API key, resolved LIVE and honouring the ``*_FILE`` mount.

    Delegates to :func:`secret_env` rather than :func:`live_env_str`. That is
    the whole point: ``secret_env`` checks ``NCE_ADMIN_API_KEY_FILE`` first and
    fails closed, and it re-reads on every call, so this stays as live as the
    plain accessor was (``monkeypatch.setenv``/``delenv`` still behave).

    Before this, the key was DECLARED with ``secret_env`` on the ``cfg`` class
    but READ here through ``os.getenv``, so a Docker/K8s file-mounted secret
    resolved to ``""`` on the live auth path -- fail-closed, but a silent
    availability trap: tenant tools stop answering the moment you mount the
    secret the way the docs tell you to.
    """
    return (secret_env("NCE_ADMIN_API_KEY", "") or "").strip()


def live_mcp_api_key() -> str:
    """The MCP API key, resolved LIVE and honouring the ``*_FILE`` mount.

    See :func:`live_admin_api_key` -- same defect, same fix, one shared
    resolution in :func:`secret_env` so the two cannot drift apart.
    """
    return (secret_env("NCE_MCP_API_KEY", "") or "").strip()


def live_mcp_namespace_id() -> str:
    return live_env_str("NCE_MCP_NAMESPACE_ID")


def _float_env(
    name: str, default: float, *, minimum: float | None = None, maximum: float | None = None
) -> float:
    """Parse a float environment variable, optionally enforcing a minimum and/or maximum."""
    raw = os.getenv(name)
    value = default if raw is None or raw.strip() == "" else float(raw)
    if minimum is not None and value < minimum:
        raise RuntimeError(f"{name} must be >= {minimum}, got {value}")
    if maximum is not None and value > maximum:
        raise RuntimeError(f"{name} must be <= {maximum}, got {value}")
    return value


# ---------------------------------------------------------------------------
# Secrets-provider seam (VI.1 / R3)
# ---------------------------------------------------------------------------
#
# Production should source secrets from a real manager (HashiCorp Vault, AWS
# Secrets Manager, Azure Key Vault) rather than committed compose/.env files.
# ``scripts/bootstrap-compose-secrets.py`` remains the *dev* path.
#
# This is a thin abstraction only: a ``SecretsProvider`` protocol plus an
# env-backed default provider. Real-manager providers are out of scope here
# (no Vault/AWS/Azure SDK dependency is added) — the seam exists so a concrete
# provider can be slotted in without touching every call site.
#
# Invariant (R3): ``NCE_MASTER_KEY`` is **secret-manager / environment only**.
# It must never be read from a database or SettingsStore. ``resolve_secret``
# therefore refuses to route the master key through any non-environment
# provider, regardless of which provider is configured.


class SecretsProvider:
    """Abstract seam for resolving named secrets at runtime.

    Implementations return the secret value for *name* or ``None`` when the
    secret is not managed by this provider (the caller then falls back to its
    configured default).
    """

    name: str = "abstract"

    def get_secret(self, name: str) -> str | None:  # pragma: no cover - interface
        raise NotImplementedError


class EnvSecretsProvider(SecretsProvider):
    """Default provider — reads secrets from the process environment.

    This is the dev path and the safe production fallback: secrets are injected
    into the environment by the orchestrator (which may itself be fed by a real
    secret manager) rather than read from a committed file at runtime.
    """

    name = "env"

    def get_secret(self, name: str) -> str | None:
        raw = os.environ.get(name)
        if raw is None:
            return None
        stripped = raw.strip()
        return stripped or None


# Names that must only ever come from the environment / secret manager and are
# never permitted to flow through a database- or file-backed provider (R3).
_ENV_ONLY_SECRETS: frozenset[str] = frozenset({"NCE_MASTER_KEY"})

_DEFAULT_SECRETS_PROVIDER = EnvSecretsProvider()
_SECRETS_PROVIDER: SecretsProvider = _DEFAULT_SECRETS_PROVIDER


def get_secrets_provider() -> SecretsProvider:
    """Return the active secrets provider (env-backed by default)."""
    return _SECRETS_PROVIDER


def set_secrets_provider(provider: SecretsProvider | None) -> None:
    """Install a secrets provider (pass ``None`` to restore the env default).

    The seam keeps provider wiring in one place; production deployments can
    install a Vault / AWS / Azure provider at startup without changing call
    sites. The env-only invariant in :func:`resolve_secret` still applies.
    """
    global _SECRETS_PROVIDER
    _SECRETS_PROVIDER = provider or _DEFAULT_SECRETS_PROVIDER


def resolve_secret(name: str, *, default: str | None = None) -> str | None:
    """Resolve *name* via the active secrets provider, then fall back to env.

    ``NCE_MASTER_KEY`` (and any other env-only secret) is always read straight
    from the environment, bypassing the provider entirely so it can never be
    sourced from a database / SettingsStore (R3).
    """
    if name in _ENV_ONLY_SECRETS:
        raw = os.environ.get(name)
        value = raw.strip() if raw is not None else None
        return value or default

    value = _SECRETS_PROVIDER.get_secret(name)
    if value is None:
        return default
    return value


class _EmbeddingConfig:
    """
    Embedding / pgvector dimension. Must stay aligned with ``memories.embedding`` and
    ``kg_nodes.embedding`` in ``schema.sql`` — changing this requires a DB migration.
    """

    VECTOR_DIM: int = int(os.getenv("EMBEDDING_VECTOR_DIM", "768"))


class _Config:
    EMBEDDING = _EmbeddingConfig

    # --- Environment mode ---
    # Set NCE_ENV=prod in production. Controls fail-fast validation and
    # whether dev-convenience defaults are accepted at startup.
    ENVIRONMENT: str = os.getenv("NCE_ENV", "dev").strip().lower()
    IS_PROD: bool = ENVIRONMENT in {"prod", "production"}
    IS_TEST: bool = ENVIRONMENT in {"test", "testing", "ci"}
    IS_DEV: bool = not IS_PROD and not IS_TEST

    # Legal entity name of the operator running this deployment. Appears in
    # generated Statement-of-Work text, including the Norwegian title-retention
    # clause. Deliberately has NO default and NO placeholder: SoW generation
    # fails closed when this is unset rather than naming a wrong or blank party
    # in a contract (D35).
    NCE_SUPPLIER_NAME: str = os.getenv("NCE_SUPPLIER_NAME", "").strip()

    # Dynamics 365 / Dataverse publisher prefix of the CRM organisation this
    # deployment reads from. Every D365 org has its own, so the sales read model
    # cannot carry one tenant's prefix in source. Resolved through the single seam
    # nce/vertical_modules/sales/source_adapters/d365.py::publisher_prefix(), which
    # validates it against ^[a-z][a-z0-9_]{0,31}$ and raises on first use when unset
    # or malformed -- the value is interpolated into SQL and OData, so it is never
    # sanitised, repaired or defaulted (D34a). Deployments with no D365 integration
    # leave it unset and are unaffected.
    NCE_D365_PUBLISHER_PREFIX: str = os.getenv("NCE_D365_PUBLISHER_PREFIX", "").strip().lower()

    # --- Database connections ---
    # ``DATABASE_URL`` is accepted as a 12-factor alias for ``PG_DSN`` (same precedence: explicit PG_DSN wins).
    # DB role password used by RLS session setup. Honours NCE_APP_PASSWORD_FILE
    # (Docker/K8s secret) via secret_env; env remains a backward-compatible fallback.
    NCE_APP_PASSWORD: str = secret_env("NCE_APP_PASSWORD", "nce_app_secret").strip()
    MONGO_URI: str = os.getenv("MONGO_URI", "mongodb://localhost:27017")
    # DSNs carry the DB password inline. ``secret_env`` lets the credential-
    # bearing DSN be supplied via a Docker/K8s secret file (PG_DSN_FILE,
    # DATABASE_URL_FILE) so it stays out of the environment / /proc/<pid>/environ.
    # ``redact_dsn`` masks the password whenever the DSN is logged.
    PG_DSN: str = (
        secret_env("PG_DSN")
        or secret_env("DATABASE_URL")
        or "postgresql://mcp_user:mcp_password@localhost:5432/memory_meta"
    )
    # Read/write split — fall back to PG_DSN when not explicitly configured
    DB_READ_URL: str = (
        secret_env("DB_READ_URL")
        or secret_env("PG_DSN")
        or secret_env("DATABASE_URL")
        or "postgresql://mcp_user:mcp_password@localhost:5432/memory_meta"
    )
    DB_WRITE_URL: str = (
        secret_env("DB_WRITE_URL")
        or secret_env("PG_DSN")
        or secret_env("DATABASE_URL")
        or "postgresql://mcp_user:mcp_password@localhost:5432/memory_meta"
    )
    PG_BOUNCER_URL: str = os.getenv("PG_BOUNCER_URL", "")
    # Least-privilege worker DSN (R4 / VI.4).  Background maintenance workers
    # (garbage collector, re-embedding worker) connect with this DSN when set,
    # so they authenticate as a *distinct* principal (e.g. ``nce_gc``) rather
    # than reusing the application role (``nce_app``).  Handled like other DSNs:
    # environment-only, never logged in cleartext, never returned by endpoints.
    # When UNSET it falls back to ``PG_DSN`` so existing deployments are
    # unchanged (backward-compatible) — segregation is opt-in via provisioning
    # a dedicated role and pointing this at it.
    NCE_GC_DSN: str = (
        secret_env("NCE_GC_DSN")
        or secret_env("PG_DSN")
        or secret_env("DATABASE_URL")
        or "postgresql://mcp_user:mcp_password@localhost:5432/memory_meta"
    )
    # REDIS_URL carries credentials inline when Redis AUTH is enabled
    # (``redis://:<password>@host:port/db`` — set ``requirepass`` on the server;
    # see docker-compose.yml, which sources the password from ${REDIS_PASSWORD}).
    # The password must ALWAYS arrive via this URL / the environment — never
    # hardcode a literal here. ``redact_dsn`` masks it in logs and exceptions.
    #
    # Residual risk (out of scope for this batch): even with AUTH, Redis is a
    # SINGLE shared store across all tenants. AUTH stops unauthenticated access
    # and lock keys are now namespace-scoped (see nce/cron_lock.py) so one tenant
    # cannot disrupt another's locks, but a fully authenticated client still
    # shares one keyspace. Per-tenant Redis isolation (separate instances or ACL
    # users per namespace) is a future hardening step, not implemented here. To
    # further reduce exposure, enable Redis TLS (``rediss://``) — see the
    # docker-compose.yml follow-up note.
    # REDIS_URL embeds the Redis AUTH password inline; secret_env supports
    # REDIS_URL_FILE so the credential-bearing URL can come from a Docker/K8s
    # secret file rather than the environment. redact_dsn masks it in logs.
    REDIS_URL: str = secret_env("REDIS_URL", "redis://localhost:6379/0")

    # --- Redis ---
    REDIS_TTL: int = int(os.getenv("REDIS_TTL", "3600"))
    REDIS_MAX_CONNECTIONS: int = int(os.getenv("REDIS_MAX_CONNECTIONS", "20"))

    # --- PostgreSQL connection pool ---
    PG_MIN_POOL: int = int(os.getenv("PG_MIN_POOL", "1"))
    PG_MAX_POOL: int = int(os.getenv("PG_MAX_POOL", "10"))
    NCE_PARTITION_LOOKAHEAD_MONTHS: int = _int_env("NCE_PARTITION_LOOKAHEAD_MONTHS", 3, minimum=1)

    # --- Garbage collector ---
    GC_INTERVAL_SECONDS: int = int(os.getenv("GC_INTERVAL_SECONDS", "3600"))
    GC_ORPHAN_AGE_SECONDS: int = int(os.getenv("GC_ORPHAN_AGE_SECONDS", "86400"))
    GC_PAGE_SIZE: int = int(os.getenv("GC_PAGE_SIZE", "500"))
    GC_MAX_CONNECT_ATTEMPTS: int = int(os.getenv("GC_MAX_CONNECT_ATTEMPTS", "5"))
    GC_CONNECT_BASE_DELAY: float = float(os.getenv("GC_CONNECT_BASE_DELAY", "2.0"))
    GC_ALERT_THRESHOLD: int = int(os.getenv("GC_ALERT_THRESHOLD", "100"))

    # --- Attachment / extraction size limits ---
    # Maximum blob size accepted by extract_bytes and store_media.
    # Oversized payloads are rejected before any I/O to prevent RQ worker OOM.
    NCE_MAX_ATTACHMENT_BYTES: int = int(
        os.getenv("NCE_MAX_ATTACHMENT_BYTES", str(20 * 1024 * 1024))
    )  # 20 MB default

    NCE_MAX_OCR_PAGES: int = _int_env("NCE_MAX_OCR_PAGES", 10, minimum=1)

    # --- Provable Forgetting (Part II.4) — envelope encryption of raw content ---
    # When enabled, store_memory encrypts the raw payload that fans out to
    # MongoDB ``episodes.raw_data`` under a fresh per-memory DEK (wrapped under
    # NCE_MASTER_KEY; see nce/envelope.py).  Read paths transparently decrypt and
    # ALWAYS remain back-compatible with legacy rows whose ``wrapped_dek IS NULL``
    # (those carry plaintext raw_data).  Default OFF so rollout is controlled —
    # flip on only once NCE_MASTER_KEY is provisioned in the target environment.
    NCE_ENVELOPE_ENCRYPTION_ENABLED: bool = _bool_env("NCE_ENVELOPE_ENCRYPTION_ENABLED", False)

    # --- MCP Sizing Limits ---
    NCE_MAX_ARGUMENTS_JSON_SIZE: int = _int_env(
        "NCE_MAX_ARGUMENTS_JSON_SIZE", 1_000_000, minimum=1024
    )
    NCE_MAX_METADATA_KEYS: int = _int_env("NCE_MAX_METADATA_KEYS", 512, minimum=1)
    NCE_MAX_METADATA_KEY_LEN: int = _int_env("NCE_MAX_METADATA_KEY_LEN", 256, minimum=1)
    NCE_MAX_METADATA_STRING_VALUE_LEN: int = _int_env(
        "NCE_MAX_METADATA_STRING_VALUE_LEN", 4096, minimum=1
    )
    NCE_MAX_METADATA_LIST_ITEMS: int = _int_env("NCE_MAX_METADATA_LIST_ITEMS", 256, minimum=1)
    NCE_MAX_CONCURRENT_TOOLS: int = _int_env("NCE_MAX_CONCURRENT_TOOLS", 16, minimum=1)

    # --- Temporal queries ---
    # Maximum lookback window for ``as_of`` temporal queries.  Prevents
    # unbounded historical searches that trigger full-table scans on
    # ``event_log``.  Set to 0 to disable the boundary (not recommended).
    NCE_MAX_TEMPORAL_LOOKBACK_DAYS: int = int(os.getenv("NCE_MAX_TEMPORAL_LOOKBACK_DAYS", "90"))

    # --- Code indexing limits ---
    # Max raw bytes allowed through index_code_file() before the file is skipped.
    NCE_MAX_CODE_INDEX_BYTES: int = _int_env(
        "NCE_MAX_CODE_INDEX_BYTES", 2 * 1024 * 1024, minimum=1024
    )
    # Max AST/line chunks extracted per file — prevents embedding queue flood.
    NCE_MAX_CODE_CHUNKS_PER_FILE: int = _int_env("NCE_MAX_CODE_CHUNKS_PER_FILE", 500, minimum=1)

    # --- Embeddings ---
    EMBEDDING_MAX_WORKERS: int = _int_env("EMBEDDING_MAX_WORKERS", 1, minimum=1)
    EMBED_BATCH_CHUNK: int = _int_env("EMBED_BATCH_CHUNK", 64, minimum=1)
    # Model identity — configurable so operators can swap the embedding model without a code change.
    NCE_EMBEDDING_MODEL_ID: str = os.getenv(
        "NCE_EMBEDDING_MODEL_ID", "jinaai/jina-embeddings-v2-base-code"
    )
    # Pin model revision for supply-chain safety; empty string means "latest" (not recommended in prod).
    NCE_EMBEDDING_MODEL_REVISION: str = os.getenv("NCE_EMBEDDING_MODEL_REVISION", "")
    # trust_remote_code=True is required for some Jina models; must be explicit in production.
    NCE_EMBEDDING_TRUST_REMOTE_CODE: bool = os.getenv(
        "NCE_EMBEDDING_TRUST_REMOTE_CODE", "false"
    ).strip().lower() in {"1", "true", "yes", "on"}
    # Input guard — reject batches that exceed these limits rather than silently truncating.
    NCE_EMBED_MAX_BATCH_TEXTS: int = _int_env("NCE_EMBED_MAX_BATCH_TEXTS", 512, minimum=1)
    NCE_EMBED_MAX_TEXT_CHARS: int = _int_env("NCE_EMBED_MAX_TEXT_CHARS", 32000, minimum=1)
    # Enterprise §8 — hardware backend / OpenVINO NPU (see nce.embeddings, openvino_npu_export).
    NCE_BACKEND: str = (os.getenv("NCE_BACKEND") or "").strip().lower()
    NCE_OPENVINO_MODEL_DIR: str = (os.getenv("NCE_OPENVINO_MODEL_DIR") or "").strip()
    NCE_OPENVINO_SEQ_LEN: int = int(os.getenv("NCE_OPENVINO_SEQ_LEN", "512"))

    # --- Contradictions / NLI ---
    NLI_MODEL_ID: str = os.getenv("NLI_MODEL_ID", "cross-encoder/nli-deberta-v3-small")
    # Idle TTL (seconds) after which the NLI model is evicted from memory.
    # Set to 0 to disable eviction (legacy always-loaded behaviour).
    NCE_NLI_IDLE_TTL_S: int = _int_env("NCE_NLI_IDLE_TTL_S", 900, minimum=0)
    NCE_CONTRADICTION_SIMILARITY_THRESHOLD: float = _float_env(
        "NCE_CONTRADICTION_SIMILARITY_THRESHOLD", 0.85, minimum=0.0
    )
    NCE_CONTRADICTION_MAX_CANDIDATES: int = _int_env(
        "NCE_CONTRADICTION_MAX_CANDIDATES", 3, minimum=1
    )
    NCE_CONTRADICTION_NLI_THRESHOLD: float = _float_env(
        "NCE_CONTRADICTION_NLI_THRESHOLD", 0.8, minimum=0.0
    )
    NCE_CONTRADICTION_LLM_MIN_CONFIDENCE: float = _float_env(
        "NCE_CONTRADICTION_LLM_MIN_CONFIDENCE", 0.6, minimum=0.0
    )

    # --- D2 / D7 — Local cognitive bundle (OpenAI-compatible HTTP on port 11435) ---
    # When NCE_COGNITIVE_BASE_URL is set (e.g. http://cognitive:11435), embeddings
    # route to POST {base}/v1/embeddings unless NCE_BACKEND selects an in-process backend.
    NCE_COGNITIVE_BASE_URL: str = (os.getenv("NCE_COGNITIVE_BASE_URL") or "").strip().rstrip("/")
    NCE_COGNITIVE_EMBEDDING_MODEL: str = (os.getenv("NCE_COGNITIVE_EMBEDDING_MODEL") or "").strip()
    # Fallback model used when the primary cognitive backend returns 429 or times out.
    NCE_COGNITIVE_FALLBACK_MODEL: str = os.getenv(
        "NCE_COGNITIVE_FALLBACK_MODEL", "text-embedding-3-small"
    ).strip()
    NCE_COGNITIVE_API_KEY: str = (os.getenv("NCE_COGNITIVE_API_KEY") or "").strip()
    # Declarative default LLM provider label for operators / future LLMProvider wiring [D2].
    NCE_LLM_PROVIDER: str = (os.getenv("NCE_LLM_PROVIDER") or "local-cognitive-model").strip()

    # --- A2A server ---
    # Base URL at which the A2A server is reachable (used in agent card discovery).
    NCE_A2A_URL: str = os.getenv("NCE_A2A_URL", "http://localhost:8004").rstrip("/")

    # --- Document bridges (Phase 2 / §10.3) — OAuth tokens from env or future bridge_tokens PG ---
    GRAPH_BRIDGE_TOKEN: str = os.getenv("GRAPH_BRIDGE_TOKEN", "")
    GDRIVE_BRIDGE_TOKEN: str = os.getenv("GDRIVE_BRIDGE_TOKEN", "")
    DROPBOX_BRIDGE_TOKEN: str = os.getenv("DROPBOX_BRIDGE_TOKEN", "")
    # Bridge worker token-resolution timeout (seconds). Prevents RQ workers
    # from hanging on slow DB/OAuth exchanges.
    BRIDGE_RESOLVE_TIMEOUT_S: float = _float_env("BRIDGE_RESOLVE_TIMEOUT_S", 10.0, minimum=0.1)

    # --- Bridge OAuth / webhooks (§10.6–10.7) ---
    BRIDGE_WEBHOOK_BASE_URL: str = os.getenv("BRIDGE_WEBHOOK_BASE_URL", "").rstrip("/")
    # When true, webhook rate limits use the first X-Forwarded-For hop (trusted proxy only).
    NCE_WEBHOOK_TRUST_PROXY: bool = _bool_env("NCE_WEBHOOK_TRUST_PROXY", False)
    WEBHOOK_MAX_BODY_BYTES: int = max(1, int(os.getenv("WEBHOOK_MAX_BODY_BYTES", "1048576")))
    WEBHOOK_RATE_LIMIT: int = max(1, int(os.getenv("WEBHOOK_RATE_LIMIT", "120")))
    WEBHOOK_RATE_PERIOD_SECONDS: int = max(1, int(os.getenv("WEBHOOK_RATE_PERIOD_SECONDS", "60")))
    WEBHOOK_DEDUP_TTL_SECONDS: int = max(60, int(os.getenv("WEBHOOK_DEDUP_TTL_SECONDS", "86400")))
    WEBHOOK_DEDUP_FAIL_OPEN: bool = _bool_env("WEBHOOK_DEDUP_FAIL_OPEN", False)
    DROPBOX_APP_SECRET: str = os.getenv("DROPBOX_APP_SECRET", "")
    GRAPH_CLIENT_STATE: str = os.getenv("GRAPH_CLIENT_STATE", "")
    DRIVE_CHANNEL_TOKEN: str = os.getenv("DRIVE_CHANNEL_TOKEN", "")
    AZURE_CLIENT_ID: str = os.getenv("AZURE_CLIENT_ID", "")
    AZURE_CLIENT_SECRET: str = os.getenv("AZURE_CLIENT_SECRET", "")
    AZURE_TENANT_ID: str = os.getenv("AZURE_TENANT_ID", "common")
    BRIDGE_OAUTH_REDIRECT_URI: str = os.getenv(
        "BRIDGE_OAUTH_REDIRECT_URI", "http://127.0.0.1:8765/bridge/oauth/callback"
    )
    GDRIVE_OAUTH_CLIENT_ID: str = os.getenv("GDRIVE_OAUTH_CLIENT_ID", "")
    GDRIVE_OAUTH_CLIENT_SECRET: str = os.getenv("GDRIVE_OAUTH_CLIENT_SECRET", "")
    DROPBOX_OAUTH_CLIENT_ID: str = os.getenv("DROPBOX_OAUTH_CLIENT_ID", "")
    BRIDGE_RENEWAL_LOOKAHEAD_HOURS: int = int(os.getenv("BRIDGE_RENEWAL_LOOKAHEAD_HOURS", "12"))
    BRIDGE_CRON_INTERVAL_MINUTES: int = int(os.getenv("BRIDGE_CRON_INTERVAL_MINUTES", "45"))

    # --- MinIO Object Storage ---
    MINIO_ENDPOINT: str = os.getenv("MINIO_ENDPOINT", "localhost:9000")
    MINIO_ACCESS_KEY: str = os.getenv("MINIO_ACCESS_KEY", "")
    MINIO_SECRET_KEY: str = os.getenv("MINIO_SECRET_KEY", "")
    MINIO_SECURE: bool = _bool_env("MINIO_SECURE", False)
    # Set to false to skip MinIO credential validation in validate().
    # Useful for test environments or deployments that do not use MinIO.
    NCE_MINIO_REQUIRED: bool = _bool_env("NCE_MINIO_REQUIRED", True)

    # --- Phase 0.1 / 0.2: Auth + Signing ---
    # NCE_API_KEY        — HMAC-SHA256 key for HTTP admin API authentication.
    #                         Required in production.  Server logs a warning if absent.
    # NCE_ADMIN_API_KEY  — Bearer token checked by require_scope("admin") in A2A/MCP.
    #                         Required in production.
    # NCE_ADMIN_USERNAME / NCE_ADMIN_PASSWORD — HTTP Basic credentials
    #                         required for non-API admin UI routes.
    # NCE_MASTER_KEY     — AES-256 master key for encrypting signing keys at rest.
    #                         Importing this module or calling validate() raises RuntimeError
    #                         if missing or under 32 UTF-8 bytes [D 0.2].
    # NCE_ADMIN_OVERRIDE — Dev-only bypass of admin scope checks. Must never be
    #                         true in production.
    # Secrets below honour the ``*_FILE`` convention (Docker/K8s secrets) via
    # ``secret_env``: e.g. setting ``NCE_API_KEY_FILE=/run/secrets/nce_api_key``
    # loads the key from that file instead of the environment, closing the
    # ``/proc/<pid>/environ`` exposure vector. ``*_FILE`` takes precedence; the
    # plain env var remains a backward-compatible fallback.
    NCE_API_KEY: str = secret_env("NCE_API_KEY", "")
    # Shared secret for MCP stdio tenant tools (namespace-scoped). Required in production.
    NCE_MCP_API_KEY: str = secret_env("NCE_MCP_API_KEY", "")
    # When set, tenant MCP tools are bound to this namespace UUID (required in prod with MCP key).
    NCE_MCP_NAMESPACE_ID: str = os.getenv("NCE_MCP_NAMESPACE_ID", "")
    NCE_ADMIN_API_KEY: str = secret_env("NCE_ADMIN_API_KEY", "")
    NCE_ADMIN_OVERRIDE: bool = _bool_env("NCE_ADMIN_OVERRIDE", False)
    # Startup WORM / RLS probe bypass (dev/support only — rejected when IS_PROD).
    NCE_BYPASS_WORM: bool = _bool_env("NCE_BYPASS_WORM", False)
    NCE_BYPASS_RLS: bool = _bool_env("NCE_BYPASS_RLS", False)
    # Admin UI may persist connector/datastore edits to a local .env file (dev only).
    NCE_ALLOW_ADMIN_DOTENV_PERSIST: bool = _bool_env(
        "NCE_ALLOW_ADMIN_DOTENV_PERSIST",
        ENVIRONMENT not in {"prod", "production"},
    )
    NCE_ADMIN_USERNAME: str = os.getenv("NCE_ADMIN_USERNAME", "")
    NCE_ADMIN_PASSWORD: str = os.getenv("NCE_ADMIN_PASSWORD", "")
    NCE_MASTER_KEY: str = secret_env("NCE_MASTER_KEY", "")
    # Selects the secrets-provider backend (VI.1). "env" (default) reads secrets
    # from the process environment — the orchestrator may feed those from a real
    # manager (Vault / AWS Secrets Manager / Azure Key Vault). See resolve_secret;
    # NCE_MASTER_KEY is always environment-only regardless of this setting (R3).
    NCE_SECRETS_PROVIDER: str = (os.getenv("NCE_SECRETS_PROVIDER") or "env").strip().lower()
    # When true, HTTP admin ``HMACAuthMiddleware`` uses ``NonceStore(cfg.REDIS_URL)``
    # for replay protection across multiple admin replicas (see nce.auth).
    NCE_DISTRIBUTED_REPLAY: bool = _bool_env("NCE_DISTRIBUTED_REPLAY", False)

    # --- PBKDF2 iteration counts (signing + admin password hashing) ---
    # NCE_PBKDF2_ITERATIONS    — v2 blob compat path (minimum 100K, NIST minimum).
    #                               Used by signing.py to decrypt legacy v2 blobs.
    # NCE_PBKDF2_ITERATIONS_V4 — v4 new-write path (minimum 600K, OWASP 2026).
    #                               auth.py clamps admin password hashing to max(600K, this).
    NCE_PBKDF2_ITERATIONS: int = _int_env("NCE_PBKDF2_ITERATIONS", 100_000, minimum=100_000)
    NCE_PBKDF2_ITERATIONS_V4: int = _int_env("NCE_PBKDF2_ITERATIONS_V4", 600_000, minimum=600_000)

    # --- Phase 0.2: JWT Bridge ---
    # NCE_JWT_SECRET     — HS256 shared secret for JWT validation (dev / testing).
    #                         Either this or NCE_JWT_PUBLIC_KEY must be set when
    #                         JWTAuthMiddleware is active.
    # NCE_JWT_PUBLIC_KEY — RS256/ES256 PEM-encoded public key for production JWT
    #                         validation.  May be a raw PEM string or a file URI
    #                         (file:///path/to/pub.pem). Takes precedence over the
    #                         secret when both are set.
    # NCE_JWT_ALGORITHM  — One of HS256 | RS256 | ES256 (default: HS256).
    # NCE_JWT_ISSUER     — Expected ``iss`` claim.  Omit to skip issuer check.
    # NCE_JWT_AUDIENCE   — Expected ``aud`` claim.  Omit to skip audience check.
    # NCE_JWT_PREFIX     — Route prefix protected by JWTAuthMiddleware.
    #                         Default: "/api/v1/" (agent-facing endpoints).
    NCE_JWT_SECRET: str = os.getenv("NCE_JWT_SECRET", "")
    NCE_JWT_PUBLIC_KEY: str = os.getenv("NCE_JWT_PUBLIC_KEY", "")
    NCE_JWT_ALGORITHM: str = (os.getenv("NCE_JWT_ALGORITHM") or "HS256").upper().strip()
    NCE_JWT_ISSUER: str = os.getenv("NCE_JWT_ISSUER", "")
    NCE_JWT_AUDIENCE: str = os.getenv("NCE_JWT_AUDIENCE", "")
    NCE_JWT_PREFIX: str = os.getenv("NCE_JWT_PREFIX", "/api/v1/")
    NCE_JWT_KEY_DIR: str = os.getenv("NCE_JWT_KEY_DIR", str(Path.cwd()))
    NCE_JWT_LEEWAY_SECONDS: int = int(os.getenv("NCE_JWT_LEEWAY_SECONDS", "30"))

    # --- Phase 3.1: Per-service JWT audience overrides ---
    # Each service (A2A, admin, etc.) can require its own ``aud`` claim value
    # to prevent token replay across system boundaries.  When set, tokens
    # intended for one service are rejected by another.
    #
    # If unset, the default is ``f"nce_{service}"`` per server.
    NCE_A2A_JWT_AUDIENCE: str = os.getenv(
        "NCE_A2A_JWT_AUDIENCE",
        "nce_a2a"
        if os.getenv("NCE_ENV", "dev").strip().lower() not in {"prod", "production"}
        else "",
    ).strip()

    # --- Phase 3.1: A2A mTLS — client certificate enforcement ---
    # When enabled, the A2A server requires a valid client TLS certificate
    # from connecting agents.  Certificates are validated by SAN or SHA-256
    # fingerprint against an explicit allowlist.
    #
    # NCE_A2A_MTLS_ENABLED           — Master switch (default: false)
    # NCE_A2A_MTLS_ALLOWED_SANS      — Comma-separated list of allowed
    #                                     Subject Alternative Name values
    #                                     (case-insensitive DNS / URI match).
    # NCE_A2A_MTLS_ALLOWED_FINGERPRINTS — Comma-separated list of allowed
    #                                     SHA-256 certificate fingerprints
    #                                     (colon-separated hex, case-insensitive).
    # NCE_A2A_MTLS_STRICT            — When true, reject any connection that
    #                                     does not present a valid client cert
    #                                     (default: true).
    # NCE_A2A_MTLS_TRUSTED_PROXY_HOP — Number of reverse-proxy hops to trust
    #                                     for X-Forwarded-Client-Cert header.
    #                                     0 = only direct TLS (uvicorn SSL).
    #                                     1 = one reverse proxy (Caddy / nginx).
    NCE_A2A_MTLS_ENABLED: bool = _bool_env("NCE_A2A_MTLS_ENABLED", False)
    NCE_A2A_MTLS_ALLOWED_SANS: list[str] = [
        s.strip().lower()
        for s in os.getenv("NCE_A2A_MTLS_ALLOWED_SANS", "").split(",")
        if s.strip()
    ]
    NCE_A2A_MTLS_ALLOWED_FINGERPRINTS: list[str] = [
        s.strip().lower()
        for s in os.getenv("NCE_A2A_MTLS_ALLOWED_FINGERPRINTS", "").split(",")
        if s.strip()
    ]
    NCE_A2A_MTLS_STRICT: bool = _bool_env("NCE_A2A_MTLS_STRICT", True)
    NCE_A2A_MTLS_TRUSTED_PROXY_HOP: int = int(os.getenv("NCE_A2A_MTLS_TRUSTED_PROXY_HOP", "1"))

    # A2A tasks/send HTTP rate limits
    NCE_A2A_HTTP_RATE_LIMIT: int = _int_env("NCE_A2A_HTTP_RATE_LIMIT", 60, minimum=1)
    NCE_A2A_HTTP_RATE_PERIOD: int = _int_env("NCE_A2A_HTTP_RATE_PERIOD", 60, minimum=1)

    # --- Tool governance last-known-good cache (audit Domain 1, CWE-636/1188) ---
    # The process-local governance cache (nce/tool_governance.py) serves the
    # last-known-good ``nce:tools:disabled`` snapshot when Redis is unreachable.
    # STALE_OK : age below which the snapshot is served without any Redis call.
    # STALE_HARD: age above which an INITIALIZED snapshot is no longer trusted —
    #             the cache fails closed (GovernanceUnavailable) rather than
    #             silently un-revoke an admin-disabled tool/skill.
    NCE_TOOL_GOVERNANCE_STALE_OK_SEC: int = _int_env(
        "NCE_TOOL_GOVERNANCE_STALE_OK_SEC", 30, minimum=1
    )
    NCE_TOOL_GOVERNANCE_STALE_HARD_SEC: int = _int_env(
        "NCE_TOOL_GOVERNANCE_STALE_HARD_SEC", 300, minimum=1
    )

    # --- Admin server mTLS (B6) ---
    # Mirror of the A2A mTLS block but scoped to the admin surface.
    # All vars default to disabled/empty so existing deployments are unaffected.
    NCE_ADMIN_MTLS_ENABLED: bool = _bool_env("NCE_ADMIN_MTLS_ENABLED", False)
    NCE_ADMIN_MTLS_STRICT: bool = _bool_env("NCE_ADMIN_MTLS_STRICT", True)
    NCE_ADMIN_MTLS_TRUSTED_PROXY_HOP: int = int(os.getenv("NCE_ADMIN_MTLS_TRUSTED_PROXY_HOP", "1"))
    NCE_ADMIN_MTLS_ALLOWED_SANS: list[str] = [
        s.strip().lower()
        for s in os.getenv("NCE_ADMIN_MTLS_ALLOWED_SANS", "").split(",")
        if s.strip()
    ]
    NCE_ADMIN_MTLS_ALLOWED_FINGERPRINTS: list[str] = [
        s.strip().lower()
        for s in os.getenv("NCE_ADMIN_MTLS_ALLOWED_FINGERPRINTS", "").split(",")
        if s.strip()
    ]

    # --- General mTLS (CC) ---
    NCE_MTLS_STRICT: bool = _bool_env("NCE_MTLS_STRICT", True)
    NCE_MTLS_CERT_PATH: str = os.getenv("NCE_MTLS_CERT_PATH", "").strip()
    NCE_MTLS_KEY_PATH: str = os.getenv("NCE_MTLS_KEY_PATH", "").strip()
    NCE_MTLS_CA_PATH: str = os.getenv("NCE_MTLS_CA_PATH", "").strip()
    # Zero-trust transport boot guard. In production the admin/a2a servers refuse
    # to start when their mTLS middleware is disabled — unless an operator sets
    # this flag to explicitly accept the weakened posture (an acknowledgement is
    # then logged CRITICAL and recorded as a WORM audit event). Default False so
    # an un-hardened prod deployment fails fast rather than silently shipping HTTP.
    NCE_MTLS_ACKNOWLEDGE_DISABLED: bool = _bool_env("NCE_MTLS_ACKNOWLEDGE_DISABLED", False)

    # Per-IP HTTP rate limits on admin_server (/api/* and sensitive POST paths).
    NCE_ADMIN_HTTP_RATE_LIMIT: int = _int_env("NCE_ADMIN_HTTP_RATE_LIMIT", 120, minimum=1)
    NCE_ADMIN_HTTP_RATE_PERIOD: int = _int_env("NCE_ADMIN_HTTP_RATE_PERIOD", 60, minimum=1)
    NCE_ADMIN_HTTP_SENSITIVE_RATE_LIMIT: int = _int_env(
        "NCE_ADMIN_HTTP_SENSITIVE_RATE_LIMIT", 30, minimum=1
    )
    NCE_ADMIN_HTTP_SENSITIVE_RATE_PERIOD: int = _int_env(
        "NCE_ADMIN_HTTP_SENSITIVE_RATE_PERIOD", 60, minimum=1
    )

    # --- Phase 0.1: HMAC replay-protection clock skew ---
    # Maximum allowed drift between client timestamp and server time (seconds).
    # Requests with timestamps outside this window are rejected as replays.
    # Batch 116: shrunk from 300 s (±5 min) to 90 s (±90 s) to tighten the replay window.
    NCE_CLOCK_SKEW_TOLERANCE_S: int = int(os.getenv("NCE_CLOCK_SKEW_TOLERANCE_S", "90"))
    # When true, a per-request nonce is required on every HMAC-protected request
    # whenever the NonceStore (Redis) is reachable.  If Redis is unreachable in
    # production the request is rejected (fail-closed); in dev/test it is allowed
    # with a warning log.  Set to false only to disable nonce enforcement cluster-wide
    # (e.g. for controlled rollouts where clients have not yet been updated).
    NCE_HMAC_NONCE_REQUIRED: bool = _bool_env("NCE_HMAC_NONCE_REQUIRED", True)

    # --- Phase 3.2: Per-namespace / per-agent quotas ---
    # When false, no quota queries run on the tool hot path.
    NCE_QUOTAS_ENABLED: bool = _bool_env("NCE_QUOTAS_ENABLED", True)
    # Rough chars-per-token for pre-flight estimates (embedding / LLM analog).
    NCE_QUOTA_TOKEN_ESTIMATE_DIVISOR: int = int(os.getenv("NCE_QUOTA_TOKEN_ESTIMATE_DIVISOR", "4"))
    # Hot-path quota increments via Redis (avoids row-level UPDATE serialization).
    NCE_QUOTA_REDIS_COUNTERS: bool = _bool_env("NCE_QUOTA_REDIS_COUNTERS", True)
    NCE_QUOTA_REDIS_FLUSH_INTERVAL_S: float = float(
        os.getenv("NCE_QUOTA_REDIS_FLUSH_INTERVAL_S", "60")
    )

    # --- Consolidation ---
    CONSOLIDATION_DECAY_SOURCES: bool = _bool_env("CONSOLIDATION_DECAY_SOURCES", False)
    CONSOLIDATION_CRON_INTERVAL_MINUTES: int = int(
        os.getenv("CONSOLIDATION_CRON_INTERVAL_MINUTES", "360")
    )
    CONSOLIDATION_HALF_LIFE_DAYS: float = float(os.getenv("CONSOLIDATION_HALF_LIFE_DAYS", "30.0"))
    # Derivation depth guard (Batch 107 / Muscles A1).
    # Memories at or above this depth are excluded from clustering input so
    # hallucination compounding across consolidation generations is capped.
    NCE_MAX_DERIVATION_DEPTH: int = _int_env("NCE_MAX_DERIVATION_DEPTH", 2, minimum=1)
    # Per-generation confidence decay factor γ: derived KG-edge confidence is
    # multiplied by γ^depth when the consolidated memory is inserted.
    NCE_DERIVATION_CONFIDENCE_DECAY: float = _float_env(
        "NCE_DERIVATION_CONFIDENCE_DECAY", 0.85, minimum=0.0, maximum=1.0
    )

    # --- Cron startup jitter ---
    # Maximum random startup delay (seconds) applied before the first cron
    # execution cycle.  Prevents thundering-herd database CPU spikes when
    # multiple NCE instances boot simultaneously (e.g. rolling deployment,
    # docker-compose scale).  The jitter is a one-time shift — subsequent
    # interval fires inherit the offset evenly.
    # Set to 0 to disable.
    CRON_STARTUP_JITTER_MAX_SECONDS: float = float(
        os.getenv("CRON_STARTUP_JITTER_MAX_SECONDS", "60.0")
    )
    OUTBOX_RELAY_INTERVAL_SECONDS: int = max(
        1, int(os.getenv("OUTBOX_RELAY_INTERVAL_SECONDS", "5"))
    )

    # --- Re-embedding worker (Phase 2.1) ---
    REEMBED_BATCH_SIZE: int = max(1, int(os.getenv("REEMBED_BATCH_SIZE", "32")))
    REEMBED_BATCHES_PER_MINUTE: int = max(1, int(os.getenv("REEMBED_BATCHES_PER_MINUTE", "20")))
    REEMBED_MAX_ROWS_PER_RUN: int = max(0, int(os.getenv("REEMBED_MAX_ROWS_PER_RUN", "0")))
    REEMBED_INCLUDE_KG_NODES: bool = _bool_env("REEMBED_INCLUDE_KG_NODES", False)
    REEMBED_MAX_TEXT_CHARS: int = max(256, int(os.getenv("REEMBED_MAX_TEXT_CHARS", "4096")))
    REEMBED_CRON_INTERVAL_MINUTES: int = max(
        1, int(os.getenv("REEMBED_CRON_INTERVAL_MINUTES", "60"))
    )
    # VRAM back-pressure guard (Batch 104 / Domain 3).
    # High-watermark: fraction of total CUDA memory at which the guard pauses
    # embedding (0.85 = 85 %).  Clamped to [0.1, 0.99].
    NCE_REEMBED_VRAM_HIGH_WATERMARK: float = _float_env(
        "NCE_REEMBED_VRAM_HIGH_WATERMARK", 0.85, minimum=0.1, maximum=0.99
    )
    # How many sleep cycles to wait before giving up and raising VRAMPressureError.
    NCE_REEMBED_VRAM_MAX_PRESSURE_WAITS: int = _int_env(
        "NCE_REEMBED_VRAM_MAX_PRESSURE_WAITS", 12, minimum=0
    )

    # --- Re-embedding commit quality gate (Batch 108 / Muscles B5) ---
    # Number of randomly sampled memories used to evaluate neighbor overlap
    # before a migration commit is allowed to proceed.
    NCE_REEMBED_GATE_SAMPLE: int = _int_env("NCE_REEMBED_GATE_SAMPLE", 200, minimum=1)
    # Minimum required Jaccard neighbor-overlap fraction (0 ≤ x ≤ 1).
    # Commits with a score below this threshold are refused unless force=true.
    NCE_REEMBED_GATE_MIN_OVERLAP: float = _float_env(
        "NCE_REEMBED_GATE_MIN_OVERLAP", 0.6, minimum=0.0, maximum=1.0
    )
    # Number of nearest neighbours retrieved per sample point for the overlap check.
    NCE_REEMBED_GATE_K: int = _int_env("NCE_REEMBED_GATE_K", 10, minimum=1)

    # --- Orchestrator artifact staging ---
    NCE_ARTIFACT_STAGING_DIR: str = os.getenv("NCE_ARTIFACT_STAGING_DIR", "")

    # --- Phase 1.2: LLM Provider API keys (BYO — no shared platform key [D3]) ---
    # All keys default to empty string; factory logs a warning if the needed
    # key is absent.  Use ref:env/<VAR> in namespace metadata to override
    # per-namespace without touching global config.
    #
    # NCE_ANTHROPIC_API_KEY     — Anthropic Claude (claude-opus-4-6, etc.)
    # NCE_OPENAI_API_KEY        — OpenAI (gpt-5, gpt-4.5-turbo)
    # NCE_AZURE_OPENAI_API_KEY  — Azure OpenAI api-key header
    # NCE_AZURE_OPENAI_ENDPOINT — Azure resource endpoint (required for azure_openai provider)
    # NCE_AZURE_OPENAI_DEPLOYMENT — Default deployment name
    # NCE_GEMINI_API_KEY        — Google AI Studio / Gemini API key
    # NCE_DEEPSEEK_API_KEY      — DeepSeek (cost-sensitive deployments)
    # NCE_MOONSHOT_API_KEY      — Moonshot / Kimi (large-context clusters)
    # NCE_OPENAI_COMPAT_BASE_URL — Base URL for openai_compatible provider
    # NCE_OPENAI_COMPAT_API_KEY  — API key for openai_compatible provider
    # NCE_OPENAI_COMPAT_MODEL    — Default model for openai_compatible provider
    NCE_ANTHROPIC_API_KEY: str = os.getenv("NCE_ANTHROPIC_API_KEY", "")
    NCE_OPENAI_API_KEY: str = os.getenv("NCE_OPENAI_API_KEY", "")
    NCE_AZURE_OPENAI_API_KEY: str = os.getenv("NCE_AZURE_OPENAI_API_KEY", "")
    NCE_AZURE_OPENAI_ENDPOINT: str = os.getenv("NCE_AZURE_OPENAI_ENDPOINT", "")
    NCE_AZURE_OPENAI_DEPLOYMENT: str = os.getenv("NCE_AZURE_OPENAI_DEPLOYMENT", "")
    NCE_GEMINI_API_KEY: str = os.getenv("NCE_GEMINI_API_KEY", "")
    NCE_DEEPSEEK_API_KEY: str = os.getenv("NCE_DEEPSEEK_API_KEY", "")
    NCE_MOONSHOT_API_KEY: str = os.getenv("NCE_MOONSHOT_API_KEY", "")
    NCE_OPENAI_COMPAT_BASE_URL: str = os.getenv("NCE_OPENAI_COMPAT_BASE_URL", "")
    NCE_OPENAI_COMPAT_API_KEY: str = os.getenv("NCE_OPENAI_COMPAT_API_KEY", "")
    NCE_OPENAI_COMPAT_MODEL: str = os.getenv("NCE_OPENAI_COMPAT_MODEL", "")

    # --- Phase 2: Observability (Prometheus + OTel) ---
    NCE_PROMETHEUS_PORT: int = int(os.getenv("NCE_PROMETHEUS_PORT", "8000"))
    NCE_OTEL_EXPORTER_OTLP_ENDPOINT: str = os.getenv(
        "NCE_OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318"
    )
    NCE_OTEL_SERVICE_NAME: str = os.getenv("NCE_OTEL_SERVICE_NAME", "nce-python")
    NCE_OBSERVABILITY_ENABLED: bool = _bool_env("NCE_OBSERVABILITY_ENABLED", True)

    # --- Phase 3: Background Task Poison Pill / Dead Letter Queue ---
    # Maximum times a background task (RQ worker) is retried before the payload
    # is routed to the dead_letter_queue table and removed from the active
    # processing loop.  Set to 0 to disable DLQ routing (all failures retry
    # indefinitely — not recommended for production).
    TASK_MAX_RETRIES: int = int(os.getenv("TASK_MAX_RETRIES", "5"))

    # --- Migration MCP tools disable switch ---
    # When true, start_migration / commit_migration / abort_migration are excluded
    # from the MCP tool list and dispatch table.  Defaults to true in production;
    # set NCE_DISABLE_MIGRATION_MCP=false explicitly to enable migration tools.
    NCE_DISABLE_MIGRATION_MCP: bool = _bool_env(
        "NCE_DISABLE_MIGRATION_MCP",
        IS_PROD,
    )
    # Redis TTL (seconds) for attempt-count keys.  After this window, a task
    # that has been failing for longer than TTL will restart its attempt
    # counter from 1.  Default 86 400 s = 24 h.
    TASK_DLQ_REDIS_TTL: int = int(os.getenv("TASK_DLQ_REDIS_TTL", "86400"))

    # --- Batch 121: DLQ auto-triage (fingerprint + circuit-breaker) ---
    # Max number of auto-replay attempts for transient failures before the
    # entry is left for manual handling with an alert.
    NCE_DLQ_AUTO_REPLAY_MAX: int = _int_env("NCE_DLQ_AUTO_REPLAY_MAX", 3, minimum=0)
    # Number of same-fingerprint DLQ entries required to open the circuit for
    # a task_name (stops further enqueues with a fast-reject + alert).
    NCE_DLQ_CIRCUIT_THRESHOLD: int = _int_env("NCE_DLQ_CIRCUIT_THRESHOLD", 3, minimum=1)
    # Redis TTL (seconds) for the circuit-open flag.  Default 3600 s = 1 h.
    NCE_DLQ_CIRCUIT_TTL_S: int = _int_env("NCE_DLQ_CIRCUIT_TTL_S", 3600, minimum=1)

    # --- Spreading Activation Telemetry Defaults (BATCH-P3-003) ---
    NCE_TELEMETRY_SPIKE_THRESHOLD: float = _float_env(
        "NCE_TELEMETRY_SPIKE_THRESHOLD", 8.0, minimum=0.0
    )
    NCE_TELEMETRY_SPIKE_THETA: float = _float_env("NCE_TELEMETRY_SPIKE_THETA", 0.25, minimum=0.0)
    NCE_TELEMETRY_SPIKE_CHARGE: float = _float_env("NCE_TELEMETRY_SPIKE_CHARGE", 2.0, minimum=0.0)

    # --- Active Learning Gamification (BATCH-P3-005) ---
    NCE_ACTIVE_LEARNING_CONFIRM_XP: int = _int_env("NCE_ACTIVE_LEARNING_CONFIRM_XP", 10, minimum=0)
    NCE_ACTIVE_LEARNING_REJECT_XP: int = _int_env("NCE_ACTIVE_LEARNING_REJECT_XP", 5, minimum=0)

    # --- Actor Trust Scores (Batch 113 / Muscles B2) ---
    # NCE_TRUST_QUARANTINE_BYPASS — trust score at or above which a mid-confidence
    #   assertion bypasses quarantine (operator-confirmed trust, default 0.8).
    # NCE_TRUST_DEFAULT — fallback trust score for actors not yet in actor_trust
    #   (Laplace prior, default 0.65).
    NCE_TRUST_QUARANTINE_BYPASS: float = _float_env(
        "NCE_TRUST_QUARANTINE_BYPASS", 0.8, minimum=0.0, maximum=1.0
    )
    NCE_TRUST_DEFAULT: float = _float_env("NCE_TRUST_DEFAULT", 0.65, minimum=1e-6, maximum=1.0)

    # --- NetBox connection (shared across all NetBox vertical modules) ---
    NCE_NETBOX_URL: str = os.getenv("NCE_NETBOX_URL", "").rstrip("/")
    NCE_NETBOX_TOKEN: str = os.getenv("NCE_NETBOX_TOKEN", "")

    # --- NetBox Discovery Defaults (BATCH-P3-NB-005) ---
    NCE_NETBOX_DEFAULT_INTERFACE_TYPE: str = os.getenv(
        "NCE_NETBOX_DEFAULT_INTERFACE_TYPE", "1000base-t"
    ).strip()

    # --- Dynamics 365 / Dataverse vertical module ---
    NCE_D365_ENABLED: bool = os.getenv("NCE_D365_ENABLED", "false").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    NCE_D365_ORG_URL: str = os.getenv("NCE_D365_ORG_URL", "").rstrip("/")
    NCE_D365_WEBHOOK_SECRET: str = os.getenv("NCE_D365_WEBHOOK_SECRET", "")
    NCE_D365_SYNC_INTERVAL_MINUTES: int = _int_env("NCE_D365_SYNC_INTERVAL_MINUTES", 60, minimum=5)
    NCE_D365_SYNC_PAGE_SIZE: int = _int_env("NCE_D365_SYNC_PAGE_SIZE", 500, minimum=10)
    NCE_D365_HIGH_PRIORITY_SALIENCE_BOOST: float = _float_env(
        "NCE_D365_HIGH_PRIORITY_SALIENCE_BOOST", 2.0, minimum=1.0
    )
    NCE_D365_API_VERSION: str = os.getenv("NCE_D365_API_VERSION", "9.2").strip()
    NCE_D365_EMPATHIC_URGENCY_KEYWORDS: str = os.getenv(
        "NCE_D365_EMPATHIC_URGENCY_KEYWORDS",
        "urgent,critical,asap,escalate,breach,sla,overdue,immediate,p1,p0",
    )
    NCE_D365_EMPATHIC_FRUSTRATION_KEYWORDS: str = os.getenv(
        "NCE_D365_EMPATHIC_FRUSTRATION_KEYWORDS",
        "disappointed,unacceptable,failed,unresolved,weeks,months,terrible,worst,again,still broken",
    )
    # Incremental sync: when true, run_full_sync pulls only Dataverse records modified
    # since the last successful sync (modifiedon > d365_integrations.last_sync_at), so
    # data is retained in NCE (graph + memories) instead of re-pulled in full each tick.
    # Opt-in (default off); does not yet detect Dataverse deletions (change-tracking TODO).
    NCE_D365_INCREMENTAL_ENABLED: bool = os.getenv(
        "NCE_D365_INCREMENTAL_ENABLED", "false"
    ).strip().lower() in ("1", "true", "yes")
    # Change-tracking delta sync: when true, the vertical detects Dataverse deletions
    # (@removed) via odata.track-changes deltaLinks and HARD-deletes the derived
    # kg_edges/kg_nodes tagged with the removed record's GUID (d365_source_id).
    # Opt-in (default off) — retirement runs ONLY when this is enabled.
    NCE_D365_CHANGE_TRACKING_ENABLED: bool = os.getenv(
        "NCE_D365_CHANGE_TRACKING_ENABLED", "false"
    ).strip().lower() in ("1", "true", "yes")

    # --- Echo suppression (Batch 119) ---
    # TTL (seconds) for the Redis echo set that prevents self-caused webhooks
    # from being re-ingested as fresh semantic signal.  Producers (Batch 129)
    # write ``SET nce:echo:{system}:{entity_id}`` with this TTL; the webhook
    # consumer checks it and, on hit, skips semantic re-ingestion while still
    # applying the deterministic KG upsert for state convergence.
    NCE_ECHO_TTL_S: int = _int_env("NCE_ECHO_TTL_S", 600, minimum=1)

    # --- D365 ↔ NetBox cross-reference bridge ---
    # Requires NCE_NETBOX_URL + NCE_NETBOX_TOKEN to be set.
    NCE_D365_NETBOX_BRIDGE_ENABLED: bool = os.getenv(
        "NCE_D365_NETBOX_BRIDGE_ENABLED", "false"
    ).strip().lower() in ("1", "true", "yes")
    # How often (minutes) to re-run the bridge sync.
    NCE_D365_NETBOX_BRIDGE_INTERVAL_MINUTES: int = _int_env(
        "NCE_D365_NETBOX_BRIDGE_INTERVAL_MINUTES", 120, minimum=10
    )
    # Minimum SequenceMatcher ratio to accept a fuzzy name match (0.0–1.0).
    NCE_D365_NETBOX_FUZZY_THRESHOLD: float = _float_env(
        "NCE_D365_NETBOX_FUZZY_THRESHOLD", 0.82, minimum=0.5
    )
    # NetBox custom field name that stores the D365 account GUID on a tenant record.
    # When set, exact-CF matches take priority over all fuzzy matching.
    NCE_D365_NETBOX_TENANT_CF_NAME: str = os.getenv(
        "NCE_D365_NETBOX_TENANT_CF_NAME", "d365_account_id"
    ).strip()

    # --- Economy vertical module — outbound EHF/PEPPOL (Module 8 Wave 13) ---
    # Safety interlock, OFF by default (roadmap 08 External blocker: "PEPPOL
    # prod in/out pending provider (Tickstar/Pagero) — sandbox-only today").
    # With this false, nce.vertical_modules.economy.peppol.do_generate_ehf
    # returns the built EHF document without ever resolving
    # NCE_ECONOMY_PEPPOL_API_KEY / NCE_ECONOMY_PEPPOL_BASE_URL (env-only,
    # resolved via resolve_secret — never registered here, same as System
    # Design's Lucid credentials) or constructing a PeppolTransport.
    NCE_ECONOMY_PEPPOL_ENABLED: bool = _bool_env("NCE_ECONOMY_PEPPOL_ENABLED", False)
    # Sandbox vs prod PEPPOL network selector (informational — echoed by
    # do_generate_ehf). The transport itself remains a 🔴 stub regardless of
    # this value until a PEPPOL access-point provider ships (see peppol.py).
    NCE_ECONOMY_PEPPOL_MODE: str = (
        (os.getenv("NCE_ECONOMY_PEPPOL_MODE") or "sandbox").strip().lower()
    )

    # --- Chain Verification ---
    NCE_CHAIN_VERIFY_INTERVAL_MINUTES: int = _int_env(
        "NCE_CHAIN_VERIFY_INTERVAL_MINUTES", 120, minimum=5
    )
    NCE_CHAIN_VERIFY_STARTUP_DEPTH: int = _int_env("NCE_CHAIN_VERIFY_STARTUP_DEPTH", 500, minimum=0)

    # --- Product EOL Watcher (Module 2 Wave 12) ---
    # How often to scan namespaces for EOL/EOS products and write replaced_by edges.
    # Default: 360 minutes (6 h).  Minimum: 5 minutes.
    NCE_PRODUCT_EOL_WATCHER_INTERVAL_MINUTES: int = _int_env(
        "NCE_PRODUCT_EOL_WATCHER_INTERVAL_MINUTES", 360, minimum=5
    )

    # --- Agreements Coverage Watcher (Module 3 Wave 5) ---
    # How often to run the coverage matrix for opted-in namespaces and dispatch
    # expiry/leakage alerts.  Default: 1440 minutes (daily).  Minimum: 5 minutes.
    NCE_AGREEMENTS_COVERAGE_WATCHER_INTERVAL_MINUTES: int = _int_env(
        "NCE_AGREEMENTS_COVERAGE_WATCHER_INTERVAL_MINUTES", 1440, minimum=5
    )

    # --- Inventory Stock Watcher (Module 11 Wave 6b) ---
    # How often to scan all namespaces' inventory_items for low-stock and
    # dead-stock flags.  Default: 1440 minutes (daily) — an operational
    # digest, not a freshness scan.  Minimum: 5 minutes.
    NCE_INVENTORY_STOCK_WATCHER_INTERVAL_MINUTES: int = _int_env(
        "NCE_INVENTORY_STOCK_WATCHER_INTERVAL_MINUTES", 1440, minimum=5
    )
    # Days of inventory_transactions inactivity before a positive-quantity
    # item is flagged dead stock.  Default: 180 days.
    NCE_INVENTORY_DEAD_STOCK_DAYS: int = _int_env("NCE_INVENTORY_DEAD_STOCK_DAYS", 180, minimum=1)
    # Safety interlock — the tick always runs and always logs, but dispatches
    # no human-facing alert unless this is explicitly turned on.  Deliberately
    # OFF by default (Batch 128 rollout-hazard shape): turning on unsolicited
    # alerting for every existing namespace on deploy day is not a decision to
    # make silently.
    NCE_INVENTORY_LOW_STOCK_ALERT_ENABLED: bool = _bool_env(
        "NCE_INVENTORY_LOW_STOCK_ALERT_ENABLED", False
    )

    # --- C6 Shared Pricing Service (Wave 12) ---
    # Maximum age (seconds) before a price row is considered stale.
    # A stale cost is flagged in the resolve_price return, never silently used.
    # Default: 86 400 s = 24 h.  Set minimum=1 to reject zero-second windows.
    NCE_PRICING_MAX_AGE: int = _int_env("NCE_PRICING_MAX_AGE", 86_400, minimum=1)

    # --- Procurement Module 1 Wave 8 — per-supplier recalibration ---
    # Rolling window size (number of recorded match decisions per supplier) that
    # gates a recalibration recompute.  Recompute is skipped while a supplier has
    # fewer than this many decisions in v3_cognitive_ledger.  Default: 100.
    NCE_PROCUREMENT_RECALIBRATE_AFTER_N: int = _int_env(
        "NCE_PROCUREMENT_RECALIBRATE_AFTER_N", 100, minimum=1
    )

    # --- Vendors Module 4 Wave 2 — scorecard min sample ---
    # Minimum number of outcome events required before a vendor scorecard produces
    # a non-neutral composite score. Below this, insufficient_data is True.
    # Default: 5.
    NCE_VENDORS_SCORECARD_MIN_SAMPLE: int = _int_env(
        "NCE_VENDORS_SCORECARD_MIN_SAMPLE", 5, minimum=1
    )

    # --- Agreements Module 3 Wave 1 — extraction-core thresholds ---
    # Autogreen threshold: values >= this are auto-approved (except money/legal). Default: 90.
    NCE_AGREEMENTS_OCR_AUTOGREEN_THRESHOLD: int = _int_env(
        "NCE_AGREEMENTS_OCR_AUTOGREEN_THRESHOLD", 90, minimum=1, maximum=100
    )
    # Review threshold: values >= this are yellow review queue, below are manual red. Default: 70.
    NCE_AGREEMENTS_OCR_REVIEW_THRESHOLD: int = _int_env(
        "NCE_AGREEMENTS_OCR_REVIEW_THRESHOLD", 70, minimum=1, maximum=100
    )

    # --- System Design Module 6 Wave 3 — similarity-recall propose ---
    # Number of past DESIGN/PROJECT memories recalled per propose call.
    # Minimum 1 to prevent empty recall.  Default: 5.
    NCE_SYSTEM_DESIGN_RECALL_TOP_K: int = _int_env("NCE_SYSTEM_DESIGN_RECALL_TOP_K", 5, minimum=1)
    # Outcome-weighting discounts recall scores by change-order / support-ticket
    # pressure and margin data from the Project/Support ledger.  DORMANT until
    # those engines backfill the ledger — default False (pure-similarity ranking).
    NCE_SYSTEM_DESIGN_OUTCOME_WEIGHTING_ENABLED: bool = _bool_env(
        "NCE_SYSTEM_DESIGN_OUTCOME_WEIGHTING_ENABLED", False
    )

    # --- Procurement Module 1 Wave 11 — PO submission autonomy ceiling ---
    # Maximum PO value (inclusive) that the C2 governor may approve without a
    # human-confirm override.  Default: 0 (safest — everything requires
    # human-confirm until autonomy is explicitly granted via env).
    # The value is compared against the ``po_value`` kwarg passed to
    # ``do_submit_po``; if ``po_value > ceiling`` the governor returns
    # ``pending_approval`` regardless of confidence.
    NCE_PROCUREMENT_AUTONOMY_PO_CEILING: float = _float_env(
        "NCE_PROCUREMENT_AUTONOMY_PO_CEILING", 0.0
    )

    # --- External Tamper Anchor (Batch 124) ---
    # Object-locked (WORM) MinIO bucket that receives per-namespace Merkle chain heads.
    # The bucket MUST be created with versioning + object-lock enabled at creation time.
    # An attacker who compromises the DB (disable trigger, rewrite history, re-stitch)
    # cannot silently erase these anchors — they live in an independent storage root.
    NCE_ANCHOR_BUCKET: str = os.getenv("NCE_ANCHOR_BUCKET", "nce-tamper-anchors")
    # How often (minutes) the anchor tick fires.  Default: 60 (hourly).
    NCE_ANCHOR_INTERVAL_MINUTES: int = _int_env("NCE_ANCHOR_INTERVAL_MINUTES", 60, minimum=1)
    # COMPLIANCE-mode object-lock retention period (days) applied to every anchor blob.
    # COMPLIANCE mode is used (not GOVERNANCE) because GOVERNANCE allows admin bypass.
    # Minimum 1 day; default 365 days (1 year).  Cannot be reduced for already-locked objects.
    NCE_ANCHOR_RETENTION_DAYS: int = _int_env("NCE_ANCHOR_RETENTION_DAYS", 365, minimum=1)

    # --- Event Retention (Batch 125) ---
    # Months of event_log history to keep.  Partitions older than this many months
    # are archived to MinIO and dropped — ONLY when the partition's seq range is
    # fully anchored (Batch 124 invariant).  Default 24 (2 years).
    NCE_EVENT_RETENTION_MONTHS: int = _int_env("NCE_EVENT_RETENTION_MONTHS", 24, minimum=1)
    # Days after which a resolved contradiction is purged (non-null resolution).
    # Purges are tenant-scoped (RLS-enforced via scoped_pg_session).  Default 180.
    NCE_CONTRADICTION_RETENTION_DAYS: int = _int_env(
        "NCE_CONTRADICTION_RETENTION_DAYS", 180, minimum=1
    )
    # Days after which a low-confidence (<0.15) kg_edge is reaped — UNLESS its
    # change_origin = 'sync' (deterministic ground truth; Batch 106).  Default 90.
    NCE_EDGE_PRUNE_AGE_DAYS: int = _int_env("NCE_EDGE_PRUNE_AGE_DAYS", 90, minimum=1)
    # How often (minutes) the retention tick fires.  Default 1440 (daily).
    NCE_RETENTION_INTERVAL_MINUTES: int = _int_env(
        "NCE_RETENTION_INTERVAL_MINUTES", 1440, minimum=1
    )

    # --- Diagnostic Log Digestion Engine ---
    NCE_DIAG_ENABLED: bool = os.getenv("NCE_DIAG_ENABLED", "false").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    NCE_DIAG_LANDING_BUCKET: str = os.getenv("NCE_DIAG_LANDING_BUCKET", "nce-diag-landing")
    NCE_DIAG_LANDING_TTL_DAYS: int = _int_env("NCE_DIAG_LANDING_TTL_DAYS", 7, minimum=1)
    NCE_DIAG_MAX_BUNDLE_MB: int = _int_env("NCE_DIAG_MAX_BUNDLE_MB", 700, minimum=1)
    NCE_DIAG_MAX_ANOMALIES: int = _int_env("NCE_DIAG_MAX_ANOMALIES", 50, minimum=1)
    NCE_DIAG_JOB_TIMEOUT_MIN: int = _int_env("NCE_DIAG_JOB_TIMEOUT_MIN", 45, minimum=1)
    NCE_DIAG_CRASH_STORM_THRESHOLD: int = _int_env("NCE_DIAG_CRASH_STORM_THRESHOLD", 10, minimum=1)
    NCE_DIAG_CRASH_STORM_WINDOW_SEC: int = _int_env(
        "NCE_DIAG_CRASH_STORM_WINDOW_SEC", 300, minimum=1
    )
    NCE_DIAG_TMPDIR: str = os.getenv("NCE_DIAG_TMPDIR", "")

    @classmethod
    def validate_minio_credentials(cls) -> None:
        """Validate that MinIO credentials are set via environment.

        No hardcoded defaults are permitted — FIX-013 requires explicit env vars.
        """
        if not cls.MINIO_ACCESS_KEY:
            raise ValueError(
                "MINIO_ACCESS_KEY must be set via the MINIO_ACCESS_KEY environment variable. "
                "No default is permitted in production."
            )
        if not cls.MINIO_SECRET_KEY:
            raise ValueError(
                "MINIO_SECRET_KEY must be set via the MINIO_SECRET_KEY environment variable. "
                "No default is permitted in production."
            )

    @classmethod
    def validate_datastore_config(cls) -> None:
        """In production, reject missing or default-value datastore connection strings."""
        if not cls.IS_PROD:
            return

        missing = [k for k in ("MONGO_URI", "PG_DSN", "REDIS_URL") if not getattr(cls, k)]
        if missing:
            raise RuntimeError(
                "CRITICAL CONFIGURATION FAILURE: Missing required production datastore config: "
                + ", ".join(missing)
            )

        _insecure_defaults = {
            "PG_DSN": "postgresql://mcp_user:mcp_password@localhost:5432/memory_meta",
            "MONGO_URI": "mongodb://localhost:27017",
            "REDIS_URL": "redis://localhost:6379/0",
        }
        for key, default in _insecure_defaults.items():
            if getattr(cls, key) == default:
                raise RuntimeError(
                    f"CRITICAL CONFIGURATION FAILURE: {key} uses development default in production."
                )

    @classmethod
    def validate_jwt_config(cls) -> None:
        """Validate JWT configuration. Fail in production if no key is set."""
        if not cls.NCE_JWT_SECRET and not cls.NCE_JWT_PUBLIC_KEY:
            if cls.IS_PROD:
                raise RuntimeError(
                    "CRITICAL CONFIGURATION FAILURE: JWT validation requires "
                    "NCE_JWT_PUBLIC_KEY or NCE_JWT_SECRET in production."
                )
            log.warning(
                "SECURITY WARNING: Neither NCE_JWT_SECRET nor NCE_JWT_PUBLIC_KEY is set. "
                "A2A sharing will be disabled."
            )

        if cls.IS_PROD and cls.NCE_JWT_ALGORITHM == "HS256":
            log.warning(
                "SECURITY WARNING: HS256 JWT is configured in production. "
                "Prefer RS256/ES256 with NCE_JWT_PUBLIC_KEY."
            )

        if cls.IS_PROD and not cls.NCE_A2A_JWT_AUDIENCE:
            raise RuntimeError(
                "CRITICAL CONFIGURATION FAILURE: NCE_A2A_JWT_AUDIENCE is required "
                "in production to prevent token replay across system boundaries."
            )

    @classmethod
    def validate(cls) -> None:
        """
        Validates environment configuration.
        Strictly halts (raises RuntimeError) if P0 security requirements are missing.
        """
        # P0: Master Key (Required for signing/encryption)
        _fail_unless_nce_master_key_ok(cls.NCE_MASTER_KEY)

        # P0: Datastore connections — reject dev defaults in production
        cls.validate_datastore_config()

        # P0: MinIO Credentials (FIX-013) — skipped when NCE_MINIO_REQUIRED=false
        if cls.NCE_MINIO_REQUIRED:
            cls.validate_minio_credentials()

        # P0: Database connections present in all environments
        missing_conns = [k for k in ("MONGO_URI", "PG_DSN", "REDIS_URL") if not getattr(cls, k)]
        if missing_conns:
            raise RuntimeError(
                f"CRITICAL CONFIGURATION FAILURE: Missing required connection strings: {', '.join(missing_conns)}"
            )

        # P1: HMAC API key
        if not cls.NCE_API_KEY:
            if cls.IS_PROD:
                raise RuntimeError(
                    "CRITICAL CONFIGURATION FAILURE: NCE_API_KEY is required in production."
                )
            log.warning(
                "SECURITY WARNING: NCE_API_KEY is not set. Admin API routes will be inaccessible."
            )

        # P1: JWT
        cls.validate_jwt_config()

        # P1: MCP stdio tenant plane
        cls.validate_mcp_api_key()
        cls.validate_mcp_namespace_binding()

        # P1: Admin plane (HTTP Basic UI + MCP admin scope)
        cls.validate_admin_credentials()

        # P1: Live migration MCP tools are high-risk in production
        cls.validate_migration_mcp_surface()

        # P1: Webhook dedup must fail closed when Redis is unavailable
        cls.validate_webhook_dedup_policy()

        # P1: D365 module — require secrets when enabled in production
        cls.validate_d365_config()

        # P1: Secrets-provider seam — reject the dev dotenv-persist path in prod
        cls.validate_secrets_provider()

    @classmethod
    def validate_secrets_provider(cls) -> None:
        """Enforce the production secrets-provider posture (VI.1 / R3).

        In production:
          * the dev ``NCE_ALLOW_ADMIN_DOTENV_PERSIST`` path (admin UI writing
            connector/datastore secrets to a local ``.env``) is rejected —
            production must source secrets from a real manager, never a
            committed/written file; and
          * ``NCE_MASTER_KEY`` must resolve from the environment / secret
            manager only — never through a database- or file-backed provider.
        """
        if not cls.IS_PROD:
            return
        if cls.NCE_ALLOW_ADMIN_DOTENV_PERSIST:
            raise RuntimeError(
                "CRITICAL CONFIGURATION FAILURE: NCE_ALLOW_ADMIN_DOTENV_PERSIST must be "
                "false in production. Production secrets must come from a secret manager "
                "(Vault / AWS Secrets Manager / Azure Key Vault) injected into the "
                "environment, not written to a local .env file."
            )
        # R3: the master key is env-only; it must never be sourced from a store.
        if "NCE_MASTER_KEY" not in _ENV_ONLY_SECRETS:
            raise RuntimeError(
                "CRITICAL CONFIGURATION FAILURE: NCE_MASTER_KEY is no longer pinned to "
                "environment-only resolution. It must never be sourced from a database "
                "or SettingsStore (R3)."
            )

    @classmethod
    def validate_d365_config(cls) -> None:
        """Fail fast when D365 is enabled in production without required secrets."""
        if not cls.NCE_D365_ENABLED or not cls.IS_PROD:
            return
        if not cls.NCE_D365_ORG_URL:
            raise RuntimeError(
                "CRITICAL CONFIGURATION FAILURE: NCE_D365_ORG_URL must be set "
                "when NCE_D365_ENABLED=true in production."
            )
        if not cls.NCE_D365_WEBHOOK_SECRET:
            raise RuntimeError(
                "CRITICAL CONFIGURATION FAILURE: NCE_D365_WEBHOOK_SECRET must be set "
                "when NCE_D365_ENABLED=true in production."
            )

    @classmethod
    def validate_webhook_dedup_policy(cls) -> None:
        """Reject fail-open webhook dedup in production (duplicate bridge deliveries)."""
        if not cls.IS_PROD or not cls.WEBHOOK_DEDUP_FAIL_OPEN:
            return
        raise RuntimeError(
            "CRITICAL CONFIGURATION FAILURE: WEBHOOK_DEDUP_FAIL_OPEN must be false in "
            "production so webhook deduplication fails closed when Redis is unavailable."
        )

    @classmethod
    def validate_migration_mcp_surface(cls) -> None:
        """Disable migration MCP tools in production unless explicitly opted in."""
        if not cls.IS_PROD or cls.NCE_DISABLE_MIGRATION_MCP:
            return
        if _bool_env("NCE_ALLOW_MIGRATION_MCP_IN_PROD", False):
            log.warning(
                "Migration MCP tools are enabled in production "
                "(NCE_ALLOW_MIGRATION_MCP_IN_PROD=true). "
                "Disable after the migration window."
            )
            return
        raise RuntimeError(
            "CRITICAL CONFIGURATION FAILURE: Migration MCP tools must not run in "
            "production unless NCE_ALLOW_MIGRATION_MCP_IN_PROD=true is set for a "
            "controlled window. Otherwise set NCE_DISABLE_MIGRATION_MCP=true."
        )

    @classmethod
    def validate_mcp_api_key(cls) -> None:
        """Require MCP tenant API key in production (stdio tool authentication)."""
        if (cls.NCE_MCP_API_KEY or "").strip():
            return
        if cls.IS_PROD:
            raise RuntimeError(
                "CRITICAL CONFIGURATION FAILURE: NCE_MCP_API_KEY is required "
                "in production for MCP stdio tenant tools."
            )
        log.warning(
            "SECURITY WARNING: NCE_MCP_API_KEY is not set. "
            "MCP tenant tools are unauthenticated in this environment."
        )

    @classmethod
    def validate_mcp_namespace_binding(cls) -> None:
        """Require a bound tenant namespace when MCP auth is enabled in production."""
        from uuid import UUID

        bound = (cls.NCE_MCP_NAMESPACE_ID or "").strip()
        if bound:
            try:
                UUID(bound)
            except ValueError as exc:
                raise RuntimeError(
                    "CRITICAL CONFIGURATION FAILURE: NCE_MCP_NAMESPACE_ID must be "
                    f"a valid UUID, got {bound!r}."
                ) from exc
            return

        if cls.IS_PROD and (cls.NCE_MCP_API_KEY or "").strip():
            raise RuntimeError(
                "CRITICAL CONFIGURATION FAILURE: NCE_MCP_NAMESPACE_ID is required "
                "in production when NCE_MCP_API_KEY is set so MCP stdio tools "
                "cannot target arbitrary tenant UUIDs."
            )
        if (cls.NCE_MCP_API_KEY or "").strip():
            log.warning(
                "SECURITY WARNING: NCE_MCP_NAMESPACE_ID is not set. "
                "MCP tenant tools accept caller-supplied namespace_id."
            )

    @classmethod
    def validate_admin_credentials(cls) -> None:
        """Require admin API key and HTTP Basic credentials in production."""
        missing: list[str] = []
        if not (cls.NCE_ADMIN_API_KEY or "").strip():
            missing.append("NCE_ADMIN_API_KEY")
        if not (cls.NCE_ADMIN_USERNAME or "").strip():
            missing.append("NCE_ADMIN_USERNAME")
        if not (cls.NCE_ADMIN_PASSWORD or "").strip():
            missing.append("NCE_ADMIN_PASSWORD")

        if cls.IS_PROD:
            if missing:
                raise RuntimeError(
                    "CRITICAL CONFIGURATION FAILURE: Missing required admin credentials: "
                    + ", ".join(missing)
                )
            stored = cls.NCE_ADMIN_PASSWORD
            if not stored.startswith("$pbkdf2$"):
                raise RuntimeError(
                    "CRITICAL CONFIGURATION FAILURE: NCE_ADMIN_PASSWORD must be a "
                    "$pbkdf2$ hash in production (plaintext passwords are forbidden)."
                )
            return

        if missing:
            log.warning(
                "SECURITY WARNING: Incomplete admin credentials (%s). "
                "Admin UI and MCP admin tools may be inaccessible.",
                ", ".join(missing),
            )


# Module-level singleton — import `cfg` everywhere inside the package.
cfg = _Config()

if cfg.IS_PROD and cfg.NCE_BYPASS_WORM:
    raise RuntimeError("NCE_BYPASS_WORM is forbidden in production")
if cfg.IS_PROD and cfg.NCE_BYPASS_RLS:
    raise RuntimeError("NCE_BYPASS_RLS is forbidden in production")
if cfg.IS_PROD and cfg.NCE_ALLOW_ADMIN_DOTENV_PERSIST:
    raise RuntimeError("NCE_ALLOW_ADMIN_DOTENV_PERSIST is forbidden in production")
if cfg.IS_PROD and cfg.NCE_ADMIN_OVERRIDE:
    raise RuntimeError(
        "NCE_ADMIN_OVERRIDE is forbidden in production. "
        "Remove this environment variable from the production configuration."
    )
if cfg.IS_PROD and os.environ.get("NCE_LOAD_DOTENV", "true").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}:
    raise RuntimeError(
        "NCE_LOAD_DOTENV must be false in production. "
        "Inject secrets via the orchestrator; do not load a .env file at runtime."
    )

_fail_unless_nce_master_key_ok(cfg.NCE_MASTER_KEY)


def assert_admin_override_not_in_production() -> None:
    """Raise if the dev-only admin override bypass is enabled in production."""
    if cfg.NCE_ADMIN_OVERRIDE and cfg.IS_PROD:
        raise RuntimeError(
            "NCE_ADMIN_OVERRIDE must not be set when NCE_ENV is production. "
            "Remove this environment variable from the production configuration."
        )


def __getattr__(name: str) -> Any:
    if name == "OrchestratorConfig":
        import warnings

        warnings.warn(
            "OrchestratorConfig is deprecated; use cfg (the Config instance) instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        return _Config
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
