"""Batch 74 — DigestSink seam + CentralSink for the Diagnostics Engine.

This module is the *cognitive-layer landing point* for a diagnostic
:class:`~nce.vertical_modules.diagnostics.streaming.Digest`: once the streaming
core (Batch 71) has folded a bundle into a bounded digest and the enrichment
seam (Batch 72) has resolved the device's NetBox context, ``CentralSink.write``
persists that digest across MongoDB (raw JSON archive) and the RLS-scoped
PostgreSQL cognitive layers (``memories``, ``kg_nodes`` / ``kg_edges``,
``topology_graph``, ``device_health_rollup``) and attests the ingestion with a
single tamper-resistant ``ingestion_completed`` ``event_log`` row.

Transaction-boundary design (CRITICAL)
--------------------------------------
``scoped_pg_session``'s docstring FORBIDS slow external I/O (Mongo / HTTP / LLM /
embedding generation) inside the transaction — a long-held transaction inflates
lock contention and vacuum bloat. ``CentralSink.write`` therefore runs in two
phases, mirroring the canonical ``MemoryOrchestrator`` store-saga
(``_store_episodic_mongodb`` → ``_store_semantic_graph_pg``):

1. **Slow I/O FIRST, OUTSIDE any ``scoped_pg_session``**
   * store the (size-capped) digest JSON to Mongo → ``digest_payload_ref``;
   * ``embed()`` a compact summary string → memory vector;
   * ``embed_batch()`` the KG node labels → node vectors.

2. **RLS-scoped PG writes, in ONE ``scoped_pg_session`` transaction** so the
   ``ingestion_completed`` event is committed ATOMICALLY with the tenant writes
   it attests (event-sourcing WORM invariant):
   * INSERT the ``memories`` row (mirrors ``_embed_and_insert_vectors``'s
     ``INSERT INTO memories`` — we inline the SQL rather than call the saga's
     ``store_memory``, which manages its OWN session/Mongo/Redis and would break
     atomicity with the event; see the boundary note on
     :meth:`CentralSink.write`);
   * upsert ``Device:`` / ``Room:`` / ``Anomaly:`` KG nodes + edges via the
     Batch-73 ``delegate_kg_upserts`` (canonical KG writer);
   * upsert physical topology edges via ``upsert_topology_edges``;
   * ``salience.reinforce()`` the memory on high-severity digests;
   * ``append_event("ingestion_completed", …)`` in the SAME transaction;
   * ``upsert_device_health`` rollup.

WORM / RLS invariants
---------------------
* ``append_event`` runs in the SAME transaction as the writes it attests; the
  caller never UPDATEs/DELETEs ``event_log``.
* ``ingestion_completed`` ``params`` are limited to the 5 registered keys
  (``ingest_id``, ``device_slug``, ``digest_payload_ref``, ``anomaly_count``,
  ``processed_lines``) — never raw log content / PII.
* All tenant SQL runs inside ``scoped_pg_session`` (RLS via ``nce.namespace_id``).
* ``NCE_MASTER_KEY`` is env-only (no key material handled here).
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from nce import embeddings as _embeddings
from nce import salience as _salience
from nce.db_utils import ScopedMongoCollection, scoped_mongo_session, scoped_pg_session
from nce.event_log import append_event
from nce.models import AssertionType, KGNode
from nce.orchestrators.diagnostic_ingest import (
    delegate_kg_upserts,
    upsert_device_health,
    upsert_topology_edges,
)

if TYPE_CHECKING:
    from nce.orchestrators.memory import MemoryOrchestrator
    from nce.vertical_modules.diagnostics.streaming import Digest

log = logging.getLogger("nce.vertical_modules.diagnostics.digest_writer")

__all__ = ["DigestSink", "CentralSink"]

# Mongo digest-doc size ceiling (honour the ≤1–2 MB goal). When the serialised
# document would exceed this, raw anomaly samples are dropped before re-serialise.
_MONGO_DOC_MAX_BYTES = 1_500_000

# Per-sample truncation applied to every retained anomaly sample in the Mongo
# doc (defence-in-depth: the streaming core already caps samples at ≤200 chars).
_SAMPLE_MAX_CHARS = 200

# Severity (syslog scale, lower = more urgent) at/below which we treat the digest
# as "high severity" and reinforce the memory's salience.
_HIGH_SEVERITY_THRESHOLD = 2

# Agent id attributed to digest-sink writes (events + KG change-origin).
_SINK_AGENT_ID = "diagnostics-ingest"


@runtime_checkable
class DigestSink(Protocol):
    """Seam for persisting a diagnostic digest into a downstream store.

    Implementations take a bounded :class:`Digest`, the resolved device context
    (the dict shape returned by ``resolve_device_context``) and the ingestion /
    namespace identifiers, and return the ``digest_payload_ref`` under which the
    raw digest was archived (e.g. the Mongo ``_id``).
    """

    async def write(
        self,
        digest: Digest,
        device_ctx: dict[str, Any],
        ingest_id: str,
        namespace_id: str | uuid.UUID,
    ) -> str:
        """Persist *digest* and return its ``digest_payload_ref``."""
        ...


class CentralSink:
    """Default :class:`DigestSink` that lands a digest in the cognitive layers.

    Reuses the running :class:`MemoryOrchestrator`'s connection pools/clients —
    it opens no pools of its own. See the module docstring for the two-phase
    transaction boundary.
    """

    def __init__(self, orchestrator: MemoryOrchestrator) -> None:
        self._orch = orchestrator

    # ------------------------------------------------------------------
    # Public seam
    # ------------------------------------------------------------------

    async def write(
        self,
        digest: Digest,
        device_ctx: dict[str, Any],
        ingest_id: str,
        namespace_id: str | uuid.UUID,
    ) -> str:
        """Land *digest* across Mongo + the RLS-scoped PG cognitive layers.

        Phase 1 (slow I/O, OUTSIDE the PG transaction): archive the size-capped
        digest JSON to Mongo and generate the memory + KG node embeddings.

        Phase 2 (ONE ``scoped_pg_session`` transaction): insert the ``memories``
        row, upsert KG nodes/edges + physical topology edges, optionally
        reinforce salience, append the ``ingestion_completed`` event, and upsert
        the device-health rollup — all atomic so the event and the tenant writes
        it attests commit or roll back together.

        Returns the Mongo ``digest_payload_ref``.
        """
        ns_str = str(namespace_id)
        device_slug = self._resolve_device_slug(device_ctx)
        anomaly_count = len(digest.anomalies)

        # ── Phase 1 — slow I/O OUTSIDE scoped_pg_session ──────────────────
        # (a) Archive the size-capped digest JSON to Mongo → digest_payload_ref.
        digest_payload_ref = await self._store_digest_mongo(digest, device_ctx, ingest_id, ns_str)

        # (b) Build the KG node/edge plan from the digest + device context.
        entities, triplets, topo_edges = self._build_graph(digest, device_ctx, device_slug)

        # (c) Generate embeddings (forbidden inside the transaction): one summary
        #     vector for the memory row and one vector per KG node label.
        summary = self._summary_text(digest, device_slug, anomaly_count)
        memory_vector = await _embeddings.embed(summary)
        node_vecs: list[list[float]] = (
            await _embeddings.embed_batch([e.label for e in entities]) if entities else []
        )

        top_severity = min((a.severity for a in digest.anomalies), default=None)
        top_anomaly_type = digest.anomalies[0].anomaly_type if digest.anomalies else None
        health_state = self._health_state(top_severity)
        anomaly_score = self._anomaly_score(top_severity)

        # ── Phase 2 — atomic RLS-scoped PG writes + the attesting event ───
        async with scoped_pg_session(self._orch.pg_pool, namespace_id) as conn:
            # store_memory's saga manages its own session/Mongo/Redis, so we
            # cannot call it here without breaking atomicity with the event.
            # We therefore inline the canonical memories INSERT (mirrors
            # MemoryOrchestrator._embed_and_insert_vectors) so the row commits in
            # the SAME transaction as the KG/topology/health upserts + event.
            memory_id = await self._insert_memory_row(
                conn,
                namespace_id=ns_str,
                summary=summary,
                vector=memory_vector,
                payload_ref=digest_payload_ref,
            )

            # Canonical KG writer (Batch 73 delegate → MemoryOrchestrator).
            # It appends its OWN `store_memory` event at the end of the same
            # transaction; that is a different event_type from the
            # `ingestion_completed` attestation appended below.
            kg_payload = _KGPayload(namespace_id=ns_str, agent_id=_SINK_AGENT_ID)
            saga_id = uuid.uuid4()
            await delegate_kg_upserts(
                self._orch,
                conn,
                payload=kg_payload,
                entities=entities,
                node_vecs=node_vecs,
                triplets=triplets,
                inserted_mongo_id=digest_payload_ref,
                target_model_ids=[],
                memory_id=memory_id,
                saga_id=saga_id,
                saga_event_id=uuid.uuid4(),
            )

            # Physical topology edges (idempotent upsert, Batch 73).
            await upsert_topology_edges(conn, namespace_id, topo_edges)

            # Reinforce salience on high-severity digests.
            if top_severity is not None and top_severity <= _HIGH_SEVERITY_THRESHOLD:
                await _salience.reinforce(
                    conn,
                    memory_id=str(memory_id),
                    agent_id=_SINK_AGENT_ID,
                    namespace_id=ns_str,
                )

            # WORM attestation — SAME transaction, 5 allowed params only.
            await append_event(
                conn=conn,
                namespace_id=_as_uuid(namespace_id),
                agent_id=_SINK_AGENT_ID,
                event_type="ingestion_completed",
                params={
                    "ingest_id": ingest_id,
                    "device_slug": device_slug,
                    "digest_payload_ref": digest_payload_ref,
                    "anomaly_count": anomaly_count,
                    "processed_lines": digest.processed_lines,
                },
            )

            # Device-health rollup (idempotent upsert, Batch 73).
            await upsert_device_health(
                conn,
                namespace_id,
                device_slug,
                health_state=health_state,
                top_anomaly_type=top_anomaly_type,
                anomaly_score=anomaly_score,
                last_ingestion_id=None,
            )

        log.debug(
            "[CentralSink] digest landed: device=%s anomalies=%d ref=%s",
            device_slug,
            anomaly_count,
            digest_payload_ref,
        )
        return digest_payload_ref

    # ------------------------------------------------------------------
    # Phase 1 helpers — slow I/O (OUTSIDE the PG transaction)
    # ------------------------------------------------------------------

    async def _store_digest_mongo(
        self,
        digest: Digest,
        device_ctx: dict[str, Any],
        ingest_id: str,
        ns_str: str,
    ) -> str:
        """Archive the size-capped digest JSON to Mongo; return the ``_id`` str."""
        doc = self._build_mongo_doc(digest, device_ctx, ingest_id, ns_str)
        async with scoped_mongo_session(self._orch.mongo_client, ns_str) as db:
            scoped_coll = ScopedMongoCollection(db.diag_digests._collection, ns_str)
            result = await scoped_coll.insert_one(doc)
        return str(result.inserted_id)

    def _build_mongo_doc(
        self,
        digest: Digest,
        device_ctx: dict[str, Any],
        ingest_id: str,
        ns_str: str,
    ) -> dict[str, Any]:
        """Serialise the digest into a Mongo doc, capped at ≤~1.5 MB.

        Samples are truncated up-front; if the doc still exceeds the ceiling the
        raw samples are dropped entirely (anomaly counts/types are retained).
        """

        def _doc(include_samples: bool) -> dict[str, Any]:
            return {
                "namespace_id": ns_str,
                "ingest_id": ingest_id,
                "device_slug": self._resolve_device_slug(device_ctx),
                "device_ctx": device_ctx,
                "processed_lines": digest.processed_lines,
                "anomalies": [
                    {
                        "anomaly_type": a.anomaly_type,
                        "severity": a.severity,
                        "occurrences": a.occurrences,
                        **({"sample": a.sample[:_SAMPLE_MAX_CHARS]} if include_samples else {}),
                    }
                    for a in digest.anomalies
                ],
                "windows": [
                    {
                        "anomaly_type": w.anomaly_type,
                        "window_start": w.window_start,
                        "count": w.count,
                    }
                    for w in digest.windows
                ],
                "samples_truncated": not include_samples,
                "ingested_at": datetime.now(timezone.utc),
            }

        doc = _doc(include_samples=True)
        # Estimate the BSON size cheaply via JSON length; drop samples if over.
        if len(json.dumps(doc, default=str).encode("utf-8")) > _MONGO_DOC_MAX_BYTES:
            doc = _doc(include_samples=False)
        return doc

    # ------------------------------------------------------------------
    # Phase 2 helpers — PG writes (INSIDE the scoped_pg_session)
    # ------------------------------------------------------------------

    async def _insert_memory_row(
        self,
        conn: Any,
        *,
        namespace_id: str,
        summary: str,
        vector: list[float],
        payload_ref: str,
    ) -> uuid.UUID:
        """INSERT one ``memories`` row, mirroring _embed_and_insert_vectors.

        Inlined (rather than routed through store_memory) so the row is atomic
        with the KG/topology/health upserts + the ingestion_completed event.
        """
        memory_id = await conn.fetchval(
            """
            INSERT INTO memories (namespace_id, agent_id, embedding, content_fts,
                                  payload_ref, pii_redacted, assertion_type,
                                  memory_type, metadata, change_origin)
            VALUES ($1::uuid, $2, $3::vector, to_tsvector('english', $4), $5,
                    $6, $7, $8, $9, $10)
            RETURNING id
            """,
            namespace_id,
            _SINK_AGENT_ID,
            json.dumps(vector),
            summary,
            payload_ref,
            False,
            "observation",
            "episodic",
            json.dumps({"source": "diagnostics_digest"}),
            "agent",
        )
        return memory_id

    # ------------------------------------------------------------------
    # Pure builders (no I/O)
    # ------------------------------------------------------------------

    def _build_graph(
        self,
        digest: Digest,
        device_ctx: dict[str, Any],
        device_slug: str,
    ) -> tuple[list[KGNode], list[_Triplet], list[dict[str, Any]]]:
        """Build KG ``Device:``/``Room:``/``Anomaly:`` nodes+edges + topo edges.

        Returns ``(entities, triplets, topology_edges)``:
        * ``entities`` — :class:`KGNode` records (label/entity_type) the canonical
          KG writer consumes (it reads ``.label`` / ``.entity_type``);
        * ``triplets`` — ``_Triplet`` records (subject/predicate/object/confidence)
          consumed by the KG writer's ``kg_edges`` insert;
        * ``topology_edges`` — mappings for ``upsert_topology_edges`` (physical
          Device→Room placement, plus Device→Device when a peer is known).
        """
        device_label = f"Device:{device_slug}"
        entities: list[KGNode] = [
            KGNode(
                label=device_label,
                entity_type="device",
                source_text=device_label,
            )
        ]
        triplets: list[_Triplet] = []
        topo_edges: list[dict[str, Any]] = []

        room = device_ctx.get("room")
        room_label: str | None = None
        if isinstance(room, str) and room.strip():
            room_label = f"Room:{room.strip()}"
            entities.append(KGNode(label=room_label, entity_type="room", source_text=room_label))
            triplets.append(
                _Triplet(
                    subject_label=device_label,
                    predicate="located_in",
                    object_label=room_label,
                    confidence=0.9,
                )
            )
            topo_edges.append(
                {
                    "source_node_id": device_label,
                    "source_node_type": "device",
                    "target_node_id": room_label,
                    "target_node_type": "room",
                    "edge_type": "located_in",
                    "confidence_score": 0.9,
                }
            )

        for anomaly in digest.anomalies:
            anomaly_label = f"Anomaly:{anomaly.anomaly_type}"
            entities.append(
                KGNode(
                    label=anomaly_label,
                    entity_type="anomaly",
                    source_text=anomaly_label,
                )
            )
            triplets.append(
                _Triplet(
                    subject_label=device_label,
                    predicate="exhibits",
                    object_label=anomaly_label,
                    confidence=0.8,
                )
            )

        return entities, triplets, topo_edges

    @staticmethod
    def _resolve_device_slug(device_ctx: dict[str, Any]) -> str:
        slug = device_ctx.get("device_slug")
        if isinstance(slug, str) and slug.strip():
            return slug.strip()
        return "unknown-device"

    @staticmethod
    def _summary_text(digest: Digest, device_slug: str, anomaly_count: int) -> str:
        """Compact, PII-free summary string used for the memory embedding."""
        types = ", ".join(a.anomaly_type for a in digest.anomalies[:8])
        return (
            f"Diagnostic digest for device {device_slug}: "
            f"{anomaly_count} anomaly type(s) over {digest.processed_lines} lines. "
            f"Types: {types or 'none'}."
        )

    @staticmethod
    def _health_state(top_severity: int | None) -> str:
        """Map syslog severity (lower = worse) to the rollup CHECK domain."""
        if top_severity is None:
            return "HEALTHY"
        if top_severity <= _HIGH_SEVERITY_THRESHOLD:
            return "CRITICAL"
        return "DEGRADED"

    @staticmethod
    def _anomaly_score(top_severity: int | None) -> float | None:
        """Normalise syslog severity (0 worst … 7 best) to a 0–1 score."""
        if top_severity is None:
            return None
        clamped = max(0, min(7, top_severity))
        return round((7 - clamped) / 7.0, 4)


# ---------------------------------------------------------------------------
# Lightweight payload/triplet shims for the canonical KG writer
# ---------------------------------------------------------------------------
#
# MemoryOrchestrator._insert_graph_nodes_and_edges only reads ``.namespace_id``
# and ``.agent_id`` off ``payload`` and ``.subject_label`` / ``.predicate`` /
# ``.object_label`` / ``.confidence`` off each triplet. We pass minimal duck-typed
# shims rather than constructing a full StoreMemoryRequest (which would require
# free-text content and re-run the PII/graph-extraction pipeline).


class _KGPayload:
    """Minimal ``payload`` stand-in for ``_insert_graph_nodes_and_edges``.

    The KG writer reads ``namespace_id`` / ``agent_id`` (for the kg_nodes/edges
    INSERTs + change-origin) and ``assertion_type.value`` (for the sibling
    ``store_memory`` event params), so the shim must carry all three.
    """

    __slots__ = ("namespace_id", "agent_id", "assertion_type")

    def __init__(self, *, namespace_id: str, agent_id: str) -> None:
        self.namespace_id = namespace_id
        self.agent_id = agent_id
        self.assertion_type = AssertionType.observation


class _Triplet:
    """Minimal triplet stand-in (matches the KG writer's attribute reads)."""

    __slots__ = ("subject_label", "predicate", "object_label", "confidence")

    def __init__(
        self,
        *,
        subject_label: str,
        predicate: str,
        object_label: str,
        confidence: float,
    ) -> None:
        self.subject_label = subject_label
        self.predicate = predicate
        self.object_label = object_label
        self.confidence = confidence


def _as_uuid(value: str | uuid.UUID) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
