"""
NCE append-only tamper-resistant event log writer (Phase 0.2 / 2.3).

Design contract
---------------

WORM guarantee
    This module has NO UPDATE or DELETE code paths.  All writes are INSERT-only.
    The Postgres role-level enforcement (``REVOKE UPDATE, DELETE ON event_log
    FROM nce_app``) is the primary WORM layer; this module is defence-in-depth.

Atomicity with Saga
    ``append_event()`` must be called inside an EXISTING asyncpg transaction
    (the Saga coordinator has already issued BEGIN).  This function never
    commits, rolls back, or opens its own transaction.  A rolled-back Saga
    produces no ``event_log`` entry — atomicity is guaranteed by the shared
    transaction.

Gap-free per-namespace sequence numbers
    ``event_seq`` is monotonically increasing per ``namespace_id`` within a single
    transaction (rolled-back allocations are not consumed).  Allocation is atomic
    via ``event_sequences``: ``INSERT … ON CONFLICT DO UPDATE``
    keyed by ``namespace_id`` (single-row upsert → row-level exclusion, no advisory lock).

Signature over DB-provided timestamp
    ``occurred_at`` is always set by the DB (``clock_timestamp()`` fetched in a
    preliminary round-trip) so the signature covers the value that is actually
    stored.  This closes the window where a Python-side clock and the DB clock
    diverge.

Point-in-time write rejection (D8)
    ``occurred_at`` is always ``now()`` on the DB.  The ``params`` dict must NOT
    contain a ``valid_from`` field with a past timestamp; this is validated
    before the INSERT.  Raw SQL back-dating is blocked by the DB schema
    (``DEFAULT now()``) and the WORM role grants.

Usage
-----
::

    async with conn.transaction():          # Saga coordinator opens the TX
        # ... other Saga mutations ...
        result = await append_event(
            conn=conn,
            namespace_id=ns_id,
            agent_id="retrieval-agent",
            event_type="store_memory",
            params={
                "saga_id": str(uuid.uuid4()),
                "memory_id": str(memory_id),
                "payload_ref": "507f1f77bcf86cd799439011",
                "assertion_type": "fact",
                "entities": [],
                "triplets": [],
            },
        )
        # result.event_id, result.event_seq, result.occurred_at are now set.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Final

import asyncpg

from nce.config import cfg
from nce.correlation import get_correlation_id
from nce.event_types import (
    EVENT_FORBIDDEN_PARAM_KEYS,
    EVENT_REQUIRED_PARAM_KEYS,
    VALID_EVENT_TYPES,
    EventType,
)
from nce.signing import SigningError, get_active_key, sign_fields

log = logging.getLogger(__name__)

# Maximum length of agent_id (per spec D4 + auth.py convention).
_AGENT_ID_MAX_LEN: Final[int] = 128

# Genesis sentinel for Merkle chain hash — 32 zero bytes.
# The first event in a namespace uses this as its "previous chain hash"
# input rather than fetching a non-existent prior row.
_GENESIS_SENTINEL: Final[bytes] = b"\x00" * 32

# Plain identifiers only (tables in public schema probes) — no quoting of schema-qualified names.
_IDENTIFIER_RE: Final[re.Pattern[str]] = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]{0,62}$")


def _validate_identifier(name: str) -> str:
    """Return *name* if it matches safe SQL identifier rules; else raise ``ValueError``."""
    if not _IDENTIFIER_RE.fullmatch(name):
        raise ValueError(f"Invalid SQL identifier: {name!r}")
    return name


# ---------------------------------------------------------------------------
# Public exceptions
# ---------------------------------------------------------------------------


class EventLogError(Exception):
    """Base class for event log write errors."""


class InvalidEventTypeError(EventLogError):
    """Raised when *event_type* is not in the allowed set."""


class EventLogTimestampError(EventLogError):
    """
    Raised if a backdated ``valid_from`` is detected in *params* (D8).

    The primary enforcement is the DB clock (``clock_timestamp()``); this is
    defence-in-depth at the Python layer.
    """


class EventLogSequenceError(EventLogError):
    """
    Raised when sequence allocation fails unexpectedly.

    Under correct usage (``event_sequences`` upsert inside the Saga TX) this should
    be rare — most often indicates a conflicting ``event_seq`` on INSERT or DB
    misconfiguration.
    """


class EventLogSigningError(EventLogError):
    """Raised when the signing key cannot be loaded or the HMAC step fails."""


class DataIntegrityError(EventLogError):
    """
    Raised when an event log row fails HMAC verification during read.
    Indicates that the row has been tampered with post-insertion.
    """


# Public surface — callers only need to import from this module.
__all__ = [
    "EventType",
    "AppendResult",
    "append_event",
    "verify_worm_enforcement",
    "verify_worm_on_table",
    "_WORM_TABLES",
    "verify_rls_enforcement",
    "verify_rls_catalog_consistency",
    "EXPECTED_TENANT_RLS_TABLES",
    "EXPECTED_SPECIAL_RLS_TABLES",
    "EXPECTED_GLOBAL_TABLES",
    "EventLogError",
    "InvalidEventTypeError",
    "EventLogTimestampError",
    "EventLogSequenceError",
    "EventLogSigningError",
    "DataIntegrityError",
    "verify_event_signature",
    "verify_merkle_chain",
]


# ---------------------------------------------------------------------------
# WORM enforcement probe
# ---------------------------------------------------------------------------

# Tables expected to be append-only (no UPDATE/DELETE at the application role).
_WORM_TABLES: tuple[str, ...] = ("event_log", "event_parents")


async def verify_worm_on_table(conn: asyncpg.Connection, table_name: str) -> None:
    """
    Runtime assertion that UPDATE and DELETE are denied on *table_name*.

    Attempts a dummy ``UPDATE`` and ``DELETE`` with a ``WHERE FALSE`` clause
    so that no rows are ever modified.  If either statement succeeds (i.e. the
    DB role has UPDATE/DELETE privileges), a ``RuntimeError`` is raised to
    **halt server startup** — the WORM guarantee is broken for that table.

    Parameters
    ----------
    conn
        An open asyncpg connection authenticated as the application role.
    table_name
        The name of the table to probe (must have an ``id`` column).

    Raises
    ------
    RuntimeError
        If UPDATE or DELETE succeeds on the target table.
    asyncpg.exceptions.InsufficientPrivilegeError
        Expected — caught internally; the function returns normally.
    asyncpg.PostgresError
        Propagated for unexpected DB errors (e.g. table missing).
    """
    table_name = _validate_identifier(table_name)
    if cfg.NCE_BYPASS_WORM:
        log.warning("[worm-probe] Bypassing WORM verification for table %s", table_name)
        return

    # Set dummy namespace ID session setting so RLS policies don't fail during WORM checks
    try:
        await conn.execute("SET nce.namespace_id = '00000000-0000-0000-0000-000000000000'")
    except Exception:
        pass

    # Probe 1: UPDATE
    try:
        try:
            await conn.execute(f"UPDATE {table_name} SET id = id WHERE FALSE")
        except asyncpg.exceptions.UndefinedColumnError:
            # Fallback for tables without 'id' (e.g., event_parents using 'event_id')
            await conn.execute(f"UPDATE {table_name} SET event_id = event_id WHERE FALSE")
    except asyncpg.exceptions.InsufficientPrivilegeError:
        log.info("[worm-probe] %s: UPDATE denied ✅", table_name)
    else:
        raise RuntimeError(
            f"WORM ENFORCEMENT FAILED: UPDATE on {table_name} succeeded.  "
            f"The database role has UPDATE privileges on the {table_name} table.  "
            f"The server cannot start without append-only guarantees.  "
            f"Check the REVOKE/GRANT statements in schema.sql."
        )

    # Probe 2: DELETE
    try:
        await conn.execute(f"DELETE FROM {table_name} WHERE FALSE")
    except asyncpg.exceptions.InsufficientPrivilegeError:
        log.info("[worm-probe] %s: DELETE denied ✅", table_name)
    else:
        raise RuntimeError(
            f"WORM ENFORCEMENT FAILED: DELETE on {table_name} succeeded.  "
            f"The database role has DELETE privileges on the {table_name} table.  "
            f"The server cannot start without append-only guarantees.  "
        )


async def verify_worm_enforcement(conn: asyncpg.Connection) -> None:
    """Probe WORM enforcement on every table listed in ``_WORM_TABLES``."""
    for worm_table in _WORM_TABLES:
        await verify_worm_on_table(conn, worm_table)


# ---------------------------------------------------------------------------
# RLS enforcement probe
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# RLS intent declarations — authoritative source for catalog verification.
# These are validated against pg_class / pg_policies at startup via
# verify_rls_catalog_consistency(). Do not add entries here without
# confirming the table exists and has RLS + namespace_id in schema.sql.
# ---------------------------------------------------------------------------

EXPECTED_TENANT_RLS_TABLES: dict[str, str] = {
    # table_name: namespace ownership column
    "memories": "namespace_id",
    "kg_nodes": "namespace_id",
    "kg_edges": "namespace_id",
    "pii_redactions": "namespace_id",
    "memory_salience": "namespace_id",
    "contradictions": "namespace_id",
    "snapshots": "namespace_id",
    "event_log": "namespace_id",
    "resource_quotas": "namespace_id",
    "outbox_events": "namespace_id",
    "saga_execution_log": "namespace_id",
    "consolidation_runs": "namespace_id",
    "bridge_subscriptions": "namespace_id",
    "dead_letter_queue": "namespace_id",
    "embedding_migrations": "namespace_id",
    "memory_embeddings": "namespace_id",
    "embedding_aspects": "namespace_id",
    "graph_schema_registry": "namespace_id",
    "query_templates": "namespace_id",
    "v3_cognitive_ledger": "namespace_id",
    "topology_graph": "namespace_id",
    "audit_log": "namespace_id",
    "active_learning_queue": "namespace_id",
    "d365_integrations": "namespace_id",
    "d365_netbox_mappings": "namespace_id",
    "d365_sync_runs": "namespace_id",
    "d365_delta_tokens": "namespace_id",
    "processed_outbox_events": "namespace_id",
    "actor_trust": "namespace_id",
    "event_parents": "namespace_id",
    "action_approval_queue": "namespace_id",
    "action_idempotency": "namespace_id",
    # Diagnostics engine (prod hotfix) — tenant-scoped diagnostic state.
    # (Also added independently on rl/refactor-sequence; merged 1:1, no dup keys.)
    "diag_ingestions": "namespace_id",
    "diag_anomalies": "namespace_id",
    "device_health_rollup": "namespace_id",
    # NOTE: d365_sync_runs / d365_delta_tokens (migrations 023/024) are declared
    # above; ml/foundation re-declared them here — kept single to avoid dup keys.
    # Shared-core foundation (Module 0): C1 entity-resolution + C5 source-mode.
    "node_ownership_registry": "namespace_id",
    "entity_merge_queue": "namespace_id",
    "source_mode_config": "namespace_id",
    "divergence_log": "namespace_id",
    # Module 0, Wave 31 (Batch 132a): shared, top-level BOM_LINE content
    # store -- written by both system_design and sales, never engine-prefixed.
    "bom_line_content": "namespace_id",
    # Product engine (Module 2): prices (W2), match feedback (W6),
    # enrichment review log (W7). Each new RLS table MUST be declared here.
    # product_catalog is NOT here: it is a global shared parts library, see
    # EXPECTED_GLOBAL_TABLES. product_prices stays tenant-scoped because
    # supplier-bid pricing is per-tenant commercial confidential.
    "product_prices": "namespace_id",
    "product_match_feedback": "namespace_id",
    "product_enrichment_log": "namespace_id",
    # Procurement engine (Module 1): consumer cache for Product's BID projections (W5).
    "procurement_bid_prices": "namespace_id",
    # System Design engine (Module 6) Phase-2: device capability attributes (W12).
    "system_design_device_capabilities": "namespace_id",
    # System Design engine (Module 6) W14: canvas geometry + the per-DESIGN
    # optimistic-concurrency version row (two key grains, one table).
    "system_design_geometry": "namespace_id",
    # System Design engine (Module 6) W16: per-node lifecycle state (status /
    # revision / salience) for DEVICE / RACK / CABLE. A node with no row here
    # has no state -- absence is meaningful and is never defaulted.
    "system_design_node_state": "namespace_id",
    # Sales engine (Module 5): read model (W2) + targets (W2) + signed baselines (W8)
    "sales_read_model": "namespace_id",
    "sales_targets": "namespace_id",
    "sales_signed_baselines": "namespace_id",
    "vendor_scorecards": "namespace_id",
    "contractor_profiles": "namespace_id",
    # Agreements Engine (Module 3): review queue (W2) & extraction runs (W2).
    "agreement_review_queue": "namespace_id",
    "agreement_extraction_runs": "namespace_id",
    # Economy engine (Module 8): BOM-line actual-cost cascade write-path (W5).
    "economy_bom_actual_costs": "namespace_id",
    # Economy engine (Module 8): balanced-ledger table behind the POSTING
    # kg_node (W6).
    "economy_postings": "namespace_id",
    # Economy engine (Module 8): recurring-revenue contract store (W10).
    "economy_contracts": "namespace_id",
    # Inventory engine (Module 11, Wave 1): locations-stock-tables.
    "stock_locations": "namespace_id",
    "inventory_items": "namespace_id",
    # Inventory engine (Module 11, Wave 11): append-only transaction ledger
    # (transactions-valuation).
    "inventory_transactions": "namespace_id",
    # Inventory engine (Module 11, Wave 4): goods-receipt record (Batch 132).
    "goods_receipts": "namespace_id",
    # Inventory engine (Module 11, Wave 10): returns/RMA + WEEE disposal state.
    "inventory_rma": "namespace_id",
    # Assets engine (Module 9, Wave 2): relational asset register (seed-from-bom).
    "assets": "namespace_id",
    # Assets engine (Module 9, Wave 5): manufacturer telemetry reading stream.
    "telemetry_samples": "namespace_id",
    # Support engine (Module 10, Wave 1): native service tickets, SLA clocks, customer health.
    "service_tickets": "namespace_id",
    "sla_clocks": "namespace_id",
    "customer_health": "namespace_id",
    # Field Tech engine (Module 12, Wave 1): work orders, checklists, time entries.
    "work_orders": "namespace_id",
    "checklists": "namespace_id",
    "time_entries": "namespace_id",
}


EXPECTED_SPECIAL_RLS_TABLES: dict[str, tuple[str, ...]] = {
    # table_name: all namespace ownership columns (multi-namespace ownership)
    "a2a_grants": ("owner_namespace_id", "target_namespace_id"),
}

EXPECTED_GLOBAL_TABLES: set[str] = {
    # Tables intentionally without RLS — shared across all tenants.
    "embedding_models",
    "kg_node_embeddings",
    "reembedding_runs",
    "event_sequences",
    # The product catalogue is a global shared parts library: a manufacturer
    # part number names the same physical part for every tenant (Sindre's
    # ruling, 2026-09-04; migration 064).
    "product_catalog",
    # Deployment state, not tenant data: which migration files this database
    # has applied (see nce/migration_ledger.py).
    "applied_migrations",
}


async def verify_rls_enforcement(conn: asyncpg.Connection, table_name: str) -> None:
    """
    Legacy runtime smoke test: unscoped ``SELECT count(*)`` expects zero rows.

    Empty tables return zero rows even when RLS is disabled, so this is weak as a guard.
    Prefer :func:`verify_rls_catalog_consistency` for authoritative policy semantics.

    Validate that RLS is active on *table_name* by attempting a ``SELECT``
    without a scoped ``nce.namespace_id``.

    When RLS is correctly configured via the ``namespace_isolation_policy``,
    the session variable falls back to ``NULL`` and all rows are filtered out
    (``namespace_id = NULL::uuid`` is never true).  A non-zero row count from
    an unscoped query signals that RLS is **not** in effect for that table.

    Parameters
    ----------
    conn
        An open asyncpg connection authenticated as the application role.
    table_name
        The name of the table to probe (must have RLS enabled and a
        ``namespace_id`` column).

    Raises
    ------
    RuntimeError
        If a non-scoped SELECT returns one or more rows, indicating RLS is
        not enforced on the table.
    asyncpg.PostgresError
        Propagated for unexpected DB errors (e.g. table missing).
    """
    table_name = _validate_identifier(table_name)
    if cfg.NCE_BYPASS_RLS:
        log.warning("[rls-probe] Bypassing RLS verification for table %s", table_name)
        return

    try:
        count = await conn.fetchval(f"SELECT count(*) FROM {table_name}")
    except asyncpg.exceptions.UndefinedTableError as exc:
        log.warning(
            "[rls-probe] %s: could not query (table may not exist yet on first run) — %s: %s",
            table_name,
            type(exc).__name__,
            exc,
        )
        return

    if count is not None and count > 0:
        raise RuntimeError(
            f"RLS ENFORCEMENT FAILED: unscoped SELECT on {table_name} "
            f"returned {count} row(s).  The namespace_isolation_policy is "
            f"not filtering rows.  Check that RLS is enabled on the table "
            f"and the policy is correctly defined."
        )

    log.info("[rls-probe] %s: unscoped SELECT returned 0 rows — RLS active ✅", table_name)


async def verify_rls_catalog_consistency(conn: asyncpg.Connection) -> None:
    """
    Query pg_class, information_schema.columns, and pg_policies to verify
    the deployed schema matches NCE's intended RLS security posture.

    Raises RuntimeError listing all failures if any mismatch is found.
    Call at startup (after pool creation) and in CI against a fresh database.
    """
    db_info = await conn.fetchrow(
        "SELECT current_user, current_database(), inet_server_addr(), inet_server_port()"
    )
    import logging as _logging

    _cat_log = _logging.getLogger("nce.security_catalog")
    _cat_log.debug(
        "[rls-probe] connected as %s | DB %s | %s:%s",
        db_info[0],
        db_info[1],
        db_info[2],
        db_info[3],
    )

    rows = await conn.fetch("""
        SELECT
            c.relname                   AS table_name,
            c.relrowsecurity            AS rls_enabled,
            c.relforcerowsecurity       AS force_rls_enabled,
            EXISTS (
                SELECT 1
                FROM pg_attribute a
                WHERE a.attrelid = c.oid
                  AND a.attname  = 'namespace_id'
                  AND a.attnum   > 0
                  AND NOT a.attisdropped
            )                           AS has_namespace_id,
            (
                SELECT count(*)
                FROM pg_policies p
                WHERE p.schemaname = 'public'
                  AND p.tablename  = c.relname
            )                           AS policy_count
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname   = 'public'
          AND c.relkind  IN ('r', 'p')
          AND c.relispartition = false
        ORDER BY c.relname
    """)

    by_table = {row["table_name"]: row for row in rows}
    errors: list[str] = []

    # --- Tenant tables ---
    for table_name in EXPECTED_TENANT_RLS_TABLES:
        row = by_table.get(table_name)
        if row is None:
            errors.append(f"{table_name}: expected tenant table does not exist in schema")
            continue
        if not row["has_namespace_id"]:
            errors.append(f"{table_name}: missing namespace_id column")
        if not row["rls_enabled"]:
            errors.append(f"{table_name}: RLS is not enabled")
        if row["policy_count"] == 0:
            errors.append(f"{table_name}: no RLS policies found")
            continue

        namespace_column = EXPECTED_TENANT_RLS_TABLES[table_name]
        policies = await conn.fetch(
            """
            SELECT policyname, qual, with_check
            FROM   pg_policies
            WHERE  schemaname = 'public'
              AND  tablename  = $1
            """,
            table_name,
        )
        combined = " ".join(f"{p['qual'] or ''} {p['with_check'] or ''}" for p in policies)
        combined_lc = combined.lower()
        if namespace_column not in combined:
            errors.append(
                f"{table_name}: RLS policies do not reference namespace column {namespace_column!r}"
            )
        if "get_nce_namespace" not in combined_lc and "nce.namespace_id" not in combined_lc:
            errors.append(
                f"{table_name}: RLS policies do not reference get_nce_namespace() "
                "or nce.namespace_id"
            )
            _cat_log.error(
                "[%s] policy missing namespace binding — qual/with_check excerpt: %s",
                table_name,
                combined[:500] + ("..." if len(combined) > 500 else ""),
            )

        if not row["force_rls_enabled"]:
            errors.append(
                f"{table_name}: FORCE ROW LEVEL SECURITY is not enabled (relforcerowsecurity=false)"
            )

    # --- Special RLS tables (multi-namespace ownership) ---
    for table_name, ownership_columns in EXPECTED_SPECIAL_RLS_TABLES.items():
        row = by_table.get(table_name)
        if row is None:
            errors.append(f"{table_name}: expected special RLS table does not exist")
            continue
        if not row["rls_enabled"]:
            errors.append(f"{table_name}: RLS is not enabled")
        if row["policy_count"] == 0:
            errors.append(f"{table_name}: no RLS policies found")
            continue
        policies = await conn.fetch(
            "SELECT policyname, qual, with_check FROM pg_policies "
            "WHERE schemaname = 'public' AND tablename = $1",
            table_name,
        )
        for ownership_column in ownership_columns:
            if not any(
                ownership_column in (str(p["qual"]) + str(p["with_check"])) for p in policies
            ):
                errors.append(
                    f"{table_name}: no policy references ownership column {ownership_column!r}"
                )

    # --- Global tables (must NOT have RLS enabled) ---
    for table_name in EXPECTED_GLOBAL_TABLES:
        row = by_table.get(table_name)
        if row is None:
            continue
        if row["rls_enabled"]:
            errors.append(
                f"{table_name}: declared global (no RLS) but RLS is enabled — "
                "move to EXPECTED_TENANT_RLS_TABLES or disable RLS intentionally"
            )

    # --- Undeclared namespace tables (drift detection) ---
    declared = (
        set(EXPECTED_TENANT_RLS_TABLES) | set(EXPECTED_SPECIAL_RLS_TABLES) | EXPECTED_GLOBAL_TABLES
    )
    for table_name, row in by_table.items():
        if (
            row["has_namespace_id"]
            and table_name not in declared
            and not table_name.endswith("_default")
        ):
            if not row["rls_enabled"]:
                errors.append(f"{table_name}: has namespace_id but RLS is NOT enabled")
            else:
                errors.append(
                    f"{table_name}: has namespace_id + RLS enabled but is not declared "
                    "in any RLS intent category — add to EXPECTED_TENANT_RLS_TABLES "
                    "or EXPECTED_SPECIAL_RLS_TABLES"
                )
                if not row["force_rls_enabled"]:
                    errors.append(
                        f"{table_name}: FORCE ROW LEVEL SECURITY is not enabled (relforcerowsecurity=false)"
                    )

    if errors:
        raise RuntimeError(
            "RLS catalog consistency check failed:\n" + "\n".join(f"  - {e}" for e in errors)
        )


# ---------------------------------------------------------------------------
# Return type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AppendResult:
    """Immutable result of a successful ``append_event`` call."""

    event_id: uuid.UUID
    """UUID of the newly created row (same as ``event_log.id``)."""

    event_seq: int
    """Monotonically increasing per-namespace sequence number (no gaps)."""

    occurred_at: datetime
    """DB-authoritative timestamp (``clock_timestamp()`` at insert time, timezone.utc)."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _build_signing_fields(
    *,
    event_id: uuid.UUID,
    namespace_id: uuid.UUID,
    agent_id: str,
    event_type: str,
    event_seq: int,
    occurred_at_iso: str,
    params: dict[str, Any],
    parent_event_id: uuid.UUID | None,
    prev_chain_hash_hex: str | None = None,
) -> dict[str, Any]:
    """
    Return the dict of immutable event fields that is signed.

    ``result_summary``, ``llm_payload_uri``, and ``llm_payload_hash`` are
    intentionally excluded — they may be written or updated (by the migration
    role) after the initial INSERT without invalidating the core integrity
    signature.
    """
    fields: dict[str, Any] = {
        "event_id": str(event_id),
        "namespace_id": str(namespace_id),
        "agent_id": agent_id,
        "event_type": event_type,
        "event_seq": event_seq,
        "occurred_at": occurred_at_iso,
        "params": params,
    }
    if parent_event_id is not None:
        fields["parent_event_id"] = str(parent_event_id)
    if prev_chain_hash_hex is not None:
        fields["prev_chain_hash"] = prev_chain_hash_hex
    return fields


def _serialise_jsonb(obj: Any) -> str | None:
    """
    Serialise *obj* to a JSON string for asyncpg ``jsonb`` parameters.

    Returns ``None`` unchanged (maps to SQL NULL).
    Sorts keys for reproducibility; datetime objects serialised via ``str()``.
    """
    if obj is None:
        return None
    return json.dumps(obj, sort_keys=True, default=str)


def _validate_params_no_backdated_timestamp(params: dict[str, Any]) -> None:
    """
    D8 defence-in-depth: reject if *params* contains a top-level string ``valid_from`` that
    parses to a time before now (UTC).

    This is intentionally narrow: it does not walk nested dicts, datetime-typed values, or
    other clock fields. Normal application code still relies on :func:`append_event`, which
    binds ``occurred_at`` from the database clock. Backdating via raw SQL with sufficient
    privileges is outside this module's guarantee unless enforced by triggers or constraints.
    """
    vf = params.get("valid_from")
    if vf is None:
        return
    try:
        if isinstance(vf, str):
            from datetime import datetime as _dt

            ts = _dt.fromisoformat(vf.replace("Z", "+00:00"))
            now = _dt.now(timezone.utc)
            if ts < now:
                raise EventLogTimestampError(
                    "D8 violation: params['valid_from'] is a past timestamp "
                    f"({vf!r}).  valid_from must always be now()."
                )
    except (ValueError, TypeError):
        pass  # Non-parseable value — let the DB enforce


# ---------------------------------------------------------------------------
# Merkle chain helpers (cryptographic chaining for WORM integrity)
# ---------------------------------------------------------------------------


def _compute_content_hash(*, signing_fields: dict[str, Any]) -> bytes:
    """
    Compute a deterministic SHA-256 content hash of the canonical signing fields.

    Uses JSON with sorted keys (identical serialisation to what
    ``_build_signing_fields`` produces) to ensure byte-identical
    hashing across Python versions and processes.

    Returns
    -------
    bytes
        32-byte SHA-256 digest.
    """
    canonical_json: str = json.dumps(signing_fields, sort_keys=True, default=str)
    return hashlib.sha256(canonical_json.encode("utf-8")).digest()


def _compute_chain_hash(*, content_hash: bytes, previous_chain_hash: bytes) -> bytes:
    """
    Compute the Merkle chain hash for the current event.

    ``chain_hash = SHA-256(content_hash || previous_chain_hash)``

    Parameters
    ----------
    content_hash : bytes
        SHA-256 hash of the canonical signing fields for the current event.
    previous_chain_hash : bytes
        The chain_hash of the immediately preceding event in the namespace,
        or ``_GENESIS_SENTINEL`` (32 zero bytes) for the first event.

    Returns
    -------
    bytes
        32-byte SHA-256 digest representing the chained hash.
    """
    return hashlib.sha256(content_hash + previous_chain_hash).digest()


async def _fetch_previous_chain_hash(conn: asyncpg.Connection, namespace_id: uuid.UUID) -> bytes:
    """
    Fetch the ``chain_hash`` of the most recent event in *namespace_id*.

    Returns ``_GENESIS_SENTINEL`` if no prior event exists (genesis case).

    Parameters
    ----------
    conn : asyncpg.Connection
        An open asyncpg connection inside an active transaction.
    namespace_id : uuid.UUID
        The namespace scope.

    Returns
    -------
    bytes
        The previous chain_hash, or the genesis sentinel (32 zero bytes).
    """
    row = await conn.fetchrow(
        """
        SELECT chain_hash
        FROM   event_log
        WHERE  namespace_id = $1
        ORDER BY event_seq DESC
        LIMIT 1
        """,
        namespace_id,
    )
    if row is None or row["chain_hash"] is None:
        return _GENESIS_SENTINEL
    chain_hash: bytes = row["chain_hash"]
    if isinstance(chain_hash, memoryview):
        chain_hash = bytes(chain_hash)
    return chain_hash


# ---------------------------------------------------------------------------
# Core sequence-allocation helpers
# ---------------------------------------------------------------------------


async def _acquire_seq_lock(conn: asyncpg.Connection, namespace_id: uuid.UUID) -> None:
    """
    Reserved hook for historical per-namespace ``pg_advisory_xact_lock`` serialization.

    Obsoleted by the atomic ``event_sequences`` counter (FIX-068 / FIX-069).  The
    ``INSERT … ON CONFLICT DO UPDATE`` on ``event_sequences`` takes a row lock on the
    namespace's counter row that is held until the surrounding transaction ends, which
    serializes ``event_seq`` allocation and the subsequent chain-hash read for that namespace.
    """
    pass  # Obsoleted by atomic event_sequences counter (FIX-068)


async def _next_event_seq(conn: asyncpg.Connection, namespace_id: uuid.UUID) -> int:
    """
    Atomically allocate the next ``event_seq`` for *namespace_id*.

    Implemented as a single-row upsert into ``event_sequences`` (partition-free)
    rather than ``MAX(event_seq)`` across ``event_log`` partitions.

    Requires an active transaction (same Saga coordinator contract as callers).
    """
    row = await conn.fetchrow(
        """
        INSERT INTO event_sequences (namespace_id, seq)
        VALUES ($1, 1)
        ON CONFLICT (namespace_id)
        DO UPDATE SET seq = event_sequences.seq + 1
        RETURNING seq
        """,
        namespace_id,
    )
    if row is None or row["seq"] is None:
        raise EventLogSequenceError(
            f"event_sequences upsert returned no seq for namespace {namespace_id}"
        )
    return int(row["seq"])


async def _fetch_db_clock(conn: asyncpg.Connection) -> datetime:
    """
    Return the current DB-side timestamp (``clock_timestamp()``) as timezone.utc-aware.

    Using the DB clock (not the Python process clock) ensures the value we
    sign is byte-identical to what the DB will store in ``occurred_at``.
    """
    ts: datetime = await conn.fetchval("SELECT clock_timestamp()")
    # asyncpg returns a timezone-aware datetime; normalise to timezone.utc.
    return ts.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# Append-event helpers (extracted for Clean Code — SRP)
# ---------------------------------------------------------------------------


def _validate_event_payload(event_type: str, agent_id: str) -> str:
    """
    Validate and normalise ``event_type`` and ``agent_id``.

    Returns the stripped ``agent_id``.

    Raises
    ------
    InvalidEventTypeError
        If ``event_type`` is not in the allowed set.
    ValueError
        If ``agent_id`` is empty or exceeds ``_AGENT_ID_MAX_LEN``.
    """
    if event_type not in VALID_EVENT_TYPES:
        raise InvalidEventTypeError(
            f"Unknown event_type {event_type!r}.  Allowed values: {sorted(VALID_EVENT_TYPES)}"
        )

    agent_id = agent_id.strip()
    if not agent_id:
        raise ValueError("agent_id must not be empty or whitespace-only.")
    if len(agent_id) > _AGENT_ID_MAX_LEN:
        raise ValueError(f"agent_id exceeds {_AGENT_ID_MAX_LEN} characters (got {len(agent_id)}).")
    return agent_id


def _validate_event_params(event_type: str, params: dict[str, Any]) -> None:
    """Defence-in-depth: enforce required/forbidden JSON keys before signing.

    Only event types declaring entries in ``EVENT_REQUIRED_PARAM_KEYS`` /
    ``EVENT_FORBIDDEN_PARAM_KEYS`` participate. Additional keys beyond the
    required set are permitted (replay fork augmentation, forward evolution).
    """
    required = EVENT_REQUIRED_PARAM_KEYS.get(event_type)
    if required:
        missing = required - params.keys()
        if missing:
            raise ValueError(
                f"append_event(..., event_type={event_type!r}): "
                f"missing required param keys: {sorted(missing)}"
            )

    forbidden = EVENT_FORBIDDEN_PARAM_KEYS.get(event_type)
    if forbidden:
        present = forbidden & params.keys()
        if present:
            raise ValueError(
                f"append_event(..., event_type={event_type!r}): "
                f"forbidden param keys present: {sorted(present)}"
            )


async def _sign_event(
    conn: asyncpg.Connection,
    *,
    event_id: uuid.UUID,
    namespace_id: uuid.UUID,
    agent_id: str,
    event_type: str,
    event_seq: int,
    occurred_at_iso: str,
    params: dict[str, Any],
    parent_event_id: uuid.UUID | None,
    prev_chain_hash_hex: str | None = None,
) -> tuple[str, bytes]:
    """
    Load the active signing key, build canonical fields, and HMAC-sign.

    Returns ``(key_id, signature_bytes)``.

    Raises
    ------
    EventLogSigningError
        If the signing key cannot be loaded or HMAC computation fails.
    """
    try:
        key_id, raw_key = await get_active_key(conn)
    except SigningError as exc:
        raise EventLogSigningError(
            f"Cannot load active signing key for event_log write: {exc}"
        ) from exc

    signing_fields_dict = _build_signing_fields(
        event_id=event_id,
        namespace_id=namespace_id,
        agent_id=agent_id,
        event_type=event_type,
        event_seq=event_seq,
        occurred_at_iso=occurred_at_iso,
        params=params,
        parent_event_id=parent_event_id,
        prev_chain_hash_hex=prev_chain_hash_hex,
    )

    try:
        signature: bytes = sign_fields(signing_fields_dict, raw_key)
    except Exception as exc:
        raise EventLogSigningError(f"HMAC signing failed: {exc}") from exc

    return key_id, signature


async def _insert_event(
    conn: asyncpg.Connection,
    *,
    event_id: uuid.UUID,
    namespace_id: uuid.UUID,
    agent_id: str,
    event_type: str,
    event_seq: int,
    occurred_at: datetime,
    params: dict[str, Any],
    result_summary: dict[str, Any] | None,
    parent_event_id: uuid.UUID | None,
    llm_payload_uri: str | None,
    llm_payload_hash: bytes | None,
    signature: bytes,
    key_id: str,
    chain_hash: bytes | None,
    correlation_id: uuid.UUID | None = None,
) -> AppendResult:
    """
    INSERT a row into ``event_log`` and return the DB-assigned result.

    Raises
    ------
    EventLogSequenceError
        On unique-violation (advisory-lock concurrency bug).
    EventLogError
        If the INSERT returns no row.
    """
    try:
        row = await conn.fetchrow(
            """
            INSERT INTO event_log (
                id, namespace_id, agent_id, event_type, event_seq,
                occurred_at, params, result_summary,
                parent_event_id, llm_payload_uri, llm_payload_hash,
                signature, signature_key_id, signature_version, chain_hash,
                correlation_id
            ) VALUES (
                $1, $2, $3, $4, $5,
                $6, $7::jsonb, $8::jsonb,
                $9, $10, $11,
                $12, $13, 2, $14,
                $15
            )
            RETURNING id, event_seq, occurred_at
            """,
            event_id,
            namespace_id,
            agent_id,
            event_type,
            event_seq,
            occurred_at,
            _serialise_jsonb(params),
            _serialise_jsonb(result_summary),
            parent_event_id,
            llm_payload_uri,
            llm_payload_hash,
            signature,
            key_id,
            chain_hash,
            correlation_id,
        )
    except asyncpg.UniqueViolationError as exc:
        raise EventLogSequenceError(
            f"Unique violation on (namespace_id, event_seq)=({namespace_id}, {event_seq}).  "
            "Possible stale event_sequences counter or concurrent insert anomaly."
        ) from exc

    if row is None:
        raise EventLogError(
            "INSERT INTO event_log returned no RETURNING row — unexpected DB behaviour."
        )

    return AppendResult(
        event_id=row["id"],
        event_seq=row["event_seq"],
        occurred_at=row["occurred_at"].astimezone(timezone.utc),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def append_event(
    *,
    conn: asyncpg.Connection,
    namespace_id: uuid.UUID,
    agent_id: str,
    event_type: str,
    params: dict[str, Any],
    result_summary: dict[str, Any] | None = None,
    parent_event_id: uuid.UUID | None = None,
    parent_event_ids: list[uuid.UUID] | None = None,
    llm_payload_uri: str | None = None,
    llm_payload_hash: bytes | None = None,
    correlation_id: uuid.UUID | None = None,
    event_id: uuid.UUID | None = None,
    replay_occurred_at: datetime | None = None,
) -> AppendResult:
    """
    Append one entry to the tamper-resistant ``event_log`` table.

    This function is INSERT-only.  It must be called inside an active asyncpg
    transaction owned by the Saga coordinator.  It never commits or rolls back
    the transaction.

    Sequence
    --------
    1. Validate ``event_type`` and ``agent_id`` (``_validate_event_payload``).
    2. Optionally validate ``params`` keys (``EVENT_*_PARAM_KEYS`` contracts).
    3. Apply D8 defence-in-depth check on ``params``.
    4. Fetch the DB clock for signing.
    5. Allocate ``event_seq`` via atomic ``event_sequences`` upsert.
    6. Generate a fresh ``event_id``.
    7. Load signing key, build canonical fields, and HMAC-sign (``_sign_event``).
    8. Compute Merkle chain hash — SHA-256(content_hash || previous_chain_hash).
       Genesis events use a 32-byte zero sentinel as the previous hash.
    9. INSERT the row (``_insert_event``).

    Returns
    -------
    AppendResult
        Frozen dataclass with ``event_id``, ``event_seq``, and ``occurred_at``.

    Raises
    ------
    InvalidEventTypeError, ValueError (including param contract breaches),
    EventLogTimestampError,
    EventLogSigningError, EventLogSequenceError, asyncpg.PostgresError
    """
    if not conn.is_in_transaction():
        raise EventLogError(
            "append_event() must be called inside an active transaction. "
            "Wrap caller code in 'async with conn.transaction():'."
        )

    # Resolve primary parent from parent_event_ids when caller omits parent_event_id.
    # The first element of parent_event_ids is the canonical primary parent for the
    # scalar column (back-compat). parent_event_id wins if both are supplied.
    if parent_event_id is None and parent_event_ids:
        parent_event_id = parent_event_ids[0]

    # Resolve correlation_id from ContextVar if not explicitly supplied.
    if correlation_id is None:
        correlation_id = get_correlation_id()

    # 1. Validate event_type and agent_id
    agent_id = _validate_event_payload(event_type, agent_id)

    # 2. Optional required/forbidden JSON key contracts per event_type
    _validate_event_params(event_type, params)

    # 1b.
    if parent_event_id is not None:
        # Partition-safe lookup requires parent_occurred_at; deferred to retrieval tier.
        # Do not add back without providing occurred_at as a query parameter.
        pass

    # 3. D8 defence-in-depth: reject backdated timestamps in params
    _validate_params_no_backdated_timestamp(params)

    # 4. Fetch DB clock (used in signature so it matches stored occurred_at)
    occurred_at: datetime = await _fetch_db_clock(conn)
    if replay_occurred_at is not None:
        replay_run_id = params.get("replay_run_id")
        if replay_run_id:
            try:
                run_id_uuid = (
                    uuid.UUID(replay_run_id) if isinstance(replay_run_id, str) else replay_run_id
                )
                run_mode = await conn.fetchval(
                    "SELECT replay_mode FROM replay_runs WHERE id = $1",
                    run_id_uuid,
                )
                if run_mode == "deterministic":
                    occurred_at = replay_occurred_at.astimezone(timezone.utc)
            except Exception as exc:
                log.warning("Failed to verify replay mode for run %s: %s", replay_run_id, exc)
    occurred_at_iso: str = occurred_at.isoformat()

    # 5. Allocate event_seq (atomic event_sequences upsert)
    try:
        event_seq: int = await _next_event_seq(conn, namespace_id)
    except EventLogSequenceError:
        raise
    except asyncpg.PostgresError:
        raise
    except Exception as exc:
        raise EventLogSequenceError(
            f"Unexpected error allocating event_seq for namespace {namespace_id}: {exc}"
        ) from exc

    # 6. Generate event UUID if not provided
    if event_id is None:
        event_id = uuid.uuid4()

    # Fetch previous chain hash (moved before signing so it can be bound into HMAC version 2)
    previous_chain_hash: bytes = await _fetch_previous_chain_hash(conn, namespace_id)

    # 7. Load signing key, build canonical fields, and HMAC-sign
    key_id, signature = await _sign_event(
        conn,
        event_id=event_id,
        namespace_id=namespace_id,
        agent_id=agent_id,
        event_type=event_type,
        event_seq=event_seq,
        occurred_at_iso=occurred_at_iso,
        params=params,
        parent_event_id=parent_event_id,
        prev_chain_hash_hex=previous_chain_hash.hex(),
    )

    # 8. Compute Merkle chain hash.
    #    content_hash = SHA-256(canonical signing fields)
    #    chain_hash   = SHA-256(content_hash || previous_chain_hash)
    #    Genesis events use _GENESIS_SENTINEL (32 zero bytes) as previous.
    signing_fields_dict = _build_signing_fields(
        event_id=event_id,
        namespace_id=namespace_id,
        agent_id=agent_id,
        event_type=event_type,
        event_seq=event_seq,
        occurred_at_iso=occurred_at_iso,
        params=params,
        parent_event_id=parent_event_id,
        prev_chain_hash_hex=previous_chain_hash.hex(),
    )
    content_hash: bytes = _compute_content_hash(signing_fields=signing_fields_dict)
    chain_hash: bytes = _compute_chain_hash(
        content_hash=content_hash,
        previous_chain_hash=previous_chain_hash,
    )

    # 9. INSERT — pass occurred_at explicitly so the stored value matches
    #    what was signed.  asyncpg maps uuid.UUID → PG UUID natively.
    result = await _insert_event(
        conn,
        event_id=event_id,
        namespace_id=namespace_id,
        agent_id=agent_id,
        event_type=event_type,
        event_seq=event_seq,
        occurred_at=occurred_at,
        params=params,
        result_summary=result_summary,
        parent_event_id=parent_event_id,
        llm_payload_uri=llm_payload_uri,
        llm_payload_hash=llm_payload_hash,
        signature=signature,
        key_id=key_id,
        chain_hash=chain_hash,
        correlation_id=correlation_id,
    )

    # 10. Write multi-parent causal DAG rows into event_parents (WORM, same TX).
    #     The scalar parent_event_id column records the primary parent only;
    #     event_parents captures the full N-parent lineage for consolidation/merges.
    if parent_event_ids:
        await conn.executemany(
            """
            INSERT INTO event_parents (event_id, parent_event_id, namespace_id)
            VALUES ($1, $2, $3)
            ON CONFLICT DO NOTHING
            """,
            [(result.event_id, pid, namespace_id) for pid in parent_event_ids],
        )

    log.info(
        "event_log: event_type=%s event_seq=%d namespace=%s agent=%s event_id=%s chain_hash=%s",
        event_type,
        result.event_seq,
        namespace_id,
        agent_id,
        result.event_id,
        chain_hash.hex()[:16],
    )
    return result


async def verify_event_signature(
    conn: asyncpg.Connection,
    record: asyncpg.Record | dict,
) -> None:
    """
    Verify the HMAC-SHA256 signature of an event log row.

    Raises ``DataIntegrityError`` if the signature does not match or if the
    signing key cannot be loaded.

    Parameters
    ----------
    conn : asyncpg.Connection
        An asyncpg Connection.
    record : asyncpg.Record or dict
        A row fetched from ``event_log`` containing all columns, notably
        ``signature`` and ``signature_key_id``.
    """
    from nce.signing import get_key_by_id, verify_fields

    record = dict(record)

    key_id = record.get("signature_key_id")
    if not key_id:
        raise DataIntegrityError("Row is missing signature_key_id.")

    try:
        raw_key = await get_key_by_id(conn, key_id)
    except Exception as exc:
        raise DataIntegrityError(f"Failed to retrieve signing key {key_id}: {exc}") from exc

    params: dict[str, Any] | None = record.get("params")
    if params is None:
        params = {}
    elif isinstance(params, str):
        params = json.loads(params)

    # Coerce occurred_at to string representation as built during sign
    occurred_at = record.get("occurred_at")
    if isinstance(occurred_at, datetime):
        occurred_at_iso = occurred_at.astimezone(timezone.utc).isoformat()
    elif isinstance(occurred_at, str):
        occurred_at_iso = occurred_at
    else:
        raise DataIntegrityError("Invalid occurred_at type in record.")

    # Branch on signature version (v2 signs prev_chain_hash_hex)
    prev_chain_hash_hex: str | None = None
    sig_version = record.get("signature_version")
    if sig_version is None:
        # If signature_version is missing (e.g. RecordingFakeConnection mock),
        # try version 2 first, fallback to version 1.
        try:
            prev_seq = record["event_seq"] - 1
            if prev_seq <= 0:
                prev_chain_hash_hex = _GENESIS_SENTINEL.hex()
            else:
                prev_row = await conn.fetchrow(
                    """
                    SELECT chain_hash
                    FROM   event_log
                    WHERE  namespace_id = $1 AND event_seq = $2
                    """,
                    record["namespace_id"],
                    prev_seq,
                )
                if prev_row is not None and prev_row["chain_hash"] is not None:
                    prev_hash = prev_row["chain_hash"]
                    if isinstance(prev_hash, memoryview):
                        prev_hash = bytes(prev_hash)
                    prev_chain_hash_hex = prev_hash.hex()
        except Exception:
            pass

        # Try version 2
        signing_fields_dict = _build_signing_fields(
            event_id=record["id"],
            namespace_id=record["namespace_id"],
            agent_id=record["agent_id"],
            event_type=record["event_type"],
            event_seq=record["event_seq"],
            occurred_at_iso=occurred_at_iso,
            params=params,
            parent_event_id=record.get("parent_event_id"),
            prev_chain_hash_hex=prev_chain_hash_hex,
        )
        expected_signature = record.get("signature")
        if isinstance(expected_signature, memoryview):
            expected_signature = bytes(expected_signature)

        is_valid = False
        if expected_signature:
            try:
                is_valid = verify_fields(signing_fields_dict, raw_key, expected_signature)
            except Exception:
                pass

        if not is_valid:
            # Fall back to version 1
            prev_chain_hash_hex = None
            signing_fields_dict = _build_signing_fields(
                event_id=record["id"],
                namespace_id=record["namespace_id"],
                agent_id=record["agent_id"],
                event_type=record["event_type"],
                event_seq=record["event_seq"],
                occurred_at_iso=occurred_at_iso,
                params=params,
                parent_event_id=record.get("parent_event_id"),
                prev_chain_hash_hex=None,
            )
    else:
        sig_version = int(sig_version)
        if sig_version == 2:
            prev_seq = record["event_seq"] - 1
            if prev_seq <= 0:
                prev_chain_hash_hex = _GENESIS_SENTINEL.hex()
            else:
                prev_row = await conn.fetchrow(
                    """
                    SELECT chain_hash
                    FROM   event_log
                    WHERE  namespace_id = $1 AND event_seq = $2
                    """,
                    record["namespace_id"],
                    prev_seq,
                )
                if prev_row is None or prev_row["chain_hash"] is None:
                    raise DataIntegrityError(
                        f"Preceding event sequence {prev_seq} not found for namespace {record['namespace_id']} "
                        f"to verify signature version 2 of event_id={record['id']}."
                    )
                prev_hash = prev_row["chain_hash"]
                if isinstance(prev_hash, memoryview):
                    prev_hash = bytes(prev_hash)
                prev_chain_hash_hex = prev_hash.hex()

        signing_fields_dict = _build_signing_fields(
            event_id=record["id"],
            namespace_id=record["namespace_id"],
            agent_id=record["agent_id"],
            event_type=record["event_type"],
            event_seq=record["event_seq"],
            occurred_at_iso=occurred_at_iso,
            params=params,
            parent_event_id=record.get("parent_event_id"),
            prev_chain_hash_hex=prev_chain_hash_hex,
        )

    expected_signature = record.get("signature")
    if not expected_signature:
        raise DataIntegrityError("Row is missing signature.")

    if isinstance(expected_signature, memoryview):
        expected_signature = bytes(expected_signature)

    try:
        is_valid = verify_fields(signing_fields_dict, raw_key, expected_signature)
    except Exception as exc:
        raise DataIntegrityError(f"Failed to verify HMAC signature: {exc}") from exc

    if not is_valid:
        log.critical(
            "DATA INTEGRITY FAILURE: event_log row tampered! event_id=%s namespace_id=%s",
            record["id"],
            record["namespace_id"],
        )
        raise DataIntegrityError(
            f"Event signature mismatch for event_id={record['id']}. Tampering detected."
        )


async def verify_merkle_chain(
    conn: asyncpg.Connection,
    *,
    namespace_id: uuid.UUID,
    start_seq: int = 1,
    end_seq: int | None = None,
    anchor_chain_hash: bytes | None = None,
    anchor_seq: int | None = None,
) -> dict[str, Any]:
    """
    Verify the Merkle hash chain for events in *namespace_id*.

    Recomputes ``chain_hash`` for every event in sequence order and compares
    against the stored value.  If any event has been inserted, deleted, or
    altered since the chain was built, the recomputed hash will diverge —
    and every subsequent event will also fail.

    The chain is anchored on ``_GENESIS_SENTINEL`` (32 zero bytes) as the
    ``previous_chain_hash`` for event_seq=1.

    Against-anchor mode
    -------------------
    When *anchor_chain_hash* and *anchor_seq* are supplied, the function
    performs an additional check after the in-DB traversal: it compares
    the recomputed chain head at *anchor_seq* against the externally anchored
    value.  A mismatch means the DB history was rewritten after the anchor was
    captured — trigger-disable + re-stitch cannot escape this check because
    the anchor lives in an independent WORM store (object-locked MinIO bucket).

    Parameters
    ----------
    conn : asyncpg.Connection
        An open asyncpg connection (transaction is NOT required — this is
        a read-only verification).
    namespace_id : uuid.UUID
        The namespace whose event chain to verify.
    start_seq : int
        First event_seq to verify (default 1 for full chain).
    end_seq : int | None
        Last event_seq to verify (default None = all events from start_seq).
    anchor_chain_hash : bytes | None
        The chain_hash value that was anchored externally.  When provided
        together with *anchor_seq*, the result dict gains an
        ``"anchor_match"`` key (bool) and ``"anchor_seq"`` key; a mismatch
        is flagged at CRITICAL level with an alert dispatch.
    anchor_seq : int | None
        The event_seq whose chain_hash was anchored.  Required when
        *anchor_chain_hash* is supplied; ignored otherwise.

    Returns
    -------
    dict[str, Any]
        ``{"valid": bool, "checked": int, "first_break": int | None,
          "last_verified_seq": int}``, and optionally ``"reason"`` when a
        full-namespace check (``start_seq == 1`` and ``end_seq is None``)
        finds ``event_sequences.seq != max(event_log.event_seq)``.

        When *anchor_chain_hash* is supplied the dict also contains
        ``"anchor_match": bool`` and ``"anchor_seq": int``.

        ``first_break`` is the event_seq where the chain first broke, or
        ``None`` if the entire chain verifies.

    Raises
    ------
    asyncpg.PostgresError
        On database errors.
    ValueError
        If *anchor_chain_hash* is provided without *anchor_seq*.
    """
    if anchor_chain_hash is not None and anchor_seq is None:
        raise ValueError("anchor_seq must be supplied together with anchor_chain_hash")
    rows = await conn.fetch(
        """
        SELECT id, namespace_id, agent_id, event_type, event_seq,
               occurred_at, params, parent_event_id, chain_hash, signature_version
        FROM   event_log
        WHERE  namespace_id = $1
          AND  event_seq >= $2
        ORDER BY event_seq ASC
        """,
        namespace_id,
        start_seq,
    )
    if end_seq is not None and rows:
        rows = [r for r in rows if r["event_seq"] <= end_seq]

    checked = 0
    first_break: int | None = None
    last_verified_seq = 0
    previous_chain_hash: bytes = _GENESIS_SENTINEL

    # If we start from seq > 1, fetch the chain_hash of seq-1 as anchor.
    if start_seq > 1:
        _prior_row = await conn.fetchrow(
            """
            SELECT chain_hash FROM event_log
            WHERE namespace_id = $1 AND event_seq = $2
            """,
            namespace_id,
            start_seq - 1,
        )
        if _prior_row is not None and _prior_row["chain_hash"] is not None:
            _prior_hash = _prior_row["chain_hash"]
            if isinstance(_prior_hash, memoryview):
                _prior_hash = bytes(_prior_hash)
            previous_chain_hash = _prior_hash

    # Build seq → recomputed_chain_hash only when an against-anchor check is
    # requested.  The common anchor-less path (admin/CI) has no use for the
    # dict; building it unconditionally wastes O(N)*~120 bytes of heap per
    # call that are discarded on return.  When anchoring IS requested,
    # accumulation stops once the target seq has been stored — events beyond
    # anchor_seq are irrelevant to the lookup and need not occupy memory.
    _need_anchor_dict: bool = anchor_chain_hash is not None and anchor_seq is not None
    seq_to_recomputed_hash: dict[int, bytes] = {}

    for raw_row in rows:
        row: dict[str, Any] = dict(raw_row)
        event_seq = int(row["event_seq"])

        # Deserialise params
        params: dict[str, Any] | None = row.get("params")
        if params is None:
            params = {}
        elif isinstance(params, str):
            params = json.loads(params)

        # Coerce occurred_at to ISO string
        occurred_at = row.get("occurred_at")
        if isinstance(occurred_at, datetime):
            occurred_at_iso = occurred_at.astimezone(timezone.utc).isoformat()
        elif isinstance(occurred_at, str):
            occurred_at_iso = occurred_at
        else:
            occurred_at_iso = str(occurred_at)

        # Build canonical signing fields (same as at insert time)
        prev_chain_hash_hex: str | None = None
        sig_version = row.get("signature_version")
        if sig_version is None:
            sig_version = 2
        else:
            sig_version = int(sig_version)

        if sig_version == 2:
            prev_chain_hash_hex = previous_chain_hash.hex()

        signing_fields_dict = _build_signing_fields(
            event_id=row["id"],
            namespace_id=row["namespace_id"],
            agent_id=row["agent_id"],
            event_type=row["event_type"],
            event_seq=event_seq,
            occurred_at_iso=occurred_at_iso,
            params=params,
            parent_event_id=row.get("parent_event_id"),
            prev_chain_hash_hex=prev_chain_hash_hex,
        )

        # Recompute the chain hash
        content_hash = _compute_content_hash(signing_fields=signing_fields_dict)
        expected_chain_hash = _compute_chain_hash(
            content_hash=content_hash,
            previous_chain_hash=previous_chain_hash,
        )

        stored_chain_hash = row.get("chain_hash")
        if isinstance(stored_chain_hash, memoryview):
            stored_chain_hash = bytes(stored_chain_hash)

        if stored_chain_hash != expected_chain_hash:
            if first_break is None:
                first_break = event_seq
                log.critical(
                    "MERKLE CHAIN BROKEN: namespace=%s event_seq=%d — "
                    "stored chain_hash does not match recomputed value.  "
                    "Tampering or insertion anomaly detected.",
                    namespace_id,
                    event_seq,
                )

        # Advance the chain for the next iteration.
        # Only record the recomputed hash when an anchor lookup is pending: skip
        # the dict write entirely on the common anchor-less path, and stop after
        # capturing the target seq on the anchored path.
        previous_chain_hash = expected_chain_hash
        if _need_anchor_dict and event_seq <= anchor_seq:  # type: ignore[operator]
            seq_to_recomputed_hash[event_seq] = expected_chain_hash
        checked += 1
        last_verified_seq = event_seq

    valid = first_break is None
    reason: str | None = None

    # Full-namespace read: allocator counter must align with tallest event_seq
    if start_seq == 1 and end_seq is None:
        seq_counter = await conn.fetchval(
            "SELECT seq FROM event_sequences WHERE namespace_id = $1",
            namespace_id,
        )
        max_seq = await conn.fetchval(
            "SELECT max(event_seq) FROM event_log WHERE namespace_id = $1",
            namespace_id,
        )
        if seq_counter is not None or max_seq is not None:
            sc = int(seq_counter) if seq_counter is not None else 0
            ms = int(max_seq) if max_seq is not None else 0
            if sc != ms:
                log.error(
                    "MERKLE SEQUENCE MISMATCH: namespace=%s event_sequences.seq=%s "
                    "max(event_seq)=%s",
                    namespace_id,
                    seq_counter,
                    max_seq,
                )
                valid = False
                reason = "event_sequences counter does not match max(event_seq)"

    if valid and checked > 0:
        log.info(
            "Merkle chain verified: namespace=%s events=%d all valid.",
            namespace_id,
            checked,
        )

    out: dict[str, Any] = {
        "valid": valid,
        "checked": checked,
        "first_break": first_break,
        "last_verified_seq": last_verified_seq,
    }
    if reason is not None:
        out["reason"] = reason

    # -----------------------------------------------------------------
    # Against-anchor check (Batch 124): compare the recomputed chain hash
    # at anchor_seq against an externally anchored hash from the WORM store.
    # A DB superuser can disable the INSERT trigger and re-stitch the in-DB
    # chain — but they cannot alter the anchored blob in the object-locked
    # bucket.  Any divergence here signals a post-anchor history rewrite.
    #
    # The recomputed hash is looked up from seq_to_recomputed_hash, which was
    # accumulated during the main verification loop above.  This is provably
    # independent of the stored chain_hash column — the loop always uses the
    # *recomputed* value as the rolling previous hash, so an attacker who
    # rewrites chain_hash in the DB cannot influence seq_to_recomputed_hash.
    # No second DB round-trip is needed.
    # -----------------------------------------------------------------
    if anchor_chain_hash is not None and anchor_seq is not None:
        recomputed_anchor_hash: bytes | None = seq_to_recomputed_hash.get(anchor_seq)

        anchor_match = (
            recomputed_anchor_hash is not None and recomputed_anchor_hash == anchor_chain_hash
        )
        out["anchor_match"] = anchor_match
        out["anchor_seq"] = anchor_seq

        if not anchor_match:
            out["valid"] = False
            log.critical(
                "TAMPER ANCHOR MISMATCH: namespace=%s anchor_seq=%d — "
                "recomputed chain_hash diverges from the externally anchored value. "
                "Possible history rewrite (trigger-disable + re-stitch) detected.",
                namespace_id,
                anchor_seq,
            )
            try:
                from nce.notifications import dispatcher as _dispatcher

                await _dispatcher.dispatch_alert(
                    f"Tamper Anchor Mismatch: Namespace {namespace_id}",
                    (
                        f"Chain hash at event_seq {anchor_seq} diverges from the "
                        f"externally anchored value for namespace {namespace_id}. "
                        "A DB-level history rewrite (trigger-disable + re-stitch) "
                        "may have occurred."
                    ),
                )
            except Exception:  # noqa: BLE001
                log.exception(
                    "Failed to dispatch tamper-anchor alert for namespace=%s", namespace_id
                )

    return out
