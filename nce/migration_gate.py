"""
Embedding-migration safety gate — the SINGLE enforcement chokepoint shared by
every caller of the migration lifecycle (MCP tools AND the admin HTTP API).

Batch 108 (security): a bad embedding-model swap must not silently corrupt
retrieval.  The gate logic used to live only in the MCP handler
(``migration_mcp_handlers.handle_commit_migration``), leaving the admin HTTP
route (``api_admin_embedding_migration_commit``) ungated.  This module hoists
the gate so it can be invoked from :class:`nce.orchestrator.NCEEngine` —
the layer through which BOTH the MCP and HTTP callers converge.

Three guards live here:

1. **Neighbor-overlap quality gate** (commit) — a random sample of migrated
   memories is compared (k-NN Jaccard) between the old and new embedding spaces.
   Below ``cfg.NCE_REEMBED_GATE_MIN_OVERLAP`` the commit is refused, the score is
   surfaced, and the degenerate-sample case fails closed (see
   :func:`nce.reembedding_migration.compute_neighbor_overlap`).

2. **force escape** (commit) — ``force=True`` proceeds past a failing gate but
   unconditionally emits a WORM ``migration_commit_forced`` audit event carrying
   the score; it can never be silently applied.

3. **Dimension preflight** (start) — refuses to start a migration whose target
   model dimension differs from the active model's, since a dim mismatch would
   silently corrupt every vector search after commit.

Pre-flight WORM audit records are written on a SEPARATE PG connection / TX
(see :func:`audit_migration_action`) so the audit survives any migration rollback.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

import asyncpg

from nce.config import cfg
from nce.event_log import append_event
from nce.reembedding_migration import GateSampleTooSmall, compute_neighbor_overlap

log = logging.getLogger("nce.migration_gate")

# System-level namespace sentinel — used for migration audit events that are
# not tenant-scoped.  The nil UUID is the conventional "no namespace" value.
_SYSTEM_NAMESPACE: UUID = UUID("00000000-0000-0000-0000-000000000000")
_MAX_EXTRA_PARAMS_KEYS: int = 16
_MAX_EXTRA_PARAMS_VALUE_LEN: int = 256


# ---------------------------------------------------------------------------
# Pre-flight audit helper — writes before the migration transaction begins
# ---------------------------------------------------------------------------


async def audit_migration_action(
    pg_pool: asyncpg.Pool,
    *,
    event_type: str,
    admin_identity: str | None,
    migration_id: str | None,
    target_model_id: str | None,
    extra_params: dict[str, Any] | None = None,
) -> None:
    """Write an irrefutable pre-flight audit event on a SEPARATE PG connection.

    This connection and transaction are independent of the migration orchestrator's
    transaction — if the migration transaction rolls back, the audit record survives.

    Raises:
        Exception: Any failure (connection, insert, signing) propagates and
            prevents the migration from proceeding.
    """
    params: dict[str, Any] = {}
    if migration_id is not None:
        params["migration_id"] = migration_id
    if target_model_id is not None:
        params["target_model_id"] = target_model_id
    if extra_params:
        if len(extra_params) > _MAX_EXTRA_PARAMS_KEYS:
            raise ValueError(f"extra_params exceeds maximum key count ({_MAX_EXTRA_PARAMS_KEYS})")
        for k, v in extra_params.items():
            if not isinstance(k, str):
                raise ValueError("extra_params keys must be strings")
            if isinstance(v, (dict, list)):
                raise ValueError(
                    f"extra_params values must be scalar, got {type(v).__name__!r} for key {k!r}"
                )
            if isinstance(v, str) and len(v) > _MAX_EXTRA_PARAMS_VALUE_LEN:
                raise ValueError(
                    f"extra_params[{k!r}] value too long (max {_MAX_EXTRA_PARAMS_VALUE_LEN} chars)"
                )
        params.update(extra_params)

    async with pg_pool.acquire(timeout=10.0) as audit_conn:
        async with audit_conn.transaction():
            result = await append_event(
                conn=audit_conn,
                namespace_id=_SYSTEM_NAMESPACE,
                agent_id=admin_identity or "system",
                event_type=event_type,
                params=params,
            )
    safe_admin = (admin_identity or "system")[:32]
    log.info(
        "[migration-audit] %s recorded — event_id=%s event_seq=%d admin=%s",
        event_type,
        result.event_id,
        result.event_seq,
        safe_admin,
    )


# ---------------------------------------------------------------------------
# Commit gate — neighbor-overlap + force escape + pre-flight audit
# ---------------------------------------------------------------------------


async def enforce_commit_gate(
    pg_pool: asyncpg.Pool,
    *,
    migration_id: str,
    force: bool,
    admin_identity: str | None,
) -> None:
    """Evaluate the neighbor-overlap quality gate before a migration commit.

    Run BEFORE the schema-switching transaction so a bad model swap cannot
    silently corrupt retrieval.

    - Below ``cfg.NCE_REEMBED_GATE_MIN_OVERLAP`` (or a degenerate/empty sample,
      surfaced as :class:`GateSampleTooSmall`) → refuse with ``ValueError`` unless
      ``force`` is set.
    - ``force=True`` → emit the WORM ``migration_commit_forced`` audit event
      carrying the score, then proceed.
    - Always emit the pre-flight ``migration_commit_requested`` audit event before
      returning so the caller can proceed to the commit.

    Raises:
        ValueError: when the gate fails and ``force`` is not set.
    """
    gate_threshold: float = cfg.NCE_REEMBED_GATE_MIN_OVERLAP

    gate_failed: bool
    gate_score: float | None
    failure_reason: str

    try:
        gate_score = await compute_neighbor_overlap(
            pg_pool,
            migration_id=migration_id,
            sample=cfg.NCE_REEMBED_GATE_SAMPLE,
            k=cfg.NCE_REEMBED_GATE_K,
        )
        gate_failed = gate_score < gate_threshold
        failure_reason = (
            f"Neighbor-overlap quality gate failed: score {gate_score:.4f} is below "
            f"threshold {gate_threshold:.4f}."
        )
    except GateSampleTooSmall as exc:
        # Degenerate / empty sample with an active model present — NOT a vacuous
        # pass.  Fail closed; force can still override (and is audited below).
        gate_score = None
        gate_failed = True
        failure_reason = f"Neighbor-overlap quality gate failed: {exc}"

    if gate_failed:
        if not force:
            raise ValueError(
                f"{failure_reason} Pass force=true to override "
                "(a migration_commit_forced audit event will be emitted)."
            )
        # force=true path — emit WORM audit event BEFORE proceeding.
        score_repr = "degenerate_sample" if gate_score is None else str(round(gate_score, 6))
        await audit_migration_action(
            pg_pool,
            event_type="migration_commit_forced",
            admin_identity=admin_identity,
            migration_id=migration_id,
            target_model_id=None,
            extra_params={
                "gate_score": score_repr,
                "gate_threshold": str(round(gate_threshold, 6)),
            },
        )
        log.warning(
            "[migration-gate] force-commit: migration_id=%s gate_score=%s threshold=%.4f",
            migration_id,
            score_repr,
            gate_threshold,
        )

    # Pre-flight WORM audit — written BEFORE the schema-switching transaction.
    await audit_migration_action(
        pg_pool,
        event_type="migration_commit_requested",
        admin_identity=admin_identity,
        migration_id=migration_id,
        target_model_id=None,
    )


# ---------------------------------------------------------------------------
# Start gate — dimension preflight + pre-flight audit
# ---------------------------------------------------------------------------


async def enforce_start_gate(
    pg_pool: asyncpg.Pool,
    *,
    target_model_id: str,
    admin_identity: str | None,
) -> None:
    """Dimension preflight + pre-flight audit before a migration is started.

    Refuse if the target model's embedding dimension does not match the currently
    active model.  A dim mismatch would silently corrupt all vector searches after
    commit (pgvector rejects mismatched ops at query time rather than at insert,
    making this hard to detect post-hoc).  The first-time-setup case (no active
    model) is allowed.

    Raises:
        ValueError: when the target model is missing or the dimension mismatches.
    """
    async with pg_pool.acquire(timeout=10.0) as conn:
        target_dim_row = await conn.fetchrow(
            "SELECT dimension FROM embedding_models WHERE id = $1::uuid",
            target_model_id,
        )
        if target_dim_row is None:
            raise ValueError(f"Target embedding model {target_model_id!r} not found")
        target_dim: int = int(target_dim_row["dimension"])

        active_dim_row = await conn.fetchrow(
            "SELECT dimension FROM embedding_models WHERE status = 'active' LIMIT 1"
        )
        if active_dim_row is not None:
            active_dim: int = int(active_dim_row["dimension"])
            if target_dim != active_dim:
                raise ValueError(
                    f"Dimension mismatch: target model has dim {target_dim}, "
                    f"active model has dim {active_dim}. "
                    "Cannot start migration across incompatible embedding dimensions."
                )

    # Pre-flight WORM audit — written BEFORE the migration transaction begins.
    await audit_migration_action(
        pg_pool,
        event_type="migration_start_requested",
        admin_identity=admin_identity,
        migration_id=None,  # generated inside the orchestrator
        target_model_id=target_model_id,
    )
