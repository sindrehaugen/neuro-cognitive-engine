"""
nce/vertical_modules/agreements/graph.py
=========================================
Cognitive-graph upserts and memory storage for the Agreements vertical module.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any
from uuid import UUID

import asyncpg

from nce import embeddings as _embeddings
from nce.db_utils import scoped_pg_session
from nce.entity_resolution.ownership import assert_owner
from nce.events.emit import emit_graph_write

log = logging.getLogger("nce.vertical_modules.agreements.graph")

_AGREEMENTS_ENGINE = "agreements"
_AGENT_ID = "agreements-graph-engine"


async def upsert_agreement_node(
    conn: asyncpg.Connection,
    namespace_id: str | UUID,
    *,
    agreement_id: str | UUID,
    supplier_id: str | None = None,
    customer_id: str | None = None,
    agreements_source_id: str | None = None,
) -> str:
    """Upsert an AGREEMENT node in kg_nodes.

    Label format: Agreement:<agreement_id>
    """
    ns_uuid = UUID(str(namespace_id)) if not isinstance(namespace_id, UUID) else namespace_id
    ag_uuid = UUID(str(agreement_id)) if not isinstance(agreement_id, UUID) else agreement_id

    await assert_owner(conn, ns_uuid, "AGREEMENT", _AGREEMENTS_ENGINE)

    label = f"Agreement:{ag_uuid}"

    # Insert node
    await conn.execute(
        """
        INSERT INTO kg_nodes (label, entity_type, namespace_id, agreements_source_id, change_origin)
        VALUES ($1, 'AGREEMENT', $2::uuid, $3, 'agent')
        ON CONFLICT (label, namespace_id) DO UPDATE
            SET entity_type = EXCLUDED.entity_type,
                agreements_source_id = COALESCE(EXCLUDED.agreements_source_id, kg_nodes.agreements_source_id),
                updated_at = NOW()
        """,
        label,
        str(ns_uuid),
        agreements_source_id,
    )

    await emit_graph_write(
        conn,
        namespace_id=ns_uuid,
        node_type="AGREEMENT",
        op="upserted",
        node_id=label,
    )

    # If supplier_id is provided, upsert VENDOR -> AGREEMENT edge
    if supplier_id:
        vendor_label = f"Vendor:{supplier_id}"
        await upsert_agreement_edge(
            conn,
            ns_uuid,
            subject_label=vendor_label,
            predicate="under",
            object_label=label,
            confidence=1.0,
            agreements_source_id=agreements_source_id,
        )

    # If customer_id is provided, upsert CUSTOMER -> AGREEMENT edge
    if customer_id:
        customer_label = f"Customer:{customer_id}"
        await upsert_agreement_edge(
            conn,
            ns_uuid,
            subject_label=customer_label,
            predicate="under",
            object_label=label,
            confidence=1.0,
            agreements_source_id=agreements_source_id,
        )

    return label


async def upsert_agreement_term_node(
    conn: asyncpg.Connection,
    namespace_id: str | UUID,
    *,
    agreement_id: str | UUID,
    term_type: str,
    value: str,
    confidence: float,
    agreements_source_id: str | None = None,
) -> str:
    """Upsert an AGREEMENT_TERM node in kg_nodes and link it to the AGREEMENT.

    Label format: AgreementTerm:<agreement_id>:<term_type>
    """
    ns_uuid = UUID(str(namespace_id)) if not isinstance(namespace_id, UUID) else namespace_id
    ag_uuid = UUID(str(agreement_id)) if not isinstance(agreement_id, UUID) else agreement_id

    await assert_owner(conn, ns_uuid, "AGREEMENT_TERM", _AGREEMENTS_ENGINE)

    label = f"AgreementTerm:{ag_uuid}:{term_type}"

    await conn.execute(
        """
        INSERT INTO kg_nodes (label, entity_type, namespace_id, agreements_source_id, change_origin)
        VALUES ($1, 'AGREEMENT_TERM', $2::uuid, $3, 'agent')
        ON CONFLICT (label, namespace_id) DO UPDATE
            SET entity_type = EXCLUDED.entity_type,
                agreements_source_id = COALESCE(EXCLUDED.agreements_source_id, kg_nodes.agreements_source_id),
                updated_at = NOW()
        """,
        label,
        str(ns_uuid),
        agreements_source_id,
    )

    await emit_graph_write(
        conn,
        namespace_id=ns_uuid,
        node_type="AGREEMENT_TERM",
        op="upserted",
        node_id=label,
    )

    # Link: AGREEMENT -[has_term]-> AGREEMENT_TERM
    agreement_label = f"Agreement:{ag_uuid}"
    await upsert_agreement_edge(
        conn,
        ns_uuid,
        subject_label=agreement_label,
        predicate="has_term",
        object_label=label,
        confidence=confidence,
        agreements_source_id=agreements_source_id,
    )

    return label


async def upsert_agreement_edge(
    conn: asyncpg.Connection,
    namespace_id: str | UUID,
    *,
    subject_label: str,
    predicate: str,
    object_label: str,
    confidence: float,
    agreements_source_id: str | None = None,
) -> None:
    """Upsert a kg_edges row under the agreements engine."""
    ns_uuid = UUID(str(namespace_id)) if not isinstance(namespace_id, UUID) else namespace_id

    await conn.execute(
        """
        INSERT INTO kg_edges (subject_label, predicate, object_label, confidence, namespace_id, agreements_source_id, change_origin)
        VALUES ($1, $2, $3, $4, $5::uuid, $6, 'agent')
        ON CONFLICT (subject_label, predicate, object_label, namespace_id) DO UPDATE
            SET confidence = EXCLUDED.confidence,
                agreements_source_id = COALESCE(EXCLUDED.agreements_source_id, kg_edges.agreements_source_id),
                updated_at = NOW()
        """,
        subject_label,
        predicate,
        object_label,
        float(confidence),
        str(ns_uuid),
        agreements_source_id,
    )


async def store_agreement_text_memory(
    pg_pool: asyncpg.Pool,
    namespace_id: str | UUID,
    *,
    agreement_id: str | UUID,
    text: str,
    source: str = "agreement_ocr",
    trigger: str = "agent",
) -> dict[str, Any]:
    """Store raw agreement text in memories and log to v3_cognitive_ledger."""
    ns_uuid = UUID(str(namespace_id)) if not isinstance(namespace_id, UUID) else namespace_id
    ag_uuid = UUID(str(agreement_id)) if not isinstance(agreement_id, UUID) else agreement_id

    if not text or not text.strip():
        log.warning("[AGREEMENT-MEM] skipped empty text for agreement_id=%s", agreement_id)
        return {"skipped": "empty text"}

    # 1. Compute embedding batch
    vectors = await _embeddings.embed_batch([text])
    vector: list[float] = vectors[0] if vectors else []
    degraded: bool = _embeddings.degraded_embedding_flag.get()
    vector_str = f"[{','.join(str(v) for v in vector)}]" if vector else None

    # Stable 24-char ObjectId for payload_ref from agreement UUID
    payload_ref = ag_uuid.hex[:24]

    row_metadata: dict[str, Any] = {
        "agreement_id": str(ag_uuid),
        "source": source,
        "trigger": trigger,
    }
    if degraded:
        row_metadata["degraded_embedding"] = True

    memory_id = uuid.uuid4()

    async with scoped_pg_session(pg_pool, ns_uuid) as conn:
        await conn.execute(
            """
            INSERT INTO memories (
                id, namespace_id, agent_id, content_fts,
                payload_ref, memory_type, assertion_type,
                embedding, pii_redacted, metadata
            ) VALUES (
                $1::uuid, $2::uuid, $3, to_tsvector('english', $4),
                $5, $6, $7, $8::vector, $9, $10::jsonb
            )
            """,
            str(memory_id),
            str(ns_uuid),
            _AGENT_ID,
            text[:4000],
            payload_ref,
            "episodic",
            "observation",
            vector_str,
            False,
            json.dumps(row_metadata),
        )

        await conn.execute(
            """
            INSERT INTO v3_cognitive_ledger (
                memory_id, namespace_id, empathic_tensor,
                tlx_scores, vad_scores, model_version
            ) VALUES (
                $1::uuid, $2::uuid, $3::float[], $4::jsonb, $5::jsonb, $6
            )
            """,
            str(memory_id),
            str(ns_uuid),
            [0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            json.dumps(
                {
                    "source": source,
                    "trigger": trigger,
                    "agreement_id": str(ag_uuid),
                    "degraded_embedding": degraded,
                }
            ),
            json.dumps({}),
            "1.0",
        )

    return {"memory_id": str(memory_id), "degraded": degraded}


async def write_agreement_to_graph_and_memories(
    pg_pool: asyncpg.Pool,
    namespace_id: str | UUID,
    *,
    agreement_id: str | UUID,
    source_doc_ref: str,
    extracted_data: dict[str, Any],
) -> None:
    """Write the agreement nodes, terms nodes, edges, and text memories to the DB."""
    ns_uuid = UUID(str(namespace_id)) if not isinstance(namespace_id, UUID) else namespace_id
    ag_uuid = UUID(str(agreement_id)) if not isinstance(agreement_id, UUID) else agreement_id

    # Helper to get value and confidence from extracted fields
    def get_val_conf(field: str) -> tuple[Any, float]:
        f_data = extracted_data.get(field)
        if not f_data:
            return None, 1.0
        if isinstance(f_data, dict):
            val = f_data.get("value")
            conf_raw = f_data.get("extractionConfidence", 100.0)
            if conf_raw > 1.0:
                conf = float(conf_raw) / 100.0
            else:
                conf = float(conf_raw)
            return val, conf
        return f_data, 1.0

    supplier_id, _ = get_val_conf("supplierId")
    customer_id, _ = get_val_conf("customerId")

    async with scoped_pg_session(pg_pool, ns_uuid) as conn:
        # 1. Upsert Agreement Node (creates node + links VENDOR / CUSTOMER edges)
        await upsert_agreement_node(
            conn,
            ns_uuid,
            agreement_id=ag_uuid,
            supplier_id=supplier_id,
            customer_id=customer_id,
            agreements_source_id=str(ag_uuid),
        )

        # 2. Upsert Agreement Term Nodes
        terms = [
            "validFrom",
            "validTo",
            "paymentTermsDays",
            "frameDiscountPct",
            "volumeCommitment",
            "kickbackTiers",
        ]
        for term in terms:
            val, conf = get_val_conf(term)
            if val is not None:
                val_str = json.dumps(val) if isinstance(val, (dict, list)) else str(val)
                await upsert_agreement_term_node(
                    conn,
                    ns_uuid,
                    agreement_id=ag_uuid,
                    term_type=term,
                    value=val_str,
                    confidence=conf,
                    agreements_source_id=str(ag_uuid),
                )

    # 3. Store agreement text in memories
    summary_parts = [f"Agreement ID: {ag_uuid}", f"Source Document: {source_doc_ref}"]
    if supplier_id:
        summary_parts.append(f"Supplier ID / Vendor: {supplier_id}")
    if customer_id:
        summary_parts.append(f"Customer ID: {customer_id}")
    for term in [
        "validFrom",
        "validTo",
        "paymentTermsDays",
        "frameDiscountPct",
        "volumeCommitment",
    ]:
        val, _ = get_val_conf(term)
        if val is not None:
            summary_parts.append(f"{term}: {val}")

    agreement_text = "\n".join(summary_parts)
    await store_agreement_text_memory(
        pg_pool,
        ns_uuid,
        agreement_id=ag_uuid,
        text=agreement_text,
        source="agreement_ocr",
        trigger="agent",
    )


async def do_upsert_agreement(engine: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Core function to write an agreement to the graph and memories."""
    from nce.mcp_args import require_namespace_id

    namespace_id = require_namespace_id(params)

    agreement_id_str = params.get("agreement_id")
    if not agreement_id_str:
        raise ValueError("agreement_id is required")

    source_doc_ref = params.get("source_doc_ref") or "unknown"
    extracted_data = params.get("extracted_data")
    if not extracted_data:
        conf = params.get("extraction_confidence")
        if conf is None:
            conf = 100.0
        extracted_data = {}
        for flat_key, camel_key in [
            ("supplier_id", "supplierId"),
            ("supplierId", "supplierId"),
            ("customer_id", "customerId"),
            ("customerId", "customerId"),
            ("valid_from", "validFrom"),
            ("validFrom", "validFrom"),
            ("valid_to", "validTo"),
            ("validTo", "validTo"),
            ("payment_terms_days", "paymentTermsDays"),
            ("paymentTermsDays", "paymentTermsDays"),
            ("frame_discount_pct", "frameDiscountPct"),
            ("frameDiscountPct", "frameDiscountPct"),
            ("volume_commitment", "volumeCommitment"),
            ("volumeCommitment", "volumeCommitment"),
            ("kickback_tiers", "kickbackTiers"),
            ("kickbackTiers", "kickbackTiers"),
        ]:
            if flat_key in params and params[flat_key] is not None:
                extracted_data[camel_key] = {
                    "value": params[flat_key],
                    "extractionConfidence": conf,
                }

    agreement_id = uuid.UUID(str(agreement_id_str))

    await write_agreement_to_graph_and_memories(
        engine.pg_pool,
        namespace_id,
        agreement_id=agreement_id,
        source_doc_ref=source_doc_ref,
        extracted_data=extracted_data,
    )

    return {
        "status": "ok",
        "agreement_id": str(agreement_id),
    }
