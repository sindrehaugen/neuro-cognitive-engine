"""Reusable mTLS client-certificate middleware for Starlette/ASGI apps.

Extracted from ``a2a_server.py`` (B6) so the same middleware can protect
both the A2A server and the Admin server without duplication.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from uuid import UUID

from starlette.datastructures import Headers
from starlette.responses import JSONResponse

from nce.a2a import A2AMTLSError, mtls_enforce

if TYPE_CHECKING:
    import asyncpg

log = logging.getLogger("nce.mtls")


class MTLSNotConfiguredError(RuntimeError):
    """Raised when mTLS strict mode is enabled but certificate paths are absent.

    Set ``NCE_MTLS_STRICT=false`` (or the per-service equivalent) to disable
    strict enforcement in local/dev environments — a warning is logged instead.
    """


def assert_bridge_mtls_configured(*, service: str = "bridge") -> None:
    """Assert that mTLS cert paths are set for bridge ingestion adapters.

    Reads ``NCE_MTLS_STRICT`` (default ``true``) and the three cert-path env
    vars.  In strict mode, raises ``MTLSNotConfiguredError`` if any path is
    missing.  In non-strict mode, logs a WARNING and returns.

    Call this from ``BridgeProvider.__init__()`` so every bridge fails fast at
    construction time rather than at runtime when a connection is attempted.
    """
    from nce.config import cfg

    strict = cfg.NCE_MTLS_STRICT
    cert = cfg.NCE_MTLS_CERT_PATH
    key = cfg.NCE_MTLS_KEY_PATH
    ca = cfg.NCE_MTLS_CA_PATH

    missing = [
        name
        for name, val in (
            ("NCE_MTLS_CERT_PATH", cert),
            ("NCE_MTLS_KEY_PATH", key),
            ("NCE_MTLS_CA_PATH", ca),
        )
        if not val
    ]

    if not missing:
        return

    msg = (
        f"mTLS cert paths not configured for {service!r} adapter. "
        f"Missing: {', '.join(missing)}. "
        "Set NCE_MTLS_CERT_PATH, NCE_MTLS_KEY_PATH, and NCE_MTLS_CA_PATH, "
        "or set NCE_MTLS_STRICT=false to disable strict enforcement."
    )
    if strict:
        raise MTLSNotConfiguredError(msg)
    log.warning("mTLS not configured (NCE_MTLS_STRICT=false): %s", msg)


# System-level namespace sentinel for non-tenant-scoped boot audit events.
# Mirrors nce.migration_gate._SYSTEM_NAMESPACE (nil UUID == "no namespace").
_SYSTEM_NAMESPACE: UUID = UUID("00000000-0000-0000-0000-000000000000")


async def assert_server_mtls_or_acknowledged(
    *,
    service: str,
    mtls_enabled: bool,
    pg_pool: asyncpg.Pool | None = None,
) -> None:
    """Boot-time zero-trust transport guard for a public server surface.

    Called from the admin / a2a server lifespans.  When running in production
    (``cfg.IS_PROD``) with this server's mTLS middleware *disabled*, refuse to
    boot unless an operator has explicitly acknowledged the weakened posture via
    ``NCE_MTLS_ACKNOWLEDGE_DISABLED=true``.

    Behaviour
    ---------
    * Non-prod, OR mTLS enabled  -> no-op (silent).
    * Prod + disabled + unacknowledged  -> raise :class:`MTLSNotConfiguredError`
      (server must not start).
    * Prod + disabled + acknowledged  -> log ``CRITICAL`` and append a WORM
      audit event, then return so the server continues to boot.

    This is a *boot-time* guard only — it never alters the request-path
    behaviour of :class:`MTLSAuthMiddleware`.

    Parameters
    ----------
    service:
        Short surface name for log / audit context (e.g. ``"admin"``, ``"a2a"``).
    mtls_enabled:
        Whether this server's mTLS middleware is enabled
        (``cfg.NCE_ADMIN_MTLS_ENABLED`` / ``cfg.NCE_A2A_MTLS_ENABLED``).
    pg_pool:
        Optional pool used to write the WORM acknowledgement event on the
        acknowledged path.  When ``None`` the CRITICAL log is still emitted but
        no event is written (the guard never blocks boot on an audit-write
        failure once the operator has acknowledged).
    """
    from nce.config import cfg

    if not cfg.IS_PROD or mtls_enabled:
        return

    if not cfg.NCE_MTLS_ACKNOWLEDGE_DISABLED:
        raise MTLSNotConfiguredError(
            f"Refusing to start {service!r} server: NCE_ENV is production but mTLS "
            "is disabled for this surface (zero-trust transport is not enforced). "
            "Enable mTLS (set the per-service NCE_*_MTLS_ENABLED + trust anchors), "
            "or set NCE_MTLS_ACKNOWLEDGE_DISABLED=true to explicitly accept the "
            "weakened posture."
        )

    # Acknowledged path: never silent — emit a CRITICAL log and a WORM event.
    log.critical(
        "mTLS DISABLED in production for %r surface — booting anyway because "
        "NCE_MTLS_ACKNOWLEDGE_DISABLED=true. Zero-trust transport is NOT enforced.",
        service,
    )

    if pg_pool is None:
        return

    # Best-effort immutable acknowledgement record. Reuses the existing
    # ``config_changed`` WORM event type (provenance-only fork projection) so no
    # new event_type / replay handler is required. The ``changes`` key is the
    # env-var name (NOT a registered setting), so this row is inert to the
    # admin settings-history folder. No secrets / PII are recorded.
    try:
        from nce.event_log import append_event

        async with pg_pool.acquire(timeout=10.0) as audit_conn:
            async with audit_conn.transaction():
                await append_event(
                    conn=audit_conn,
                    namespace_id=_SYSTEM_NAMESPACE,
                    agent_id="system",
                    event_type="config_changed",
                    params={
                        "actor": "system",
                        "reason": "mtls_disabled_acknowledged",
                        "changes": {
                            "NCE_MTLS_ACKNOWLEDGE_DISABLED": {
                                "service": service,
                                "mtls_enabled": False,
                                "environment": cfg.ENVIRONMENT,
                            }
                        },
                    },
                )
    except Exception:
        # The operator has already acknowledged the disabled posture; an
        # audit-write failure must not block boot. Surface it loudly instead.
        log.exception(
            "Failed to record mtls_disabled_acknowledged WORM event for %r surface",
            service,
        )


# Default JSON-RPC error code for mTLS failures
DEFAULT_MTLS_ERROR_CODE = -32015

_MAX_HEADER_VALUE_BYTES: int = 16_384  # 16 KB — generous for base64-encoded DER certs


class MTLSAuthMiddleware:
    """
    Starlette ASGI middleware that enforces mTLS client certificate validation.

    Parameters
    ----------
    app
        The downstream ASGI application.
    protected_prefix : str
        URL paths starting with this prefix are protected (default ``"/"``).
    enabled : bool
        Whether mTLS enforcement is active.  When ``False`` the middleware
        is a no-op pass-through.
    strict : bool
        If ``True``, missing client certificates raise an error.
        If ``False``, missing certificates are allowed (useful for
        rolling deployments).
    trusted_proxy_hops : int
        Number of trusted reverse-proxy hops in front of the server.
        When > 0, the middleware inspects ``X-Forwarded-*`` headers.
    allowed_sans : list[str]
        Allowed Subject Alternative Names (lower-cased DNS names).
    allowed_fingerprints : list[str]
        Allowed certificate SHA-256 fingerprints (colon-separated hex).
    error_code : int
        JSON-RPC error code returned on mTLS failures (default ``-32010``).
    """

    def __init__(
        self,
        app,
        *,
        protected_prefix: str = "/",
        enabled: bool = False,
        strict: bool = True,
        trusted_proxy_hops: int = 0,
        allowed_sans: list[str] | None = None,
        allowed_fingerprints: list[str] | None = None,
        error_code: int = DEFAULT_MTLS_ERROR_CODE,
    ) -> None:
        self.app = app
        self.protected_prefix = protected_prefix
        self.enabled = enabled
        self.strict = strict
        self.trusted_proxy_hops = trusted_proxy_hops
        self.allowed_sans = [s.lower().strip() for s in (allowed_sans or [])]
        self.allowed_fingerprints = [f.lower().strip() for f in (allowed_fingerprints or [])]
        self.error_code = error_code

        if self.enabled and not self.allowed_sans and not self.allowed_fingerprints:
            raise ValueError(
                "MTLSAuthMiddleware: enabled=True but no trust anchors configured. "
                "Provide at least one allowed_sans or allowed_fingerprints entry."
            )
        if not self.enabled:
            log.warning(
                "MTLSAuthMiddleware: mTLS is DISABLED for prefix %s — "
                "all requests will pass through without certificate validation",
                self.protected_prefix,
            )

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or not self.enabled:
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        prefix = self.protected_prefix
        if not (path == prefix or path.startswith(prefix.rstrip("/") + "/")):
            await self.app(scope, receive, send)
            return

        headers_obj = Headers(scope=scope)
        headers: dict[str, str] = {}
        for key, value in headers_obj.items():
            if len(value) > _MAX_HEADER_VALUE_BYTES:
                log.warning(
                    "mTLS: oversized header dropped name=%s len=%d path=%s",
                    key[:32],
                    len(value),
                    path,
                )
                continue
            headers[key.lower()] = value

        try:
            mtls_enforce(
                scope=scope,
                headers=headers,
                enabled=True,
                strict=self.strict,
                trusted_proxy_hops=self.trusted_proxy_hops,
                allowed_sans=self.allowed_sans,
                allowed_fingerprints=self.allowed_fingerprints,
            )
        except A2AMTLSError as exc:
            log.warning(
                "mTLS rejection: path=%s reason=%s client_ip=%s",
                path,
                exc,
                scope.get("client", ("unknown", 0))[0],
            )
            request_id = headers.get("x-request-id")
            response = JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "error": {
                        "code": self.error_code,
                        "message": "mTLS client certificate validation failed",
                        "data": {"reason": "mtls_validation_failed"},
                    },
                    "id": request_id,
                },
                status_code=401,
                headers={"WWW-Authenticate": "TLS"},
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)
