"""
tests/unit/test_customer_portal_sessions.py
===========================================
Unit tests for server-verified customer portal session tokens (Wave T-6).

Validates:
  1. Cryptographic token generation (cp_sess_<48 hex chars>) and deterministic SHA-256 hashing.
  2. Scope binding: (namespace_id, customer_scope_id, email).
  3. Expiry and TTL enforcement.
  4. Explicit session revocation.
  5. Lookup by (namespace_id, customer_scope_id) pair.
  6. Protection against token spoofing or non-existent tokens.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from nce.vertical_modules.customer_portal.sessions import (
    PortalSessionStore,
    hash_session_token,
)


def test_token_format_and_hashing() -> None:
    """Session token is cp_sess_<48hex> and hashing is SHA-256 deterministic."""
    store = PortalSessionStore()
    ns = uuid.uuid4()
    scope = uuid.uuid4()
    email = "customer@example.com"

    session = store.create_session(ns, scope, email)
    assert session.token.startswith("cp_sess_")
    # Prefix 'cp_sess_' (8 chars) + 48 hex chars = 56 chars
    assert len(session.token) == 56
    assert session.token_hash == hash_session_token(session.token)
    assert len(session.token_hash) == 64  # SHA-256 hex digest
    assert session.namespace_id == ns
    assert session.customer_scope_id == scope
    assert session.email == email
    assert session.is_valid is True


def test_resolve_valid_session() -> None:
    """A freshly created session resolves cleanly and updates last_accessed_at."""
    store = PortalSessionStore()
    ns = uuid.uuid4()
    scope = uuid.uuid4()
    email = "portal.user@client.no"

    session = store.create_session(ns, scope, email)
    resolved = store.resolve_session(session.token)
    assert resolved is not None
    assert resolved.token == session.token
    assert resolved.email == email
    assert resolved.namespace_id == ns
    assert resolved.customer_scope_id == scope
    assert resolved.last_accessed_at is not None


def test_resolve_invalid_and_malformed_tokens() -> None:
    """Malformed, non-prefixed, or non-existent tokens resolve to None."""
    store = PortalSessionStore()
    assert store.resolve_session("") is None
    assert store.resolve_session("invalid-token") is None
    assert store.resolve_session("cp_sess_000000000000000000000000000000000000000000000000") is None


def test_session_expiry() -> None:
    """Expired session tokens fail validation."""
    store = PortalSessionStore()
    ns = uuid.uuid4()
    scope = uuid.uuid4()

    # Create session with 1 second TTL
    session = store.create_session(ns, scope, "expire@test.com", ttl_seconds=1)
    assert session.is_valid is True

    # Artificially shift expires_at to the past
    session.expires_at = datetime.now(timezone.utc) - timedelta(seconds=10)
    assert session.is_valid is False
    assert store.resolve_session(session.token) is None


def test_session_revocation() -> None:
    """Revoked sessions become immediately invalid and non-resolvable."""
    store = PortalSessionStore()
    ns = uuid.uuid4()
    scope = uuid.uuid4()

    session = store.create_session(ns, scope, "revoke@test.com")
    assert store.resolve_session(session.token) is not None

    revoked = store.revoke_session(session.token)
    assert revoked is True
    assert session.is_valid is False
    assert session.revoked_at is not None
    assert store.resolve_session(session.token) is None

    # Revoking again returns False
    assert store.revoke_session("cp_sess_nonexistent") is False


def test_get_active_session_for_scope() -> None:
    """Store indexes active session by (namespace_id, customer_scope_id)."""
    store = PortalSessionStore()
    ns = uuid.uuid4()
    scope = uuid.uuid4()

    assert store.get_active_session_for_scope(ns, scope) is None

    session = store.create_session(ns, scope, "scope@test.com")
    found = store.get_active_session_for_scope(ns, scope)
    assert found is not None
    assert found.token == session.token

    # Once revoked, no active session for scope
    store.revoke_session(session.token)
    assert store.get_active_session_for_scope(ns, scope) is None


def test_store_clear() -> None:
    """Clearing store purges all active sessions."""
    store = PortalSessionStore()
    ns = uuid.uuid4()
    scope = uuid.uuid4()

    session = store.create_session(ns, scope, "clear@test.com")
    assert store.resolve_session(session.token) is not None

    store.clear()
    assert store.resolve_session(session.token) is None
    assert store.get_active_session_for_scope(ns, scope) is None
