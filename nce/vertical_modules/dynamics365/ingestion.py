"""
nce/vertical_modules/dynamics365/ingestion.py
==============================================
Semantic Track: Dataverse case notes and activities → NCE memory + Empathic Tensor.

``DataverseIngestionWorker`` is called from the RQ task context
(``nce.tasks.process_d365_event``) and handles:

  * Case note / annotation text  → embeddings + memories + v3_cognitive_ledger
  * Activity timeline text       → same pipeline
  * SLA breach events            → WORM event_log entry + memories

Empathic Tensor extraction uses a lightweight heuristic approach (no external
LLM required) so latency stays low inside the background worker.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import asyncpg

from nce.config import cfg

if TYPE_CHECKING:
    import redis.asyncio as aioredis
    from motor.motor_asyncio import AsyncIOMotorClient

log = logging.getLogger("nce.vertical_modules.dynamics365.ingestion")


# Keywords for heuristic Empathic Tensor extraction.
# Configurable via env (NCE_D365_EMPATHIC_URGENCY_KEYWORDS / FRUSTRATION_KEYWORDS).
def _urgency_keywords() -> list[str]:
    return [
        k.strip().lower() for k in cfg.NCE_D365_EMPATHIC_URGENCY_KEYWORDS.split(",") if k.strip()
    ]


def _frustration_keywords() -> list[str]:
    return [
        k.strip().lower()
        for k in cfg.NCE_D365_EMPATHIC_FRUSTRATION_KEYWORDS.split(",")
        if k.strip()
    ]


class DataverseIngestionWorker:
    """
    Semantic-track ingestion worker for Dataverse events.

    Parameters
    ----------
    pg_pool:
        asyncpg connection pool (RLS-scoped connections obtained via ``scoped_pg_session``).
    mongo_client:
        Motor async MongoDB client.
    redis_client:
        Async Redis client.
    namespace_id:
        Tenant namespace UUID.
    """

    def __init__(
        self,
        pg_pool: asyncpg.Pool,
        mongo_client: AsyncIOMotorClient,
        redis_client: aioredis.Redis,
        namespace_id: uuid.UUID,
    ) -> None:
        self._pg_pool = pg_pool
        self._mongo = mongo_client
        self._redis = redis_client
        self._ns = namespace_id

    # ------------------------------------------------------------------
    # Public ingestion methods
    # ------------------------------------------------------------------

    async def _get_pii_config(self, conn: asyncpg.Connection):
        import json

        from nce.models import NamespacePIIConfig

        ns_row = await conn.fetchrow(
            "SELECT metadata FROM namespaces WHERE id = $1::uuid", self._ns
        )
        if ns_row:
            meta = json.loads(ns_row["metadata"])
            if "pii" in meta:
                return NamespacePIIConfig(**meta["pii"])
        return NamespacePIIConfig()

    async def ingest_case_note(
        self,
        incident_id: str,
        annotation_text: str,
        account_name: str = "",
        agent_id: str = "d365-bridge",
    ) -> dict[str, Any]:
        """
        Ingest a Dataverse annotation (case note) into the Semantic Track.

        Pipeline:
        1. Embed text via ``nce.embeddings.embed_batch``
        2. Store raw doc to MongoDB ``d365_annotations`` → get 24-char ObjectId
        3. INSERT into ``memories`` (episodic, observation)
        4. Compute Empathic Tensor and INSERT into ``v3_cognitive_ledger``
        5. Upsert ``kg_edge``: ``Incident:{id} HAS_NOTE Annotation:{mongo_id}``
        """
        if not annotation_text or not annotation_text.strip():
            return {"skipped": "empty annotation text"}

        from nce.db_utils import scoped_pg_session
        from nce.pii import process as pii_process

        async with scoped_pg_session(self._pg_pool, str(self._ns)) as conn:
            pii_config = await self._get_pii_config(conn)

        pii_result = await pii_process(annotation_text, pii_config)
        sanitized_text = pii_result.sanitized_text

        # 1. Embed
        from nce import embeddings as _embeddings

        vectors = await _embeddings.embed_batch([sanitized_text])
        vector = vectors[0] if vectors else []

        # 2. MongoDB store
        mongo_id = await self._store_to_mongo(
            "d365_annotations",
            {
                "incident_id": incident_id,
                "account_name": account_name,
                "annotation_text": sanitized_text,
                "source": "d365_annotation",
                "namespace_id": str(self._ns),
                "ingested_at": datetime.now(timezone.utc),
                "pii_redacted": pii_result.redacted,
            },
        )

        # 3. INSERT into memories
        memory_id = await self._insert_memory(
            content=f"Case note for incident {incident_id}: {sanitized_text[:500]}",
            summary=sanitized_text[:1000],
            payload_ref=mongo_id,
            agent_id=agent_id,
            memory_type="episodic",
            assertion_type="observation",
            vector=vector,
            pii_redacted=pii_result.redacted,
            vault=pii_result.vault_entries,
        )

        # 4. Empathic Tensor
        tensor = self._extract_empathic_tensor(sanitized_text)
        await self._insert_cognitive_ledger(memory_id, tensor, {"incident_id": incident_id})

        # 5. kg_edge: Incident HAS_NOTE Annotation
        await self._upsert_kg_edge(
            subject=f"Incident:{incident_id}",
            predicate="HAS_NOTE",
            object_=f"Annotation:{mongo_id}",
            confidence=0.9,
        )

        log.info("[D365-INGEST] case note ingested incident=%s memory=%s", incident_id, memory_id)
        return {
            "memory_id": str(memory_id),
            "mongo_id": mongo_id,
            "empathic_tensor": tensor,
        }

    async def ingest_activity(
        self,
        activity_type: str,
        subject: str,
        body_text: str,
        related_entity_id: str,
        related_entity_type: str = "incident",
        agent_id: str = "d365-bridge",
    ) -> dict[str, Any]:
        """
        Ingest a Dataverse activity (email, phone call, task) into the Semantic Track.
        """
        text = f"{subject}\n\n{body_text}".strip()
        if not text:
            return {"skipped": "empty activity text"}

        from nce.db_utils import scoped_pg_session
        from nce.pii import process as pii_process

        async with scoped_pg_session(self._pg_pool, str(self._ns)) as conn:
            pii_config = await self._get_pii_config(conn)

        subject_result = await pii_process(subject, pii_config)
        body_result = await pii_process(body_text, pii_config)
        sanitized_subject = subject_result.sanitized_text
        sanitized_body = body_result.sanitized_text
        combined_sanitized = f"{sanitized_subject}\n\n{sanitized_body}".strip()
        combined_vault = subject_result.vault_entries + body_result.vault_entries
        is_redacted = subject_result.redacted or body_result.redacted

        from nce import embeddings as _embeddings

        vectors = await _embeddings.embed_batch([combined_sanitized])
        vector = vectors[0] if vectors else []

        mongo_id = await self._store_to_mongo(
            "d365_annotations",
            {
                "activity_type": activity_type,
                "subject": sanitized_subject,
                "body_text": sanitized_body,
                "related_entity_id": related_entity_id,
                "related_entity_type": related_entity_type,
                "source": f"d365_{activity_type}",
                "namespace_id": str(self._ns),
                "ingested_at": datetime.now(timezone.utc),
                "pii_redacted": is_redacted,
            },
        )

        memory_id = await self._insert_memory(
            content=f"{activity_type.title()}: {sanitized_subject}",
            summary=combined_sanitized[:1000],
            payload_ref=mongo_id,
            agent_id=agent_id,
            memory_type="episodic",
            assertion_type="observation",
            vector=vector,
            pii_redacted=is_redacted,
            vault=combined_vault,
        )

        tensor = self._extract_empathic_tensor(combined_sanitized)
        await self._insert_cognitive_ledger(memory_id, tensor, {"activity_type": activity_type})

        # kg_edge: Activity LINKED_TO related entity
        entity_label = f"{related_entity_type.title()}:{related_entity_id}"
        await self._upsert_kg_edge(
            subject=f"Activity:{activity_type}:{mongo_id[:12]}",
            predicate="LINKED_TO",
            object_=entity_label,
            confidence=0.9,
        )

        log.info(
            "[D365-INGEST] activity ingested type=%s related=%s memory=%s",
            activity_type,
            related_entity_id,
            memory_id,
        )
        return {"memory_id": str(memory_id), "mongo_id": mongo_id, "empathic_tensor": tensor}

    async def ingest_sla_breach(
        self,
        conn: asyncpg.Connection,
        incident_id: str,
        breach_type: str,
        account_name: str,
        agent_id: str = "d365-bridge",
        impacted_services: list[str] | dict[str, float] | None = None,
    ) -> dict[str, Any]:
        """
        Ingest an SLA breach as a WORM event_log entry + memories row.

        **Must be called inside an existing asyncpg transaction.**

        Breach types: ``"first_response"`` | ``"resolution"``
        """
        from nce.event_log import append_event
        from nce.pii import process as pii_process

        pii_config = await self._get_pii_config(conn)
        account_result = await pii_process(account_name, pii_config)
        sanitized_account = account_result.sanitized_text

        summary = (
            f"SLA breach ({breach_type}) for incident {incident_id} "
            f"linked to account '{sanitized_account}'."
        )

        from nce import embeddings as _embeddings

        vectors = await _embeddings.embed_batch([summary])
        vector = vectors[0] if vectors else []

        mongo_id = await self._store_to_mongo(
            "d365_annotations",
            {
                "incident_id": incident_id,
                "breach_type": breach_type,
                "account_name": sanitized_account,
                "source": "d365_sla_breach",
                "namespace_id": str(self._ns),
                "ingested_at": datetime.now(timezone.utc),
                "pii_redacted": account_result.redacted,
            },
        )

        memory_id = await self._insert_memory_with_conn(
            conn=conn,
            content=summary,
            summary=summary,
            payload_ref=mongo_id,
            agent_id=agent_id,
            memory_type="episodic",
            assertion_type="fact",
            vector=vector,
            pii_redacted=account_result.redacted,
            vault=account_result.vault_entries,
        )

        # WORM event_log write — immutable, signed
        await append_event(
            conn=conn,
            namespace_id=self._ns,
            agent_id=agent_id,
            event_type="d365_sla_breach",
            params={
                "incident_id": incident_id,
                "breach_type": breach_type,
                "account_name": sanitized_account,
                "memory_id": str(memory_id),
                "mongo_id": mongo_id,
            },
        )

        # kg_edge: SLABreach BREACHED_BY Account
        await self._upsert_kg_edge_with_conn(
            conn=conn,
            subject=f"SLABreach:{incident_id}:{breach_type}",
            predicate="BREACHED_BY",
            object_=f"Account:{sanitized_account}",
            confidence=1.0,
        )

        log.info(
            "[D365-INGEST] SLA breach ingested incident=%s type=%s memory=%s",
            incident_id,
            breach_type,
            memory_id,
        )

        tickets = []
        if cfg.NCE_NETBOX_URL and cfg.NCE_NETBOX_TOKEN:
            degradations: dict[str, float] = {}
            if isinstance(impacted_services, dict):
                degradations = {str(k): float(v) for k, v in impacted_services.items()}
            elif isinstance(impacted_services, list):
                degradations = {str(srv): 1.0 for srv in impacted_services}

            if degradations:
                from nce.db_utils import scoped_pg_session
                from nce.vertical_modules.netbox.circuits import (
                    NetBoxCircuitEscalator,
                    NetBoxCircuitsClient,
                )

                async with scoped_pg_session(self._pg_pool, str(self._ns)) as esc_conn:
                    netbox_client = NetBoxCircuitsClient(cfg.NCE_NETBOX_URL, cfg.NCE_NETBOX_TOKEN)
                    escalator = NetBoxCircuitEscalator(netbox_client)
                    tickets = await escalator.evaluate_and_escalate(
                        conn=esc_conn,
                        namespace_id=self._ns,
                        telemetry_degradations=degradations,
                    )
                    for ticket in tickets:
                        await append_event(
                            conn=esc_conn,
                            namespace_id=self._ns,
                            agent_id=agent_id,
                            event_type="circuit_escalation_generated",
                            params=ticket,
                        )

        return {"memory_id": str(memory_id), "mongo_id": mongo_id, "tickets": tickets}

    # ------------------------------------------------------------------
    # Empathic Tensor extraction
    # ------------------------------------------------------------------

    def _extract_empathic_tensor(self, text: str) -> list[float]:
        """
        Derive a 6-element Empathic Tensor from raw text using keyword heuristics.

        Layout matches ``v3_cognitive_ledger.empathic_tensor``:
            [0] Valence   — positive/negative sentiment  (-10 → +10 mapped to 0 → 10)
            [1] Arousal   — calm vs. hyper-alert
            [2] Dominance — assertiveness
            [3] Urgency   — time pressure / escalation
            [4] Magnitude — absolute sentiment strength
            [5] Frustration — the StressTracker burnout-trigger index

        No external LLM needed — keeps worker latency low.
        """
        if not text:
            return [5.0, 0.0, 5.0, 0.0, 0.0, 0.0]

        lower = text.lower()
        words = lower.split()
        word_count = max(len(words), 1)

        # --- Valence: basic positive/negative keyword scoring ---
        _pos_kw = ["resolved", "fixed", "thank", "great", "excellent", "working", "success"]
        _neg_kw = [
            "error",
            "fail",
            "broken",
            "down",
            "outage",
            "critical",
            "blocked",
            "urgent",
            "escalate",
            "breach",
            "unacceptable",
            "disappointed",
            "terrible",
        ]
        pos_score = sum(1 for w in _pos_kw if w in lower)
        neg_score = sum(1 for w in _neg_kw if w in lower)
        valence = min(10.0, max(0.0, 5.0 + (pos_score - neg_score) * 1.5))

        # --- Urgency: density of urgency keywords ---
        urgency_count = sum(1 for kw in _urgency_keywords() if kw in lower)
        urgency = min(10.0, urgency_count * 2.5)

        # --- Frustration: keyword density + negative valence contribution ---
        frustration_count = sum(1 for kw in _frustration_keywords() if kw in lower)
        neg_valence_weight = max(0.0, (5.0 - valence))  # higher when valence is negative
        frustration = min(10.0, frustration_count * 1.5 + neg_valence_weight * 0.8)

        # --- Arousal: urgency proxy (high urgency = high arousal) ---
        arousal = min(10.0, urgency * 0.8 + neg_score * 0.5)

        # --- Dominance: longer texts / imperative phrasing = more assertive ---
        dominance = min(10.0, word_count / 50.0 * 3.0)

        # --- Magnitude: absolute sentiment strength ---
        magnitude = abs(valence - 5.0) * 2.0

        return [
            round(valence, 3),
            round(arousal, 3),
            round(dominance, 3),
            round(urgency, 3),
            round(magnitude, 3),
            round(frustration, 3),
        ]

    # ------------------------------------------------------------------
    # Private DB helpers
    # ------------------------------------------------------------------

    async def _store_to_mongo(self, collection_name: str, document: dict[str, Any]) -> str:
        """Store document in MongoDB and return 24-char hex ObjectId string."""
        db = self._mongo.memory_archive
        collection = getattr(db, collection_name)
        result = await collection.insert_one(document)
        return str(result.inserted_id)

    async def _insert_memory(
        self,
        content: str,
        summary: str,
        payload_ref: str,
        agent_id: str,
        memory_type: str,
        assertion_type: str,
        vector: list[float],
        pii_redacted: bool = False,
        vault: list[dict[str, Any]] | None = None,
        origin_event_id: uuid.UUID | None = None,
    ) -> uuid.UUID:
        """Insert a row into ``memories`` using a scoped RLS connection."""
        from nce.db_utils import scoped_pg_session

        async with scoped_pg_session(self._pg_pool, str(self._ns)) as conn:
            return await self._insert_memory_with_conn(
                conn=conn,
                content=content,
                summary=summary,
                payload_ref=payload_ref,
                agent_id=agent_id,
                memory_type=memory_type,
                assertion_type=assertion_type,
                vector=vector,
                pii_redacted=pii_redacted,
                vault=vault,
                origin_event_id=origin_event_id,
            )

    async def _insert_memory_with_conn(
        self,
        conn: asyncpg.Connection,
        content: str,
        summary: str,
        payload_ref: str,
        agent_id: str,
        memory_type: str,
        assertion_type: str,
        vector: list[float],
        pii_redacted: bool = False,
        vault: list[dict[str, Any]] | None = None,
        origin_event_id: uuid.UUID | None = None,
    ) -> uuid.UUID:
        """Insert a row into ``memories`` on an already-open connection."""
        memory_id = uuid.uuid4()
        vector_str = f"[{','.join(str(v) for v in vector)}]" if vector else None
        await conn.execute(
            """
            INSERT INTO memories (
                id, namespace_id, agent_id, content_fts,
                payload_ref, memory_type, assertion_type, embedding, pii_redacted,
                change_origin, origin_event_id
            ) VALUES (
                $1::uuid, $2::uuid, $3, to_tsvector('english', $4),
                $5, $6, $7, $8::vector, $9,
                'webhook', $10::uuid
            )
            """,
            str(memory_id),
            str(self._ns),
            agent_id,
            summary[:4000],
            payload_ref,
            memory_type,
            assertion_type,
            vector_str,
            pii_redacted,
            str(origin_event_id) if origin_event_id is not None else None,
        )
        if vault:
            await conn.executemany(
                """
                INSERT INTO pii_redactions (namespace_id, memory_id, token, encrypted_value, entity_type)
                VALUES ($1, $2, $3, $4, $5)
                """,
                [
                    (
                        str(self._ns),
                        memory_id,
                        v["token"],
                        v["encrypted_value"],
                        v["entity_type"],
                    )
                    for v in vault
                ],
            )
        return memory_id

    async def _insert_cognitive_ledger(
        self,
        memory_id: uuid.UUID,
        tensor: list[float],
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Insert an Empathic Tensor row into ``v3_cognitive_ledger``."""
        import json as _json

        from nce.db_utils import scoped_pg_session

        async with scoped_pg_session(self._pg_pool, str(self._ns)) as conn:
            await conn.execute(
                """
                INSERT INTO v3_cognitive_ledger (
                    memory_id, namespace_id, empathic_tensor, tlx_scores, vad_scores, model_version
                ) VALUES (
                    $1::uuid, $2::uuid, $3::float[], $4::jsonb, $5::jsonb, $6
                )
                """,
                str(memory_id),
                str(self._ns),
                tensor,
                _json.dumps(metadata or {}),
                _json.dumps({"valence": tensor[0], "arousal": tensor[1], "dominance": tensor[2]}),
                "1.0",
            )

    async def _upsert_kg_edge(
        self,
        subject: str,
        predicate: str,
        object_: str,
        confidence: float,
    ) -> None:
        """Upsert a single kg_edge using a scoped RLS connection."""
        from nce.db_utils import scoped_pg_session

        async with scoped_pg_session(self._pg_pool, str(self._ns)) as conn:
            await self._upsert_kg_edge_with_conn(conn, subject, predicate, object_, confidence)

    async def _upsert_kg_edge_with_conn(
        self,
        conn: asyncpg.Connection,
        subject: str,
        predicate: str,
        object_: str,
        confidence: float,
    ) -> None:
        """Upsert a single kg_edge on an already-open connection.

        Authority-precedence: a 'sync'-authored edge's change_origin is never
        downgraded to 'webhook' on conflict.

        OBS-1 note: the CASE intentionally guards only 'sync' (not 'webhook').
        A webhook event may legitimately overwrite a prior webhook — same-tier
        writes use last-write-wins semantics.  Only 'sync' (the highest-authority
        bulk-reconciliation origin) is preserved unconditionally.
        """
        await conn.execute(
            """
            INSERT INTO kg_edges (subject_label, predicate, object_label, confidence, namespace_id,
                                  change_origin)
            VALUES ($1, $2, $3, $4, $5::uuid, 'webhook')
            ON CONFLICT (subject_label, predicate, object_label, namespace_id) DO UPDATE
                SET confidence = EXCLUDED.confidence,
                    change_origin = CASE
                        WHEN kg_edges.change_origin = 'sync' THEN 'sync'
                        ELSE EXCLUDED.change_origin
                    END,
                    updated_at = NOW()
            """,
            subject,
            predicate,
            object_,
            confidence,
            str(self._ns),
        )
