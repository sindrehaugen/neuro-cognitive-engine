"""
nce/signing_credentials.py
==========================
Wave Q-4: Operator-entered signing provider credentials storage & resolution.

Manages tenant-scoped, encrypted-at-rest credentials for electronic signature
and national eID brokers (e.g., Criipto, Signicat, Oneflow):
  - Strict write-only semantics: secret is accepted on write and NEVER returned.
  - Encrypted at rest under NCE_MASTER_KEY (AES-256-GCM / PBKDF2/Argon2id).
  - Fingerprint generation for operator feedback (e.g. ••••1234).
  - Explicit precedence resolution between database credentials and secret_env().
  - Per-tenant namespace isolation via row-level security.
  - Immutable WORM audit event appended on every write.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Literal

import asyncpg

from nce.config import secret_env
from nce.event_log import append_event
from nce.signing import MasterKey, decrypt_signing_key, encrypt_signing_key, require_master_key

logger = logging.getLogger("nce.signing_credentials")

PrecedenceOrder = Literal["db_first", "env_first"]


# Below this length the trailing 4 characters would be most of the secret,
# so no tail is revealed at all.
_MIN_LEN_FOR_TAIL = 8


def compute_secret_fingerprint(secret: str) -> str:
    """Derive a safe, non-secret visual fingerprint for operator confirmation.

    Reveals at most the trailing 4 characters, and only when the secret is long
    enough that those 4 are a small fraction of it.

    🔴 A short secret gets NO tail at all. This fingerprint is written into
    ``event_log`` as ``secret_fingerprint``, and ``event_log`` is WORM -- it
    cannot be deleted. The previous version returned ``f"••••{cleaned}"`` for
    anything <= 4 characters, i.e. the ENTIRE secret, permanently. A real client
    secret is never that short, but a typo or a truncated paste is, and the
    operator UI accepts whatever it is given.
    """
    cleaned = (secret or "").strip()
    if not cleaned:
        return "••••none"
    if len(cleaned) < _MIN_LEN_FOR_TAIL:
        return "••••"
    return f"••••{cleaned[-4:]}"


def normalize_provider(provider: str) -> str:
    """Normalize provider identifier string."""
    norm = (provider or "").strip().lower()
    if not norm:
        raise ValueError("Provider name must not be empty.")
    return norm


async def save_signing_credential(
    conn: asyncpg.Connection,
    namespace_id: uuid.UUID | str,
    provider: str,
    client_id: str,
    client_secret: str,
    *,
    actor: str = "operator",
    metadata: dict[str, Any] | None = None,
    master_key: MasterKey | None = None,
) -> dict[str, Any]:
    """Save an operator-entered signing credential for a tenant namespace.

    Strictly write-only:
      - Encrypts client_secret with NCE_MASTER_KEY via encrypt_signing_key.
      - Derives non-secret fingerprint for UI feedback.
      - Inserts or updates signing_credentials row.
      - Appends an immutable config_changed WORM event (containing NO secret).
      - Returns status confirmation WITHOUT plaintext or ciphertext secret.
    """
    ns_uuid = (
        uuid.UUID(str(namespace_id)) if not isinstance(namespace_id, uuid.UUID) else namespace_id
    )
    norm_provider = normalize_provider(provider)

    clean_client_id = (client_id or "").strip()
    if not clean_client_id:
        raise ValueError("client_id must not be empty.")

    clean_secret = (client_secret or "").strip()
    if not clean_secret:
        raise ValueError("client_secret must not be empty.")

    # Encrypt secret at rest under NCE_MASTER_KEY
    if master_key is not None:
        encrypted_bytes = encrypt_signing_key(clean_secret.encode("utf-8"), master_key)
    else:
        with require_master_key() as mk:
            encrypted_bytes = encrypt_signing_key(clean_secret.encode("utf-8"), mk)

    fingerprint = compute_secret_fingerprint(clean_secret)
    meta_dict = metadata or {}
    meta_json = json.dumps(meta_dict)

    # Perform UPSERT into signing_credentials within an atomic transaction
    sql = """
        INSERT INTO signing_credentials (
            namespace_id,
            provider,
            client_id,
            encrypted_secret,
            secret_fingerprint,
            metadata,
            updated_at
        )
        VALUES ($1, $2, $3, $4, $5, $6::jsonb, now())
        ON CONFLICT (namespace_id, provider) DO UPDATE SET
            client_id = EXCLUDED.client_id,
            encrypted_secret = EXCLUDED.encrypted_secret,
            secret_fingerprint = EXCLUDED.secret_fingerprint,
            metadata = EXCLUDED.metadata,
            updated_at = now()
        RETURNING id, created_at, updated_at;
    """
    async with conn.transaction():
        row = await conn.fetchrow(
            sql,
            ns_uuid,
            norm_provider,
            clean_client_id,
            encrypted_bytes,
            fingerprint,
            meta_json,
        )
        if not row:
            raise RuntimeError(f"Failed to upsert signing credentials for provider={norm_provider}")

        # Append WORM audit event
        await append_event(
            conn=conn,
            namespace_id=ns_uuid,
            agent_id=actor,
            event_type="config_changed",
            params={
                "actor": actor,
                "changes": {
                    "signing_credentials": {
                        "provider": norm_provider,
                        "client_id": clean_client_id,
                        "secret_fingerprint": fingerprint,
                        "action": "saved",
                    }
                },
                "reason": f"Operator saved signing credentials for provider '{norm_provider}'",
            },
        )

    return {
        "status": "ok",
        "id": str(row["id"]),
        "namespace_id": str(ns_uuid),
        "provider": norm_provider,
        "client_id": clean_client_id,
        "secret_fingerprint": fingerprint,
        "configured": True,
        "metadata": meta_dict,
        "created_at": row["created_at"].isoformat(),
        "updated_at": row["updated_at"].isoformat(),
    }


async def get_signing_credential_status(
    conn: asyncpg.Connection,
    namespace_id: uuid.UUID | str,
    provider: str | None = None,
) -> list[dict[str, Any]] | dict[str, Any]:
    """Retrieve non-secret credential status for operator inspection.

    NEVER returns plaintext secrets or encrypted_secret ciphertext.
    """
    ns_uuid = (
        uuid.UUID(str(namespace_id)) if not isinstance(namespace_id, uuid.UUID) else namespace_id
    )

    if provider:
        norm_provider = normalize_provider(provider)
        sql = """
            SELECT id, namespace_id, provider, client_id, secret_fingerprint, metadata, created_at, updated_at
            FROM signing_credentials
            WHERE namespace_id = $1 AND provider = $2;
        """
        row = await conn.fetchrow(sql, ns_uuid, norm_provider)
        if not row:
            return {
                "configured": False,
                "provider": norm_provider,
                "namespace_id": str(ns_uuid),
            }
        meta = row["metadata"]
        if isinstance(meta, str):
            meta = json.loads(meta)
        return {
            "configured": True,
            "id": str(row["id"]),
            "namespace_id": str(row["namespace_id"]),
            "provider": row["provider"],
            "client_id": row["client_id"],
            "secret_fingerprint": row["secret_fingerprint"],
            "metadata": meta,
            "created_at": row["created_at"].isoformat(),
            "updated_at": row["updated_at"].isoformat(),
        }

    sql = """
        SELECT id, namespace_id, provider, client_id, secret_fingerprint, metadata, created_at, updated_at
        FROM signing_credentials
        WHERE namespace_id = $1
        ORDER BY provider ASC;
    """
    rows = await conn.fetch(sql, ns_uuid)
    result = []
    for r in rows:
        meta = r["metadata"]
        if isinstance(meta, str):
            meta = json.loads(meta)
        result.append(
            {
                "configured": True,
                "id": str(r["id"]),
                "namespace_id": str(r["namespace_id"]),
                "provider": r["provider"],
                "client_id": r["client_id"],
                "secret_fingerprint": r["secret_fingerprint"],
                "metadata": meta,
                "created_at": r["created_at"].isoformat(),
                "updated_at": r["updated_at"].isoformat(),
            }
        )
    return result


async def delete_signing_credential(
    conn: asyncpg.Connection,
    namespace_id: uuid.UUID | str,
    provider: str,
    *,
    actor: str = "operator",
) -> dict[str, Any]:
    """Delete tenant signing credential for a provider."""
    ns_uuid = (
        uuid.UUID(str(namespace_id)) if not isinstance(namespace_id, uuid.UUID) else namespace_id
    )
    norm_provider = normalize_provider(provider)

    sql = "DELETE FROM signing_credentials WHERE namespace_id = $1 AND provider = $2 RETURNING id;"
    async with conn.transaction():
        row = await conn.fetchrow(sql, ns_uuid, norm_provider)
        if not row:
            return {"status": "not_found", "provider": norm_provider, "namespace_id": str(ns_uuid)}

        await append_event(
            conn=conn,
            namespace_id=ns_uuid,
            agent_id=actor,
            event_type="config_changed",
            params={
                "actor": actor,
                "changes": {
                    "signing_credentials": {
                        "provider": norm_provider,
                        "action": "deleted",
                    }
                },
                "reason": f"Operator deleted signing credentials for provider '{norm_provider}'",
            },
        )

    return {"status": "ok", "provider": norm_provider, "namespace_id": str(ns_uuid)}


async def resolve_signing_credential(
    conn: asyncpg.Connection | None,
    namespace_id: uuid.UUID | str,
    provider: str,
    *,
    precedence: PrecedenceOrder = "db_first",
    master_key: MasterKey | None = None,
) -> tuple[str | None, str | None, str]:
    """Resolve (client_id, client_secret, source) for a signing broker.

    Precedence behavior:
      - "db_first" (default): Checks tenant's encrypted database row first.
        If found, unwraps via master key. If absent, falls back to environment
        variables via secret_env(). Operator UI overrides container config.
      - "env_first": Checks environment variables first. If absent, falls back
        to tenant's encrypted database row. Enforces static infrastructure config.

    Returns:
      (client_id, client_secret, source) where source is 'db', 'env', or 'none'.
    """
    ns_uuid = (
        uuid.UUID(str(namespace_id)) if not isinstance(namespace_id, uuid.UUID) else namespace_id
    )
    norm_provider = normalize_provider(provider)

    def _check_env() -> tuple[str | None, str | None]:
        p_upper = norm_provider.upper()
        cid = (secret_env(f"NCE_SIGNING_{p_upper}_CLIENT_ID", "") or "").strip() or None
        sec = (secret_env(f"NCE_SIGNING_{p_upper}_CLIENT_SECRET", "") or "").strip() or None
        if cid and sec:
            return cid, sec
        return None, None

    async def _check_db() -> tuple[str | None, str | None]:
        if conn is None:
            return None, None
        sql = """
            SELECT client_id, encrypted_secret
            FROM signing_credentials
            WHERE namespace_id = $1 AND provider = $2;
        """
        row = await conn.fetchrow(sql, ns_uuid, norm_provider)
        if not row:
            return None, None
        cid = str(row["client_id"])
        enc_bytes = bytes(row["encrypted_secret"])
        if master_key is not None:
            decrypted = decrypt_signing_key(enc_bytes, master_key).decode("utf-8")
        else:
            with require_master_key() as mk:
                decrypted = decrypt_signing_key(enc_bytes, mk).decode("utf-8")
        return cid, decrypted

    if precedence == "db_first":
        db_cid, db_sec = await _check_db()
        if db_cid and db_sec:
            return db_cid, db_sec, "db"
        env_cid, env_sec = _check_env()
        if env_cid and env_sec:
            return env_cid, env_sec, "env"
        return None, None, "none"

    if precedence == "env_first":
        env_cid, env_sec = _check_env()
        if env_cid and env_sec:
            return env_cid, env_sec, "env"
        db_cid, db_sec = await _check_db()
        if db_cid and db_sec:
            return db_cid, db_sec, "db"
        return None, None, "none"

    raise ValueError(f"Unknown precedence: {precedence!r}")
