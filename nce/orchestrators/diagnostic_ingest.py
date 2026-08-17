"""
Diagnostic-ingest writer — thin, idempotent persistence for network-ops diagnostics.

This module is a *writer only*: it provides idempotent upserts for the two
diagnostic-rollup tables (``topology_graph`` edges and ``device_health_rollup``)
and DELEGATES knowledge-graph node/edge persistence to the canonical
``MemoryOrchestrator._insert_graph_nodes_and_edges`` rather than duplicating the
``kg_nodes`` / ``kg_edges`` SQL.

WORM / RLS invariants
---------------------
* All tenant SQL here runs inside a caller-managed ``scoped_pg_session``
  transaction (RLS enforced via ``nce.namespace_id``). The upsert functions take
  an already-scoped ``conn`` and MUST NOT open their own pool session.
* If a caller emits provenance via ``append_event`` around these writes, it must
  do so in the SAME transaction as the write.
* ``event_log`` is append-only — these functions never UPDATE/DELETE it and never
  place raw content / PII in ``params``.
* ``NCE_MASTER_KEY`` is env-only (no key material is handled in this module).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

import asyncpg

if TYPE_CHECKING:
    from nce.orchestrators.memory import MemoryOrchestrator

__all__ = [
    "upsert_topology_edges",
    "upsert_device_health",
    "delegate_kg_upserts",
]


async def upsert_topology_edges(
    conn: asyncpg.Connection,
    namespace_id: str | uuid.UUID,
    edges: Sequence[Mapping[str, Any]],
) -> None:
    """Idempotently upsert topology edges into ``topology_graph``.

    Each edge mapping must supply ``source_node_id``, ``source_node_type``,
    ``target_node_id``, ``target_node_type`` and ``edge_type``; ``confidence_score``,
    ``decay_coefficient`` and ``metadata`` are optional (sensible defaults applied).

    Conflict target mirrors the ``uq_topology_edge`` unique index
    ``(namespace_id, source_node_id, target_node_id, edge_type)``: re-applying the
    same logical edge UPDATEs the existing row (refreshing confidence/metadata and
    bumping ``updated_at`` / ``last_verified``) instead of inserting a duplicate.

    MUST be called inside a caller-managed ``scoped_pg_session`` transaction; this
    function does NOT open its own session (``conn`` is already RLS-scoped).
    """
    if not edges:
        return

    ns_str = str(namespace_id)
    src_ids: list[str] = []
    src_types: list[str] = []
    tgt_ids: list[str] = []
    tgt_types: list[str] = []
    edge_types: list[str] = []
    decays: list[float] = []
    confidences: list[float] = []
    metadatas: list[str] = []

    for edge in edges:
        src_ids.append(str(edge["source_node_id"]))
        src_types.append(str(edge["source_node_type"]))
        tgt_ids.append(str(edge["target_node_id"]))
        tgt_types.append(str(edge["target_node_type"]))
        edge_types.append(str(edge["edge_type"]))
        decays.append(float(edge.get("decay_coefficient", 0.001)))
        confidences.append(float(edge.get("confidence_score", 0.9)))
        metadatas.append(json.dumps(edge.get("metadata", {})))

    await conn.execute(
        """
        INSERT INTO topology_graph (
            namespace_id, source_node_id, source_node_type,
            target_node_id, target_node_type, edge_type,
            decay_coefficient, confidence_score, metadata,
            last_verified, updated_at
        )
        SELECT $1::uuid,
               unnest($2::text[]), unnest($3::text[]),
               unnest($4::text[]), unnest($5::text[]),
               unnest($6::text[]),
               unnest($7::float8[]), unnest($8::float8[]),
               unnest($9::jsonb[]),
               now(), now()
        ON CONFLICT (namespace_id, source_node_id, target_node_id, edge_type) DO UPDATE
            SET source_node_type  = EXCLUDED.source_node_type,
                target_node_type  = EXCLUDED.target_node_type,
                decay_coefficient = EXCLUDED.decay_coefficient,
                confidence_score  = EXCLUDED.confidence_score,
                metadata          = EXCLUDED.metadata,
                last_verified     = now(),
                updated_at        = now()
        """,
        ns_str,
        src_ids,
        src_types,
        tgt_ids,
        tgt_types,
        edge_types,
        decays,
        confidences,
        metadatas,
    )


async def upsert_device_health(
    conn: asyncpg.Connection,
    namespace_id: str | uuid.UUID,
    device_slug: str,
    health_state: str,
    top_anomaly_type: str | None,
    anomaly_score: float | None,
    last_ingestion_id: str | uuid.UUID | None,
) -> None:
    """Idempotently upsert the latest health rollup for a device.

    Conflict target mirrors the ``device_health_rollup`` primary key
    ``(namespace_id, device_slug)``: re-applying for the same device UPDATEs the
    single rollup row (refreshing state/anomaly/ingestion and bumping
    ``last_seen_at``) instead of inserting a duplicate.

    MUST be called inside a caller-managed ``scoped_pg_session`` transaction; this
    function does NOT open its own session (``conn`` is already RLS-scoped).
    """
    last_ingestion = str(last_ingestion_id) if last_ingestion_id is not None else None

    await conn.execute(
        """
        INSERT INTO device_health_rollup (
            namespace_id, device_slug, health_state,
            top_anomaly_type, anomaly_score, last_ingestion_id,
            last_seen_at
        )
        VALUES ($1::uuid, $2, $3, $4, $5, $6::uuid, now())
        ON CONFLICT (namespace_id, device_slug) DO UPDATE
            SET health_state      = EXCLUDED.health_state,
                top_anomaly_type  = EXCLUDED.top_anomaly_type,
                anomaly_score     = EXCLUDED.anomaly_score,
                last_ingestion_id = EXCLUDED.last_ingestion_id,
                last_seen_at      = now()
        """,
        str(namespace_id),
        device_slug,
        health_state,
        top_anomaly_type,
        anomaly_score,
        last_ingestion,
    )


async def delegate_kg_upserts(
    orchestrator: MemoryOrchestrator,
    conn: asyncpg.Connection,
    **kwargs: Any,
) -> None:
    """Delegate knowledge-graph node/edge persistence to the canonical writer.

    This module deliberately does NOT duplicate the ``kg_nodes`` / ``kg_edges``
    upsert SQL. Diagnostic ingestion that needs to persist KG nodes/edges should
    route through this helper, which forwards to
    ``MemoryOrchestrator._insert_graph_nodes_and_edges`` — the single source of
    truth for the KG ON CONFLICT logic (see ``nce/orchestrators/memory.py``).

    ``conn`` must already be inside a caller-managed ``scoped_pg_session``
    transaction; ``kwargs`` are forwarded verbatim (``payload``, ``entities``,
    ``node_vecs``, ``triplets``, ``inserted_mongo_id``, ``target_model_ids``,
    ``memory_id``, ``saga_id``, optional ``saga_event_id``).
    """
    await orchestrator._insert_graph_nodes_and_edges(conn, **kwargs)
