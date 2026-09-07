"""
nce/vertical_modules/customer_portal/sessions.py
================================================
Server-Verified Sessions for Customer Portal (Charter Layer 1 / Wave T-6).

Guarantees:
  1. Authoritative Scope Binding: Session tokens bind exclusively to
     (namespace_id, customer_scope_id, email).
  2. Cryptographically Strong Tokens: cp_sess_<48 hex chars> generated with secrets module.
  3. Token Hashing at Rest: Tokens are indexed and looked up via SHA-256 digests.
  4. Expiry & Revocation: Active TTL enforcement with instant revocation support.
  5. Immutable Tenant Boundary: Callers cannot name their own tenant; middleware
     resolves scope strictly from the verified session.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from uuid import UUID

log = logging.getLogger("nce.vertical_modules.customer_portal.sessions")

DEFAULT_SESSION_TTL_SECONDS: int = 86400  # 24 hours


def hash_session_token(token: str) -> str:
    """Compute deterministic SHA-256 digest of a session token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass
class PortalSession:
    """Authenticated customer portal session record."""

    token: str
    token_hash: str
    namespace_id: UUID
    customer_scope_id: UUID
    email: str
    auth_provider: str = "magic_link"
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: datetime = field(
        default_factory=lambda: (
            datetime.now(timezone.utc) + timedelta(seconds=DEFAULT_SESSION_TTL_SECONDS)
        )
    )
    revoked_at: datetime | None = None
    last_accessed_at: datetime | None = None

    @property
    def is_valid(self) -> bool:
        """Check whether the session is unexpired and unrevoked."""
        if self.revoked_at is not None:
            return False
        now = datetime.now(timezone.utc)
        return self.expires_at > now


class PortalSessionStore:
    """In-memory thread-safe storage for active customer portal sessions."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions_by_hash: dict[str, PortalSession] = {}
        self._by_scope: dict[tuple[UUID, UUID], str] = {}

    def create_session(
        self,
        namespace_id: UUID | str,
        customer_scope_id: UUID | str,
        email: str,
        auth_provider: str = "magic_link",
        ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS,
    ) -> PortalSession:
        """Mint a new session token, persist it in the store, and return the session."""
        ns_uuid = (
            namespace_id if isinstance(namespace_id, UUID) else UUID(str(namespace_id).strip())
        )
        scope_uuid = (
            customer_scope_id
            if isinstance(customer_scope_id, UUID)
            else UUID(str(customer_scope_id).strip())
        )
        token_secret = secrets.token_hex(24)
        token = f"cp_sess_{token_secret}"
        token_hash = hash_session_token(token)

        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(seconds=ttl_seconds)

        session = PortalSession(
            token=token,
            token_hash=token_hash,
            namespace_id=ns_uuid,
            customer_scope_id=scope_uuid,
            email=(email or "").strip().lower(),
            auth_provider=auth_provider,
            created_at=now,
            expires_at=expires_at,
        )

        with self._lock:
            self._sessions_by_hash[token_hash] = session
            self._by_scope[(ns_uuid, scope_uuid)] = token_hash

        log.debug(
            "Created customer portal session for namespace=%s, scope=%s, email=%s (expires_at=%s)",
            ns_uuid,
            scope_uuid,
            email,
            expires_at.isoformat(),
        )
        return session

    def resolve_session(self, token: str) -> PortalSession | None:
        """Validate token and return session if active, unexpired, and unrevoked."""
        if not token or not isinstance(token, str):
            return None
        cleaned_token = token.strip()
        if not cleaned_token.startswith("cp_sess_"):
            return None

        token_hash = hash_session_token(cleaned_token)
        with self._lock:
            session = self._sessions_by_hash.get(token_hash)
            if session is None:
                return None
            if not session.is_valid:
                log.info(
                    "Customer portal session expired or revoked for token_hash=%s (expires_at=%s, revoked=%s)",
                    token_hash[:8],
                    session.expires_at.isoformat(),
                    bool(session.revoked_at),
                )
                return None
            session.last_accessed_at = datetime.now(timezone.utc)
            return session

    def revoke_session(self, token: str) -> bool:
        """Mark a session as permanently revoked."""
        if not token:
            return False
        token_hash = hash_session_token(token.strip())
        with self._lock:
            session = self._sessions_by_hash.get(token_hash)
            if session:
                session.revoked_at = datetime.now(timezone.utc)
                return True
        return False

    def get_active_session_for_scope(
        self,
        namespace_id: UUID | str,
        customer_scope_id: UUID | str,
    ) -> PortalSession | None:
        """Find the latest active session for a specific (namespace, customer_scope) tuple."""
        try:
            ns_uuid = (
                namespace_id if isinstance(namespace_id, UUID) else UUID(str(namespace_id).strip())
            )
            scope_uuid = (
                customer_scope_id
                if isinstance(customer_scope_id, UUID)
                else UUID(str(customer_scope_id).strip())
            )
        except Exception:
            return None

        with self._lock:
            token_hash = self._by_scope.get((ns_uuid, scope_uuid))
            if token_hash:
                session = self._sessions_by_hash.get(token_hash)
                if session and session.is_valid:
                    return session
        return None

    def clear(self) -> None:
        """Clear all stored sessions (used for test setup/teardown)."""
        with self._lock:
            self._sessions_by_hash.clear()
            self._by_scope.clear()


# Global default instance for the application lifecycle
default_session_store: PortalSessionStore = PortalSessionStore()
