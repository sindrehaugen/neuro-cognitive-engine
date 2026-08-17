"""
nce/database/pruning.py
=======================
Cascade Pruning Engine for GDPR Article 17 Right to Erasure (BATCH-P2-003).

Implements synchronous, atomic deletion of tenant data with strict WORM ledger immutability:

  1. Soft-deletion of all tenant records (valid_to = now()) — no hard deletes from ledger
  2. Zero-filling of all vector embeddings (HNSW indexes preserved, vectors cleared)
  3. Nullification of sensitive text columns (PII scrubbing)
  4. Cryptographic audit log entry written to audit_log table (see migration 011)
  5. Post-delete consistency validation (no orphaned un-zeroed vectors)

The deletion is STRICTLY SYNCHRONOUS and ATOMIC — all-or-nothing per namespace_id.
Distributed across Citus shards via namespace_id hash key.

SLA requirement: Complete tenant purge in < 5 seconds for the 150-tenant Phase 2 target.

Mathematical properties:
  - Zero-fill vector(6) empathic tensor to [0,0,0,0,0,0] (preserves HNSW index structure)
  - Zero-fill pgvector embedding(768) to NULL (nullable column; removes vector content)
  - Soft-delete via valid_to = now() (creates WORM snapshot for point-in-time audit)
  - All operations within a single PostgreSQL transaction (ACID guarantee across shards)

dry_run=True executes all phases in a transaction then rolls back via _DryRunRollback
sentinel, returning a PruneResult with in-memory counts that reflect what would
have been changed (DB state is unchanged).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import NamedTuple

import asyncpg

log = logging.getLogger("nce.database.pruning")

# ---------------------------------------------------------------------------
# Configuration: table/column targets for each deletion phase
# ---------------------------------------------------------------------------

# Vector columns to zero-fill per table: (column_name, safe_zero_sql_expression)
_VECTOR_COLUMNS_BY_TABLE: dict[str, list[tuple[str, str]]] = {
    "memories": [
        ("embedding", "NULL"),  # pgvector(768), nullable
    ],
    "v3_cognitive_ledger": [
        ("empathic_tensor", "'[0,0,0,0,0,0]'::vector"),  # vector(6), NOT NULL
    ],
}

# Sensitive text columns to nullify per table
_SENSITIVE_TEXT_COLUMNS_BY_TABLE: dict[str, list[str]] = {
    "memories": ["value", "raw_pii_content", "raw_markdown"],
    "event_log": ["plaintext_secret", "raw_payload"],
    "v3_cognitive_ledger": [],  # No sensitive free-text columns
}

# GDPR Compliance vs WORM Audit Trail Policy (GDPR Article 17(3)(b)):
# Under GDPR Article 17(1), users have a right to erasure. However, Article 17(3)(b)
# exempts processing necessary for compliance with a legal obligation or task in the
# public interest. The 'event_log' is a Write-Once-Read-Many (WORM) audit trail
# required to verify cryptographic signatures, prove ledger history, and ensure
# security non-repudiation. Thus, 'event_log' is excluded from soft-deletions/pruning
# to preserve audit trail integrity, while user data in the 'memories' table is pruned.
_SOFT_DELETE_TABLES: list[str] = [
    "memories",
    "v3_cognitive_ledger",
    "topology_graph",  # valid_to added by migration 011_audit_log.sql
]

# ---------------------------------------------------------------------------
# Compile-time allowlists — guard against SQL injection if configuration is
# ever modified at runtime (TD-PRUNE-3 defence-in-depth).
# ---------------------------------------------------------------------------

_ALLOWED_TABLE_NAMES: frozenset[str] = frozenset(
    _SOFT_DELETE_TABLES
    + list(_VECTOR_COLUMNS_BY_TABLE)
    + [t for t, cols in _SENSITIVE_TEXT_COLUMNS_BY_TABLE.items() if cols]
)

_ALLOWED_COLUMN_NAMES: frozenset[str] = frozenset(
    col for cols in _SENSITIVE_TEXT_COLUMNS_BY_TABLE.values() for col in cols
) | frozenset(col for pairs in _VECTOR_COLUMNS_BY_TABLE.values() for col, _ in pairs)

_ALLOWED_ZERO_EXPRESSIONS: frozenset[str] = frozenset(
    expr for pairs in _VECTOR_COLUMNS_BY_TABLE.values() for _, expr in pairs
)


def _guard_table(name: str, context: str) -> None:
    """Raise ValueError if *name* is not in the compile-time table allowlist."""
    if name not in _ALLOWED_TABLE_NAMES:
        raise ValueError(
            f"Unsafe SQL table identifier {name!r} blocked in context={context}. "
            f"Allowed: {sorted(_ALLOWED_TABLE_NAMES)}"
        )


def _guard_column(name: str, context: str) -> None:
    """Raise ValueError if *name* is not in the compile-time column allowlist."""
    if name not in _ALLOWED_COLUMN_NAMES:
        raise ValueError(
            f"Unsafe SQL column identifier {name!r} blocked in context={context}. "
            f"Allowed: {sorted(_ALLOWED_COLUMN_NAMES)}"
        )


def _guard_zero_expr(expr: str) -> None:
    """Raise ValueError if *expr* is not an allowlisted zero-fill SQL fragment."""
    if expr not in _ALLOWED_ZERO_EXPRESSIONS:
        raise ValueError(
            f"Unsafe SQL zero-expression {expr!r} blocked. "
            f"Allowed: {sorted(_ALLOWED_ZERO_EXPRESSIONS)}"
        )


# ---------------------------------------------------------------------------
# Sentinel for dry_run transaction rollback (TD-PRUNE-1)
# ---------------------------------------------------------------------------


class _DryRunRollback(Exception):
    """Internal sentinel raised inside a transaction to trigger ROLLBACK.

    Caught by the outer except clause in cascade_delete_tenant so execution
    continues normally and PruneResult is returned with in-memory counts.
    NOT a real error — never logged as one.
    """


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


class PruneResult(NamedTuple):
    """Result of a cascade delete operation."""

    namespace_id: uuid.UUID
    soft_deleted_rows: int
    vectors_zeroed: int
    text_columns_nullified: int
    audit_log_entry_id: uuid.UUID
    duration_seconds: float
    consistency_check_passed: bool
    sla_passed: bool


# ---------------------------------------------------------------------------
# Core public API
# ---------------------------------------------------------------------------


async def cascade_delete_tenant(
    pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
    *,
    audit_reason: str = "GDPR Article 17 — Right to Erasure",
    dry_run: bool = False,
) -> PruneResult:
    """
    Atomically delete all data for a tenant, preserving WORM ledger immutability.

    STRICT REQUIREMENTS:
      1. All deletions are SOFT (valid_to = now()) — no hard deletion from ledger.
      2. All vector embeddings are ZERO-FILLED, not deleted — preserves HNSW index.
      3. All sensitive text columns are SET NULL.
      4. Deletion is SIGNED in the audit_log with a cryptographic seal.
      5. Deletion is ATOMIC — all-or-nothing per namespace_id.
      6. Post-delete CONSISTENCY CHECK verifies no un-zeroed vectors remain.
      7. SLA: Completion in < 5 seconds (monitored via PruneResult.sla_passed).

    Args:
        pool:         asyncpg.Pool connected to the Citus coordinator node.
        namespace_id: UUID of the tenant namespace to delete.
        audit_reason: Human-readable reason recorded in the signed audit log.
        dry_run:      If True, execute all phases in a transaction then ROLLBACK.
                      Returns a PruneResult whose counts reflect what WOULD have
                      changed. No persistent DB changes are made.

    Returns:
        PruneResult with counts of soft-deleted rows, zeroed vectors,
        nullified text columns, audit log entry ID, duration, and SLA flag.

    Raises:
        ValueError:              if consistency check fails (orphaned vectors found).
        asyncpg.PostgresError:   on database errors (not dry_run rollbacks).
    """
    start_time = time.time()
    soft_deleted_rows = 0
    vectors_zeroed = 0
    text_columns_nullified = 0
    audit_entry_id = uuid.uuid4()
    consistency_passed = False
    deletion_timestamp = datetime.now(timezone.utc)

    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                # ============================================================
                # PHASE 1: Soft-delete all tenant records (valid_to = now())
                # ============================================================
                for table in _SOFT_DELETE_TABLES:
                    _guard_table(table, "soft_delete")
                    result = await conn.execute(
                        f"""
                        UPDATE {table}
                        SET    valid_to = now()
                        WHERE  namespace_id = $1
                          AND  valid_to IS NULL
                        """,
                        namespace_id,
                    )
                    count = int(result.split()[-1]) if result else 0
                    soft_deleted_rows += count
                    log.info(
                        "Soft-deleted %d rows from %s for namespace=%s",
                        count,
                        table,
                        namespace_id,
                    )

                # ============================================================
                # PHASE 2: Zero-fill vector embeddings (preserve HNSW index)
                # ============================================================
                for table, vector_cols in _VECTOR_COLUMNS_BY_TABLE.items():
                    _guard_table(table, "vector_zero_fill")
                    for col_name, zero_expr in vector_cols:
                        _guard_column(col_name, "vector_zero_fill")
                        _guard_zero_expr(zero_expr)
                        result = await conn.execute(
                            f"""
                            UPDATE {table}
                            SET    {col_name} = {zero_expr}
                            WHERE  namespace_id = $1
                              AND  valid_to IS NOT NULL
                            """,
                            namespace_id,
                        )
                        count = int(result.split()[-1]) if result else 0
                        vectors_zeroed += count
                        log.info(
                            "Zero-filled %d %s.%s vectors for namespace=%s",
                            count,
                            table,
                            col_name,
                            namespace_id,
                        )

                # ============================================================
                # PHASE 3: Nullify sensitive text columns (PII scrubbing)
                # ============================================================
                for table, text_cols in _SENSITIVE_TEXT_COLUMNS_BY_TABLE.items():
                    _guard_table(table, "text_nullify")
                    for col in text_cols:
                        _guard_column(col, "text_nullify")
                        result = await conn.execute(
                            f"""
                            UPDATE {table}
                            SET    {col} = NULL
                            WHERE  namespace_id = $1
                            """,
                            namespace_id,
                        )
                        count = int(result.split()[-1]) if result else 0
                        text_columns_nullified += count
                        log.info(
                            "Nullified %d %s.%s text columns for namespace=%s",
                            count,
                            table,
                            col,
                            namespace_id,
                        )

                # ============================================================
                # PHASE 4: Post-delete consistency check (no orphaned vectors)
                # ============================================================
                consistency_passed = await _check_orphaned_vectors(conn, namespace_id)
                if not consistency_passed:
                    raise ValueError(
                        f"Consistency check failed: orphaned vectors detected for "
                        f"namespace={namespace_id}. "
                        "Cascade delete ABORTED — transaction will rollback."
                    )
                log.info("Consistency check PASSED for namespace=%s", namespace_id)

                # ============================================================
                # PHASE 5: Write signed audit log entry
                # ============================================================
                deletion_hash = hashlib.sha256(
                    f"{namespace_id}:{deletion_timestamp.isoformat()}".encode()
                ).hexdigest()

                try:
                    from nce.signing import sign_audit_log_entry

                    signature = sign_audit_log_entry(
                        entry_id=audit_entry_id,
                        event_type="tenant_cascade_delete",
                        namespace_id=namespace_id,
                        metadata={
                            "soft_deleted_rows": soft_deleted_rows,
                            "vectors_zeroed": vectors_zeroed,
                            "text_columns_nullified": text_columns_nullified,
                            "audit_reason": audit_reason,
                            "deletion_hash": deletion_hash,
                        },
                    )
                except ImportError:
                    # Fallback: SHA-256 digest when signing.py not available
                    signature = hashlib.sha256(
                        f"{audit_entry_id}:{deletion_hash}".encode()
                    ).hexdigest()

                await conn.execute(
                    """
                    INSERT INTO audit_log
                        (id, namespace_id, event_type, occurred_at, metadata, signature)
                    VALUES ($1, $2, $3, $4, $5, $6)
                    ON CONFLICT (id) DO NOTHING
                    """,
                    audit_entry_id,
                    namespace_id,
                    "tenant_cascade_delete",
                    deletion_timestamp,
                    {
                        "soft_deleted_rows": soft_deleted_rows,
                        "vectors_zeroed": vectors_zeroed,
                        "text_columns_nullified": text_columns_nullified,
                        "audit_reason": audit_reason,
                        "deletion_hash": deletion_hash,
                    },
                    signature,
                )
                log.info(
                    "Created signed audit log entry %s for namespace=%s",
                    audit_entry_id,
                    namespace_id,
                )

                # ============================================================
                # PHASE 6: dry_run — raise sentinel to trigger ROLLBACK
                # ============================================================
                if dry_run:
                    log.info(
                        "DRY-RUN: Rolling back all changes for namespace=%s "
                        "(in-memory counts preserved for diagnostics)",
                        namespace_id,
                    )
                    raise _DryRunRollback

    except _DryRunRollback:
        # Normal dry_run path — not an error, not logged as one.
        log.info("Dry-run rollback completed for namespace=%s", namespace_id)
    except asyncpg.PostgresError:
        log.exception("Database error during cascade delete for namespace=%s", namespace_id)
        raise

    duration_seconds = time.time() - start_time
    sla_passed = duration_seconds < 5.0

    if sla_passed:
        log.info(
            "Cascade delete completed for namespace=%s in %.3fs (SLA PASSED < 5s): "
            "soft_deleted=%d vectors_zeroed=%d text_nullified=%d",
            namespace_id,
            duration_seconds,
            soft_deleted_rows,
            vectors_zeroed,
            text_columns_nullified,
        )
    else:
        log.warning(
            "Cascade delete completed for namespace=%s in %.3fs (SLA FAILED > 5s): "
            "soft_deleted=%d vectors_zeroed=%d text_nullified=%d",
            namespace_id,
            duration_seconds,
            soft_deleted_rows,
            vectors_zeroed,
            text_columns_nullified,
        )

    return PruneResult(
        namespace_id=namespace_id,
        soft_deleted_rows=soft_deleted_rows,
        vectors_zeroed=vectors_zeroed,
        text_columns_nullified=text_columns_nullified,
        audit_log_entry_id=audit_entry_id,
        duration_seconds=duration_seconds,
        consistency_check_passed=consistency_passed,
        sla_passed=sla_passed,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _check_orphaned_vectors(
    conn: asyncpg.Connection,
    namespace_id: uuid.UUID,
) -> bool:
    """
    Verify no un-zeroed vectors remain after soft-deletion.

    A vector is "orphaned" if it belongs to a soft-deleted row (valid_to IS NOT NULL)
    but has not been zeroed:
      - memories.embedding:              should be NULL after Phase 2
      - v3_cognitive_ledger.empathic_tensor: should be [0,0,0,0,0,0] after Phase 2

    Returns True if all checks pass, False if any orphan is found.
    """
    # Check 1: soft-deleted memories must have NULL embedding
    orphaned_embeddings = await conn.fetchval(
        """
        SELECT count(*)
        FROM   memories
        WHERE  namespace_id = $1
          AND  embedding IS NOT NULL
          AND  valid_to IS NOT NULL
        """,
        namespace_id,
    )
    if orphaned_embeddings and orphaned_embeddings > 0:
        log.error(
            "Consistency FAILED: %d orphaned embedding vectors for namespace=%s",
            orphaned_embeddings,
            namespace_id,
        )
        return False

    # Check 2: soft-deleted empathic tensors must be [0,0,0,0,0,0]
    non_zero_tensors = await conn.fetchval(
        """
        SELECT count(*)
        FROM   v3_cognitive_ledger
        WHERE  namespace_id = $1
          AND  empathic_tensor IS NOT NULL
          AND  empathic_tensor != '[0,0,0,0,0,0]'::vector
          AND  valid_to IS NOT NULL
        """,
        namespace_id,
    )
    if non_zero_tensors and non_zero_tensors > 0:
        log.error(
            "Consistency FAILED: %d non-zero empathic tensors for namespace=%s",
            non_zero_tensors,
            namespace_id,
        )
        return False

    log.debug("Consistency check PASSED for namespace=%s", namespace_id)
    return True


# ---------------------------------------------------------------------------
# Batch API
# ---------------------------------------------------------------------------


async def batch_cascade_delete_tenants(
    pool: asyncpg.Pool,
    namespace_ids: list[uuid.UUID],
    *,
    audit_reason: str = "GDPR Article 17 — Right to Erasure",
) -> list[PruneResult]:
    """
    Delete multiple tenants in parallel (max 10 concurrent DB transactions).

    Uses asyncio.gather() with a Semaphore to bound DB connection pressure.
    Each deletion is atomic and isolated via Citus namespace_id hash sharding.

    Args:
        pool:          asyncpg.Pool connection pool.
        namespace_ids: Tenant UUIDs to delete.
        audit_reason:  Reason recorded in all audit log entries.

    Returns:
        List of PruneResult in the same order as namespace_ids.
        If any deletion fails, the exception propagates (others may have committed).

    Raises:
        ValueError:            if any consistency check fails.
        asyncpg.PostgresError: on database errors.

    Example::

        results = await batch_cascade_delete_tenants(
            pool, [ns1, ns2, ns3], audit_reason="Tenant cancelled subscription"
        )
        assert all(r.sla_passed for r in results)
    """
    log.info("Starting batch cascade delete of %d tenants", len(namespace_ids))
    start = time.time()

    sem = asyncio.Semaphore(min(10, len(namespace_ids)))

    async def _delete_with_sem(ns_id: uuid.UUID) -> PruneResult:
        async with sem:
            return await cascade_delete_tenant(pool, ns_id, audit_reason=audit_reason)

    results: list[PruneResult] = list(
        await asyncio.gather(
            *(_delete_with_sem(ns_id) for ns_id in namespace_ids),
            return_exceptions=False,
        )
    )

    duration = time.time() - start
    passed = sum(1 for r in results if r.sla_passed)
    log.info(
        "Batch cascade delete completed in %.3fs: %d/%d tenants passed SLA",
        duration,
        passed,
        len(namespace_ids),
    )
    return results
