#!/usr/bin/env python3
"""
One-off migration: encrypt legacy plaintext OAuth tokens in bridge_subscriptions.

Early Phase 0/1 deployments stored raw token strings directly in
``oauth_access_token_enc`` before the ``encrypt_signing_key`` wire format
was introduced.  This script detects such legacy blobs and re-encrypts
them using the canonical ``bridge_repo.save_token`` format.

Idempotent: rows that are already properly encrypted are skipped.

Usage::

    PG_DSN="postgresql://..." python scripts/migrate_bridge_tokens.py

Or via docker compose::

    docker compose exec admin python scripts/migrate_bridge_tokens.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import uuid
from pathlib import Path
from typing import Any

import asyncpg

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nce.config import cfg
from nce.signing import (
    SigningKeyDecryptionError,
    decrypt_signing_key,
    encrypt_signing_key,
    require_master_key,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("migrate-bridge-tokens")

BATCH_SIZE = 100


def _is_valid_encrypted_blob(blob: bytes) -> bool:
    """Return True if *blob* looks like a properly encrypted signing key blob."""
    # All encrypt_signing_key outputs start with a version prefix:
    # TC2\x01, TC3\x01, TC4\x01, or legacy TC1\x01
    return blob.startswith((b"TC1\x01", b"TC2\x01", b"TC3\x01", b"TC4\x01"))


def _attempt_decrypt(blob: bytes) -> bytes | None:
    """Try to decrypt *blob*; return plaintext bytes or None on failure."""
    mk = require_master_key()
    try:
        return decrypt_signing_key(blob, mk)
    except SigningKeyDecryptionError:
        return None
    except Exception as exc:
        logger.debug("Decryption raised %s: %s", type(exc).__name__, exc)
        return None


def _coerce_to_json_dict(plaintext: bytes) -> dict[str, Any]:
    """Turn *plaintext* (JSON string or raw token) into a dict payload."""
    try:
        parsed = json.loads(plaintext.decode("utf-8"))
        if isinstance(parsed, dict):
            return parsed
    except (json.JSONDecodeError, UnicodeDecodeError):
        pass
    # Raw token string → wrap in minimal dict
    return {"access_token": plaintext.decode("utf-8", errors="replace")}


async def _process_row(
    conn: asyncpg.Connection,
    row: asyncpg.Record,
) -> tuple[bool, str]:
    """
    Inspect and migrate a single bridge_subscriptions row.

    Returns (updated, reason).
    """
    bridge_id: uuid.UUID = row["id"]
    raw_blob: bytes | None = row.get("oauth_access_token_enc")

    if raw_blob is None:
        return False, "null"

    if isinstance(raw_blob, memoryview):
        raw_blob = bytes(raw_blob)

    if _is_valid_encrypted_blob(raw_blob):
        # Looks encrypted — try decrypt to verify it is actually valid
        plaintext = _attempt_decrypt(raw_blob)
        if plaintext is not None:
            try:
                parsed = json.loads(plaintext.decode("utf-8"))
                if isinstance(parsed, dict) and "access_token" in parsed:
                    return False, "already_encrypted"
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass
            # Decrypts but is not a JSON dict → old format, needs re-encrypt
            payload = _coerce_to_json_dict(plaintext)
        else:
            # Looks encrypted but decrypt fails → corrupted or plaintext with
            # coincidental prefix. Treat as plaintext.
            payload = _coerce_to_json_dict(raw_blob)
    else:
        # Definitely plaintext (no version prefix)
        payload = _coerce_to_json_dict(raw_blob)

    # Re-encrypt via canonical format
    canonical_json = json.dumps(payload, sort_keys=True).encode("utf-8")
    with require_master_key() as mk:
        new_blob = encrypt_signing_key(canonical_json, mk)

    await conn.execute(
        """
        UPDATE bridge_subscriptions
        SET oauth_access_token_enc = $1,
            updated_at = NOW()
        WHERE id = $2
        """,
        new_blob,
        bridge_id,
    )
    return True, "migrated"


async def _main() -> int:
    dsn = os.getenv("PG_DSN") or cfg.PG_DSN
    logger.info("Connecting to PostgreSQL …")
    conn = await asyncpg.connect(dsn)

    try:
        rows = await conn.fetch(
            """
            SELECT id, oauth_access_token_enc
            FROM   bridge_subscriptions
            WHERE  oauth_access_token_enc IS NOT NULL
            ORDER BY updated_at DESC
            """
        )

        if not rows:
            logger.info("No rows with non-NULL oauth_access_token_enc found.")
            return 0

        logger.info("Found %d row(s) to inspect.", len(rows))

        migrated = 0
        skipped = 0
        errors = 0

        for row in rows:
            try:
                async with conn.transaction():
                    updated, reason = await _process_row(conn, row)
                if updated:
                    migrated += 1
                    logger.info("Migrated %s (reason=%s)", row["id"], reason)
                else:
                    skipped += 1
                    logger.debug("Skipped %s (reason=%s)", row["id"], reason)
            except Exception as exc:
                errors += 1
                logger.exception("Failed to process %s: %s", row["id"], exc)

        logger.info(
            "Migration complete: %d migrated, %d skipped, %d errors out of %d rows.",
            migrated,
            skipped,
            errors,
            len(rows),
        )
        return 0 if errors == 0 else 1
    except Exception as exc:
        logger.exception("Migration failed: %s", exc)
        return 1
    finally:
        await conn.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
