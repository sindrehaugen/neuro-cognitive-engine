"""
nce/jwt_auth.py

Phase 0.2 — JWT Bridge: Bearer-token middleware for agent-scoped API endpoints.

Public API (imported by admin_server.py, a2a_server.py, or any Starlette app):
  JWTAuthMiddleware   — Starlette middleware; validates ``Authorization: Bearer``
                        tokens and attaches a ``NamespaceContext`` to
                        ``request.state.namespace_ctx``.
  decode_agent_token  — Callable helper for non-middleware use (e.g. WebSocket
                        handshake or background task runners).

Integration with the HMAC core
-------------------------------
This module is **additive** to ``nce.auth``.  It does not replace the
``HMACAuthMiddleware``; it provides a parallel auth path for *agent-facing*
endpoints that authenticate with short-lived JWT Bearer tokens rather than a
shared HMAC secret.

Both middlewares produce the same ``NamespaceContext`` (from ``nce.auth``),
so the orchestrator write path receives identical identity objects regardless
of which auth scheme was used.  Typical stack order::

    app.add_middleware(HMACAuthMiddleware, protected_prefix="/api/admin/", ...)
    app.add_middleware(JWTAuthMiddleware,  protected_prefix="/api/v1/",    ...)

JWT Claims (NCE namespace)
------------------------------
Required:
  namespace_id  (str, UUID format)  — maps to ``NamespaceContext.namespace_id``
Optional:
  agent_id      (str, max 128 chars) — maps to ``NamespaceContext.agent_id``;
                                       falls back to ``"default"`` when absent.
Standard claims:
  exp           — token expiry; validated automatically by PyJWT.
  iat           — issued-at; no additional check beyond PyJWT defaults.
  iss           — issuer; validated when ``NCE_JWT_ISSUER`` is configured.
  aud           — audience; validated when ``NCE_JWT_AUDIENCE`` is configured.
  sub           — subject; informational only, not used for auth decisions.
  jti           — JWT ID; available for caller-side replay detection if needed.

Supported algorithms
---------------------
  Symmetric (HMAC):   HS256, HS384, HS512.  Set ``NCE_JWT_SECRET``.
                      Suitable for development and internal service-to-service.
  Asymmetric (RSA):   RS256, RS384, RS512.  Set ``NCE_JWT_PUBLIC_KEY`` (PEM).
  Asymmetric (ECDSA): ES256, ES384, ES512.  Set ``NCE_JWT_PUBLIC_KEY`` (PEM).
  Asymmetric (PSS):   PS256, PS384, PS512.  Set ``NCE_JWT_PUBLIC_KEY`` (PEM).

``NCE_JWT_PUBLIC_KEY`` may be:
  - A raw PEM string (begins with ``-----BEGIN``), OR
  - A ``file://`` URI pointing to a PEM file on disk.

When both ``NCE_JWT_PUBLIC_KEY`` and ``NCE_JWT_SECRET`` are set, the
public key takes precedence (asymmetric algorithms are preferred in prod).

JSON-RPC 2.0 error codes (server-defined range, extends nce.auth)
----------------------------------------------------------------------
  -32005  JWT validation failed  (expired, bad signature, decode error)
  -32006  JWT missing required claim  (``namespace_id`` absent)
  -32007  JWT claim invalid  (``namespace_id`` not a valid UUID)
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any
from uuid import UUID

import jwt  # PyJWT >= 2.8
from jwt.exceptions import (
    DecodeError,
    ExpiredSignatureError,
    InvalidAudienceError,
    InvalidIssuerError,
    InvalidTokenError,
    MissingRequiredClaimError,
)
from pydantic import ValidationError
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from nce.auth import NamespaceContext, jsonrpc_error_response, validate_agent_id
from nce.config import cfg

log = logging.getLogger("nce.jwt_auth")

# ---------------------------------------------------------------------------
# JSON-RPC 2.0 error codes (JWT-specific, extends nce.auth -32001..-32004)
# ---------------------------------------------------------------------------

_CODE_JWT_INVALID: int = -32005  # expired / bad signature / decode error
_CODE_JWT_MISSING_CLAIM: int = -32006  # namespace_id absent
_CODE_JWT_BAD_CLAIM: int = -32007  # namespace_id not a valid UUID

_HTTP_UNAUTHORIZED: int = 401
_HTTP_BAD_REQUEST: int = 400

# Algorithms that require an asymmetric public key
_ASYMMETRIC_ALGORITHMS: frozenset[str] = frozenset(
    {"RS256", "RS384", "RS512", "ES256", "ES384", "ES512", "PS256", "PS384", "PS512"}
)
_HMAC_ALGORITHMS: frozenset[str] = frozenset({"HS256", "HS384", "HS512"})


# ---------------------------------------------------------------------------
# Helpers — JSON-RPC 2.0 error responses (same shape as nce.auth)
# ---------------------------------------------------------------------------


def _jsonrpc_error(
    code: int,
    message: str,
    reason: str,
    request_id: Any = None,
) -> JSONResponse:
    """Build a strict JSON-RPC 2.0 error response.

    HTTP status:
      401 — authentication / token errors (-32005, -32006, -32007)
      400 — only for malformed claim values outside the UUID check

    401 responses include ``WWW-Authenticate: Bearer realm="nce"``
    per RFC 6750 §3 so HTTP clients know the expected scheme.
    """
    headers: dict[str, str] = {}
    if code in (_CODE_JWT_INVALID, _CODE_JWT_MISSING_CLAIM, _CODE_JWT_BAD_CLAIM):
        headers["WWW-Authenticate"] = 'Bearer realm="nce"'
    return jsonrpc_error_response(
        code,
        message,
        reason,
        headers=headers,
        request_id=request_id,
    )


# ---------------------------------------------------------------------------
# Key loading
# ---------------------------------------------------------------------------


@lru_cache(maxsize=4)
def _load_public_key(raw: str) -> str:
    """Resolve a public key from an env-var value.

    Accepts:
      - Raw PEM string (starts with ``-----BEGIN``).
      - ``file:///absolute/path/to/key.pem`` URI.

    The file path is validated against an allowed base directory
    (``NCE_JWT_KEY_DIR`` env var, defaults to CWD) to prevent
    path traversal.

    Returns the PEM string.
    Raises ``ValueError`` on unresolvable input.
    """
    from pathlib import Path

    stripped = raw.strip()
    if stripped.startswith("-----"):
        return stripped
    if stripped.startswith("file://"):
        path_str = stripped[len("file://") :]
        key_path = Path(path_str).resolve(strict=False)

        # Validate against allowed base directory
        allowed_dir_raw = cfg.NCE_JWT_KEY_DIR
        try:
            allowed_base = Path(allowed_dir_raw).resolve(strict=True)
        except FileNotFoundError as exc:
            raise ValueError(f"NCE_JWT_KEY_DIR does not exist: {allowed_dir_raw!r}") from exc

        if not key_path.is_relative_to(allowed_base):
            raise ValueError(f"NCE_JWT_PUBLIC_KEY path escapes allowed directory: {path_str!r}")

        if not key_path.is_file():
            raise ValueError(f"NCE_JWT_PUBLIC_KEY file not found: {path_str!r}")
        return key_path.read_text(encoding="utf-8").strip()
    raise ValueError(
        f"NCE_JWT_PUBLIC_KEY must be a PEM string or a file:// URI; got: {stripped[:40]!r}…"
    )


def _build_jwt_key(algorithm: str) -> Any:
    """Return the key object / string to pass to ``jwt.decode()``.

    Priority:
      1. ``NCE_JWT_PUBLIC_KEY`` — asymmetric algorithms only.
      2. ``NCE_JWT_SECRET``     — HMAC algorithms only.

    Raises ``RuntimeError`` on server misconfiguration:
      - public key set but algorithm is not asymmetric
      - asymmetric algorithm but no public key
      - unrecognised algorithm
      - no key configured at all
    """
    if cfg.NCE_JWT_PUBLIC_KEY:
        if algorithm not in _ASYMMETRIC_ALGORITHMS:
            raise RuntimeError(
                f"NCE_JWT_PUBLIC_KEY is set but algorithm {algorithm!r} is not "
                f"asymmetric. Use one of: {sorted(_ASYMMETRIC_ALGORITHMS)}"
            )
        return _load_public_key(cfg.NCE_JWT_PUBLIC_KEY)
    if algorithm in _ASYMMETRIC_ALGORITHMS:
        raise RuntimeError(
            f"Algorithm {algorithm!r} requires NCE_JWT_PUBLIC_KEY to be set but it is empty."
        )
    if algorithm not in _HMAC_ALGORITHMS:
        raise RuntimeError(
            f"Unsupported JWT algorithm {algorithm!r}. "
            f"Supported: {sorted(_ASYMMETRIC_ALGORITHMS | _HMAC_ALGORITHMS)}"
        )
    if cfg.NCE_JWT_SECRET:
        return cfg.NCE_JWT_SECRET
    raise RuntimeError(
        "JWT key not configured: set NCE_JWT_SECRET (HS256/HS384/HS512) or "
        "NCE_JWT_PUBLIC_KEY (RS256/RS384/RS512/ES256/ES384/ES512/PS256/PS384/PS512)."
    )


# ---------------------------------------------------------------------------
# Core decode + claim extraction
# ---------------------------------------------------------------------------


class JWTDecodeError(Exception):
    """Wraps PyJWT errors with structured metadata for middleware handling."""

    def __init__(self, code: int, message: str, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.message = message
        self.reason = reason


def decode_agent_token(
    token: str,
    audience: str | None = None,
) -> NamespaceContext:
    """Validate a JWT Bearer token and extract ``NamespaceContext``.

    This is the canonical extraction function used by both the middleware
    and any non-HTTP callers (WebSocket handshakes, background tasks, etc.).

    Args:
        token: Raw JWT string (without the ``Bearer`` prefix).
        audience:
            Expected ``aud`` claim value.  When provided (non-None, non-empty),
            this value is used instead of ``cfg.NCE_JWT_AUDIENCE`` **and**
            is strictly enforced — a token with a different or missing ``aud``
            is rejected with ``InvalidAudienceError``.

            When ``None``, falls back to the global config.  If the resolved
            value is still ``None``, ``aud`` is **not** required or validated
            (PyJWT accepts tokens with or without it).

    Returns:
        A frozen ``NamespaceContext`` with ``namespace_id`` and ``agent_id``.

    Raises:
        JWTDecodeError: On any validation failure; carries JSON-RPC code +
                        human-readable message + machine-readable reason.
        RuntimeError:   On server misconfiguration (missing key).
    """
    algorithm = cfg.NCE_JWT_ALGORITHM
    try:
        key = _build_jwt_key(algorithm)
    except RuntimeError as exc:
        log.error("JWT key build failed (server misconfiguration): %s", exc)
        raise JWTDecodeError(
            _CODE_JWT_INVALID,
            "Authentication failed",
            "jwt_server_misconfigured",
        ) from exc

    resolved_audience = audience if audience is not None else (cfg.NCE_JWT_AUDIENCE or None)
    resolved_issuer = cfg.NCE_JWT_ISSUER or None
    required_claims = ["exp"]
    if resolved_issuer:
        required_claims.append("iss")
    if resolved_audience:
        required_claims.append("aud")
    if cfg.IS_PROD:
        required_claims.extend(["iat", "nbf"])
    decode_options: dict[str, Any] = {"require": required_claims}

    decode_kwargs: dict[str, Any] = {
        "algorithms": [algorithm],
        "options": decode_options,
        "leeway": cfg.NCE_JWT_LEEWAY_SECONDS,
    }
    if resolved_issuer:
        decode_kwargs["issuer"] = resolved_issuer
    if resolved_audience:
        decode_kwargs["audience"] = resolved_audience

    try:
        payload: dict[str, Any] = jwt.decode(token, key, **decode_kwargs)
    except ExpiredSignatureError as exc:
        log.warning("JWT expired: %s", exc)
        raise JWTDecodeError(
            _CODE_JWT_INVALID,
            "Authentication failed",
            "jwt_expired",
        ) from exc
    except MissingRequiredClaimError as exc:
        log.warning("JWT missing standard claim: %s", exc)
        raise JWTDecodeError(
            _CODE_JWT_INVALID,
            "Authentication failed",
            f"jwt_missing_standard_claim:{exc}",
        ) from exc
    except InvalidAudienceError as exc:
        log.warning("JWT audience mismatch: %s", exc)
        raise JWTDecodeError(
            _CODE_JWT_INVALID,
            "Authentication failed",
            "jwt_audience_mismatch",
        ) from exc
    except InvalidIssuerError as exc:
        log.warning("JWT issuer mismatch: %s", exc)
        raise JWTDecodeError(
            _CODE_JWT_INVALID,
            "Authentication failed",
            "jwt_issuer_mismatch",
        ) from exc
    except DecodeError as exc:
        log.warning("JWT decode error: %s", exc)
        raise JWTDecodeError(
            _CODE_JWT_INVALID,
            "Authentication failed",
            "jwt_decode_error",
        ) from exc
    except InvalidTokenError as exc:
        log.warning("JWT invalid: %s", exc)
        raise JWTDecodeError(
            _CODE_JWT_INVALID,
            "Authentication failed",
            "jwt_invalid",
        ) from exc

    # --- Extract NCE-specific claims ---
    raw_ns = payload.get("namespace_id")
    if not raw_ns:
        log.warning("JWT missing 'namespace_id' claim; sub=%s", payload.get("sub"))
        raise JWTDecodeError(
            _CODE_JWT_MISSING_CLAIM,
            "Authentication failed",
            "missing_claim:namespace_id",
        )

    try:
        namespace_id = UUID(str(raw_ns).strip())
    except ValueError as exc:
        log.warning("JWT 'namespace_id' claim is not a valid UUID")
        raise JWTDecodeError(
            _CODE_JWT_BAD_CLAIM,
            "Authentication failed",
            "invalid_claim:namespace_id",
        ) from exc

    raw_agent = payload.get("agent_id")
    agent_id = validate_agent_id(str(raw_agent or "default"))

    # C3 principal-session claims (Wave 23 / IDOR fix).
    # Both fields are sourced exclusively from the verified JWT payload —
    # never from a request header. Absent or invalid values fall to safe defaults.
    raw_kind = payload.get("principal_kind")
    principal_kind = str(raw_kind or "employee").strip().lower()
    if principal_kind not in {"employee", "contractor", "external-customer"}:
        log.debug("JWT principal_kind %r not recognised — defaulting to employee", raw_kind)
        principal_kind = "employee"

    external_scope_id: UUID | None = None
    if principal_kind in {"contractor", "external-customer"}:
        raw_scope = payload.get("external_scope_id")
        if raw_scope:
            try:
                parsed_scope = UUID(str(raw_scope).strip())
                # Reject the nil-UUID sentinel — it is reserved for deny-when-unset.
                if parsed_scope.int != 0:
                    external_scope_id = parsed_scope
                else:
                    log.debug("JWT external_scope_id is the nil-UUID sentinel — ignoring")
            except (ValueError, AttributeError):
                log.debug("JWT external_scope_id %r is not a valid UUID — ignoring", raw_scope)

    try:
        return NamespaceContext(
            namespace_id=namespace_id,
            agent_id=agent_id,
            principal_kind=principal_kind,
            external_scope_id=external_scope_id,
        )
    except ValidationError as exc:
        log.error("NamespaceContext construction failed (unexpected): %s", exc)
        raise JWTDecodeError(
            _CODE_JWT_BAD_CLAIM,
            "Authentication failed",
            "invalid_namespace_context",
        ) from exc


# ---------------------------------------------------------------------------
# Starlette middleware
# ---------------------------------------------------------------------------


class JWTAuthMiddleware(BaseHTTPMiddleware):
    """JWT Bearer token authentication middleware for agent-scoped endpoints.

    Validates ``Authorization: Bearer <token>`` for all requests whose path
    starts with ``protected_prefix``.  On success, the resolved
    ``NamespaceContext`` is attached as ``request.state.namespace_ctx`` so
    downstream route handlers and the orchestrator write path can consume it
    without re-parsing the token.

    Error contract (JSON-RPC 2.0):
      -32005  Any JWT validation failure (expired, bad signature, decode error)
      -32006  ``namespace_id`` claim absent
      -32007  ``namespace_id`` claim is not a valid UUID

    Compatibility:
      This middleware is **not** a replacement for ``HMACAuthMiddleware``.
      Both may be mounted simultaneously on different route prefixes::

          app.add_middleware(HMACAuthMiddleware, protected_prefix="/api/admin/", ...)
          app.add_middleware(JWTAuthMiddleware,  protected_prefix="/api/v1/",    ...)

    Args:
        app:               The ASGI application to wrap.
        protected_prefix:  URL path prefix that requires JWT authentication.
                           Defaults to ``cfg.NCE_JWT_PREFIX`` (``/api/v1/``).
        expected_audience:
            Expected ``aud`` claim value for this service.  When set, tokens
            whose ``aud`` does not match are rejected — prevents replay of
            tokens issued for other services (web frontend, admin UI, etc.)
            against this endpoint.

            Falls back to ``cfg.NCE_JWT_AUDIENCE`` when ``None``.
    """

    def __init__(
        self,
        app: Any,
        *,
        protected_prefix: str | None = None,
        expected_audience: str | None = None,
    ) -> None:
        super().__init__(app)
        self._protected_prefix: str = (
            protected_prefix if protected_prefix is not None else cfg.NCE_JWT_PREFIX
        )
        self._expected_audience: str | None = expected_audience
        # Eagerly check key availability; log once at startup rather than per-request.
        try:
            _build_jwt_key(cfg.NCE_JWT_ALGORITHM)
        except RuntimeError as exc:
            log.warning(
                "JWTAuthMiddleware: %s — all protected routes under %r will "
                "return 401 until the key is configured.",
                exc,
                self._protected_prefix,
            )

    async def dispatch(self, request: Request, call_next: Any) -> Any:
        if not request.url.path.startswith(self._protected_prefix):
            return await call_next(request)

        auth_header = request.headers.get("authorization", "").strip()
        if not auth_header:
            log.debug(
                "JWT auth rejected: no Authorization header for %s %s",
                request.method,
                request.url.path,
            )
            return _jsonrpc_error(
                _CODE_JWT_INVALID,
                "Authentication failed",
                "missing_authorization_header",
            )

        scheme, _, token_value = auth_header.partition(" ")
        if scheme.lower() != "bearer" or not token_value.strip():
            log.debug(
                "JWT auth rejected: expected 'Bearer <token>', got scheme=%r for %s %s",
                scheme,
                request.method,
                request.url.path,
            )
            return _jsonrpc_error(
                _CODE_JWT_INVALID,
                "Authentication failed",
                "invalid_authorization_scheme",
            )

        token_value = token_value.strip()
        if len(token_value) > 8192:
            log.debug(
                "JWT auth rejected: token exceeds size limit (%d bytes) for %s %s",
                len(token_value),
                request.method,
                request.url.path,
            )
            return _jsonrpc_error(
                _CODE_JWT_INVALID,
                "Authentication failed",
                "jwt_too_large",
            )

        try:
            namespace_ctx = decode_agent_token(
                token_value,
                audience=self._expected_audience,
            )
        except JWTDecodeError as exc:
            return _jsonrpc_error(exc.code, exc.message, exc.reason)

        request.state.namespace_ctx = namespace_ctx
        log.debug(
            "JWT auth OK: namespace=%s agent=%s path=%s",
            namespace_ctx.namespace_id,
            namespace_ctx.agent_id,
            request.url.path,
        )
        return await call_next(request)


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------

__all__ = [
    "JWTAuthMiddleware",
    "JWTDecodeError",
    "decode_agent_token",
]
