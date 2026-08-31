"""
nce/auth.py

Phase 0.1 — Multi-Tenant Namespacing: HMAC-SHA256 API Authentication.

Public API (imported by admin_server.py and the orchestrator write path):
  - HMACAuthMiddleware       — Starlette middleware; protects HTTP admin endpoints
  - optional_hmac_nonce_store() — Optional asyncio Redis NonceStore (pooled) when NCE_DISTRIBUTED_REPLAY is set
  - resolve_namespace(...)   — Extract + validate namespace_id UUID from headers
  - validate_agent_id(...)   — Strip / truncate agent_id to safe form
  - set_namespace_context(conn, namespace_id) — SET LOCAL for Postgres RLS
  - assume_namespace(conn, namespace_id, *, agent, pg_pool, reason) — Impersonate with mandatory WORM audit
  - audited_session(pg_pool, namespace_id, *, agent_id, event_type, params, reason) — Generalised WORM-audited scoped session (Phase 3)

JSON-RPC 2.0 error codes used by this module (server-defined range -32000 to -32099):
  -32001  Authentication failed  (missing / invalid signature)
  -32002  Replay detected        (timestamp outside drift window)
  -32003  Invalid namespace      (missing / malformed UUID)
  -32004  Invalid agent_id       (should never surface; validate_agent_id never raises)

Signature scheme
----------------
Required request headers:
  X-NCE-Timestamp:  <unix_epoch_seconds>        integer, UTC
  Authorization:       HMAC-SHA256 <hex_signature>

canonical_message = METHOD\\nPATH\\nTIMESTAMP[\\nSHA256_HEX(raw_body)]
signature         = HMAC-SHA256(NCE_API_KEY, canonical_message)

Notes:
  - Body hash is omitted for requests with an empty body (GET, etc.)
  - Comparison is always constant-time (hmac.compare_digest)
  - Timestamps outside ±5 minutes are rejected (replay protection)
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import hmac as _hmac
import inspect
import logging
import os
import secrets
import threading
import time
from base64 import b64decode
from binascii import Error as BinasciiError
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

from cachetools import TTLCache
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from nce.config import cfg
from nce.db_utils import POOL_ACQUIRE_TIMEOUT

log = logging.getLogger("nce.auth")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TIMESTAMP_DRIFT_SECONDS: int = cfg.NCE_CLOCK_SKEW_TOLERANCE_S
_NONCE_TTL_SECONDS: int = _TIMESTAMP_DRIFT_SECONDS * 2  # 600 s — auto-cleanup
_NONCE_KEY_PREFIX: str = "nce:nonce:"

# ---------------------------------------------------------------------------
# PBKDF2 password hashing (OWASP 2026)
# ---------------------------------------------------------------------------
# OWASP 2026 recommended minimum: 600,000 iterations for PBKDF2-HMAC-SHA256.
# Used by :func:`hash_admin_password` / :func:`verify_admin_password`.
_PBKDF2_ITERATIONS: int = max(600_000, cfg.NCE_PBKDF2_ITERATIONS)
_PBKDF2_HASH_FORMAT_PREFIX: str = "$pbkdf2$"  # format: $pbkdf2$iterations$salt_hex$hash_hex
_PBKDF2_SALT_LEN: int = 16
_PBKDF2_DKLEN: int = 32

# JSON-RPC 2.0 server-defined error codes
_CODE_AUTH_FAILED: int = -32001
_CODE_REPLAY: int = -32002
_CODE_INVALID_NAMESPACE: int = -32003
_CODE_INVALID_AGENT: int = -32004
_CODE_SCOPE_FORBIDDEN: int = -32005

_HTTP_UNAUTHORIZED: int = 401
_HTTP_BAD_REQUEST: int = 400


def hash_admin_password(password: str, iterations: int | None = None) -> str:
    """Hash an admin password using PBKDF2-HMAC-SHA256.

    Produces a string in the format::

        $pbkdf2$<iterations>$<salt_hex>$<hash_hex>

    The salt is randomly generated (16 bytes).  Defaults to
    ``_PBKDF2_ITERATIONS`` (600,000, OWASP 2026).

    Args:
        password:   The plaintext password to hash.
        iterations: Optional override for the iteration count (must be ≥ 100,000).

    Returns:
        A ``$pbkdf2$``-prefixed hash string suitable for storage.
    """
    iters = iterations or _PBKDF2_ITERATIONS
    if iters < 100_000:
        raise ValueError(f"PBKDF2 iterations must be at least 100,000; got {iters}")
    salt = os.urandom(_PBKDF2_SALT_LEN)
    dk = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        iters,
        dklen=_PBKDF2_DKLEN,
    )
    return f"{_PBKDF2_HASH_FORMAT_PREFIX}{iters}${salt.hex()}${dk.hex()}"


def verify_admin_password(
    password: str, stored_hash: str, *, auto_upgrade: bool = True
) -> tuple[bool, str | None]:
    """Verify a plaintext password against a stored PBKDF2 hash.

    Supports auto-upgrade: if the stored hash uses fewer iterations than
    ``_PBKDF2_ITERATIONS`` (OWASP 2026 minimum), a new hash is returned
    so the caller can persist the upgraded hash.  This ensures existing
    passwords are silently re-hashed with the stronger iteration count
    on their next successful login — no lock-out.

    Also accepts plaintext passwords for backward compatibility
    (no ``$pbkdf2$`` prefix), comparing with ``secrets.compare_digest``.
    Plaintext passwords are DEPRECATED and should be migrated.

    Args:
        password:     The plaintext password to verify.
        stored_hash:  The stored hash string (``$pbkdf2$``-prefixed or plaintext).
        auto_upgrade: If True and the stored hash is below OWASP 2026, return
                      the upgraded hash.  Set to False for read-only verification.

    Returns:
        A tuple ``(valid: bool, upgraded_hash: str | None)``.
        ``upgraded_hash`` is non-None only when ``auto_upgrade=True`` and the
        stored hash needs upgrading.
    """
    if not stored_hash:
        return False, None

    # Backward compat: plaintext comparison (DEPRECATED) — never in production
    if not stored_hash.startswith(_PBKDF2_HASH_FORMAT_PREFIX):
        if cfg.IS_PROD:
            log.error(
                "Plaintext NCE_ADMIN_PASSWORD is forbidden in production; store a $pbkdf2$ hash."
            )
            return False, None
        valid = secrets.compare_digest(password, stored_hash)
        if valid and auto_upgrade:
            upgraded = hash_admin_password(password)
            log.info(
                "Admin password upgraded from plaintext to PBKDF2-HMAC-SHA256 (%d iterations).",
                _PBKDF2_ITERATIONS,
            )
            return True, upgraded
        return valid, None

    # Parse $pbkdf2$<iterations>$<salt_hex>$<hash_hex>
    rest = stored_hash[len(_PBKDF2_HASH_FORMAT_PREFIX) :]
    parts = rest.split("$", 2)
    if len(parts) != 3:
        log.warning("Invalid PBKDF2 hash format (expected 3 parts, got %d).", len(parts))
        return False, None

    iterations_str, salt_hex, hash_hex = parts
    try:
        stored_iterations = int(iterations_str)
    except ValueError:
        log.warning("Invalid PBKDF2 iteration count: %r", iterations_str)
        return False, None

    if stored_iterations < 100_000:
        log.warning("PBKDF2 hash with too few iterations: %d", stored_iterations)
        return False, None

    try:
        salt = bytes.fromhex(salt_hex)
    except ValueError:
        log.warning("Invalid PBKDF2 salt hex: %r", salt_hex)
        return False, None

    try:
        expected_hash = bytes.fromhex(hash_hex)
    except ValueError:
        log.warning("Invalid PBKDF2 hash hex: %r", hash_hex)
        return False, None

    if len(salt) != _PBKDF2_SALT_LEN or len(expected_hash) != _PBKDF2_DKLEN:
        log.warning(
            "PBKDF2 hash dimensions mismatch: salt=%d (expected %d), hash=%d (expected %d).",
            len(salt),
            _PBKDF2_SALT_LEN,
            len(expected_hash),
            _PBKDF2_DKLEN,
        )
        return False, None

    derived = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        stored_iterations,
        dklen=_PBKDF2_DKLEN,
    )

    if not secrets.compare_digest(derived, expected_hash):
        return False, None

    # Auto-upgrade: if stored iterations < current target, return re-hashed password
    if auto_upgrade and stored_iterations < _PBKDF2_ITERATIONS:
        upgraded = hash_admin_password(password)
        log.info(
            "Admin password auto-upgraded: %d → %d PBKDF2 iterations.",
            stored_iterations,
            _PBKDF2_ITERATIONS,
        )
        return True, upgraded

    return True, None


async def hash_admin_password_async(password: str, iterations: int | None = None) -> str:
    """Async wrapper: runs :func:`hash_admin_password` in a worker thread."""
    return await asyncio.to_thread(hash_admin_password, password, iterations)


async def verify_admin_password_async(
    password: str,
    stored_hash: str,
    *,
    auto_upgrade: bool = True,
) -> tuple[bool, str | None]:
    """Async wrapper: runs :func:`verify_admin_password` in a worker thread."""
    return await asyncio.to_thread(
        verify_admin_password,
        password,
        stored_hash,
        auto_upgrade=auto_upgrade,
    )


# ---------------------------------------------------------------------------
# Helpers — JSON-RPC 2.0 error responses
# ---------------------------------------------------------------------------


def jsonrpc_error_response(
    code: int,
    message: str,
    reason: str,
    *,
    status_code: int | None = None,
    headers: dict[str, str] | None = None,
    request_id: Any = None,
) -> JSONResponse:
    """Build a strict JSON-RPC 2.0 HTTP JSONResponse."""
    if status_code is None:
        status_code = (
            _HTTP_UNAUTHORIZED
            if code in (_CODE_AUTH_FAILED, _CODE_REPLAY, -32005, -32006, -32007)
            else _HTTP_BAD_REQUEST
        )
    return JSONResponse(
        status_code=status_code,
        content={
            "jsonrpc": "2.0",
            "error": {
                "code": code,
                "message": message,
                "data": {"reason": reason},
            },
            "id": request_id,
        },
        headers=headers or None,
    )


def _jsonrpc_error(
    code: int,
    message: str,
    reason: str,
    request_id: Any = None,
) -> JSONResponse:
    """Build a strict JSON-RPC 2.0 error response."""
    return jsonrpc_error_response(
        code,
        message,
        reason,
        request_id=request_id,
    )


# ---------------------------------------------------------------------------
# RBAC scope enforcement — ScopeError + require_scope decorator
# ---------------------------------------------------------------------------


class ScopeError(Exception):
    """Raised when the caller lacks the required scope for an MCP tool.

    Propagates through :func:`call_tool` to produce a JSON-RPC 2.0 error
    response with code ``-32005`` (scope forbidden), distinct from generic
    input-validation errors (which map to ``-32602`` / invalid params).

    Attributes:
        required_scope: The scope the caller failed to present (e.g. ``"admin"``).
        reason:         Human-readable explanation of the rejection.
    """

    def __init__(self, required_scope: str, reason: str = "") -> None:
        self.required_scope = required_scope
        self.reason = reason
        super().__init__(f"MCP scope '{required_scope}' required: {reason}")


def _admin_override_enabled() -> bool:
    from nce.config import live_admin_override_enabled

    return live_admin_override_enabled()


def _admin_server_api_key() -> str:
    from nce.config import live_admin_api_key

    return live_admin_api_key()


def _mcp_server_api_key() -> str:
    from nce.config import live_mcp_api_key

    return live_mcp_api_key()


def _mcp_bound_namespace_id() -> str:
    from nce.config import live_mcp_namespace_id

    return live_mcp_namespace_id()


# MCP tools that require admin scope (handlers also use @require_scope("admin")).
MCP_ADMIN_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "unredact_memory",
        "replay_observe",
        "replay_reconstruct",
        "replay_fork",
        "replay_status",
        "start_migration",
        "migration_status",
        "validate_migration",
        "commit_migration",
        "abort_migration",
        "manage_namespace",
        "verify_memory",
        "trigger_consolidation",
        "consolidation_status",
        "manage_quotas",
        "rotate_signing_key",
        "get_health",
        "list_dlq",
        "replay_dlq",
        "purge_dlq",
        "resolve_contradiction",
    }
)


def enforce_mcp_tool_auth(tool_name: str, arguments: dict[str, Any]) -> None:
    """Enforce admin or tenant scope before MCP tool dispatch in ``server.call_tool``."""
    import os

    from nce.tool_registry import TOOL_REGISTRY

    spec = TOOL_REGISTRY.get(tool_name)
    if tool_name in MCP_ADMIN_TOOL_NAMES or (spec is not None and spec.admin_only):
        _validate_scope("admin", arguments)
    else:
        if not arguments.get("mcp_api_key") and not arguments.get("admin_api_key"):
            env_key = os.environ.get("NCE_MCP_API_KEY", "")
            if env_key:
                arguments["mcp_api_key"] = env_key
        try:
            _validate_scope("tenant", arguments)
            _bind_mcp_tenant_namespace(arguments)
        finally:
            arguments.pop("mcp_api_key", None)
            arguments.pop("admin_api_key", None)


def _bind_mcp_tenant_namespace(arguments: dict[str, Any]) -> None:
    """Pin or validate ``namespace_id`` when ``NCE_MCP_NAMESPACE_ID`` is configured."""
    from uuid import UUID

    bound = _mcp_bound_namespace_id()
    if not bound:
        return

    try:
        bound_uuid = str(UUID(bound))
    except ValueError:
        raise ScopeError("tenant", "invalid server NCE_MCP_NAMESPACE_ID") from None

    provided = arguments.get("namespace_id")
    if provided is None or provided == "":
        arguments["namespace_id"] = bound_uuid
        return

    try:
        provided_uuid = str(UUID(str(provided)))
    except ValueError:
        raise ScopeError("tenant", "invalid namespace_id") from None

    if provided_uuid != bound_uuid:
        log.warning(
            "Tenant scope rejected: namespace_id mismatch (bound=%s provided=%s)",
            bound_uuid,
            provided_uuid,
        )
        raise ScopeError("tenant", "namespace_id does not match configured MCP tenant")


def _validate_scope(scope: str, arguments: dict[str, Any]) -> None:
    """Validate that the caller possesses the required RBAC scope.

    For ``"admin"`` scope — validates ``admin_api_key`` against the
    ``NCE_ADMIN_API_KEY`` environment variable (constant-time comparison).
    Accepts ``NCE_ADMIN_OVERRIDE=true`` as a development bypass.

    For ``"tenant"`` scope — implicitly granted to all authenticated callers
    in the current authentication model.  Reserved for future JWT-based
    enforcement.

    Raises:
        ScopeError: When the caller lacks the required scope.
    """
    if scope == "admin":
        # Dev override — intentionally first so local dev is frictionless
        if _admin_override_enabled():
            return

        server_key = _admin_server_api_key()
        if not server_key:
            raise ScopeError(
                "admin",
                "Server misconfigured: NCE_ADMIN_API_KEY is not set. "
                "Set the environment variable or enable NCE_ADMIN_OVERRIDE for development.",
            )

        provided_key: str | None = arguments.get("admin_api_key")
        if not provided_key or not isinstance(provided_key, str) or not provided_key.strip():
            raise ScopeError("admin", "missing admin_api_key")

        if not secrets.compare_digest(provided_key.strip(), server_key):
            log.warning("Admin scope rejected: invalid admin_api_key")
            raise ScopeError("admin", "invalid admin_api_key")

    elif scope == "tenant":
        server_key = _mcp_server_api_key()
        if not server_key:
            from nce.config import cfg

            if cfg.IS_PROD:
                raise ScopeError(
                    "tenant",
                    "Server misconfigured: NCE_MCP_API_KEY is not set.",
                )
            return

        provided_key = arguments.get("mcp_api_key")
        if not provided_key or not isinstance(provided_key, str) or not provided_key.strip():
            raise ScopeError("tenant", "missing mcp_api_key")

        if not secrets.compare_digest(provided_key.strip(), server_key):
            log.warning("Tenant scope rejected: invalid mcp_api_key")
            raise ScopeError("tenant", "invalid mcp_api_key")

    else:
        raise ScopeError(scope, f"unknown scope '{scope}'")


def require_scope(scope: str):
    """Decorator: enforce RBAC scope before MCP handler execution.

    Usage::

        @require_scope("admin")
        async def handle_manage_namespace(engine, arguments, admin_identity=None) -> str:
            ...

    The decorator inspects the **second positional argument** (always the
    ``arguments`` dict per MCP handler conventions) to extract auth context.
    On success, it strips :data:`nce.mcp_args._MCP_AUTH_KEYS` from
    ``arguments`` so that ``extra='forbid'`` Pydantic domain models never
    see transport-level keys.  If the wrapped handler declares an
    ``admin_identity`` parameter the value is forwarded as a keyword argument.

    Raises:
        ScopeError: If the caller lacks the required scope.  Propagate this
            exception unchanged through your dispatch layer so the MCP
            framework produces a JSON-RPC error response.
    """
    from nce.mcp_args import _MCP_AUTH_KEYS

    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            # MCP handler convention: arguments is always the 2nd positional arg
            if len(args) < 2:
                raise ScopeError(scope, "handler missing arguments parameter")
            arguments = args[1]
            if not isinstance(arguments, dict):
                raise ScopeError(scope, "handler arguments is not a dict")

            # 1. Validate RBAC scope
            _validate_scope(scope, arguments)

            # 2. Extract admin_identity before stripping (forwarded as kwarg)
            admin_identity = arguments.get("admin_identity")

            # 3. Strip auth keys so extra='forbid' models never see them
            clean_args = {k: v for k, v in arguments.items() if k not in _MCP_AUTH_KEYS}

            # 4. Rebuild positional args with cleaned arguments dict
            new_args = (args[0], clean_args) + args[2:]

            # 5. Forward admin_identity if the handler accepts it
            sig = inspect.signature(func)
            if "admin_identity" in sig.parameters:
                kwargs = {**kwargs, "admin_identity": admin_identity}

            return await func(*new_args, **kwargs)

        return wrapper

    return decorator


# ---------------------------------------------------------------------------
# Administrative MCP Endpoints Rate Limiting
# ---------------------------------------------------------------------------


class RateLimitError(Exception):
    """Raised when an admin tool rate limit is exceeded.

    Attributes:
        key: The rate limit key suffix that was exceeded (e.g. 'tenant:uuid').
        limit: The limit count.
        period: The window period in seconds.
    """

    def __init__(self, key: str, limit: int, period: int) -> None:
        self.key = key
        self.limit = limit
        self.period = period
        super().__init__(
            f"Rate limit exceeded: max {limit} requests per {period}s for key suffix '{key}'"
        )


_IN_MEMORY_LIMITS: TTLCache = TTLCache(maxsize=10000, ttl=300)
_rate_limit_lock = threading.Lock()

# Atomic Lua script for sliding-window rate limiting.
# Returns 1 if allowed, 0 if limit exceeded.
_RATE_LIMIT_LUA = """
local key = KEYS[1]
local window_start = ARGV[1]
local now = ARGV[2]
local limit = tonumber(ARGV[3])
local period = tonumber(ARGV[4])

redis.call('ZREMRANGEBYSCORE', key, 0, window_start)
local count = redis.call('ZCARD', key)
if count >= limit then
    return 0
end
redis.call('ZADD', key, now, now)
redis.call('EXPIRE', key, period)
return 1
"""


def _check_in_memory_rate_limit(key: str, limit: int, period: int) -> bool:
    """Safe local sliding window fallback if Redis is unavailable/offline."""
    now = time.time()
    with _rate_limit_lock:
        if key not in _IN_MEMORY_LIMITS:
            _IN_MEMORY_LIMITS[key] = []
        timestamps = [t for t in _IN_MEMORY_LIMITS[key] if t > now - period]
        if len(timestamps) >= limit:
            _IN_MEMORY_LIMITS[key] = timestamps
            return False
        timestamps.append(now)
        _IN_MEMORY_LIMITS[key] = timestamps
        return True


def admin_rate_limit(limit: int = 10, period: int = 60):
    """Decorator to enforce sliding-window rate limiting on admin MCP handlers.

    Resolves keys using:
    1. Target `namespace_id` in arguments if present (tenant-scoped).
    2. `admin_identity` if present.
    3. Falling back to the tool's function name.

    Uses Redis sorted sets (ZSET) with automatic expiration, gracefully falling
    back to thread-safe in-memory sliding windows on any Redis failure or omission.
    """

    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            if len(args) < 2:
                return await func(*args, **kwargs)

            engine = args[0]
            arguments = args[1]

            if not isinstance(arguments, dict):
                return await func(*args, **kwargs)

            # Resolve key suffix
            namespace_id = arguments.get("namespace_id")
            admin_identity = arguments.get("admin_identity") or kwargs.get("admin_identity")

            if namespace_id:
                key_suffix = f"tenant:{namespace_id}"
            elif admin_identity:
                key_suffix = f"identity:{admin_identity}"
            else:
                key_suffix = f"tool:{func.__name__}"

            key = f"nce:ratelimit:admin:{key_suffix}"

            # Sliding window zset logic (atomic via Lua)
            redis_client = getattr(engine, "redis_client", None)
            allowed = True

            if redis_client is not None:
                try:
                    now = time.time()
                    clear_before = now - period
                    result = await redis_client.eval(
                        _RATE_LIMIT_LUA,
                        1,
                        key,
                        str(clear_before),
                        str(now),
                        str(limit),
                        str(period),
                    )
                    allowed = bool(result)
                except Exception as exc:
                    log.warning("Redis rate limiter failed, falling back to RAM: %s", exc)
                    allowed = _check_in_memory_rate_limit(key, limit, period)
            else:
                allowed = _check_in_memory_rate_limit(key, limit, period)

            if not allowed:
                log.warning("Rate limit exceeded for key %s (limit: %d/%ds)", key, limit, period)
                raise RateLimitError(key_suffix, limit, period)

            return await func(*args, **kwargs)

        return wrapper

    return decorator


# ---------------------------------------------------------------------------
# Pydantic V2 models
# ---------------------------------------------------------------------------


class HMACAuthContext(BaseModel):
    """Validated authentication context extracted from HTTP request headers.

    Immutable after construction (model_config frozen=True).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    timestamp: int = Field(..., description="Unix epoch seconds (from X-NCE-Timestamp)")
    signature: str = Field(..., description="Lowercase hex HMAC (from Authorization header)")

    @field_validator("timestamp")
    @classmethod
    def timestamp_must_be_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("timestamp must be a positive integer")
        return v

    @field_validator("signature", mode="before")
    @classmethod
    def signature_must_be_hex(cls, v: str) -> str:
        stripped = (v or "").strip()
        if not stripped:
            raise ValueError("signature must not be empty")
        try:
            int(stripped, 16)
        except ValueError as exc:
            raise ValueError("signature must be a valid lowercase hex string") from exc
        return stripped.lower()


class NamespaceContext(BaseModel):
    """Validated namespace + agent scoping for multi-tenant RLS.

    Used by the orchestrator write path to carry the resolved identity.

    C3 principal-session fields (Wave 23):
      principal_kind   — "employee" | "contractor" | "external-customer".
                         Populated from the ``principal_kind`` JWT claim when present;
                         defaults to "employee" (safest tier — no external scope set).
      external_scope_id — UUID of the external scope, populated from the
                          ``external_scope_id`` JWT claim.  Only present for
                          contractor / external-customer principals.  NEVER sourced
                          from a request header — the JWT is the authenticated source.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    namespace_id: UUID | None = Field(
        default=None, description="Namespace UUID (from X-NCE-Namespace-ID)"
    )
    agent_id: str = Field(default="default", description="Agent identifier (from X-NCE-Agent-ID)")
    principal_kind: str = Field(
        default="employee",
        description=(
            "Principal tier: 'employee' | 'contractor' | 'external-customer'. "
            "Sourced exclusively from the verified JWT claim 'principal_kind'."
        ),
    )
    external_scope_id: UUID | None = Field(
        default=None,
        description=(
            "External scope UUID for contractor / external-customer principals. "
            "Sourced exclusively from the verified JWT claim 'external_scope_id'. "
            "None for employee principals (deny-when-unset preserved)."
        ),
    )

    @field_validator("agent_id", mode="before")
    @classmethod
    def clean_agent_id(cls, v: Any) -> str:
        # Delegate to the canonical validate_agent_id so the normalisation
        # rule (strip, truncate to 128, default "default") lives in one place.
        return validate_agent_id(str(v) if v is not None else "")

    @field_validator("principal_kind", mode="before")
    @classmethod
    def validate_principal_kind(cls, v: Any) -> str:
        allowed = {"employee", "contractor", "external-customer"}
        val = str(v or "employee").strip().lower()
        if val not in allowed:
            # Unknown kind → safe default (employee = no external scope set)
            return "employee"
        return val


# ---------------------------------------------------------------------------
# Core HMAC helpers
# ---------------------------------------------------------------------------


def _compute_signature(
    api_key: str,
    method: str,
    path: str,
    timestamp: int,
    body_bytes: bytes,
) -> str:
    """Compute the expected HMAC-SHA256 hex signature for a request.

    canonical_message = METHOD\\nPATH\\nTIMESTAMP[\\nSHA256_HEX(body)]
    """
    parts = [method.upper(), path, str(timestamp)]
    if body_bytes:
        parts.append(hashlib.sha256(body_bytes).hexdigest())
    canonical = "\n".join(parts)
    return _hmac.new(
        api_key.encode("utf-8"),
        canonical.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def verify_hmac(
    api_key: str,
    method: str,
    path: str,
    timestamp: int,
    body_bytes: bytes,
    provided_sig: str,
) -> bool:
    """Constant-time HMAC-SHA256 verification.

    Returns True when the signature is valid, False otherwise.
    An empty api_key always returns False (server misconfigured).
    """
    if not api_key:
        return False
    expected = _compute_signature(api_key, method, path, timestamp, body_bytes)
    return _hmac.compare_digest(expected, provided_sig.strip().lower())


# ---------------------------------------------------------------------------
# Phase 0.1 public helpers (used by orchestrator.py and write path)
# ---------------------------------------------------------------------------


def resolve_namespace(request_headers: dict[str, str]) -> UUID | None:
    """Extract and validate the namespace_id UUID from request headers.

    Header: X-NCE-Namespace-ID: <uuid>

    Returns None if missing.
    """
    raw = request_headers.get("x-nce-namespace-id", "").strip()
    if not raw:
        return None
    try:
        return UUID(raw)
    except ValueError as exc:
        raise ValueError(f"Invalid namespace UUID: {raw!r}") from exc


def validate_agent_id(agent_id: str) -> str:
    """Sanitise agent_id: strip whitespace, truncate to 128 chars.

    Returns 'default' for blank input.  Never raises.
    """
    cleaned = (agent_id or "").strip()[:128]
    return cleaned if cleaned else "default"


async def set_namespace_context(conn: Any, namespace_id: UUID) -> None:
    """Set the Postgres session variable consumed by RLS policies.

    Uses set_config(..., true) which applies only for the current transaction
    (equivalent to SET LOCAL).  Must NOT use bare SET — that would leak across
    pooled connections.

    For privileged impersonation with audit logging, use
    :func:`assume_namespace` instead.

    Args:
        conn:         An asyncpg Connection (or compatible).
        namespace_id: The validated namespace UUID.
    """
    await conn.execute(
        "SELECT set_config('nce.namespace_id', $1, true)",
        str(namespace_id),
    )


async def _reset_rls_context(conn: Any) -> None:
    """Reset the Postgres RLS session variable to empty string.

    Uses set_config(..., true) so the reset is scoped to the current
    transaction and does not leak across pooled connections.

    Call this in a ``finally`` block after every ``yield conn`` that
    previously called :func:`set_namespace_context`.
    """
    await conn.execute(
        "SELECT set_config('nce.namespace_id', '', true)",
    )


# ---------------------------------------------------------------------------
# Internal — WORM audit write on a separate auto-committing connection
# ---------------------------------------------------------------------------


async def _write_audit_event(
    pg_pool: Any,
    namespace_id: UUID,
    agent_id: str,
    event_type: str,
    params: dict[str, Any],
    *,
    result_summary: dict[str, Any] | None = None,
) -> None:
    """Write a single cryptographically signed WORM audit event on an
    independent connection + transaction that is committed immediately.

    This is the atomic audit primitive used by both
    :func:`assume_namespace` and :func:`audited_session`.

    The audit record is committed BEFORE control returns to the caller,
    guaranteeing it survives any subsequent rollback on the caller's
    connection.

    Args:
        pg_pool:        An ``asyncpg.Pool`` — a **separate** connection is
                        acquired for the audit write.
        namespace_id:   The namespace the event pertains to.
        agent_id:       The principal performing the action.
        event_type:     Must be a valid :data:`~nce.event_log.EventType`.
        params:         Arbitrary JSON-serialisable params for the event.
        result_summary: Optional summary (defaults to ``{"status": "audited"}``).

    Raises:
        RuntimeError: If the audit event cannot be written or committed
                      (fail-closed — no action proceeds without audit).
    """
    from nce.event_log import append_event

    try:
        async with pg_pool.acquire(timeout=POOL_ACQUIRE_TIMEOUT) as audit_conn:
            async with audit_conn.transaction():
                await append_event(
                    conn=audit_conn,
                    namespace_id=namespace_id,
                    agent_id=agent_id,
                    event_type=event_type,
                    params=params,
                    result_summary=result_summary or {"status": "audited"},
                )
    except Exception as exc:
        log.critical(
            "_write_audit_event: FAILED for event_type=%r agent=%r ns=%s: %s",
            event_type,
            agent_id,
            namespace_id,
            exc,
        )
        raise RuntimeError(
            f"Audit write failed for event_type={event_type!r} namespace={namespace_id}"
        ) from exc


# ---------------------------------------------------------------------------
# Public — audited_session (Phase 3 generalised privileged operation wrapper)
# ---------------------------------------------------------------------------


@asynccontextmanager
async def audited_session(
    pg_pool: Any,
    namespace_id: UUID,
    *,
    agent_id: str,
    event_type: str,
    params: dict[str, Any] | None = None,
    reason: str = "",
):
    """Generalised WORM-audited scoped session for privileged operations.

    Yields an asyncpg Connection with ``nce.namespace_id`` already set
    via SET LOCAL (RLS-scoped).  A cryptographically signed audit event is
    written and committed on a **separate** connection BEFORE the session is
    yielded — the audit trail survives any exception or rollback inside the
    caller's ``with`` block.

    Usage::

        async with audited_session(
            pg_pool, namespace_id,
            agent_id="admin-support",
            event_type="admin_memory_recall",
            params={"query": "security incidents"},
            reason="ticket-12345",
        ) as conn:
            # conn is scoped to namespace_id via RLS
            rows = await conn.fetch("SELECT ... FROM memories ...")

    Design contract (fail-closed):
        If the pre-flight audit write fails, ``RuntimeError`` is raised
        and the ``with`` block body never executes — no silent privileged
        access without an audit trail.

    Args:
        pg_pool:       An ``asyncpg.Pool`` — used both for the audit write
                       (separate connection) and the yielded session.
        namespace_id:  The target namespace UUID to scope the session to.
        agent_id:      The principal performing the privileged operation
                       (stored in the audit event's ``agent_id`` column).
        event_type:    Must be a valid :data:`~nce.event_log.EventType`.
        params:        Arbitrary JSON-serialisable params for the audit event.
                       If *reason* is provided it is merged under the
                       ``"reason"`` key (truncated to 256 chars).
        reason:        Optional operational context (e.g. ticket ref).
                       Truncated to 256 chars, merged into *params*.

    Yields:
        asyncpg.Connection:  A connection scoped to *namespace_id* via RLS.

    Raises:
        RuntimeError: If the pre-flight audit event cannot be written.
    """
    # --- Step 1: Write WORM audit on an independent, auto-committing connection ---
    audit_params: dict[str, Any] = dict(params or {})
    if reason:
        audit_params["reason"] = reason[:256]

    await _write_audit_event(
        pg_pool=pg_pool,
        namespace_id=namespace_id,
        agent_id=agent_id,
        event_type=event_type,
        params=audit_params,
        result_summary={"status": "audited_session_begin"},
    )

    # --- Step 2: Yield a fresh RLS-scoped connection (SET LOCAL requires TX) ---
    async with pg_pool.acquire(timeout=POOL_ACQUIRE_TIMEOUT) as conn:
        async with conn.transaction():
            await set_namespace_context(conn, namespace_id)
            log.info(
                "audited_session: agent=%r ns=%s event_type=%r reason=%r",
                agent_id,
                namespace_id,
                event_type,
                reason[:64],
            )
            try:
                yield conn
            finally:
                await _reset_rls_context(conn)


# ---------------------------------------------------------------------------
# Public — assume_namespace (privileged impersonation via existing connection)
# ---------------------------------------------------------------------------


async def assume_namespace(
    conn: Any,
    namespace_id: UUID,
    *,
    impersonating_agent: str,
    pg_pool: Any,
    reason: str = "",
) -> None:
    """Impersonate a tenant namespace WITH mandatory WORM audit logging.

    Delegates the audit write to :func:`_write_audit_event` (the same
    primitive used by :func:`audited_session`), ensuring consistent
    irrefutable logging across all privileged operations.

    Writes an irrefutable ``namespace_impersonated`` event on a **separate**
    connection + transaction that is committed BEFORE the session variable
    is set.  This guarantees the audit trail survives even if the caller's
    transaction subsequently rolls back.

    Design contract (fail-closed):
        If the audit INSERT/COMMIT fails, ``RuntimeError`` is raised and
        ``SET LOCAL`` is never executed — no silent impersonation.

    The audit event records:
        - ``impersonating_agent``: the admin / principal performing the act
        - ``target namespace_id``
        - ``reason`` (optional, truncated to 256 chars)
        - DB-clock timestamp (cryptographically signed per WORM contract)

    After the audit write is committed, sets ``nce.namespace_id`` on
    *conn* via SET LOCAL (equivalent to :func:`set_namespace_context`).

    Args:
        conn:                The caller's asyncpg Connection (session variable
                             is set on this connection).
        namespace_id:        The target namespace UUID to impersonate.
        impersonating_agent: The admin / principal identifier performing the
                             impersonation (stored in the audit event's
                             ``agent_id`` column).
        pg_pool:             An ``asyncpg.Pool`` used to acquire a **separate**
                             connection for the audit write.
        reason:              Optional operational context (e.g. ticket ref).
                             Truncated to 256 characters.

    Raises:
        RuntimeError: If the audit event cannot be written or committed.
    """
    # --- Step 1: Write audit event on an INDEPENDENT connection ---
    # This transaction commits BEFORE we touch the caller's connection,
    # so the audit trail survives any subsequent rollback on *conn*.
    await _write_audit_event(
        pg_pool=pg_pool,
        namespace_id=namespace_id,
        agent_id=impersonating_agent,
        event_type="namespace_impersonated",
        params={
            "impersonated_namespace_id": str(namespace_id),
            "impersonating_agent": impersonating_agent,
            "reason": reason[:256],
        },
        result_summary={"status": "assumed"},
    )

    # --- Step 2: Only after audit is committed, set the session variable ---
    _in_tx_checker = getattr(conn, "is_in_transaction", None)
    if callable(_in_tx_checker) and not _in_tx_checker():
        raise RuntimeError(
            "assume_namespace requires an active PostgreSQL transaction on *conn* "
            "so SET LOCAL nce.namespace_id persists for subsequent queries."
        )
    await set_namespace_context(conn, namespace_id)
    log.info(
        "assume_namespace: agent=%r assumed namespace=%s reason=%r",
        impersonating_agent,
        namespace_id,
        reason[:64],
    )


# ---------------------------------------------------------------------------
# Distributed nonce store (Redis SETNX)
# ---------------------------------------------------------------------------


class NonceStore:
    """Redis-backed distributed replay cache shared across all instances.

    Uses ``SET key value NX PX ttl`` — an atomic compare-and-set — to
    ensure each nonce (HMAC signature hex) is accepted exactly once
    cluster-wide.

    Uses :mod:`redis.asyncio` with a connection pool so nonce checks do not
    block the Starlette event loop.  Transient connection errors on a pooled
    connection are surfaced on the next command; failures are fail-closed.

    Fail-closed:  any Redis connection or command error causes
    ``check_and_store()`` to return ``False``, rejecting the request.

    Args:
        redis_url: Redis connection URL (e.g. ``redis://localhost:6379/0``).
        ttl:       Key time-to-live in **seconds**.  Must cover the
                   maximum allowed clock-skew window so that a nonce
                   naturally expires before a fresh request with the
                   same timestamp range could legitimately arrive.
        max_connections: Async connection pool size (default 100).  The
                   ``optional_hmac_nonce_store`` factory passes
                   ``cfg.REDIS_MAX_CONNECTIONS`` when enabled.
    """

    def __init__(
        self,
        redis_url: str,
        ttl: int = _NONCE_TTL_SECONDS,
        *,
        max_connections: int = 100,
    ) -> None:
        self._redis_url = redis_url
        self._ttl = ttl
        self._max_connections = max_connections
        self._pool: Any = None  # redis.asyncio.ConnectionPool — lazy initialised
        self._redis: Any = None  # redis.asyncio.Redis — lazy initialised

    async def _get_redis(self) -> Any:
        """Lazy-init the asyncio Redis client and connection pool."""
        if self._redis is None:
            from redis.asyncio import ConnectionPool, Redis

            self._pool = ConnectionPool.from_url(
                self._redis_url,
                max_connections=self._max_connections,
            )
            self._redis = Redis(connection_pool=self._pool)
        return self._redis

    async def ping(self) -> bool:
        """Return ``True`` if Redis is reachable, ``False`` on any error.

        Used by :class:`HMACAuthMiddleware` to distinguish "Redis down" from
        "nonce already seen" when ``NCE_HMAC_NONCE_REQUIRED`` is enabled.
        """
        try:
            r = await self._get_redis()
            await r.ping()
            return True
        except Exception:
            return False

    async def check_and_store(self, nonce: str) -> bool:
        """Atomically check and store a nonce.

        Returns ``True`` if the nonce is **new** (request accepted).
        Returns ``False`` if the nonce was already seen (replay) **or**
        if Redis is unreachable (fail-closed).

        Key format: ``nce:nonce:<hex_signature>``
        TTL:        configured ``_ttl`` seconds (auto-cleanup).
        """
        try:
            r = await self._get_redis()
            key = f"{_NONCE_KEY_PREFIX}{nonce}"
            # SET key value NX PX ttl_ms → True if new, None if already exists
            result = await r.set(key, "1", nx=True, px=self._ttl * 1000)
            return result is True
        except Exception:
            log.exception("NonceStore Redis error — rejecting request (fail-closed)")
            return False

    async def aclose(self) -> None:
        """Close the Redis client and underlying pool (optional shutdown hook)."""
        if self._redis is not None:
            await self._redis.aclose()
            self._redis = None
        if self._pool is not None:
            await self._pool.disconnect()
            self._pool = None


def optional_hmac_nonce_store() -> NonceStore | None:
    """Return a Redis :class:`NonceStore` when distributed HMAC replay protection is enabled.

    Reads ``cfg.NCE_DISTRIBUTED_REPLAY``. When falsy or unset, returns ``None`` so
    :class:`HMACAuthMiddleware` keeps timestamp-only replay checks (single-instance /
    local dev). When truthy, uses ``cfg.REDIS_URL``.

    The store uses ``redis.asyncio`` with ``max_connections=cfg.REDIS_MAX_CONNECTIONS``.

    Warns and returns ``None`` if replay protection is requested but ``REDIS_URL`` is blank.
    """
    if not cfg.NCE_DISTRIBUTED_REPLAY:
        return None
    url = (cfg.REDIS_URL or "").strip()
    if not url:
        log.warning(
            "NCE_DISTRIBUTED_REPLAY enabled but REDIS_URL is empty — skipping NonceStore; "
            "HMAC replay protection is timestamp-only."
        )
        return None
    log.info("HMAC distributed replay: NonceStore active (Redis).")
    return NonceStore(url, max_connections=cfg.REDIS_MAX_CONNECTIONS)


# ---------------------------------------------------------------------------
# Starlette HMAC middleware
# ---------------------------------------------------------------------------


class HMACAuthMiddleware(BaseHTTPMiddleware):
    """HMAC-SHA256 authentication middleware for Starlette admin endpoints.

    All routes under ``protected_prefix`` (default: ``/api/``) require:
      - ``X-NCE-Timestamp`` header (Unix epoch, integer)
      - ``Authorization: HMAC-SHA256 <hex_signature>`` header
      - ``X-NCE-Nonce`` header (required when Redis reachable + ``nonce_required``)

    Failed auth attempts always return a strict JSON-RPC 2.0 error body so
    that HTTP and MCP clients can parse failures uniformly.

    Replay protection (two-layer):
      1. Timestamp check: ± ``_TIMESTAMP_DRIFT_SECONDS`` from server time.
      2. Distributed nonce check: the ``X-NCE-Nonce`` value is atomically stored
         in Redis via SETNX.  A repeated nonce within the drift window is rejected
         even if the timestamp is fresh.  When ``nonce_required=True`` (default)
         and Redis is reachable, a missing nonce is also rejected.

    Fail-closed posture (when ``nonce_required=True``):
      - Production: Redis unreachable → reject (no nonce ≡ no admission).
      - Development / test: Redis unreachable → allow with a WARNING log.

    Args:
        app:               The ASGI application to wrap.
        protected_prefix:  URL path prefix that requires authentication.
        api_key:           The shared HMAC secret (``NCE_API_KEY``).
        nonce_store:       Optional asyncio ``NonceStore`` for distributed replay
                           protection (``redis.asyncio`` pooled).  If ``None``, only
                           timestamp-based replay protection is active unless
                           ``nonce_required=True`` in prod (which then rejects).
        nonce_required:    Whether a ``X-NCE-Nonce`` header is mandatory when
                           Redis is reachable.  Defaults to
                           ``cfg.NCE_HMAC_NONCE_REQUIRED`` (``True``).
    """

    def __init__(
        self,
        app: Any,
        *,
        protected_prefix: str = "/api/",
        api_key: str = "",
        nonce_store: NonceStore | None = None,
        nonce_required: bool | None = None,
    ) -> None:
        super().__init__(app)
        self._protected_prefix = protected_prefix
        self._api_key = api_key
        self._nonce_store = nonce_store
        self._nonce_required: bool = (
            cfg.NCE_HMAC_NONCE_REQUIRED if nonce_required is None else nonce_required
        )
        if not api_key:
            log.warning(
                "HMACAuthMiddleware initialised with empty api_key — "
                "all protected routes will return 401 until NCE_API_KEY is set"
            )
        if nonce_store is None:
            log.info(
                "HMACAuthMiddleware: no NonceStore provided — "
                "replay protection is timestamp-only (single-instance mode)"
            )

    # ------------------------------------------------------------------
    # Dispatch helpers (extracted per Clean Code — Prompt 24)
    # ------------------------------------------------------------------

    async def _extract_hmac_context(self, request: Request) -> HMACAuthContext | JSONResponse:
        """Extract and validate X-NCE-Timestamp + Authorization headers.

        Returns an ``HMACAuthContext`` on success, or a JSON-RPC error response.
        """
        timestamp_raw = request.headers.get("x-nce-timestamp", "").strip()
        auth_header = request.headers.get("authorization", "").strip()

        if not timestamp_raw or not auth_header:
            return _jsonrpc_error(
                _CODE_AUTH_FAILED, "Authentication failed", "missing_auth_headers"
            )

        scheme, _, sig_value = auth_header.partition(" ")
        if scheme.upper() != "HMAC-SHA256" or not sig_value.strip():
            return _jsonrpc_error(
                _CODE_AUTH_FAILED,
                "Authentication failed",
                "invalid_authorization_scheme",
            )

        try:
            return HMACAuthContext(
                timestamp=int(timestamp_raw),
                signature=sig_value.strip(),
            )
        except (ValueError, Exception) as exc:
            log.warning("HMAC header validation error: %s", exc)
            return _jsonrpc_error(
                _CODE_AUTH_FAILED, "Authentication failed", "malformed_auth_headers"
            )

    def _verify_timestamp(self, ctx: HMACAuthContext) -> JSONResponse | None:
        """Replay protection — reject if timestamp is outside drift window."""
        now = int(time.time())
        if abs(now - ctx.timestamp) > _TIMESTAMP_DRIFT_SECONDS:
            log.warning(
                "HMAC replay check failed: request_ts=%d server_ts=%d delta=%d",
                ctx.timestamp,
                now,
                abs(now - ctx.timestamp),
            )
            return _jsonrpc_error(
                _CODE_REPLAY,
                "Request timestamp out of acceptable range",
                "replay_or_clock_skew",
            )
        return None

    def _verify_signature(
        self,
        request: Request,
        ctx: HMACAuthContext,
        body_bytes: bytes,
    ) -> JSONResponse | None:
        """HMAC-SHA256 signature verification."""
        if not verify_hmac(
            self._api_key,
            request.method,
            request.url.path,
            ctx.timestamp,
            body_bytes,
            ctx.signature,
        ):
            log.warning(
                "HMAC signature mismatch: method=%s path=%s",
                request.method,
                request.url.path,
            )
            return _jsonrpc_error(_CODE_AUTH_FAILED, "Authentication failed", "invalid_signature")
        return None

    async def _check_nonce(
        self,
        ctx: HMACAuthContext,
        nonce_value: str,
    ) -> JSONResponse | None:
        """Mandatory nonce enforcement + distributed replay check.

        Behaviour matrix (``nonce_required=True``):
          - Redis up  + nonce present  → SETNX; reject if already seen.
          - Redis up  + nonce absent   → reject (nonce_missing).
          - Redis down + prod          → reject (fail-closed).
          - Redis down + dev/test      → allow with WARNING log.

        When ``nonce_required=False``:
          - nonce_store absent → skip (timestamp-only protection).
          - nonce_store present + nonce present → check replay as before.
          - nonce_store present + nonce absent → skip (optional).
        """
        if not self._nonce_required:
            # Legacy / opt-out path: only check if store present and nonce provided
            if self._nonce_store is not None and nonce_value:
                if not await self._nonce_store.check_and_store(nonce_value):
                    log.warning("HMAC nonce replay detected: nonce=%s...", nonce_value[:16])
                    return _jsonrpc_error(
                        _CODE_REPLAY,
                        "Request already processed (replay detected)",
                        "replay_nonce_conflict",
                    )
            return None

        # --- nonce_required=True path ---
        if self._nonce_store is None:
            # No Redis store configured at all
            if cfg.IS_PROD:
                log.error(
                    "HMAC nonce required but NonceStore is not configured — rejecting (prod fail-closed)"
                )
                return _jsonrpc_error(
                    _CODE_REPLAY,
                    "Authentication failed",
                    "nonce_store_unavailable",
                )
            log.warning(
                "HMAC nonce required but NonceStore is not configured — allowing in dev/test"
            )
            return None

        # Store is configured; probe reachability
        redis_up = await self._nonce_store.ping()

        if not redis_up:
            if cfg.IS_PROD:
                log.error(
                    "HMAC nonce required but Redis is unreachable — rejecting (prod fail-closed)"
                )
                return _jsonrpc_error(
                    _CODE_REPLAY,
                    "Authentication failed",
                    "nonce_store_unavailable",
                )
            log.warning("HMAC nonce required but Redis is unreachable — allowing in dev/test")
            return None

        # Redis is up: nonce header must be present
        if not nonce_value:
            log.warning("HMAC nonce required but X-NCE-Nonce header is missing")
            return _jsonrpc_error(
                _CODE_AUTH_FAILED,
                "Authentication failed",
                "nonce_missing",
            )

        # Replay check: SET NX
        if not await self._nonce_store.check_and_store(nonce_value):
            log.warning("HMAC nonce replay detected: nonce=%s...", nonce_value[:16])
            return _jsonrpc_error(
                _CODE_REPLAY,
                "Request already processed (replay detected)",
                "replay_nonce_conflict",
            )

        return None

    def _resolve_namespace_context(self, request: Request) -> NamespaceContext | JSONResponse:
        """Extract namespace_id + agent_id from headers, store on request.state."""
        try:
            ns_id = resolve_namespace(dict(request.headers))
            agent_id = validate_agent_id(request.headers.get("x-nce-agent-id", "default"))
            ns_ctx = NamespaceContext(namespace_id=ns_id, agent_id=agent_id)
            request.state.namespace_ctx = ns_ctx
            return ns_ctx
        except (ValueError, ValidationError) as exc:
            log.warning("Namespace resolution failed: %s", exc)
            return _jsonrpc_error(_CODE_INVALID_NAMESPACE, "Invalid namespace context", str(exc))

    async def dispatch(self, request: Request, call_next: Any) -> Any:
        """HMAC-SHA256 authentication middleware entry point."""
        if not request.url.path.startswith(self._protected_prefix):
            return await call_next(request)

        if not self._api_key:
            log.error("HMAC auth rejected: NCE_API_KEY is empty (server misconfigured)")
            return _jsonrpc_error(
                _CODE_AUTH_FAILED, "Authentication failed", "server_misconfigured"
            )

        # Step 1: Extract + validate headers
        ctx_or_err = await self._extract_hmac_context(request)
        if isinstance(ctx_or_err, JSONResponse):
            return ctx_or_err
        ctx: HMACAuthContext = ctx_or_err

        # Step 2: Timestamp replay protection
        ts_err = self._verify_timestamp(ctx)
        if ts_err is not None:
            return ts_err

        # Step 3: Read body + verify signature
        body_bytes = await request.body()
        sig_err = self._verify_signature(request, ctx, body_bytes)
        if sig_err is not None:
            return sig_err

        # Step 4: Nonce enforcement + distributed replay check
        nonce_value = request.headers.get("x-nce-nonce", "").strip()
        nonce_err = await self._check_nonce(ctx, nonce_value)
        if nonce_err is not None:
            return nonce_err

        # Step 5: Resolve namespace context
        ns_result = self._resolve_namespace_context(request)
        if isinstance(ns_result, JSONResponse):
            return ns_result

        return await call_next(request)


class BasicAuthMiddleware(BaseHTTPMiddleware):
    """HTTP Basic auth for non-API admin UI routes."""

    def __init__(
        self,
        app: Any,
        *,
        protected_prefix: str = "/",
        username: str = "",
        password: str = "",
        excluded_prefixes: tuple[str, ...] = ("/api/",),
        realm: str = "NCE Admin",
    ) -> None:
        super().__init__(app)
        self._protected_prefix = protected_prefix
        self._excluded_prefixes = excluded_prefixes
        self._username = username
        # password may be a $pbkdf2$ hash (OWASP 2026) or plaintext (deprecated).
        # Plaintext passwords are compared with constant-time digest and logged.
        self._password = password
        self._realm = realm
        if not username or not password:
            log.warning(
                "BasicAuthMiddleware initialised with empty credentials — "
                "all protected UI routes will return 401 until configured"
            )
        elif not password.startswith(_PBKDF2_HASH_FORMAT_PREFIX):
            log.warning(
                "BasicAuthMiddleware password is plaintext — "
                "migrate to PBKDF2-HMAC-SHA256 ($pbkdf2$ prefix) for OWASP 2026 compliance.  "
                "Generate with: nce.auth.hash_admin_password('<password>')"
            )

    def _challenge(self) -> Response:
        return Response(
            status_code=401,
            headers={"WWW-Authenticate": f'Basic realm="{self._realm}"'},
        )

    async def dispatch(self, request: Request, call_next: Any) -> Any:
        if cfg.NCE_ADMIN_OVERRIDE:
            return await call_next(request)

        path = request.url.path
        if not path.startswith(self._protected_prefix):
            return await call_next(request)
        if any(path.startswith(prefix) for prefix in self._excluded_prefixes):
            return await call_next(request)

        if not self._username or not self._password:
            return self._challenge()

        auth_header = request.headers.get("authorization", "").strip()
        scheme, _, token = auth_header.partition(" ")
        if scheme.lower() != "basic" or not token:
            return self._challenge()

        try:
            decoded = b64decode(token).decode("utf-8")
            username, _, password = decoded.partition(":")
        except (ValueError, BinasciiError, UnicodeDecodeError):
            return self._challenge()

        valid_pw, _ = await verify_admin_password_async(
            password, self._password, auto_upgrade=False
        )
        if not (secrets.compare_digest(username, self._username) and valid_pw):
            return self._challenge()

        return await call_next(request)


# ---------------------------------------------------------------------------
# Admin HTTP rate limiting (Starlette admin_server)
# ---------------------------------------------------------------------------

_ADMIN_HTTP_SENSITIVE_PREFIXES: tuple[str, ...] = (
    "/api/admin/",
    "/api/gc/",
    "/api/replay/",
    "/api/snapshot/",
    "/api/a2a/",
    "/api/search",
)


def _admin_http_client_ip(request: Request) -> str:
    """Client IP for admin HTTP rate limiting (direct connection only)."""
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def _admin_http_is_sensitive(path: str, method: str) -> bool:
    if method.upper() != "POST":
        return False
    return any(path.startswith(prefix) for prefix in _ADMIN_HTTP_SENSITIVE_PREFIXES)


async def _check_admin_http_rate_limit(
    redis_client: Any | None,
    key: str,
    limit: int,
    period: int,
) -> bool:
    if redis_client is not None:
        try:
            now = time.time()
            clear_before = now - period
            result = await redis_client.eval(
                _RATE_LIMIT_LUA,
                1,
                key,
                str(clear_before),
                str(now),
                str(limit),
                str(period),
            )
            return bool(result)
        except Exception as exc:
            log.warning("Admin HTTP Redis rate limiter failed, falling back to RAM: %s", exc)
    return _check_in_memory_rate_limit(key, limit, period)


class AdminHTTPRateLimitMiddleware(BaseHTTPMiddleware):
    """Per-IP sliding-window rate limits for admin_server HTTP routes."""

    async def dispatch(self, request: Request, call_next: Any) -> Any:
        path = request.url.path
        if path == "/healthz":
            return await call_next(request)

        sensitive = _admin_http_is_sensitive(path, request.method)
        limit = (
            cfg.NCE_ADMIN_HTTP_SENSITIVE_RATE_LIMIT if sensitive else cfg.NCE_ADMIN_HTTP_RATE_LIMIT
        )
        period = (
            cfg.NCE_ADMIN_HTTP_SENSITIVE_RATE_PERIOD
            if sensitive
            else cfg.NCE_ADMIN_HTTP_RATE_PERIOD
        )
        client_ip = _admin_http_client_ip(request)
        key = f"nce:ratelimit:admin:http:{client_ip}:{'sensitive' if sensitive else 'general'}"

        redis_client = getattr(request.app.state, "redis_client", None)
        allowed = await _check_admin_http_rate_limit(redis_client, key, limit, period)
        if not allowed:
            log.warning(
                "Admin HTTP rate limit exceeded ip=%s path=%s method=%s",
                client_ip,
                path,
                request.method,
            )
            return JSONResponse(
                status_code=429,
                content={"error": "Rate limit exceeded"},
            )

        return await call_next(request)
