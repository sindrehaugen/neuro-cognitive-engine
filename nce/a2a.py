"""
Phase 3.1 — A2A (Agent-to-Agent) Protocol

Facilitates secure memory sharing between entirely separate AI agents.
Provides a cryptographic handshake allowing Agent A to grant read scopes
to Agent B for specific namespaces, memories, KG nodes, or subgraphs.

JSON-RPC 2.0 error codes defined here:
  -32010  Unauthorized — token missing, invalid, expired, or revoked
  -32011  Scope violation — resource not covered by the granted scopes
  -32012  Bad skill / bad request parameters
"""

import hashlib
import json
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from uuid import UUID, uuid4

try:
    from cryptography import x509 as crypto_x509

    _CRYPTOGRAPHY_AVAILABLE = True
except ImportError:  # pragma: no cover
    _CRYPTOGRAPHY_AVAILABLE = False

import asyncpg  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field

from nce.auth import NamespaceContext, set_namespace_context

log = logging.getLogger("nce.a2a")


async def _append_a2a_event(
    conn: asyncpg.Connection,
    owner_ctx: NamespaceContext,
    event_type: str,
    params: dict,
) -> None:
    """Helper to set namespace context and append a2a audit event cleanly."""
    assert owner_ctx.namespace_id is not None, (
        "Namespace ID cannot be None when writing an A2A event"
    )
    await set_namespace_context(conn, owner_ctx.namespace_id)
    from nce.event_log import append_event

    await append_event(
        conn=conn,
        namespace_id=owner_ctx.namespace_id,
        agent_id=owner_ctx.agent_id,
        event_type=event_type,
        params=params,
    )


# ---------------------------------------------------------------------------
# JSON-RPC 2.0 error codes
# ---------------------------------------------------------------------------
A2A_CODE_UNAUTHORIZED = -32010  # token invalid / expired / revoked / constraints
A2A_CODE_SCOPE_VIOLATION = -32011  # resource not within granted scopes
A2A_CODE_BAD_REQUEST = -32012  # missing / invalid parameters


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


class A2AScope(BaseModel):
    """Defines the resource type and permissions granted in an A2A share."""

    model_config = ConfigDict(extra="forbid")

    resource_type: Literal["namespace", "memory", "kg_node", "subgraph"] = Field(
        ..., description="Type of resource being shared"
    )
    resource_id: str = Field(..., description="UUID or label of the resource")
    permissions: list[Literal["read"]] = Field(
        default=["read"],
        description="Granted permissions (currently only 'read' is supported)",
    )


class A2AGrantRequest(BaseModel):
    """Request payload to create a new A2A sharing grant."""

    model_config = ConfigDict(extra="forbid")

    target_namespace_id: UUID | None = Field(
        None,
        description="Restrict to a specific receiving namespace (None = any bearer)",
    )
    target_agent_id: str | None = Field(
        None, description="Restrict to a specific receiving agent (None = any agent)"
    )
    scopes: list[A2AScope] = Field(..., min_length=1)
    expires_in_seconds: int = Field(
        3600, ge=60, le=86400 * 30, description="Token validity duration (max 30 days)"
    )
    can_delegate: bool = Field(
        False, description="Whether the token can be delegated/re-granted downstream"
    )
    one_time: bool = Field(False, description="Restrict token to a single successful usage")


class A2AGrantResponse(BaseModel):
    """Response payload containing the secure sharing token."""

    model_config = ConfigDict(extra="forbid")

    grant_id: UUID
    sharing_token: str
    expires_at: datetime


class VerifiedGrant(BaseModel):
    """Result of a successful token verification — includes owner identity and scopes."""

    model_config = ConfigDict(extra="forbid")

    grant_id: UUID
    owner_namespace_id: UUID
    owner_agent_id: str
    scopes: list[A2AScope]
    expires_at: datetime
    can_delegate: bool = False
    one_time: bool = False


class A2AAuthorizationError(Exception):
    """
    Raised when an A2A token is invalid, expired, revoked, or violates
    namespace/agent constraints. Maps to JSON-RPC error -32010.
    """

    code: int = A2A_CODE_UNAUTHORIZED


class A2AScopeViolationError(Exception):
    """
    Raised when the requested resource is not covered by any granted scope.
    Maps to JSON-RPC error -32011.
    """

    code: int = A2A_CODE_SCOPE_VIOLATION


class A2AMTLSError(Exception):
    """
    Raised when mTLS client certificate validation fails.

    This is a *network-edge* rejection — the connection is dropped before
    any application-layer processing occurs.  Maps to HTTP 401 with a
    JSON-RPC -32010 error body when surfaced through the A2A server.
    """

    code: int = A2A_CODE_UNAUTHORIZED


# ---------------------------------------------------------------------------
# mTLS client certificate validation
# ---------------------------------------------------------------------------


def _normalise_fingerprint(raw: str) -> str:
    """
    Normalise a certificate fingerprint to lowercase colon-separated hex.

    Accepts colon-separated hex (``AA:BB:CC:...``) or raw hex (``AABBCC...``).
    Returns lowercase colon-separated form for consistent comparison.
    """
    stripped = raw.replace(":", "").replace("-", "").replace(" ", "").strip().lower()
    if len(stripped) < 8 or not all(c in "0123456789abcdef" for c in stripped):
        raise A2AMTLSError(f"Invalid fingerprint format: {raw!r}")
    return ":".join(stripped[i : i + 2] for i in range(0, len(stripped), 2))


def _parse_sans_from_cert_dict(cert: dict[str, Any]) -> set[str]:
    """
    Extract Subject Alternative Names from a parsed client certificate dict.

    The cert dict is what uvicorn places in ``request.scope["ssl_object"]``
    or what we reconstruct from reverse-proxy headers.  Looks for:

    - ``san`` / ``subjectAltName`` (list of DNS/URI strings)
    - ``commonName`` (CN) as a fallback
    """
    sans: set[str] = set()

    # Direct SAN list
    san_list = cert.get("san") or cert.get("subjectAltName") or []
    if isinstance(san_list, list):
        for entry in san_list:
            if isinstance(entry, str):
                for part in entry.replace("DNS:", "").replace("URI:", "").split(","):
                    part = part.strip().lower()
                    if part:
                        sans.add(part)
            elif isinstance(entry, dict):
                dns = entry.get("DNS") or entry.get("dns") or ""
                if dns:
                    sans.add(dns.strip().lower())

    # Common Name fallback (if no explicit SANs)
    cn = cert.get("commonName") or cert.get("CN") or ""
    if cn and isinstance(cn, str):
        sans.add(cn.strip().lower())

    return sans


def _parse_fingerprint_from_cert_dict(cert: dict[str, Any]) -> str | None:
    """
    Extract the SHA-256 fingerprint from a parsed client certificate dict.

    Looks for ``fingerprint`` (hex string or colon-separated),
    ``sha256``, or ``sha256_fingerprint`` keys.
    Returns normalised fingerprint or None if not present.
    """
    raw = cert.get("sha256_fingerprint") or cert.get("sha256") or cert.get("fingerprint") or ""
    if not raw:
        return None
    try:
        return _normalise_fingerprint(raw)
    except A2AMTLSError:
        log.warning("mTLS: unparseable fingerprint in cert dict: %r", raw)
        return None


def parse_client_cert_from_scope(scope: dict[str, Any]) -> dict[str, Any] | None:
    """
    Extract the client TLS certificate from an ASGI scope dict.

    When uvicorn is started with ``--ssl-keyfile``, ``--ssl-certfile``, and
    ``--ssl-client-cert-required`` (or the ``ssl`` config dict equivalent),
    the client certificate is available in ``scope["ssl_object"]``.

    Returns a dict with ``fingerprint``, ``san``, ``subject`` keys, or None
    if no client certificate was presented.
    """
    # Direct SSL object from uvicorn (dict or ssl.SSLObject)
    ssl_obj = scope.get("ssl_object") or scope.get("client_cert")
    if ssl_obj is None:
        return None

    if isinstance(ssl_obj, dict):
        # Already a dict — uvicorn >= 0.31 with ssl_object as dict
        return ssl_obj if ssl_obj else None

    # ssl.SSLObject / ssl.SSLSocket — extract via getpeercert()
    try:
        peer_cert = ssl_obj.getpeercert(binary_form=False)
        if peer_cert is None:
            return None
    except Exception:
        log.debug("mTLS: getpeercert() failed on ssl_object", exc_info=True)
        return None

    return _ssl_cert_to_dict(peer_cert)


def _ssl_cert_to_dict(peer_cert: dict[str, Any]) -> dict[str, Any]:
    """
    Convert a Python ssl.getpeercert() dict to our internal cert dict format.

    The ssl module returns subject as a tuple of tuples, and subjectAltName
    as a tuple of (type, value) tuples.
    """
    result: dict[str, Any] = {}

    # Subject → commonName
    subject = peer_cert.get("subject", [])
    if isinstance(subject, (list, tuple)):
        for item in subject:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                key, value = item[0], item[1]
                if key == "commonName":
                    result["commonName"] = value
                elif key == "organizationName":
                    result.setdefault("organizationName", value)

    # subjectAltName
    san_raw = peer_cert.get("subjectAltName", [])
    sans: list[str] = []
    if isinstance(san_raw, (list, tuple)):
        for entry in san_raw:
            if isinstance(entry, (list, tuple)) and len(entry) >= 2:
                san_type, san_value = entry[0], entry[1]
                sans.append(f"{san_type}:{san_value}")
    result["san"] = sans if sans else result.get("san", [])

    # Fingerprint (the ssl module might not provide this; we compute from DER if available)
    result["fingerprint"] = peer_cert.get("fingerprint", "")

    return result


def _parse_pem_cert(pem_b64_or_raw: str) -> dict[str, Any] | None:
    """Parse a PEM-encoded certificate string (base64 or raw PEM) into a cert dict.

    Returns dict with ``fingerprint``, ``san``, ``subject``, ``pem`` keys,
    or None if parsing fails.
    """
    if not _CRYPTOGRAPHY_AVAILABLE:
        return None

    pem_str = pem_b64_or_raw.strip()
    # Traefik may send base64-encoded PEM; try decoding first
    if not pem_str.startswith("-----"):
        try:
            pem_str = __import__("base64").b64decode(pem_str).decode("ascii")
        except Exception:
            pass  # Leave as-is if not base64

    if not pem_str.startswith("-----BEGIN CERTIFICATE-----"):
        return None

    try:
        cert = crypto_x509.load_pem_x509_certificate(pem_str.encode("ascii"))
    except Exception:
        return None

    out: dict[str, Any] = {}

    # SHA-256 fingerprint
    try:
        from cryptography.hazmat.primitives import hashes as _hashes

        fp = cert.fingerprint(_hashes.SHA256())
        out["fingerprint"] = fp.hex(":").lower()
    except Exception:
        pass

    # Subject
    try:
        out["subject"] = cert.subject.rfc4514_string()
    except Exception:
        pass

    # CommonName as SAN fallback
    try:
        cn = cert.subject.get_attributes_for_oid(crypto_x509.NameOID.COMMON_NAME)
        if cn:
            out["commonName"] = cn[0].value
    except Exception:
        pass

    # SANs
    sans: list[str] = []
    try:
        ext = cert.extensions.get_extension_for_oid(
            crypto_x509.ExtensionOID.SUBJECT_ALTERNATIVE_NAME
        )
        for name in ext.value:  # type: ignore[union-attr, attr-defined]
            if isinstance(name, crypto_x509.DNSName):
                sans.append(name.value.lower())
            elif isinstance(name, crypto_x509.IPAddress):
                sans.append(str(name.value).lower())
            elif isinstance(name, crypto_x509.GeneralName) and hasattr(name, "value"):
                sans.append(str(name.value).lower())
    except Exception:
        pass
    if sans:
        out["san"] = sans
        out["san_set"] = set(sans)

    out["pem"] = pem_str
    return out


def parse_client_cert_from_headers(headers: dict[str, str]) -> dict[str, Any] | None:
    """
    Extract client certificate info from reverse-proxy headers.

    Supported header formats:

    * ``X-Forwarded-Client-Cert`` — standard format used by Caddy, Envoy,
      nginx.  May contain ``Hash=...``, ``SAN=...``, ``Subject=...``
      semicolon-delimited fields.
    * ``X-Forwarded-Tls-Client-Cert`` — raw PEM-encoded client certificate
      sent by Traefik.
    * ``X-SSL-Client-Cert`` — raw PEM-encoded client certificate (some proxies).
    * ``X-Client-Cert-SAN`` — dedicated SAN header (simpler setups).

    Returns a cert dict with ``fingerprint``, ``san``, ``subject`` keys,
    or None if no proxy cert headers are present.
    """
    cert_dict: dict[str, Any] = {}
    found_any = False

    # Traefik: X-Forwarded-Tls-Client-Cert (full PEM)
    traefik_cert = headers.get("x-forwarded-tls-client-cert", "")
    if traefik_cert:
        parsed = _parse_pem_cert(traefik_cert)
        if parsed:
            found_any = True
            cert_dict.update(parsed)

    # X-Forwarded-Client-Cert (Caddy / Envoy style)
    fwd_cert = headers.get("x-forwarded-client-cert", "")
    if fwd_cert:
        found_any = True
        # Format: "Hash=<hex>;SAN=<san_list>;Subject=<subject>;..."
        parts = [p.strip() for p in fwd_cert.split(";")]
        for part in parts:
            if "=" not in part:
                continue
            key, _, value = part.partition("=")
            key = key.strip().lower()
            value = value.strip()
            if key in ("hash", "fingerprint", "sha256"):
                try:
                    cert_dict["fingerprint"] = _normalise_fingerprint(value)
                except A2AMTLSError:
                    log.warning("mTLS: unparseable fingerprint in X-Forwarded-Client-Cert")
            elif key == "san":
                sans = [s.strip().lower() for s in value.split(",") if s.strip()]
                cert_dict.setdefault("san", []).extend(sans)
                cert_dict.setdefault("san_set", set()).update(s.lower() for s in sans)
            elif key == "subject":
                cert_dict["subject"] = value
            elif key == "cert":
                cert_dict["pem"] = value

    # X-Client-Cert-SAN — simpler dedicated header
    san_header = headers.get("x-client-cert-san", "")
    if san_header:
        found_any = True
        sans = [s.strip().lower() for s in san_header.split(",") if s.strip()]
        cert_dict.setdefault("san", []).extend(sans)

    # X-Client-Cert-Fingerprint
    fp_header = headers.get("x-client-cert-fingerprint", "")
    if fp_header:
        found_any = True
        try:
            cert_dict["fingerprint"] = _normalise_fingerprint(fp_header)
        except A2AMTLSError:
            log.warning("mTLS: unparseable fingerprint in X-Client-Cert-Fingerprint")

    # X-Client-Cert-CN
    cn_header = headers.get("x-client-cert-cn", "")
    if cn_header:
        found_any = True
        cert_dict["commonName"] = cn_header.strip()
        cert_dict.setdefault("san", []).append(cn_header.strip().lower())

    if not found_any:
        return None

    return cert_dict


def validate_mtls_cert(
    cert_dict: dict[str, Any],
    allowed_sans: list[str] | None = None,
    allowed_fingerprints: list[str] | None = None,
) -> str:
    """
    Validate a parsed client certificate against allowlists.

    Validation order:
    1. Fingerprint check (preferred — unambiguous, no DNS spoofing risk).
    2. SAN check (convenient for wildcard or multi-domain certs).

    Returns the matched identity string (fingerprint or SAN) on success.
    Raises A2AMTLSError if no allowlist is configured or no match is found.

    Args:
        cert_dict: Parsed certificate dict from parse_client_cert_from_scope()
                   or parse_client_cert_from_headers().
        allowed_sans: List of allowed SAN values (lowercase).
        allowed_fingerprints: List of allowed SHA-256 fingerprints (lowercase,
                              colon-separated hex).
    """
    allowed_sans = [s.lower() for s in (allowed_sans or [])]
    allowed_fingerprints = [_normalise_fingerprint(f) for f in (allowed_fingerprints or [])]

    if not allowed_sans and not allowed_fingerprints:
        raise A2AMTLSError(
            "mTLS is enabled but no allowed SANs or fingerprints are configured. "
            "Set NCE_A2A_MTLS_ALLOWED_SANS or NCE_A2A_MTLS_ALLOWED_FINGERPRINTS."
        )

    # ---- Fingerprint check (preferred) ----
    cert_fp = _parse_fingerprint_from_cert_dict(cert_dict)
    if cert_fp in allowed_fingerprints:
        log.info("mTLS: client cert matched by fingerprint (len=%d)", len(cert_fp))
        return f"fp:{cert_fp}"

    # ---- SAN check ----
    cert_sans = _parse_sans_from_cert_dict(cert_dict)
    for san in cert_sans:
        san_lower = san.lower()
        if san_lower in allowed_sans:
            log.info("mTLS: client cert matched by SAN: %s", san_lower)
            return f"san:{san_lower}"

    # No match
    fp_display = cert_fp[:16] + "..." if cert_fp else "none"
    sans_display = ", ".join(sorted(cert_sans)[:5]) if cert_sans else "none"
    raise A2AMTLSError(
        f"Client certificate not in allowlist. Fingerprint: {fp_display}, SANs: {sans_display}"
    )


def mtls_enforce(
    scope: dict[str, Any],
    headers: dict[str, str],
    enabled: bool = False,
    strict: bool = True,
    trusted_proxy_hops: int = 1,
    allowed_sans: list[str] | None = None,
    allowed_fingerprints: list[str] | None = None,
) -> str | None:
    """
    Enforce mTLS client certificate validation for the A2A server.

    This is the main entry point — call it from middleware or route handlers.

    Resolution order:
    1. If TRUSTED_PROXY_HOP > 0, parse from reverse-proxy headers first.
    2. Otherwise (or as fallback), parse from ASGI SSL scope.
    3. If mTLS is not enabled, return None (pass-through).
    4. If strict mode and no cert presented, raise A2AMTLSError.
    5. If non-strict mode and no cert, return None.
    6. Validate cert against allowlists; raise on failure.

    Returns the matched identity string on success, None if mTLS is disabled
    or non-strict mode with no cert presented.

    Raises A2AMTLSError on any validation failure.
    """
    if not enabled:
        return None

    cert_dict: dict[str, Any] | None = None

    # Prefer proxy headers when there's a trusted reverse proxy
    if trusted_proxy_hops > 0:
        cert_dict = parse_client_cert_from_headers(headers)

    # Fallback to direct SSL scope
    if cert_dict is None:
        cert_dict = parse_client_cert_from_scope(scope)

    if cert_dict is None:
        if strict:
            raise A2AMTLSError(
                "mTLS strict mode: no client certificate presented. "
                "Ensure the reverse proxy is configured to request and forward client certificates."
            )
        log.debug("mTLS: no client cert presented (non-strict, allowing)")
        return None

    return validate_mtls_cert(
        cert_dict,
        allowed_sans=allowed_sans,
        allowed_fingerprints=allowed_fingerprints,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _hash_token(token: str) -> bytes:
    """Compute SHA-256 hash of the token for secure storage (compare-safe)."""
    return hashlib.sha256(token.encode("utf-8")).digest()


def _jsonrpc_error(code: int, message: str, reason: str) -> dict[str, Any]:
    """Build a compliant JSON-RPC 2.0 error response dict."""
    return {
        "jsonrpc": "2.0",
        "error": {
            "code": code,
            "message": message,
            "data": {"reason": reason},
        },
        "id": None,
    }


async def _resolve_scope_namespaces(conn: asyncpg.Connection, scope: A2AScope) -> list[UUID]:
    """Resolve all potential namespace IDs that the resource in scope might belong to."""
    if scope.resource_type in ("namespace", "subgraph"):
        try:
            return [UUID(scope.resource_id)]
        except ValueError:
            raise A2AScopeViolationError(
                f"Invalid UUID string for namespace/subgraph resource: {scope.resource_id!r}"
            )

    if scope.resource_type == "memory":
        try:
            mem_uuid = UUID(scope.resource_id)
        except ValueError:
            raise A2AScopeViolationError(
                f"Invalid UUID string for memory resource: {scope.resource_id!r}"
            )
        ns_val = await conn.fetchval(
            "SELECT namespace_id FROM memories WHERE id = $1 LIMIT 1",
            mem_uuid,
        )
        if ns_val is None:
            raise A2AScopeViolationError(f"Memory with ID {scope.resource_id} not found.")
        return [ns_val]

    if scope.resource_type == "kg_node":
        # Check if resource_id is a UUID
        try:
            node_uuid = UUID(scope.resource_id)
            ns_val = await conn.fetchval(
                "SELECT namespace_id FROM kg_nodes WHERE id = $1 LIMIT 1",
                node_uuid,
            )
            if ns_val is None:
                raise A2AScopeViolationError(f"KG Node with ID {scope.resource_id} not found.")
            return [ns_val]
        except ValueError:
            # It's a label, not a UUID. Find all namespaces where this node label exists.
            rows = await conn.fetch(
                "SELECT DISTINCT namespace_id FROM kg_nodes WHERE label = $1",
                scope.resource_id,
            )
            if not rows:
                return []
            return [row["namespace_id"] for row in rows]

    raise A2AScopeViolationError(
        f"Unsupported resource type for delegation: {scope.resource_type!r}"
    )


# ---------------------------------------------------------------------------
# Core grant lifecycle (raw asyncpg — no ORM)
# ---------------------------------------------------------------------------


async def create_grant(
    conn: asyncpg.Connection,
    owner_ctx: NamespaceContext,
    request: A2AGrantRequest,
) -> A2AGrantResponse:
    """
    Create a new A2A sharing grant.

    Generates a cryptographically secure token, stores its SHA-256 hash,
    and returns the raw token to the caller (Agent A) to share out-of-band.
    The raw token is *never* stored — only the hash is persisted.
    """
    # Scope validation for delegation (transitive re-grant prevention)
    for scope in request.scopes:
        ns_ids = await _resolve_scope_namespaces(conn, scope)
        for ns_id in ns_ids:
            if ns_id != owner_ctx.namespace_id:
                has_delegate_grant = await conn.fetchval(
                    """
                    SELECT EXISTS (
                        SELECT 1 FROM a2a_grants
                        WHERE owner_namespace_id = $1
                          AND (target_namespace_id = $2 OR target_namespace_id IS NULL)
                          AND can_delegate = true
                          AND status = 'active'
                          AND expires_at > now()
                    )
                    """,
                    ns_id,
                    owner_ctx.namespace_id,
                )
                if not has_delegate_grant:
                    raise A2AScopeViolationError(
                        f"Caller namespace {owner_ctx.namespace_id} does not have delegable access "
                        f"to target namespace {ns_id}."
                    )

    token = f"nce_a2a_{secrets.token_urlsafe(32)}"
    token_hash = _hash_token(token)
    grant_id = uuid4()
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=request.expires_in_seconds)
    scopes_json = json.dumps([s.model_dump() for s in request.scopes])

    async with conn.transaction():
        await conn.execute(
            """
            INSERT INTO a2a_grants (
                id, owner_namespace_id, owner_agent_id,
                target_namespace_id, target_agent_id,
                scopes, token_hash, status, expires_at, can_delegate,
                one_time, usage_count
            ) VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, 'active', $8, $9, $10, 0)
            """,
            grant_id,
            owner_ctx.namespace_id,
            owner_ctx.agent_id,
            request.target_namespace_id,
            request.target_agent_id,
            scopes_json,
            token_hash,
            expires_at,
            request.can_delegate,
            request.one_time,
        )

        await _append_a2a_event(
            conn,
            owner_ctx=owner_ctx,
            event_type="a2a_grant_created",
            params={
                "grant_id": str(grant_id),
                "target_namespace_id": (
                    str(request.target_namespace_id) if request.target_namespace_id else None
                ),
                "target_agent_id": request.target_agent_id,
                "scope_count": len(request.scopes),
                "expires_at": expires_at.isoformat(),
                "can_delegate": request.can_delegate,
                "one_time": request.one_time,
            },
        )

    log.info(
        "A2A grant created: grant_id=%s owner_ns=%s target_ns=%s scopes=%d expires=%s can_delegate=%s",
        grant_id,
        owner_ctx.namespace_id,
        request.target_namespace_id,
        len(request.scopes),
        expires_at.isoformat(),
        request.can_delegate,
    )
    return A2AGrantResponse(grant_id=grant_id, sharing_token=token, expires_at=expires_at)


async def verify_token(
    conn: asyncpg.Connection,
    token: str,
    consumer_ctx: NamespaceContext,
) -> VerifiedGrant:
    """
    Verify an A2A sharing token presented by Agent B.

    Validates token existence (via hash lookup), expiration, and
    namespace/agent binding constraints using raw asyncpg.
    Returns a VerifiedGrant (owner identity + scopes) on success.
    Raises A2AAuthorizationError on any failure — the error message is
    intentionally non-specific to prevent information leakage.
    """
    token_hash = _hash_token(token)

    row = await conn.fetchrow(
        """
        SELECT id, owner_namespace_id, owner_agent_id,
               target_namespace_id, target_agent_id,
               scopes, expires_at, status, can_delegate,
               one_time, usage_count
        FROM a2a_grants
        WHERE token_hash = $1 AND status = 'active'
        """,
        token_hash,
    )

    if not row:
        log.warning("A2A token verification failed: not found or inactive hash=<redacted>")
        raise A2AAuthorizationError("Invalid or revoked sharing token.")

    # Normalise timezone awareness for comparison
    expires_at: datetime = row["expires_at"]
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)

    if expires_at < datetime.now(timezone.utc):
        await conn.execute(
            "UPDATE a2a_grants SET status = 'expired' WHERE id = $1 AND status = 'active'",
            row["id"],
        )
        log.info("A2A token auto-expired: grant_id=%s", row["id"])
        raise A2AAuthorizationError("Sharing token has expired.")

    # One-time usage check
    one_time = row["one_time"] if "one_time" in row else False
    usage_count = row["usage_count"] if "usage_count" in row else 0
    if one_time and usage_count >= 1:
        log.warning("A2A token reuse rejected: grant_id=%s", row["id"])
        raise A2AAuthorizationError("One-time sharing token has already been used.")

    # Namespace binding check
    if row["target_namespace_id"] is not None:
        if row["target_namespace_id"] != consumer_ctx.namespace_id:
            log.warning(
                "A2A token namespace mismatch: grant_id=%s expected=%s got=%s",
                row["id"],
                row["target_namespace_id"],
                consumer_ctx.namespace_id,
            )
            raise A2AAuthorizationError("Token is not valid for this namespace.")

    # Agent binding check
    if row["target_agent_id"] is not None:
        if row["target_agent_id"] != consumer_ctx.agent_id:
            log.warning(
                "A2A token agent mismatch: grant_id=%s expected=%s got=%s",
                row["id"],
                row["target_agent_id"],
                consumer_ctx.agent_id,
            )
            raise A2AAuthorizationError("Token is not valid for this agent.")

    # Successful verification - increment usage count and deactivate if one_time
    await conn.execute(
        """
        UPDATE a2a_grants
        SET usage_count = usage_count + 1,
            status = CASE WHEN one_time = true THEN 'expired' ELSE status END
        WHERE id = $1
        """,
        row["id"],
    )

    scopes_data = json.loads(row["scopes"])
    scopes = [A2AScope.model_validate(s) for s in scopes_data]

    return VerifiedGrant(
        grant_id=row["id"],
        owner_namespace_id=row["owner_namespace_id"],
        owner_agent_id=row["owner_agent_id"],
        scopes=scopes,
        expires_at=expires_at,
        can_delegate=row["can_delegate"],
        one_time=row["one_time"] if "one_time" in row else False,
    )


async def revoke_grant(
    conn: asyncpg.Connection,
    grant_id: UUID,
    owner_ctx: NamespaceContext,
) -> bool:
    """
    Revoke an active A2A sharing grant.

    Only the owning namespace can revoke a grant.
    Returns True if successfully revoked, False if not found / already inactive.
    """
    async with conn.transaction():
        result = await conn.execute(
            """
            UPDATE a2a_grants
            SET status = 'revoked'
            WHERE id = $1
              AND owner_namespace_id = $2
              AND status = 'active'
            """,
            grant_id,
            owner_ctx.namespace_id,
        )
        revoked = result == "UPDATE 1"
        if revoked:
            await _append_a2a_event(
                conn,
                owner_ctx=owner_ctx,
                event_type="a2a_grant_revoked",
                params={"grant_id": str(grant_id)},
            )

    if revoked:
        log.info(
            "A2A grant revoked: grant_id=%s owner_ns=%s",
            grant_id,
            owner_ctx.namespace_id,
        )
    else:
        log.warning(
            "A2A revoke no-op: grant_id=%s owner_ns=%s (not found or already inactive)",
            grant_id,
            owner_ctx.namespace_id,
        )
    return revoked


async def list_grants(
    conn: asyncpg.Connection,
    owner_ctx: NamespaceContext,
    include_inactive: bool = False,
) -> list[dict[str, Any]]:
    """
    List A2A grants owned by the given namespace.

    Returns active grants by default. Set include_inactive=True to include
    revoked and expired grants (useful for audit trails).
    Token hashes are never returned — callers only see grant metadata.
    """
    if include_inactive:
        rows = await conn.fetch(
            """
            SELECT id, owner_agent_id, target_namespace_id, target_agent_id,
                   scopes, status, expires_at, created_at, can_delegate,
                   one_time, usage_count
            FROM a2a_grants
            WHERE owner_namespace_id = $1
            ORDER BY created_at DESC
            LIMIT 500
            """,
            owner_ctx.namespace_id,
        )
    else:
        rows = await conn.fetch(
            """
            SELECT id, owner_agent_id, target_namespace_id, target_agent_id,
                   scopes, status, expires_at, created_at, can_delegate,
                   one_time, usage_count
            FROM a2a_grants
            WHERE owner_namespace_id = $1
              AND status = 'active'
              AND expires_at > now()
            ORDER BY created_at DESC
            LIMIT 500
            """,
            owner_ctx.namespace_id,
        )

    return [
        {
            "grant_id": str(row["id"]),
            "owner_agent_id": row["owner_agent_id"],
            "target_namespace_id": (
                str(row["target_namespace_id"]) if row["target_namespace_id"] else None
            ),
            "target_agent_id": row["target_agent_id"],
            "scopes": json.loads(row["scopes"]),
            "status": row["status"],
            "expires_at": row["expires_at"].isoformat(),
            "can_delegate": row["can_delegate"],
            "one_time": row["one_time"],
            "usage_count": row["usage_count"],
            "created_at": row["created_at"].isoformat(),
        }
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Scope enforcement (pure — no I/O)
# ---------------------------------------------------------------------------


def enforce_scope(
    scopes: list[A2AScope],
    resource_type: str,
    resource_id: str,
    resource_namespace_id: str,
) -> None:
    """
    Enforce that at least one granted scope covers the requested resource.

    Namespace-scoped grants are treated as wildcards: a grant for
    resource_type="namespace" implicitly covers all memories and KG nodes
    within that namespace.

    Raises A2AScopeViolationError (JSON-RPC -32011) if access is denied.
    This function performs no I/O — call it after verify_token().
    """
    for scope in scopes:
        if "read" not in scope.permissions:
            continue

        # Exact match: specific resource type and ID
        if scope.resource_type == resource_type and scope.resource_id == resource_id:
            return

        # Namespace wildcard: a namespace grant covers memories and KG nodes within it
        if (
            scope.resource_type == "namespace"
            and scope.resource_id == str(resource_namespace_id)
            and resource_type
            in (
                "memory",
                "kg_node",
                "subgraph",
            )
        ):
            return

    raise A2AScopeViolationError(
        f"Access denied: no grant covers {resource_type}/{resource_id}. "
        f"Available scopes: {[f'{s.resource_type}/{s.resource_id}' for s in scopes]}"
    )


# ---------------------------------------------------------------------------
# Extended A2A Lifecycle operations (Phase 3.1 Suite Enrichment)
# ---------------------------------------------------------------------------


async def verify_grant_status(
    conn: asyncpg.Connection,
    ctx: NamespaceContext,
    sharing_token: str | None = None,
    grant_id: UUID | None = None,
) -> dict[str, Any]:
    """
    Verify the validity, status, scopes, and expiration of a grant.
    Resolves by either sharing_token or grant_id.
    Enforces NamespaceContext boundaries.
    """
    if (sharing_token is None) == (grant_id is None):
        raise ValueError("Must provide exactly one of 'sharing_token' or 'grant_id'.")

    if sharing_token is not None:
        token_hash = _hash_token(sharing_token)
        row = await conn.fetchrow(
            """
            SELECT id, owner_namespace_id, owner_agent_id,
                   target_namespace_id, target_agent_id,
                   scopes, expires_at, status, created_at, can_delegate,
                   one_time, usage_count
            FROM a2a_grants
            WHERE token_hash = $1
            """,
            token_hash,
        )
    else:
        row = await conn.fetchrow(
            """
            SELECT id, owner_namespace_id, owner_agent_id,
                   target_namespace_id, target_agent_id,
                   scopes, expires_at, status, created_at, can_delegate,
                   one_time, usage_count
            FROM a2a_grants
            WHERE id = $1
            """,
            grant_id,
        )

    if not row:
        log.warning("A2A grant verification failed: grant not found")
        raise A2AAuthorizationError("Grant not found.")

    # Timezone-aware expiration check and auto-expiry transition
    expires_at: datetime = row["expires_at"]
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)

    status = row["status"]
    if status == "active" and expires_at < datetime.now(timezone.utc):
        await conn.execute(
            "UPDATE a2a_grants SET status = 'expired' WHERE id = $1",
            row["id"],
        )
        status = "expired"
        log.info("A2A token auto-expired during status check: grant_id=%s", row["id"])

    # Enforce NamespaceContext security boundaries (must be owner or target)
    owner_ns = row["owner_namespace_id"]
    target_ns = row["target_namespace_id"]
    target_agent = row["target_agent_id"]

    is_owner = ctx.namespace_id == owner_ns
    is_target = False

    if target_ns is None:
        # Unrestricted target namespace (any bearer is target)
        is_target = True
    elif ctx.namespace_id == target_ns:
        if target_agent is None or ctx.agent_id == target_agent:
            is_target = True

    if not (is_owner or is_target):
        log.warning(
            "A2A status check forbidden: caller=%s owner_ns=%s target_ns=%s",
            ctx.namespace_id,
            owner_ns,
            target_ns,
        )
        raise A2AAuthorizationError("Unauthorized access to grant status.")

    return {
        "grant_id": str(row["id"]),
        "owner_namespace_id": str(owner_ns),
        "owner_agent_id": row["owner_agent_id"],
        "target_namespace_id": str(target_ns) if target_ns else None,
        "target_agent_id": target_agent,
        "scopes": json.loads(row["scopes"]),
        "status": status,
        "expires_at": expires_at.isoformat(),
        "can_delegate": row["can_delegate"],
        "one_time": row["one_time"] if "one_time" in row else False,
        "usage_count": row["usage_count"] if "usage_count" in row else 0,
        "created_at": row["created_at"].isoformat(),
    }


async def update_grant_scopes(
    conn: asyncpg.Connection,
    owner_ctx: NamespaceContext,
    grant_id: UUID,
    scopes: list[A2AScope],
    mode: Literal["replace", "append"] = "replace",
) -> dict[str, Any]:
    """
    Dynamically mutate the scopes on an active grant owned by owner_ctx.namespace_id.
    """
    row = await conn.fetchrow(
        """
        SELECT id, owner_namespace_id, scopes, expires_at, status
        FROM a2a_grants
        WHERE id = $1 AND owner_namespace_id = $2
        """,
        grant_id,
        owner_ctx.namespace_id,
    )

    if not row:
        log.warning("A2A update scopes failed: grant not found or unauthorized")
        raise A2AAuthorizationError("Grant not found or unauthorized.")

    if row["status"] != "active":
        raise A2AAuthorizationError("Cannot update scopes of an inactive grant.")

    expires_at: datetime = row["expires_at"]
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at < datetime.now(timezone.utc):
        raise A2AAuthorizationError("Cannot update scopes of an expired grant.")

    # Apply append or replace strategy
    if mode == "append":
        existing_data = json.loads(row["scopes"])
        merged_scopes = [A2AScope.model_validate(s) for s in existing_data]

        # Merge input scopes, filtering out exact duplicates
        for ns in scopes:
            if ns not in merged_scopes:
                merged_scopes.append(ns)
        final_scopes = merged_scopes
    elif mode == "replace":
        final_scopes = scopes
    else:
        raise ValueError(f"Invalid scope update mode: {mode!r}")

    # Validate final scopes list
    if not final_scopes:
        raise ValueError("A grant must have at least one active scope.")

    scopes_json = json.dumps([s.model_dump() for s in final_scopes])

    async with conn.transaction():
        await conn.execute(
            "UPDATE a2a_grants SET scopes = $1::jsonb WHERE id = $2",
            scopes_json,
            grant_id,
        )

        await _append_a2a_event(
            conn,
            owner_ctx=owner_ctx,
            event_type="a2a_grant_updated",
            params={
                "grant_id": str(grant_id),
                "mode": mode,
                "scope_count": len(final_scopes),
            },
        )

    log.info(
        "A2A grant scopes updated: grant_id=%s owner_ns=%s mode=%s final_scopes=%d",
        grant_id,
        owner_ctx.namespace_id,
        mode,
        len(final_scopes),
    )
    return {
        "grant_id": str(grant_id),
        "status": "updated",
        "scopes": [s.model_dump() for s in final_scopes],
    }


async def inspect_grant(
    conn: asyncpg.Connection,
    owner_ctx: NamespaceContext,
    grant_id: UUID,
) -> dict[str, Any]:
    """
    Retrieve single grant metadata for audit audit trails (excludes token_hash).
    """
    row = await conn.fetchrow(
        """
        SELECT id, owner_namespace_id, owner_agent_id,
               target_namespace_id, target_agent_id,
               scopes, status, expires_at, created_at, can_delegate,
               one_time, usage_count
        FROM a2a_grants
        WHERE id = $1 AND owner_namespace_id = $2
        """,
        grant_id,
        owner_ctx.namespace_id,
    )

    if not row:
        log.warning("A2A inspect failed: grant not found or unauthorized")
        raise A2AAuthorizationError("Grant not found or unauthorized.")

    expires_at: datetime = row["expires_at"]
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)

    return {
        "grant_id": str(row["id"]),
        "owner_namespace_id": str(row["owner_namespace_id"]),
        "owner_agent_id": row["owner_agent_id"],
        "target_namespace_id": str(row["target_namespace_id"])
        if row["target_namespace_id"]
        else None,
        "target_agent_id": row["target_agent_id"],
        "scopes": json.loads(row["scopes"]),
        "status": row["status"],
        "expires_at": expires_at.isoformat(),
        "can_delegate": row["can_delegate"],
        "one_time": row["one_time"] if "one_time" in row else False,
        "usage_count": row["usage_count"] if "usage_count" in row else 0,
        "created_at": row["created_at"].isoformat(),
    }
