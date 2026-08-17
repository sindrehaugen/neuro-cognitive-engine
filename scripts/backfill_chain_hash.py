#!/usr/bin/env python3
"""
One-off migration: backfill chain_hash for existing event_log rows.

All rows inserted before the Merkle chain feature have ``chain_hash = NULL``.
This script walks every namespace in event_seq order, recomputes the
chain_hash deterministically, and UPDATEs each row.

Because ``event_log`` has a WORM trigger (``trg_event_log_worm``) that
rejects UPDATE/DELETE, the script temporarily disables the trigger,
performs the backfill, then re-enables it.  This requires a PostgreSQL
role with ``TRIGGER`` privilege on ``event_log`` (typically the owner
or a superuser).

Idempotent: safe to run multiple times.  Recomputes every chain_hash
and only UPDATEs rows where the stored value differs.

Usage::

    PG_DSN="postgresql://..." python scripts/backfill_chain_hash.py

Or via docker compose::

    docker compose exec admin python scripts/backfill_chain_hash.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import asyncpg

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nce.config import cfg
from nce.event_log import (
    _GENESIS_SENTINEL,
    _build_signing_fields,
    _compute_chain_hash,
    _compute_content_hash,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("backfill-chain-hash")

BATCH_SIZE = 500

_WORM_TRIGGER = "trg_event_log_worm"


async def _worm_trigger_enabled(conn: asyncpg.Connection) -> bool:
    """Return True when the WORM trigger is enabled (not DISABLED)."""
    row = await conn.fetchrow(
        """
        SELECT t.tgenabled
        FROM   pg_trigger t
        JOIN   pg_class c ON c.oid = t.tgrelid
        WHERE  c.relname = 'event_log'
          AND  NOT t.tgisinternal
          AND  t.tgname = $1
        """,
        _WORM_TRIGGER,
    )
    if row is None:
        raise RuntimeError(f"WORM trigger {_WORM_TRIGGER!r} not found on event_log")
    # tgenabled: O=origin enabled, D=disabled, A=always, R=replica
    return row["tgenabled"] != "D"


async def _set_worm_trigger(conn: asyncpg.Connection, *, enabled: bool) -> None:
    state = "ENABLE" if enabled else "DISABLE"
    await conn.execute(f"ALTER TABLE event_log {state} TRIGGER {_WORM_TRIGGER}")
    if await _worm_trigger_enabled(conn) != enabled:
        raise RuntimeError(
            f"Failed to {state.lower()} WORM trigger {_WORM_TRIGGER!r}; "
            "aborting to protect audit integrity."
        )


# ---------------------------------------------------------------------------
# Backfill logic
# ---------------------------------------------------------------------------


async def _fetch_namespaces_with_nulls(conn: asyncpg.Connection) -> list[uuid.UUID]:
    """Return namespace_ids that have at least one row with chain_hash IS NULL."""
    rows = await conn.fetch(
        """
        SELECT DISTINCT namespace_id
        FROM   event_log
        WHERE  chain_hash IS NULL
        """
    )
    return [r["namespace_id"] for r in rows]


async def _fetch_events_for_namespace(
    conn: asyncpg.Connection, namespace_id: uuid.UUID
) -> list[asyncpg.Record]:
    """Return every event for *namespace_id* ordered by event_seq ASC."""
    rows = await conn.fetch(
        """
        SELECT id, namespace_id, agent_id, event_type, event_seq,
               occurred_at, params, parent_event_id, chain_hash
        FROM   event_log
        WHERE  namespace_id = $1
        ORDER BY event_seq ASC
        """,
        namespace_id,
    )
    return rows


def _coerce_chain_hash(val: Any) -> bytes | None:
    if val is None:
        return None
    if isinstance(val, memoryview):
        return bytes(val)
    if isinstance(val, bytes):
        return val
    return None


def _recompute_chain_hash(row: asyncpg.Record, previous_chain_hash: bytes) -> bytes:
    """Recompute the chain_hash for a single event row."""
    params: dict[str, Any] | None = row.get("params")
    if params is None:
        params = {}
    elif isinstance(params, str):
        params = json.loads(params)

    occurred_at = row.get("occurred_at")
    if isinstance(occurred_at, datetime):
        occurred_at_iso = occurred_at.astimezone(timezone.utc).isoformat()
    elif isinstance(occurred_at, str):
        occurred_at_iso = occurred_at
    else:
        occurred_at_iso = str(occurred_at)

    signing_fields = _build_signing_fields(
        event_id=row["id"],
        namespace_id=row["namespace_id"],
        agent_id=row["agent_id"],
        event_type=row["event_type"],
        event_seq=int(row["event_seq"]),
        occurred_at_iso=occurred_at_iso,
        params=params,
        parent_event_id=row.get("parent_event_id"),
    )
    content_hash = _compute_content_hash(signing_fields=signing_fields)
    return _compute_chain_hash(content_hash=content_hash, previous_chain_hash=previous_chain_hash)


async def _backfill_namespace(conn: asyncpg.Connection, namespace_id: uuid.UUID) -> tuple[int, int]:
    """
    Backfill chain_hash for every event in *namespace_id*.

    Returns (checked, updated).
    """
    rows = await _fetch_events_for_namespace(conn, namespace_id)
    if not rows:
        return 0, 0

    previous_chain_hash = _GENESIS_SENTINEL
    checked = 0
    updated = 0

    for row in rows:
        expected = _recompute_chain_hash(row, previous_chain_hash)
        stored = _coerce_chain_hash(row.get("chain_hash"))

        if stored != expected:
            await conn.execute(
                """
                UPDATE event_log
                SET chain_hash = $1
                WHERE id = $2
                  AND namespace_id = $3
                  AND event_seq = $4
                """,
                expected,
                row["id"],
                namespace_id,
                row["event_seq"],
            )
            updated += 1

        previous_chain_hash = expected
        checked += 1

    return checked, updated


async def _main() -> int:
    dsn = os.getenv("PG_DSN") or cfg.PG_DSN
    logger.info("Connecting to PostgreSQL …")
    conn = await asyncpg.connect(dsn)

    try:
        logger.info("Checking for rows with chain_hash IS NULL …")
        namespaces = await _fetch_namespaces_with_nulls(conn)
        if not namespaces:
            logger.info("No rows with NULL chain_hash found. Nothing to do.")
            return 0

        logger.info("Found %d namespace(s) needing backfill.", len(namespaces))

        if await _worm_trigger_enabled(conn):
            logger.warning(
                "WORM trigger is enabled; disabling temporarily for chain_hash backfill."
            )
        logger.info("Disabling WORM trigger %s …", _WORM_TRIGGER)
        await _set_worm_trigger(conn, enabled=False)

        total_checked = 0
        total_updated = 0
        for ns_id in namespaces:
            checked, updated = await _backfill_namespace(conn, ns_id)
            total_checked += checked
            total_updated += updated
            logger.info(
                "Namespace %s: checked=%d updated=%d",
                ns_id,
                checked,
                updated,
            )

        logger.info(
            "Backfill complete: %d namespaces, %d events checked, %d events updated.",
            len(namespaces),
            total_checked,
            total_updated,
        )

        logger.info("Re-enabling WORM trigger %s …", _WORM_TRIGGER)
        await _set_worm_trigger(conn, enabled=True)

        return 0
    except Exception as exc:
        logger.exception("Backfill failed: %s", exc)
        try:
            await _set_worm_trigger(conn, enabled=True)
            logger.info("WORM trigger re-enabled after error.")
        except Exception:
            logger.critical(
                "CRITICAL: Could not re-enable WORM trigger %s. Manual intervention required.",
                _WORM_TRIGGER,
            )
        return 1
    finally:
        await conn.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
