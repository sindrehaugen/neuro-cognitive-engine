"""
PostgreSQL access for `bridge_subscriptions` (Appendix H.2 / GAPS audit).
Used by MCP bridge tools and the renewal cron.

OAuth token encryption (Phase 3 — Item 12)
------------------------------------------
``save_token`` and ``get_token`` are the canonical storage-layer hooks for
OAuth access/refresh token payloads.  Every token payload stored via
``save_token`` is AES-256-GCM encrypted under the ``NCE_MASTER_KEY``
before it touches Postgres, using the same ``SecureKeyBuffer``-backed
pipeline as ``nce/signing.py``.  The ``oauth_access_token_enc`` column
never contains plaintext.

Callers **must** use ``save_token`` / ``get_token`` rather than raw
``encrypt_signing_key`` / ``decrypt_signing_key`` so that all token
encryption flows through a single auditable code path.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import asyncpg

from nce.bridge_providers import BRIDGE_PROVIDERS
from nce.signing import (
    decrypt_signing_key,
    encrypt_signing_key,
    require_master_key,
)

# asyncpg.Record behaves like Mapping for known keys

# Columns permitted for dynamic UPDATE from ``update_subscription`` (MCP / renewal tooling).
ALLOWED_SUBSCRIPTION_UPDATE_FIELDS = frozenset(
    {
        "resource_id",
        "subscription_id",
        "cursor",
        "status",
        "expires_at",
        "client_state",
    }
)


async def insert_subscription(
    conn: asyncpg.Connection,
    *,
    user_id: str,
    namespace_id: uuid.UUID,
    provider: str,
    resource_id: str,
    status: str = "REQUESTED",
    subscription_id: str | None = None,
    cursor: str | None = None,
    expires_at: datetime | None = None,
    client_state: str | None = None,
    row_id: uuid.UUID | None = None,
) -> uuid.UUID:
    rid = row_id or uuid.uuid4()
    await conn.execute(
        """
        INSERT INTO bridge_subscriptions (
            id, user_id, namespace_id, provider, resource_id, subscription_id,
            cursor, status, expires_at, client_state, updated_at
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, NOW())
        """,
        rid,
        user_id,
        namespace_id,
        provider,
        resource_id,
        subscription_id,
        cursor,
        status,
        expires_at,
        client_state,
    )
    return rid


async def fetch_expiring(
    conn: asyncpg.Connection,
    *,
    within: timedelta,
    limit: int = 100,
) -> list[asyncpg.Record]:
    return await conn.fetch(
        """
        SELECT *
        FROM bridge_subscriptions
        WHERE status = 'ACTIVE'
          AND expires_at IS NOT NULL
          AND expires_at < NOW() + $1::interval
        ORDER BY expires_at ASC
        LIMIT $2
        """,
        within,
        limit,
    )


async def get_by_id(conn: asyncpg.Connection, bridge_id: uuid.UUID) -> asyncpg.Record | None:
    return await conn.fetchrow(
        "SELECT * FROM bridge_subscriptions WHERE id = $1",
        bridge_id,
    )


async def list_for_user(
    conn: asyncpg.Connection,
    user_id: str,
    *,
    include_disconnected: bool = False,
) -> list[asyncpg.Record]:
    if include_disconnected:
        return await conn.fetch(
            """
            SELECT * FROM bridge_subscriptions
            WHERE user_id = $1
            ORDER BY updated_at DESC
            """,
            user_id,
        )
    return await conn.fetch(
        """
        SELECT * FROM bridge_subscriptions
        WHERE user_id = $1 AND status <> 'DISCONNECTED'
        ORDER BY updated_at DESC
        """,
        user_id,
    )


async def update_subscription(
    conn: asyncpg.Connection,
    bridge_id: uuid.UUID,
    **fields: Any,
) -> None:
    if not fields:
        return
    unknown = set(fields) - ALLOWED_SUBSCRIPTION_UPDATE_FIELDS
    if unknown:
        raise ValueError(
            f"update_subscription: disallowed field(s) {sorted(unknown)}; "
            f"allowed={sorted(ALLOWED_SUBSCRIPTION_UPDATE_FIELDS)}"
        )
    keys = sorted(fields.keys())
    vals = [fields[k] for k in keys]
    set_parts = [f"{k} = ${i + 1}" for i, k in enumerate(keys)]
    vals.append(bridge_id)
    num = len(vals)
    q = (
        f"UPDATE bridge_subscriptions SET {', '.join(set_parts)}, "
        f"updated_at = NOW() WHERE id = ${num}"
    )
    await conn.execute(q, *vals)


async def fetch_oauth_token_enc(
    conn: asyncpg.Connection,
    provider: str,
    *,
    client_state: str | None = None,
    subscription_id: str | None = None,
    resource_id: str | None = None,
) -> bytes | None:
    """Return encrypted OAuth access token bytes for an ACTIVE bridge row, if any."""
    if provider not in BRIDGE_PROVIDERS:
        return None
    clauses: list[str] = []
    args: list[Any] = [provider]
    idx = 2
    if client_state:
        clauses.append(f"client_state = ${idx}")
        args.append(client_state)
        idx += 1
    if subscription_id:
        clauses.append(f"subscription_id = ${idx}")
        args.append(subscription_id)
        idx += 1
    if resource_id:
        clauses.append(f"resource_id = ${idx}")
        args.append(resource_id)
        idx += 1
    if not clauses:
        return None

    row = await conn.fetchrow(
        f"""
        SELECT oauth_access_token_enc
        FROM bridge_subscriptions
        WHERE provider = $1
          AND status = 'ACTIVE'
          AND oauth_access_token_enc IS NOT NULL
          AND ({' OR '.join(clauses)})
        LIMIT 1
        """,
        *args,
    )
    return bytes(row["oauth_access_token_enc"]) if row and row["oauth_access_token_enc"] else None


async def fetch_active_subscription(
    conn: asyncpg.Connection,
    provider: str,
    *,
    client_state: str | None = None,
    subscription_id: str | None = None,
    resource_id: str | None = None,
) -> asyncpg.Record | None:
    """Return the entire bridge subscription record for an ACTIVE bridge row, if any."""
    if provider not in BRIDGE_PROVIDERS:
        return None
    clauses: list[str] = []
    args: list[Any] = [provider]
    idx = 2
    if client_state:
        clauses.append(f"client_state = ${idx}")
        args.append(client_state)
        idx += 1
    if subscription_id:
        clauses.append(f"subscription_id = ${idx}")
        args.append(subscription_id)
        idx += 1
    if resource_id:
        clauses.append(f"resource_id = ${idx}")
        args.append(resource_id)
        idx += 1
    if not clauses:
        return None

    return await conn.fetchrow(
        f"""
        SELECT *
        FROM bridge_subscriptions
        WHERE provider = $1
          AND status = 'ACTIVE'
          AND ({' OR '.join(clauses)})
        LIMIT 1
        """,
        *args,
    )


async def mark_status(conn: asyncpg.Connection, bridge_id: uuid.UUID, status: str) -> None:
    await conn.execute(
        """
        UPDATE bridge_subscriptions
        SET status = $2, updated_at = NOW()
        WHERE id = $1
        """,
        bridge_id,
        status,
    )


def subscription_to_public_dict(rec: asyncpg.Record) -> dict[str, Any]:
    """JSON-serialisable row for MCP responses (no secrets)."""
    return {
        "id": str(rec["id"]),
        "user_id": rec["user_id"],
        "provider": rec["provider"],
        "resource_id": rec["resource_id"],
        "subscription_id": rec["subscription_id"],
        "cursor": rec["cursor"],
        "status": rec["status"],
        "expires_at": rec["expires_at"].isoformat() if rec["expires_at"] else None,
        "client_state_set": bool(rec["client_state"]),
        "created_at": rec["created_at"].isoformat() if rec["created_at"] else None,
        "updated_at": rec["updated_at"].isoformat() if rec["updated_at"] else None,
    }


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# OAuth token encryption (Phase 3 — Item 12)
# ---------------------------------------------------------------------------


async def save_token(
    conn: asyncpg.Connection,
    bridge_id: uuid.UUID,
    token_payload: dict[str, Any],
) -> None:
    """Encrypt *token_payload* with AES-256-GCM and persist to ``bridge_subscriptions``.

    ``token_payload`` must be a JSON-serialisable dict, typically::

        {
            "access_token": "...",
            "refresh_token": "...",
            "expires_at": 1715731200.0,
        }

    The payload is serialised to JSON, encrypted under the
    ``NCE_MASTER_KEY`` via ``encrypt_signing_key`` (which internally
    uses ``SecureKeyBuffer`` to zero derived key material after use), and
    stored in the ``oauth_access_token_enc`` BYTEA column.

    Raises ``MasterKeyMissingError`` if ``NCE_MASTER_KEY`` is absent.
    """
    plaintext = json.dumps(token_payload).encode("utf-8")
    with require_master_key() as mk:
        ciphertext = encrypt_signing_key(plaintext, mk)
    await conn.execute(
        """
        UPDATE bridge_subscriptions
        SET oauth_access_token_enc = $2,
            updated_at = NOW()
        WHERE id = $1
        """,
        bridge_id,
        ciphertext,
    )


async def get_token(
    conn: asyncpg.Connection,
    bridge_id: uuid.UUID,
) -> dict[str, Any] | None:
    """Retrieve and decrypt the OAuth token payload for *bridge_id*.

    Returns the decrypted token payload dict, or ``None`` if the row does
    not exist or has no stored token.

    Decryption uses ``decrypt_signing_key`` (AES-256-GCM with
    ``SecureKeyBuffer``-protected derived keys) under the
    ``NCE_MASTER_KEY``.

    Raises ``SigningKeyDecryptionError`` if the stored ciphertext cannot be
    authenticated (wrong master key or corrupted blob).
    """
    row = await conn.fetchrow(
        "SELECT oauth_access_token_enc FROM bridge_subscriptions WHERE id = $1",
        bridge_id,
    )
    if not row or not row["oauth_access_token_enc"]:
        return None
    with require_master_key() as mk:
        plaintext = decrypt_signing_key(bytes(row["oauth_access_token_enc"]), mk).decode("utf-8")
    return json.loads(plaintext)
