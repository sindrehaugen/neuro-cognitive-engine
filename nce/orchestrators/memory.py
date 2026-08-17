"""
Memory Orchestrator — extracted from NCEEngine per Clean Code SRP split.

Handles all memory lifecycle operations: store, search, recall, verify, and PII unredaction.
Receives shared connection pools (PG, Mongo, Redis, MinIO) via constructor injection.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import asyncpg
import redis.asyncio as redis
from bson import ObjectId
from minio import Minio
from motor.motor_asyncio import AsyncIOMotorClient

from nce import embeddings as _embeddings
from nce.auth import set_namespace_context, validate_agent_id
from nce.config import cfg
from nce.db_utils import scoped_mongo_session, scoped_pg_session
from nce.models import (
    _SAFE_ID_RE,
    ArtifactPayload,
    AssertionType,
    MediaPayload,
    MemoryType,
    NamespacePIIConfig,
    SagaState,
    StoreMemoryRequest,
)
from nce.mongo_bulk import fetch_episodes_raw_by_ref, normalize_payload_ref
from nce.observability import SagaMetrics, get_tracer
from nce.orchestrators._base import OrchestratorBase

log = logging.getLogger("nce-orchestrator.memory")

# Agent IDs that identify the admin/operator path (not an autonomous agent call).
_OPERATOR_AGENT_IDS: frozenset[str] = frozenset({"system", "admin", "operator"})


def _saga_change_origin(agent_id: str) -> str:
    """Return the change_origin value for a saga write.

    An `agent_id` that belongs to the operator/admin set means a human or
    infrastructure operator initiated the write → 'operator'.
    Any other (non-empty) agent_id is an autonomous agent → 'agent'.
    """
    if agent_id in _OPERATOR_AGENT_IDS:
        return "operator"
    return "agent"


class MemoryOrchestrator(OrchestratorBase):
    """Domain orchestrator for memory storage, search, recall, and integrity."""

    def __init__(
        self,
        pg_pool: asyncpg.Pool,
        mongo_client: AsyncIOMotorClient,
        redis_client: redis.Redis,
        minio_client: Minio | None = None,
        pg_read_pool: asyncpg.Pool | None = None,
    ):
        super().__init__(pg_pool, mongo_client=mongo_client, redis_client=redis_client)
        self.pg_read_pool = pg_read_pool
        self.minio_client = minio_client

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _enqueue_outbox(
        self,
        conn,
        *,
        namespace_id: str,
        aggregate_type: str,
        aggregate_id: str,
        event_type: str,
        payload: dict,
        headers: dict | None = None,
    ) -> None:
        """Write a domain event to the transactional outbox.

        Called inside an existing PG transaction so the event is
        atomically committed with the business write.
        """
        await conn.execute(
            """
            INSERT INTO outbox_events (
                namespace_id, aggregate_type, aggregate_id, event_type, payload, headers
            )
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            namespace_id,
            aggregate_type,
            aggregate_id,
            event_type,
            json.dumps(payload),
            json.dumps(headers or {}),
        )

    # ------------------------------------------------------------------
    # store_memory — Private helpers
    # ------------------------------------------------------------------

    async def _apply_pii_pipeline(self, payload: StoreMemoryRequest, *, conn=None):
        """Phase 0.3: PII Redaction Pipeline + Graph Extraction.

        Returns (pii_result, sanitized_summary, sanitized_heavy, entities, triplets).
        """
        pii_config = NamespacePIIConfig()

        async def _fetch_ns_config(c):
            ns_row = await c.fetchrow(
                "SELECT metadata FROM namespaces WHERE id = $1", payload.namespace_id
            )
            if ns_row:
                meta = json.loads(ns_row["metadata"])
                if "pii" in meta:
                    return NamespacePIIConfig(**meta["pii"])
            return NamespacePIIConfig()

        if conn is not None:
            pii_config = await _fetch_ns_config(conn)
        else:
            async with scoped_pg_session(self.pg_pool, payload.namespace_id) as c:
                pii_config = await _fetch_ns_config(c)

        from nce.pii import process as pii_process

        pii_result = await pii_process(payload.summary, pii_config)
        sanitized_summary = pii_result.sanitized_text
        sanitized_heavy = (await pii_process(payload.heavy_payload, pii_config)).sanitized_text

        from nce.graph_extractor import extract_async as graph_extract_async

        # graph_extract_async runs spaCy NLP in a dedicated background thread pool
        # to ensure the event loop remains responsive to other requests.
        entities, triplets = await graph_extract_async(sanitized_summary)

        return pii_result, sanitized_summary, sanitized_heavy, entities, triplets

    async def _embed_and_insert_vectors(
        self,
        conn,
        *,
        payload,
        sanitized_summary,
        vector,
        pii_result,
        inserted_mongo_id,
        target_model_ids,
        user_id,
        session_id,
        wrapped_dek=None,
        dek_key_id=None,
        origin_event_id: uuid.UUID | None = None,
    ):
        """Insert memory row + memory_embeddings + PII vault (inside PG tx).

        Returns the new memory_id (UUID).  ``wrapped_dek``/``dek_key_id`` are the
        envelope-encryption handles for the Mongo ``raw_data`` ciphertext (Part
        II.4); both are NULL for legacy / unencrypted writes.
        ``origin_event_id`` is the pre-generated event_log UUID for the sibling
        store_memory event (set so memories.origin_event_id is populated
        atomically in the same transaction — Batch C1 change-origin).
        """
        metadata = dict(payload.metadata) if payload.metadata else {}
        if (
            getattr(_embeddings, "degraded_embedding_flag", None)
            and _embeddings.degraded_embedding_flag.get()
        ):
            metadata["degraded_embedding"] = True

        change_origin = _saga_change_origin(payload.agent_id)

        memory_id = await conn.fetchval(
            """
            INSERT INTO memories (user_id, session_id, namespace_id, agent_id, embedding,
                                  content_fts, payload_ref, pii_redacted, assertion_type,
                                  memory_type, metadata, wrapped_dek, dek_key_id,
                                  change_origin, origin_event_id)
            VALUES ($1, $2, $3, $4, $5::vector, to_tsvector('english', $6), $7, $8, $9, $10,
                    $11, $12, $13, $14, $15::uuid)
            RETURNING id
            """,
            user_id,
            session_id,
            payload.namespace_id,
            payload.agent_id,
            json.dumps(vector),
            sanitized_summary,
            inserted_mongo_id,
            pii_result.redacted,
            payload.assertion_type.value,
            payload.memory_type.value,
            json.dumps(metadata),
            wrapped_dek,
            dek_key_id,
            change_origin,
            str(origin_event_id) if origin_event_id is not None else None,
        )

        for model_id in target_model_ids:
            await conn.execute(
                "INSERT INTO memory_embeddings (memory_id, model_id, embedding, namespace_id) VALUES ($1, $2, $3::vector, $4) ON CONFLICT DO NOTHING",
                memory_id,
                model_id,
                json.dumps(vector),
                payload.namespace_id,
            )

        if pii_result.vault_entries:
            await conn.executemany(
                """
                INSERT INTO pii_redactions (namespace_id, memory_id, token, encrypted_value, entity_type)
                VALUES ($1, $2, $3, $4, $5)
                """,
                [
                    (
                        payload.namespace_id,
                        memory_id,
                        v["token"],
                        v["encrypted_value"],
                        v["entity_type"],
                    )
                    for v in pii_result.vault_entries
                ],
            )

        return memory_id

    async def _insert_graph_nodes_and_edges(
        self,
        conn,
        *,
        payload,
        entities,
        node_vecs,
        triplets,
        inserted_mongo_id,
        target_model_ids,
        memory_id,
        saga_id,
        saga_event_id: uuid.UUID | None = None,
    ):
        """Insert kg_nodes, kg_node_embeddings, kg_edges, and event_log (inside PG tx).

        Uses UNNEST batching to reduce round-trips from O(N) to O(1).
        ``saga_event_id`` is the pre-generated UUID for the store_memory event
        appended at the end of this method (Batch C1 change-origin wiring).
        """
        change_origin = _saga_change_origin(payload.agent_id)

        # ── Batch-insert kg_nodes ────────────────────────────────────────
        if entities and node_vecs:
            labels = [e.label for e in entities]
            entity_types = [e.entity_type for e in entities]
            embeddings = [json.dumps(v) for v in node_vecs]

            returned_rows = await conn.fetch(
                """
                INSERT INTO kg_nodes (label, entity_type, embedding, payload_ref, namespace_id,
                                      change_origin)
                SELECT unnest($1::text[]), unnest($2::text[]), unnest($3::text[])::vector, $4,
                       $5::uuid, $6
                ON CONFLICT (label, namespace_id) DO UPDATE
                    SET entity_type  = EXCLUDED.entity_type,
                        embedding    = EXCLUDED.embedding,
                        payload_ref = EXCLUDED.payload_ref,
                        change_origin = CASE
                            WHEN kg_nodes.change_origin IN ('sync', 'webhook') THEN kg_nodes.change_origin
                            ELSE EXCLUDED.change_origin
                        END,
                        updated_at   = NOW()
                RETURNING id, label
                """,
                labels,
                entity_types,
                embeddings,
                inserted_mongo_id,
                payload.namespace_id,
                change_origin,
            )
            label_to_id = {row["label"]: row["id"] for row in returned_rows}

            # Safety net: fetch IDs for any labels not returned (should not happen with DO UPDATE)
            missing_labels = [lbl for lbl in labels if lbl not in label_to_id]
            if missing_labels:
                rows = await conn.fetch(
                    "SELECT id, label FROM kg_nodes WHERE namespace_id = $1 AND label = ANY($2::text[])",
                    payload.namespace_id,
                    missing_labels,
                )
                for row in rows:
                    label_to_id[row["label"]] = row["id"]

            # ── Batch-insert kg_node_embeddings ──────────────────────────
            if target_model_ids and label_to_id:
                emb_node_ids: list[str] = []
                emb_model_ids: list[str] = []
                emb_vectors: list[str] = []
                for entity, node_vec in zip(entities, node_vecs):
                    nid = label_to_id.get(entity.label)
                    if nid is None:
                        continue
                    for model_id in target_model_ids:
                        emb_node_ids.append(nid)
                        emb_model_ids.append(model_id)
                        emb_vectors.append(json.dumps(node_vec))
                if emb_node_ids:
                    await conn.execute(
                        """
                        INSERT INTO kg_node_embeddings (node_id, model_id, embedding)
                        SELECT unnest($1::uuid[]), unnest($2::uuid[]), unnest($3::text[])::vector
                        ON CONFLICT DO NOTHING
                        """,
                        emb_node_ids,
                        emb_model_ids,
                        emb_vectors,
                    )

        # ── Batch-insert kg_edges ────────────────────────────────────────
        if triplets:
            subjs = [t.subject_label for t in triplets]
            preds = [t.predicate for t in triplets]
            objs = [t.object_label for t in triplets]
            confs = [t.confidence for t in triplets]
            await conn.execute(
                """
                INSERT INTO kg_edges (subject_label, predicate, object_label, confidence,
                                      payload_ref, namespace_id, change_origin)
                SELECT unnest($1::text[]), unnest($2::text[]), unnest($3::text[]),
                       unnest($4::float[]), $5, $6::uuid, $7
                ON CONFLICT (subject_label, predicate, object_label, namespace_id) DO UPDATE
                    SET confidence   = EXCLUDED.confidence,
                        payload_ref = EXCLUDED.payload_ref,
                        change_origin = CASE
                            WHEN kg_edges.change_origin IN ('sync', 'webhook') THEN kg_edges.change_origin
                            ELSE EXCLUDED.change_origin
                        END,
                        updated_at   = NOW()
                """,
                subjs,
                preds,
                objs,
                confs,
                inserted_mongo_id,
                payload.namespace_id,
                change_origin,
            )

        # Phase 2.2: Append to event log
        from nce.event_log import append_event

        serialized_entities = [{"label": e.label, "entity_type": e.entity_type} for e in entities]
        serialized_triplets = [
            {
                "subject_label": t.subject_label,
                "predicate": t.predicate,
                "object_label": t.object_label,
                "confidence": t.confidence,
            }
            for t in triplets
        ]

        await append_event(
            conn=conn,
            namespace_id=payload.namespace_id,
            agent_id=payload.agent_id,
            event_type="store_memory",
            params={
                "saga_id": str(saga_id),
                "memory_id": str(memory_id),
                "payload_ref": inserted_mongo_id,
                "assertion_type": payload.assertion_type.value,
                "entities": serialized_entities,
                "triplets": serialized_triplets,
            },
            event_id=saga_event_id,
        )

    # ------------------------------------------------------------------
    # Saga Execution Log helpers (Item A — crash-recovery)
    # ------------------------------------------------------------------

    async def _saga_log_start(self, saga_type: str, payload: StoreMemoryRequest) -> str:
        """Insert a 'started' saga row on an independent connection."""
        async with self.pg_pool.acquire(timeout=10.0) as conn:
            async with conn.transaction():
                await set_namespace_context(conn, payload.namespace_id)
                row = await conn.fetchrow(
                    """
                    INSERT INTO saga_execution_log (saga_type, namespace_id, agent_id, state, payload)
                    VALUES ($1, $2::uuid, $3, 'started', $4::jsonb)
                    RETURNING id
                    """,
                    saga_type,
                    str(payload.namespace_id),
                    payload.agent_id,
                    json.dumps(
                        {
                            # WORM-content gate / VII.5: the saga log is a mutable
                            # recovery table, but it must NOT persist pre-redaction
                            # content. Store recovery references only — never the raw
                            # `summary` or free-form `metadata` (both may carry PII
                            # before the Phase 0.3 redaction pipeline runs).
                            "memory_type": payload.memory_type.value,
                            "assertion_type": payload.assertion_type.value,
                        }
                    ),
                )
        return str(row["id"])

    async def _saga_log_transition(
        self, saga_id: str, state: SagaState, payload_patch: dict | None = None
    ) -> None:
        """Update saga state on an independent connection, optionally merging payload."""
        async with self.pg_pool.acquire(timeout=10.0) as conn:
            if payload_patch:
                await conn.execute(
                    """
                    UPDATE saga_execution_log
                    SET state = $1, updated_at = NOW(),
                        payload = payload || $3::jsonb
                    WHERE id = $2::uuid
                    """,
                    state,
                    saga_id,
                    json.dumps(payload_patch),
                )
            else:
                await conn.execute(
                    """
                    UPDATE saga_execution_log
                    SET state = $1, updated_at = NOW()
                    WHERE id = $2::uuid
                    """,
                    state,
                    saga_id,
                )

    async def _apply_rollback_on_failure(
        self,
        *,
        e,
        payload,
        collection,
        inserted_mongo_id,
        inserted_result,
        memory_id,
        pg_committed,
        saga_id=None,
    ):
        """Phase-aware universal rollback — cleans Mongo, PG, and safety-cleanup.

        Each store is rolled back independently; failures in one step are caught,
        logged, and do NOT block remaining steps nor mask the original exception.
        """
        log.error("[SAGA] Transaction failed: %s", e)
        SagaMetrics.record_failure("overall")

        # 1. Mongo rollback
        if inserted_mongo_id and inserted_result is not None:
            log.warning("[ROLLBACK] Removing orphaned Mongo doc %s", inserted_mongo_id)
            try:
                await collection.delete_one({"_id": inserted_result.inserted_id})
                log.info("[ROLLBACK] Mongo doc %s removed.", inserted_mongo_id)
            except Exception as mongo_exc:
                log.error("[ROLLBACK] Mongo cleanup failed: %s", mongo_exc)
                SagaMetrics.record_failure("mongo_rollback")

        # 2. Postgres rollback
        if pg_committed and memory_id:
            log.warning(
                "[ROLLBACK] PG transaction committed; removing all artifacts "
                "for memory_id=%s  payload_ref=%s",
                memory_id,
                inserted_mongo_id,
            )
            try:
                async with self.pg_pool.acquire(timeout=10.0) as conn:
                    async with conn.transaction():
                        await set_namespace_context(conn, payload.namespace_id)
                        await conn.execute(
                            "DELETE FROM memory_embeddings WHERE memory_id = $1", memory_id
                        )
                        await conn.execute(
                            "DELETE FROM pii_redactions WHERE memory_id = $1", memory_id
                        )
                        await conn.execute(
                            "DELETE FROM kg_node_embeddings "
                            "WHERE node_id IN (SELECT id FROM kg_nodes WHERE payload_ref = $1)",
                            inserted_mongo_id,
                        )
                        await conn.execute(
                            "DELETE FROM kg_edges WHERE payload_ref = $1", inserted_mongo_id
                        )
                        await conn.execute(
                            "DELETE FROM kg_nodes WHERE payload_ref = $1", inserted_mongo_id
                        )
                        from nce.event_log import append_event

                        await append_event(
                            conn=conn,
                            namespace_id=payload.namespace_id,
                            agent_id=payload.agent_id,
                            event_type="store_memory_rolled_back",
                            params={
                                "saga_id": str(saga_id) if saga_id else "",
                                "memory_id": str(memory_id),
                                "reason": str(e)[:256],
                                "payload_ref": inserted_mongo_id or "",
                            },
                        )
                        await conn.execute(
                            "UPDATE memories SET valid_to = now() WHERE id = $1 AND valid_to IS NULL",
                            memory_id,
                        )
                log.info("[ROLLBACK] PG artifacts removed for memory_id=%s", memory_id)
            except Exception as pg_exc:
                log.error("[ROLLBACK] PG cleanup failed (GC will reap): %s", pg_exc)
                SagaMetrics.record_failure("pg_rollback")
        elif inserted_mongo_id:
            log.warning(
                "[ROLLBACK] PG transaction did not commit; "
                "attempting safety cleanup by payload_ref=%s",
                inserted_mongo_id,
            )
            try:
                async with self.pg_pool.acquire(timeout=10.0) as conn:
                    async with conn.transaction():
                        await conn.execute(
                            "DELETE FROM kg_edges WHERE payload_ref = $1", inserted_mongo_id
                        )
                        await conn.execute(
                            "DELETE FROM kg_nodes WHERE payload_ref = $1", inserted_mongo_id
                        )
                        await conn.execute(
                            "UPDATE memories SET valid_to = now() WHERE payload_ref = $1 AND valid_to IS NULL",
                            inserted_mongo_id,
                        )
            except Exception as pg_exc:
                log.error("[ROLLBACK] PG safety cleanup failed (GC will reap): %s", pg_exc)
                SagaMetrics.record_failure("pg_rollback")

        if saga_id:
            try:
                await self._saga_log_transition(saga_id, SagaState.ROLLED_BACK)
            except Exception as saga_exc:
                log.error("[SAGA-LOG] Failed to transition to rolled_back: %s", saga_exc)

        log.info("[ROLLBACK] Tri-Stack remains pure.")

    # ------------------------------------------------------------------
    # store_memory (Saga Pattern)
    # ------------------------------------------------------------------

    async def _compute_salience_score(self, payload: StoreMemoryRequest) -> float:
        """Computes the decayed salience score (R) from metadata or derived_from memory.

        Batch 113: applies an actor-trust multiplier to the raw confidence prior so
        that high-trust actors have their stated confidence boosted and low-trust
        actors have it discounted at store time.  The multiplier is clamped to
        [0.5, 1.0] so it can only reduce or preserve (never inflate beyond 1.0 ×
        the raw prior).
        """
        R = None
        if payload.metadata:
            for k in ("confidence", "salience", "R"):
                if k in payload.metadata:
                    try:
                        R = float(payload.metadata[k])
                        break
                    except (ValueError, TypeError):
                        pass

        if R is None:
            R = 1.0  # default
            if payload.derived_from:
                try:
                    async with scoped_pg_session(self.pg_pool, payload.namespace_id) as conn:
                        parent_row = await conn.fetchrow(
                            "SELECT salience_score, updated_at FROM memory_salience WHERE memory_id = $1::uuid ORDER BY agent_id = 'system' DESC, agent_id = $2 DESC, updated_at DESC LIMIT 1",
                            payload.derived_from[0],
                            payload.agent_id,
                        )
                        if parent_row:
                            half_life_days = 30.0
                            ns_row = await conn.fetchrow(
                                "SELECT metadata FROM namespaces WHERE id = $1::uuid",
                                payload.namespace_id,
                            )
                            if ns_row and ns_row["metadata"]:
                                ns_meta = (
                                    json.loads(ns_row["metadata"])
                                    if isinstance(ns_row["metadata"], str)
                                    else ns_row["metadata"]
                                )
                                half_life_days = float(
                                    ns_meta.get("cognitive", {}).get("half_life_days", 30.0)
                                )

                            from nce.salience import compute_decayed_score

                            R = compute_decayed_score(
                                s_last=float(parent_row["salience_score"]),
                                updated_at=parent_row["updated_at"],
                                half_life_days=half_life_days,
                                memory_id=str(payload.derived_from[0]),
                            )
                except Exception as exc:
                    log.warning("[ACTIVE-LEARNING] Failed to compute parent memory decay: %s", exc)

        # Batch 113 — trust multiplier: scale raw confidence prior by actor trust.
        # Multiplier range [0.5, 1.0]: cannot inflate beyond the stated value but
        # discounts low-trust actors by up to 50 %.  Trust resolution failures
        # degrade gracefully (get_actor_trust returns cfg.NCE_TRUST_DEFAULT).
        try:
            from nce.active_learning import get_actor_trust

            actor_trust = await get_actor_trust(
                self.pg_pool, payload.namespace_id, payload.agent_id
            )
            # Normalise trust (default 0.65) so that default trust → multiplier = 1.0
            # and the adjustment is proportional to deviation from the default.
            trust_multiplier = max(0.5, min(1.0, actor_trust / cfg.NCE_TRUST_DEFAULT))
            R = R * trust_multiplier
        except Exception as exc:
            log.warning("[ACTIVE-LEARNING] Trust multiplier lookup failed, skipping: %s", exc)

        return R

    async def _quarantine_if_needed(
        self, payload: StoreMemoryRequest, R: float, bypass: bool
    ) -> dict | None:
        """Quarantines the memory if R is below the trust-derived threshold.

        Batch 113: the quarantine threshold is now dynamic — high-trust actors
        get a *lower* threshold (mid-confidence assertions pass through), while
        low-trust actors face a *higher* effective bar.  The fixed 0.65 constant
        from Batch 112 is replaced by ``quarantine_threshold(operator_trust)``.

        Bypass (operator-confirmed path) always skips this gate.
        """
        if bypass:
            return None

        from nce.active_learning import get_actor_trust, quarantine_threshold

        actor_trust = await get_actor_trust(self.pg_pool, payload.namespace_id, payload.agent_id)
        threshold = quarantine_threshold(actor_trust)

        log.debug(
            "[ACTIVE-LEARNING] agent=%s trust=%.3f threshold=%.3f R=%.3f",
            payload.agent_id,
            actor_trust,
            threshold,
            R,
        )

        if R < threshold:
            async with scoped_pg_session(self.pg_pool, payload.namespace_id) as conn:
                from nce.active_learning import ActiveLearningManager

                al_mgr = ActiveLearningManager(self.pg_pool)
                queue_item_id = await al_mgr.quarantine_memory(conn, payload, R)
                return {
                    "quarantined": True,
                    "queue_item_id": str(queue_item_id),
                    "R": R,
                    "actor_trust": actor_trust,
                }
        return None

    async def _store_episodic_mongodb(
        self, payload: StoreMemoryRequest, sanitized_heavy: str, pii_result: Any
    ) -> tuple[str, Any, bytes | None, str | None]:
        """STEP 1: Episodic Commit (MongoDB).

        Part II.4 (Provable Forgetting): when ``NCE_ENVELOPE_ENCRYPTION_ENABLED``
        is set, the raw payload (``raw_data``) is encrypted under a fresh
        per-memory DEK before it touches Mongo, and the wrapped DEK + key id are
        returned so they can be persisted on the ``memories`` row.  When the flag
        is off, ``raw_data`` is stored as plaintext and ``(None, None)`` is
        returned (the legacy / back-compatible shape).
        """
        user_id = payload.metadata.get("user_id") if payload.metadata else None
        session_id = payload.metadata.get("session_id") if payload.metadata else None

        raw_data: Any = sanitized_heavy
        wrapped_dek: bytes | None = None
        dek_key_id: str | None = None
        if cfg.NCE_ENVELOPE_ENCRYPTION_ENABLED:
            from nce.envelope import encrypt_raw_data

            raw_data, wrapped_dek, dek_key_id = encrypt_raw_data(sanitized_heavy)

        async with scoped_mongo_session(self.mongo_client, payload.namespace_id) as db:
            from pymongo.write_concern import WriteConcern

            from nce.db_utils import ScopedMongoCollection

            # Apply write concern majority + journaling to underlying collection, then re-wrap
            raw_coll = db.episodes._collection
            if not hasattr(raw_coll, "_mock_self"):
                raw_coll = raw_coll.with_options(write_concern=WriteConcern(w="majority", j=True))
            scoped_coll = ScopedMongoCollection(raw_coll, str(payload.namespace_id))

            inserted_result = await scoped_coll.insert_one(
                {
                    "user_id": user_id,
                    "session_id": session_id,
                    "namespace_id": str(payload.namespace_id),
                    "type": payload.memory_type.value,
                    "raw_data": raw_data,
                    "metadata": payload.metadata,
                    "pii_redacted": pii_result.redacted,
                    "pii_entities_found": pii_result.entities_found,
                    "ingested_at": datetime.now(timezone.utc),
                }
            )
        inserted_mongo_id = str(inserted_result.inserted_id)
        log.debug("[Mongo] Inserted episode. id=%s", inserted_mongo_id)
        return inserted_mongo_id, inserted_result, wrapped_dek, dek_key_id

    async def _store_semantic_graph_pg(
        self,
        payload: StoreMemoryRequest,
        sanitized_summary: str,
        vector: list[float],
        node_vecs: list[list[float]],
        pii_result: Any,
        inserted_mongo_id: str,
        entities: list,
        triplets: list,
        saga_id: str,
        user_id: str | None,
        session_id: str | None,
        wrapped_dek: bytes | None = None,
        dek_key_id: str | None = None,
    ) -> UUID:
        """STEP 2 + 2b + 2c: Atomic Semantic + Graph Commit (single PG transaction)."""
        # Pre-generate the store_memory event UUID so it can be stored on the
        # memories row as origin_event_id (Batch C1 change-origin requirement).
        saga_event_id = uuid.uuid4()

        async with scoped_pg_session(self.pg_pool, payload.namespace_id) as conn:
            # Fetch active and migrating models
            models = await conn.fetch(
                "SELECT id FROM embedding_models WHERE status IN ('active', 'migrating')"
            )
            target_model_ids = [m["id"] for m in models]

            async with conn.transaction():
                memory_id = await self._embed_and_insert_vectors(
                    conn,
                    payload=payload,
                    sanitized_summary=sanitized_summary,
                    vector=vector,
                    pii_result=pii_result,
                    inserted_mongo_id=inserted_mongo_id,
                    target_model_ids=target_model_ids,
                    user_id=user_id,
                    session_id=session_id,
                    wrapped_dek=wrapped_dek,
                    dek_key_id=dek_key_id,
                    origin_event_id=saga_event_id,
                )

                await self._insert_graph_nodes_and_edges(
                    conn,
                    payload=payload,
                    entities=entities,
                    node_vecs=node_vecs,
                    triplets=triplets,
                    inserted_mongo_id=inserted_mongo_id,
                    target_model_ids=target_model_ids,
                    memory_id=memory_id,
                    saga_id=saga_id,
                    saga_event_id=saga_event_id,
                )

                # STEP 2c: Transactional Outbox — atomically publish
                await self._enqueue_outbox(
                    conn,
                    namespace_id=str(payload.namespace_id),
                    aggregate_type="memory",
                    aggregate_id=str(memory_id),
                    event_type="memory.stored",
                    payload={
                        "saga_id": str(saga_id),
                        "memory_id": str(memory_id),
                        "payload_ref": inserted_mongo_id,
                        "assertion_type": payload.assertion_type.value,
                        "memory_type": payload.memory_type.value,
                        "entities_count": len(entities),
                        "triplets_count": len(triplets),
                    },
                    headers={"source": "store_memory"},
                )
        log.debug(
            "[PG] Atomic commit: vector + %d nodes + %d edges. mongo_ref=%s",
            len(entities),
            len(triplets),
            inserted_mongo_id,
        )
        return memory_id

    async def _cache_working_memory_redis(
        self,
        namespace_id: UUID,
        user_id: str | None,
        session_id: str | None,
        sanitized_summary: str,
    ) -> None:
        """STEP 3: Working Memory (Redis)."""
        if user_id and session_id:
            try:
                redis_key = f"cache:{namespace_id}:{user_id}:{session_id}"
                await self.redis_client.setex(redis_key, cfg.REDIS_TTL, sanitized_summary)
                log.debug("[Redis] Summary cached. key=%s", redis_key)
            except Exception:
                log.warning("[Redis] Cache write failed; core write remains valid.", exc_info=True)

    async def _detect_contradictions_sync(
        self,
        payload: StoreMemoryRequest,
        memory_id: UUID,
        sanitized_summary: str,
        vector: list[float],
        triplets: list,
    ) -> Any:
        """STEP 4: Contradiction Detection."""
        contradiction_result = None
        if payload.check_contradictions:
            from nce.contradictions import detect_contradictions

            try:
                contradiction_result = await detect_contradictions(
                    self.pg_pool,
                    self.mongo_client,
                    str(payload.namespace_id),
                    str(memory_id),
                    sanitized_summary,
                    payload.assertion_type.value,
                    vector,
                    payload.agent_id,
                    triplets,
                    detection_path="sync",
                )
            except Exception as e:
                log.error("Contradiction detection failed: %s", e)
        return contradiction_result

    async def _run_store_memory_saga(self, payload: StoreMemoryRequest) -> dict:
        """Executes the core transactional write saga across MongoDB, PG, and Redis."""
        with SagaMetrics("store_memory"):
            inserted_mongo_id: str | None = None
            inserted_result = None
            memory_id: UUID | None = None
            pg_committed = False
            saga_id = await self._saga_log_start("store_memory", payload)

            try:
                # --- Phase 0.3: PII Redaction + Graph Extraction ---
                (
                    pii_result,
                    sanitized_summary,
                    sanitized_heavy,
                    entities,
                    triplets,
                ) = await self._apply_pii_pipeline(payload)

                # STEP 1: Episodic Commit (MongoDB)
                user_id = payload.metadata.get("user_id") if payload.metadata else None
                session_id = payload.metadata.get("session_id") if payload.metadata else None

                (
                    inserted_mongo_id,
                    inserted_result,
                    wrapped_dek,
                    dek_key_id,
                ) = await self._store_episodic_mongodb(payload, sanitized_heavy, pii_result)

                # Pre-compute all embeddings OUTSIDE the PG transaction
                all_texts = [sanitized_summary] + [e.label for e in entities]
                all_vectors = await _embeddings.embed_batch(all_texts)
                vector = all_vectors[0]
                node_vecs = all_vectors[1:]

                # STEP 2 + 2b + 2c: Atomic Semantic + Graph Commit (single PG transaction)
                memory_id = await self._store_semantic_graph_pg(
                    payload=payload,
                    sanitized_summary=sanitized_summary,
                    vector=vector,
                    node_vecs=node_vecs,
                    pii_result=pii_result,
                    inserted_mongo_id=inserted_mongo_id,
                    entities=entities,
                    triplets=triplets,
                    saga_id=saga_id,
                    user_id=user_id,
                    session_id=session_id,
                    wrapped_dek=wrapped_dek,
                    dek_key_id=dek_key_id,
                )

                # Mark committed once exited from PG session block successfully
                pg_committed = True

            except Exception as e:
                collection = self.mongo_client.memory_archive.episodes
                await self._apply_rollback_on_failure(
                    e=e,
                    payload=payload,
                    collection=collection,
                    inserted_mongo_id=inserted_mongo_id,
                    inserted_result=inserted_result,
                    memory_id=memory_id,
                    pg_committed=pg_committed,
                    saga_id=saga_id,
                )
                raise

            # --- PG committed; all subsequent failures are advisory ---
            try:
                await self._saga_log_transition(
                    saga_id, SagaState.PG_COMMITTED, payload_patch={"memory_id": str(memory_id)}
                )
            except Exception:
                log.warning("[SAGA] PG_COMMITTED transition failed.", exc_info=True)

            # STEP 3: Working Memory (Redis)
            await self._cache_working_memory_redis(
                payload.namespace_id, user_id, session_id, sanitized_summary
            )

            # STEP 4: Contradiction Detection
            contradiction_result = await self._detect_contradictions_sync(
                payload, memory_id, sanitized_summary, vector, triplets
            )

            try:
                await self._saga_log_transition(saga_id, SagaState.COMPLETED)
            except Exception:
                log.warning("[SAGA] COMPLETED transition failed.", exc_info=True)

            return {
                "quarantined": False,
                "payload_ref": inserted_mongo_id,
                "contradiction": contradiction_result,
            }

    async def store_memory(self, payload: StoreMemoryRequest) -> dict:
        """
        Saga Pattern: MongoDB → PostgreSQL → Redis.
        PG failure triggers automatic Mongo rollback.
        """
        tracer = get_tracer()
        with tracer.start_as_current_span("orchestrator.store_memory") as span:
            span.set_attribute("nce.namespace_id", str(payload.namespace_id))

            # Active Learning Loop (BATCH-P3-005)
            bypass = False
            if payload.metadata:
                if payload.metadata.get("bypass_quarantine"):
                    bypass = True

            R = await self._compute_salience_score(payload)

            quarantine_result = await self._quarantine_if_needed(payload, R, bypass)
            if quarantine_result is not None:
                return quarantine_result

            # Bypass or R >= 0.65 -> proceed with write saga (Slow I/O outside PG transaction)
            res = await self._run_store_memory_saga(payload)
            log.debug("Saga memory storage execution complete")
            return res

    # ------------------------------------------------------------------
    # store_artifact (formerly store_media)
    # ------------------------------------------------------------------

    async def store_artifact(self, payload: ArtifactPayload) -> str:
        """Upload artifact to MinIO, index summary into Tri-Stack."""
        tracer = get_tracer()
        with tracer.start_as_current_span("orchestrator.store_artifact") as span:
            span.set_attribute("nce.artifact_type", payload.media_type)
            with SagaMetrics("store_artifact"):
                import pathlib

                staging_root = cfg.NCE_ARTIFACT_STAGING_DIR
                if staging_root:
                    base = pathlib.Path(staging_root).resolve()
                    candidate = (base / pathlib.Path(payload.file_path_on_disk).name).resolve()
                    if not (candidate.is_file() and base in candidate.parents):
                        raise FileNotFoundError(
                            f"Artifact not found or outside staging dir: {payload.file_path_on_disk!r}"
                        )
                else:
                    candidate = pathlib.Path(payload.file_path_on_disk).resolve()
                    if not candidate.is_file():
                        raise FileNotFoundError(
                            f"Artifact file not found: {payload.file_path_on_disk!r}"
                        )
                safe_path = str(candidate)

                if self.minio_client is None:
                    raise RuntimeError("MinIO client not configured — cannot store media.")

                bucket_name = f"mcp-{payload.media_type}"
                file_ext = os.path.splitext(safe_path)[1]
                object_name = (
                    f"{payload.namespace_id}/{payload.session_id}/{uuid.uuid4().hex}{file_ext}"
                )

                # Enforce that the object name carries the namespace prefix
                ns_prefix = f"{payload.namespace_id}/"
                if not object_name.startswith(ns_prefix):
                    raise PermissionError(
                        "Access denied: Object name must start with namespace prefix."
                    )

                await asyncio.to_thread(
                    self.minio_client.fput_object,
                    bucket_name,
                    object_name,
                    safe_path,
                )
                log.info(
                    "[MinIO] Uploaded artifact to %s/%s",
                    bucket_name,
                    object_name,
                )

                media_metadata = {
                    "bucket": bucket_name,
                    "object_name": object_name,
                    "media_type": payload.media_type,
                    "original_path": payload.file_path_on_disk,
                    "user_id": payload.user_id,
                    "session_id": payload.session_id,
                }

                memory_req = StoreMemoryRequest(
                    namespace_id=payload.namespace_id,
                    agent_id="system",
                    content=payload.summary,
                    summary=payload.summary,
                    heavy_payload=payload.summary,
                    metadata=media_metadata,
                    memory_type=MemoryType.episodic,
                    assertion_type=AssertionType.observation,
                )

                try:
                    res = await self.store_memory(memory_req)
                except Exception:
                    # Saga rollback: remove orphaned MinIO object
                    log.warning(
                        "[MinIO-ROLLBACK] Removing orphaned object %s/%s",
                        bucket_name,
                        object_name,
                    )
                    try:
                        await asyncio.to_thread(
                            self.minio_client.remove_object,
                            bucket_name,
                            object_name,
                        )
                    except Exception as minio_exc:
                        log.error(
                            "[MinIO-ROLLBACK] Failed to remove %s/%s: %s",
                            bucket_name,
                            object_name,
                            minio_exc,
                        )
                        from nce.observability import MINIO_ORPHAN_CLEANUP_FAILURES_TOTAL

                        MINIO_ORPHAN_CLEANUP_FAILURES_TOTAL.inc()
                    raise
                return res["payload_ref"]

    async def store_media(self, payload: MediaPayload) -> str:
        """[DEPRECATED] Alias for store_artifact."""
        import warnings

        warnings.warn(
            "store_media is deprecated; use store_artifact instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        return await self.store_artifact(payload)

    # ------------------------------------------------------------------
    # verify_memory
    # ------------------------------------------------------------------

    def _db_pool(self, read_only: bool = False) -> asyncpg.Pool:
        """Return read-replica pool for reads when available."""
        if read_only and self.pg_read_pool is not None:
            return self.pg_read_pool
        return self.pg_pool

    async def verify_memory(self, memory_id: str, as_of: datetime | None = None) -> dict:
        """[Phase 0.2] Verify integrity and causal provenance of a memory."""
        from nce.signing import (
            decrypt_signing_key,
            require_master_key,
            verify_fields,
        )

        async with self._db_pool(read_only=True).acquire(timeout=10.0) as conn:
            if as_of:
                row = await conn.fetchrow(
                    """
                    SELECT m.*, sk.encrypted_key, sk.key_id as signing_key_id
                    FROM memories m
                    LEFT JOIN signing_keys sk ON sk.key_id = m.signature_key_id
                    WHERE m.id = $1 AND m.valid_from <= $2
                      AND (m.valid_to IS NULL OR m.valid_to > $2)
                    ORDER BY m.valid_from DESC LIMIT 1
                    """,
                    UUID(memory_id),
                    as_of,
                )
            else:
                row = await conn.fetchrow(
                    """
                    SELECT m.*, sk.encrypted_key, sk.key_id as signing_key_id
                    FROM memories m
                    LEFT JOIN signing_keys sk ON sk.key_id = m.signature_key_id
                    WHERE m.id = $1 AND m.valid_to IS NULL
                    """,
                    UUID(memory_id),
                )

            if not row:
                return {"valid": False, "reason": "memory_not_found"}

            if row["signature_key_id"] is None:
                return {"valid": None, "reason": "not_signed", "payload_hash": None}

            with require_master_key() as master_key:
                raw_key = decrypt_signing_key(bytes(row["encrypted_key"]), master_key)

            fields = {
                "namespace_id": str(row["namespace_id"]),
                "agent_id": row["agent_id"],
                "payload_ref": row["payload_ref"],
                "created_at": row["created_at"].isoformat(),
                "assertion_type": row["assertion_type"],
            }

            is_valid = verify_fields(fields, raw_key, bytes(row["signature"]))

            payload_hash = None
            # Cache the payload hash in Redis to avoid recalculating on every call.
            # The cache is invalidated if the memory is updated (payload_ref changes).
            cache_key = f"mem_verify_hash:{memory_id}"
            try:
                cached_hash = await self.redis_client.get(cache_key)
                if cached_hash is not None:
                    payload_hash = cached_hash.decode("utf-8")
            except Exception:
                pass  # Cache miss or Redis down — recalculate below

            if payload_hash is None:
                async with scoped_mongo_session(self.mongo_client, row["namespace_id"]) as s_db:
                    doc = await s_db.episodes.find_one({"_id": row["payload_ref"]})
                if doc:
                    # Part II.4: hash the *decrypted* content so the payload hash is
                    # stable across the plaintext→ciphertext rollout (legacy rows
                    # have wrapped_dek NULL and read as plaintext).
                    from nce.envelope import maybe_decrypt_raw_data

                    wrapped = row["wrapped_dek"]
                    content = maybe_decrypt_raw_data(
                        doc.get("raw_data", ""),
                        bytes(wrapped) if wrapped is not None else None,
                    )
                    payload_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
                    try:
                        await self.redis_client.setex(cache_key, cfg.REDIS_TTL, payload_hash)
                    except Exception:
                        pass  # Non-critical cache write

            return {
                "valid": is_valid,
                "reason": "ok" if is_valid else "signature_mismatch",
                "signed_at": row["created_at"].isoformat(),
                "key_id": row["signing_key_id"],
                "payload_hash": payload_hash,
            }

    # ------------------------------------------------------------------
    # unredact_memory
    # ------------------------------------------------------------------

    async def unredact_memory(self, memory_id: str, namespace_id: str, agent_id: str) -> dict:
        """[Phase 0.3] Reverse pseudonymisation for a given memory (admin-only)."""
        from nce.event_log import append_event
        from nce.signing import decrypt_signing_key, require_master_key

        # Phase 1 — RLS-scoped PG read (FIX-025: never raw pool.acquire for tenant paths).
        async with scoped_pg_session(self.pg_pool, namespace_id) as conn:
            ns_row = await conn.fetchrow(
                "SELECT metadata FROM namespaces WHERE id = $1", namespace_id
            )
            if (
                not ns_row
                or "pii" not in ns_row["metadata"]
                or not ns_row["metadata"]["pii"].get("reversible")
            ):
                raise ValueError(
                    "Namespace PII policy does not allow unredaction (reversible=False)."
                )

            mem_row = await conn.fetchrow(
                "SELECT payload_ref, pii_redacted, wrapped_dek FROM memories WHERE id = $1",
                memory_id,
            )
            if not mem_row:
                raise ValueError("Memory not found.")
            if not mem_row["pii_redacted"]:
                return {"status": "not_redacted"}

            vault_rows = await conn.fetch(
                "SELECT token, encrypted_value FROM pii_redactions WHERE memory_id = $1",
                memory_id,
            )
            if not vault_rows:
                return {"status": "no_vault_entries"}

            payload_ref = mem_row["payload_ref"]
            wrapped_dek = mem_row["wrapped_dek"]
            vault_list = [
                {"token": r["token"], "encrypted_value": r["encrypted_value"]} for r in vault_rows
            ]

        # Phase 2 — Mongo + local crypto (no DB connection held).
        async with scoped_mongo_session(self.mongo_client, namespace_id) as s_db:
            doc = await s_db.episodes.find_one({"_id": ObjectId(payload_ref)})
        if not doc:
            raise ValueError("MongoDB payload missing.")

        # Part II.4: decrypt the raw payload under its wrapped DEK; legacy rows
        # (wrapped_dek NULL) read as plaintext.
        from nce.envelope import maybe_decrypt_raw_data

        stored_raw = doc.get("raw_data", "")
        if not wrapped_dek and not isinstance(stored_raw, str):
            return {"status": "raw_data_not_string"}
        raw_data = maybe_decrypt_raw_data(
            stored_raw, bytes(wrapped_dek) if wrapped_dek is not None else None
        )

        with require_master_key() as mk:
            for v_row in vault_list:
                token = v_row["token"]
                encrypted_val = v_row["encrypted_value"]
                try:
                    original_val = decrypt_signing_key(encrypted_val, mk).decode("utf-8")
                    raw_data = raw_data.replace(token, original_val)
                except Exception as e:
                    log.warning("Failed to decrypt token %s: %s", token, e)

        # Phase 3 — append audit event under RLS.
        async with scoped_pg_session(self.pg_pool, namespace_id) as conn:
            async with conn.transaction():
                await append_event(
                    conn=conn,
                    namespace_id=UUID(namespace_id),
                    agent_id=agent_id,
                    event_type="unredact",
                    params={"memory_id": memory_id},
                    result_summary={
                        "status": "success",
                        "tokens_unredacted": len(vault_list),
                    },
                )

        return {"status": "success", "unredacted_text": raw_data}

    # ------------------------------------------------------------------
    # shred_memory — Part II.4 Provable Forgetting
    # ------------------------------------------------------------------

    async def shred_memory(
        self,
        memory_id: str,
        namespace_id: str,
        agent_id: str,
    ) -> dict:
        """[Part II.4] Provably forget a memory across every store.

        Performs the full provable-forgetting sequence and returns a verifiable
        *deletion receipt*.  The cryptographic guarantee is: after this call the
        raw payload is **cryptographically unrecoverable** (its per-memory DEK is
        destroyed) and every plaintext *derivative* is deleted; the immutable
        ``event_log`` retains only the *fact* of deletion — never the content.

        Sequence (the durable steps run inside one RLS-scoped PG transaction so
        they commit atomically with the signed WORM event):

          1. Destroy the DEK — zero ``memories.wrapped_dek`` / ``dek_key_id`` so
             the encrypted Mongo ``episodes.raw_data`` becomes undecryptable.
          2. Delete the plaintext derivatives — ``memories.content_fts`` and
             ``memories.embedding`` (zeroed in place) plus ``memory_embeddings``.
          3. ATMS-cascade-delete the KG labels/edges (``kg_nodes`` / ``kg_edges``
             keyed by ``payload_ref``) and derived/consolidated dependent
             memories (reuses the Batch-23 ATMS mechanism).
          4. Delete the ``pii_redactions`` rows.
          5. Append a signed, **content-free** ``memory_shredded`` WORM event
             (refs + counts + key-id only).

        The best-effort, out-of-transaction steps (durable once the PG tx has
        committed; a partial failure leaves the content cryptographically
        unrecoverable regardless, and is surfaced in the receipt's ``warnings``):

          6. Purge the Redis working-memory cache key(s).
          7. ``remove_object`` the MinIO media object(s).
          8. Overwrite the Mongo ciphertext with a tombstone (defence-in-depth;
             the content is already unrecoverable once the DEK is destroyed).

        RLS: all tenant SQL runs inside ``scoped_session`` so a caller cannot
        shred a memory outside their own namespace.
        """
        from nce.atms import evaluate_atms_intervention, persist_atms_invalidation
        from nce.event_log import append_event

        ns_uuid = UUID(str(namespace_id))
        mem_uuid = UUID(str(memory_id))

        # ── Durable phase: DEK destroy + derivative deletes + WORM event ──────
        # All inside one RLS-scoped transaction; append_event shares the tx.
        async with scoped_pg_session(self.pg_pool, namespace_id) as conn:
            async with conn.transaction():
                # Defence-in-depth on top of RLS: the row must live in this
                # namespace, else the SELECT returns nothing and we abort.
                row = await conn.fetchrow(
                    """
                    SELECT payload_ref, dek_key_id,
                           (wrapped_dek IS NOT NULL) AS was_encrypted,
                           user_id, session_id, agent_id, metadata
                    FROM memories
                    WHERE id = $1::uuid AND namespace_id = $2::uuid
                    """,
                    mem_uuid,
                    ns_uuid,
                )
                if not row:
                    raise PermissionError(f"Memory {memory_id} not accessible in your namespace")

                payload_ref = row["payload_ref"]
                dek_key_id = row["dek_key_id"]
                was_encrypted = bool(row["was_encrypted"])
                metadata = row["metadata"]
                if isinstance(metadata, str):
                    try:
                        metadata = json.loads(metadata)
                    except Exception:
                        metadata = {}
                metadata = metadata or {}

                # 1+2. Destroy the DEK and the plaintext derivatives on the
                # memories row.  Zeroing wrapped_dek crypto-shreds the Mongo
                # ciphertext; content_fts/embedding are reversible derivatives.
                await conn.execute(
                    """
                    UPDATE memories
                    SET wrapped_dek = NULL,
                        dek_key_id = NULL,
                        content_fts = NULL,
                        embedding = NULL,
                        valid_to = COALESCE(valid_to, now())
                    WHERE id = $1::uuid AND namespace_id = $2::uuid
                    """,
                    mem_uuid,
                    ns_uuid,
                )

                # 2b. Delete derived embedding vectors.
                res_emb = await conn.execute(
                    "DELETE FROM memory_embeddings WHERE memory_id = $1::uuid",
                    mem_uuid,
                )
                embeddings_deleted = int(res_emb.split()[-1]) if res_emb else 0

                # 3. ATMS-cascade-delete KG labels/edges + derived memories.
                #    KG nodes/edges are keyed by the Mongo payload_ref (the
                #    content fanned out under that ref); delete them outright —
                #    labels are plaintext content that cannot be encrypted.
                nodes_deleted = 0
                edges_deleted = 0
                if payload_ref:
                    await conn.execute(
                        "DELETE FROM kg_node_embeddings "
                        "WHERE node_id IN (SELECT id FROM kg_nodes WHERE payload_ref = $1)",
                        payload_ref,
                    )
                    res_edges = await conn.execute(
                        "DELETE FROM kg_edges WHERE payload_ref = $1", payload_ref
                    )
                    edges_deleted = int(res_edges.split()[-1]) if res_edges else 0
                    res_nodes = await conn.execute(
                        "DELETE FROM kg_nodes WHERE payload_ref = $1", payload_ref
                    )
                    nodes_deleted = int(res_nodes.split()[-1]) if res_nodes else 0

                # 3b. Cascade soft-deletion to derived/consolidated dependents
                #     and topology edges via the Batch-23 ATMS mechanism.
                cascade_set: set[str] = {str(mem_uuid)}
                topo_cascade = await evaluate_atms_intervention(conn, ns_uuid, str(mem_uuid))
                cascade_set.update(topo_cascade)

                max_cascade = 100
                todo = [str(mem_uuid)]
                visited = {str(mem_uuid)}
                while todo and len(visited) < max_cascade:
                    current = todo.pop()
                    dep_rows = await conn.fetch(
                        """
                        SELECT id FROM memories
                        WHERE namespace_id = $1::uuid
                          AND (derived_from @> jsonb_build_array($2::text)
                               OR derived_from @> jsonb_build_array($2::uuid))
                          AND valid_to IS NULL
                        """,
                        ns_uuid,
                        current,
                    )
                    for dep in dep_rows:
                        dep_id = str(dep["id"])
                        if dep_id not in visited:
                            visited.add(dep_id)
                            todo.append(dep_id)
                            if len(visited) >= max_cascade:
                                break
                cascade_set.update(visited)
                await persist_atms_invalidation(conn, ns_uuid, cascade_set)

                # 4. Delete the PII vault rows (encrypted derivatives).
                res_pii = await conn.execute(
                    "DELETE FROM pii_redactions WHERE memory_id = $1::uuid",
                    mem_uuid,
                )
                pii_deleted = int(res_pii.split()[-1]) if res_pii else 0

                # 5. Append the signed, content-free memory_shredded WORM event.
                #    Carries refs + counts + key-id ONLY — never any content,
                #    entity string, summary, or PII.  A content-free digest binds
                #    the receipt to the destroyed artifacts without revealing them.
                shred_facts = {
                    "memory_id": str(mem_uuid),
                    "payload_ref": payload_ref or "",
                    "dek_key_id": dek_key_id or "",
                    "was_encrypted": was_encrypted,
                    "cascade_count": len(cascade_set),
                }
                receipt_digest = hashlib.sha256(
                    json.dumps(shred_facts, sort_keys=True).encode("utf-8")
                ).hexdigest()

                append_result = await append_event(
                    conn=conn,
                    namespace_id=ns_uuid,
                    agent_id=agent_id,
                    event_type="memory_shredded",
                    params={
                        "memory_id": str(mem_uuid),
                        "payload_ref": payload_ref or "",
                        "dek_key_id": dek_key_id or "",
                        "was_encrypted": was_encrypted,
                        "kg_nodes_deleted": nodes_deleted,
                        "kg_edges_deleted": edges_deleted,
                        "embeddings_deleted": embeddings_deleted,
                        "pii_redactions_deleted": pii_deleted,
                        "cascade_ids": sorted(cascade_set),
                        "receipt_digest": receipt_digest,
                    },
                    result_summary={
                        "status": "success",
                        "cascade_count": len(cascade_set),
                    },
                )

        # ── Best-effort phase (post-commit): Redis, MinIO, Mongo tombstone ────
        # The cryptographic guarantee already holds; these reduce the residual
        # ciphertext/cache surface.  Failures are recorded in `warnings`, never
        # raised — the durable forget has already committed.
        warnings: list[str] = []

        # 6. Purge Redis working-memory cache key(s).
        redis_keys_purged = 0
        if self.redis_client is not None:
            user_id = row["user_id"]
            session_id = row["session_id"]
            mem_agent = row["agent_id"]
            candidate_keys: list[str] = []
            if user_id and session_id:
                candidate_keys.append(f"cache:{namespace_id}:{user_id}:{session_id}")
            if mem_agent:
                candidate_keys.append(f"cache:{namespace_id}:{mem_agent}")
            candidate_keys.append(f"mem_verify_hash:{memory_id}")
            for key in candidate_keys:
                try:
                    redis_keys_purged += int(await self.redis_client.delete(key))
                except Exception as exc:
                    warnings.append(f"redis_purge_failed:{key}:{exc}")

        # 7. remove_object the MinIO media object(s).
        minio_objects_removed = 0
        bucket = metadata.get("bucket")
        object_name = metadata.get("object_name")
        if bucket and object_name:
            if self.minio_client is None:
                warnings.append("minio_object_present_but_client_unconfigured")
            else:
                try:
                    # Enforce that the object name carries the namespace prefix
                    ns_prefix = f"{namespace_id}/"
                    if not object_name.startswith(ns_prefix):
                        raise PermissionError(
                            "Access denied: Object name does not belong to this namespace."
                        )
                    await asyncio.to_thread(self.minio_client.remove_object, bucket, object_name)
                    minio_objects_removed = 1
                except Exception as exc:
                    warnings.append(f"minio_remove_failed:{bucket}/{object_name}:{exc}")

        # 8. Overwrite the Mongo ciphertext with a tombstone (defence-in-depth).
        if payload_ref and self.mongo_client is not None:
            try:
                oid = ObjectId(payload_ref)
                async with scoped_mongo_session(self.mongo_client, namespace_id) as s_db:
                    await s_db.episodes.update_one(
                        {"_id": oid},
                        {
                            "$set": {
                                "raw_data": None,
                                "shredded": True,
                                "shredded_at": datetime.now(timezone.utc),
                            },
                            "$unset": {"metadata": ""},
                        },
                    )
            except Exception as exc:
                warnings.append(f"mongo_tombstone_failed:{payload_ref}:{exc}")

        # ── Build the verifiable deletion receipt ─────────────────────────────
        receipt = {
            "memory_id": str(mem_uuid),
            "namespace_id": str(ns_uuid),
            "dek_destroyed": was_encrypted,
            "dek_key_id": dek_key_id or "",
            "payload_ref": payload_ref or "",
            "derivatives_deleted": {
                "content_fts": True,
                "embedding": True,
                "memory_embeddings": embeddings_deleted,
                "kg_nodes": nodes_deleted,
                "kg_edges": edges_deleted,
                "pii_redactions": pii_deleted,
            },
            "cascade_count": len(cascade_set),
            "redis_keys_purged": redis_keys_purged,
            "minio_objects_removed": minio_objects_removed,
            "receipt_digest": receipt_digest,
            "worm_event": {
                "event_id": str(append_result.event_id),
                "event_seq": append_result.event_seq,
                "occurred_at": append_result.occurred_at.isoformat(),
                "event_type": "memory_shredded",
            },
            "warnings": warnings,
            "guarantee": (
                "raw payload is cryptographically unrecoverable (DEK destroyed) "
                "and all plaintext derivatives are deleted; the immutable log "
                "retains only the fact of deletion, never the content. "
                "Note: entity/triplet strings recorded in prior store_memory WORM "
                "events at write time persist there by design."
            ),
        }

        # Self-verify the WORM event signature so the receipt ships verified.
        async with scoped_pg_session(self.pg_pool, namespace_id) as conn:
            event_row = await conn.fetchrow(
                """
                SELECT id, namespace_id, agent_id, event_type, event_seq,
                       occurred_at, params, signature, signature_key_id,
                       signature_version, chain_hash
                FROM event_log
                WHERE id = $1 AND namespace_id = $2::uuid
                """,
                append_result.event_id,
                ns_uuid,
            )
            from nce.event_log import DataIntegrityError, verify_event_signature

            try:
                await verify_event_signature(conn, event_row)
                receipt["verified"] = True
            except DataIntegrityError:
                receipt["verified"] = False

        return {"status": "success", "receipt": receipt}

    # ------------------------------------------------------------------
    # recall_memory / recall_recent
    # ------------------------------------------------------------------

    async def recall_memory(
        self,
        namespace_id: str,
        user_id: str,
        session_id: str,
        as_of: datetime | None = None,
    ) -> str | None:
        """Legacy single-result recall."""
        results = await self.recall_recent(
            namespace_id,
            agent_id=user_id,
            limit=1,
            as_of=as_of,
            user_id=user_id,
            session_id=session_id,
        )
        return results[0] if results else None

    async def recall_recent(
        self,
        namespace_id: str,
        agent_id: str = "default",
        limit: int = 10,
        as_of: datetime | None = None,
        user_id: str | None = None,
        session_id: str | None = None,
        offset: int = 0,
    ) -> list[str]:
        """[Phase 2.2] Retrieve N most recent episodic memories for an agent."""
        if not namespace_id:
            raise ValueError("namespace_id is required")

        agent_id = validate_agent_id(agent_id)
        if offset < 0:
            raise ValueError("offset must be >= 0")
        offset = int(offset)
        if user_id and not _SAFE_ID_RE.match(user_id):
            raise ValueError("Invalid user_id format")
        if session_id and not _SAFE_ID_RE.match(session_id):
            raise ValueError("Invalid session_id format")

        if not as_of and limit == 1 and offset == 0:
            if user_id and session_id:
                redis_key = f"cache:{namespace_id}:{user_id}:{session_id}"
            elif not user_id and not session_id:
                redis_key = f"cache:{namespace_id}:{agent_id}"
            else:
                redis_key = None

            if redis_key:
                cached = await self.redis_client.get(redis_key)
                if cached:
                    log.debug("[Redis] Cache hit. key=%s", redis_key)
                    return [cached.decode()]

        async with scoped_pg_session(self._db_pool(read_only=True), namespace_id) as conn:
            filters = ["namespace_id = $1", "memory_type = 'episodic'"]
            params: list[Any] = [UUID(str(namespace_id))]
            p_idx = 2

            if user_id:
                filters.append(f"user_id = ${p_idx}")
                params.append(user_id)
                p_idx += 1
            if session_id:
                filters.append(f"session_id = ${p_idx}")
                params.append(session_id)
                p_idx += 1
            if not user_id and not session_id:
                filters.append(f"agent_id = ${p_idx}")
                params.append(agent_id)
                p_idx += 1

            if as_of:
                filters.append(f"valid_from <= ${p_idx}")
                params.append(as_of)
                p_idx += 1
                filters.append(f"(valid_to IS NULL OR valid_to > ${p_idx - 1})")
                order_by = "valid_from DESC"
            else:
                filters.append("valid_to IS NULL")
                order_by = "created_at DESC"

            sql = f"""
                SELECT payload_ref, wrapped_dek FROM memories
                WHERE {" AND ".join(filters)}
                ORDER BY {order_by} LIMIT ${p_idx} OFFSET ${p_idx + 1}
            """
            params.extend([limit, offset])
            rows = await conn.fetch(sql, *params)

        if not rows:
            return []

        from nce.envelope import maybe_decrypt_raw_data

        db = self.mongo_client.memory_archive
        keys = [normalize_payload_ref(r["payload_ref"]) for r in rows]
        # Part II.4: hydrate raw_data and transparently decrypt rows that carry a
        # wrapped DEK; legacy rows (wrapped_dek NULL) read as plaintext.
        raw_by_ref = await fetch_episodes_raw_by_ref(db, keys, decode_bytes=False)

        results = []
        for row in rows:
            key = normalize_payload_ref(row["payload_ref"])
            raw = raw_by_ref.get(key)
            if raw is None:
                continue
            wrapped = row["wrapped_dek"]
            txt = maybe_decrypt_raw_data(raw, bytes(wrapped) if wrapped is not None else None)
            if txt:
                results.append(txt)

        if not as_of and limit == 1 and offset == 0 and results:
            if user_id and session_id:
                redis_key = f"cache:{namespace_id}:{user_id}:{session_id}"
            elif not user_id and not session_id:
                redis_key = f"cache:{namespace_id}:{agent_id}"
            else:
                redis_key = None

            if redis_key:
                await self.redis_client.setex(redis_key, cfg.REDIS_TTL, results[0])

        return results

    # ------------------------------------------------------------------
    # semantic_search
    # ------------------------------------------------------------------

    async def semantic_search(
        self,
        query: str,
        namespace_id: str,
        agent_id: str,
        limit: int = 5,
        offset: int = 0,
        as_of=None,
    ) -> list[dict]:
        """Semantic search with pgvector cosine + FTS hybrid ranking."""
        from nce.semantic_search import semantic_search

        return await semantic_search(
            pg_pool=self.pg_pool,
            mongo_client=self.mongo_client,
            embedding_fn=_embeddings.embed,
            query=query,
            namespace_id=namespace_id,
            agent_id=agent_id,
            limit=limit,
            offset=offset,
            as_of=as_of,
        )
