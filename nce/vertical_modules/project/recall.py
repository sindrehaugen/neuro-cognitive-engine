"""
nce/vertical_modules/project/recall.py
======================================
Domain core: P5 AI cognitive recall and HR lead suggestions.

Features:
1. do_recall_similar_projects(engine, params) -> list[dict]
   Retrieve similar past slipped projects from memories joined with the cognitive ledger,
   ranked by embedding similarity.
2. do_suggest_pl(engine, params) -> dict
   Calls HR engine's tool via A2A with graceful degradation.
3. do_record_project_outcome(engine, params) -> dict
   Idempotent writer for project outcome signals to memories and v3_cognitive_ledger.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import TYPE_CHECKING, Any
from uuid import UUID

from nce.config import cfg
from nce.db_utils import scoped_pg_session
from nce.embeddings import embed

if TYPE_CHECKING:
    from nce.orchestrator import NCEEngine

log = logging.getLogger("nce.vertical_modules.project.recall")


async def do_recall_similar_projects(
    engine: NCEEngine,
    params: dict[str, Any],
) -> list[dict[str, Any]]:
    """Recall similar past projects that slipped.

    Parameters
    ----------
    engine:
        NCEEngine instance.
    params:
        {
            "namespace_id": str | UUID,   # required
            "project_id":   str,          # required — e.g. "PROJECT:XYZ"
            "description":  str,          # optional query description override
            "query":        str,          # optional keyword query for content_fts filtering
            "top_k":        int,          # optional limit (default to NCE_PROJECT_RECALL_TOP_K or 5)
        }
    """
    raw_ns = params.get("namespace_id")
    if not raw_ns:
        raise ValueError("do_recall_similar_projects: 'namespace_id' is required")
    ns_uuid = UUID(str(raw_ns)) if not isinstance(raw_ns, UUID) else raw_ns

    project_id = str(params.get("project_id") or "").strip()
    if not project_id:
        raise ValueError("do_recall_similar_projects: 'project_id' is required")

    top_k_val = params.get("top_k")
    if top_k_val is None:
        top_k_val = getattr(cfg, "NCE_PROJECT_RECALL_TOP_K", 5)
    if top_k_val is None:
        top_k_val = 5
    top_k = max(1, int(top_k_val))

    bom_descriptions: list[str] = []
    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        # Query contains edges to fetch child BOM lines of this project
        bom_rows = await conn.fetch(
            """
            SELECT object_label
            FROM kg_edges
            WHERE subject_label = $1
              AND predicate = 'contains'
              AND namespace_id = $2::uuid
            ORDER BY object_label
            """,
            project_id,
            str(ns_uuid),
        )
        bom_descriptions = [r["object_label"] for r in bom_rows]

    query_text = params.get("description")
    if not query_text:
        if bom_descriptions:
            query_text = f"Project {project_id} containing BOM lines: " + ", ".join(
                bom_descriptions
            )
        else:
            query_text = f"Project {project_id}"

    # Embed the query text
    vector = await embed(query_text)
    vector_json = json.dumps(vector)

    query_kw = params.get("query")

    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        if query_kw:
            rows = await conn.fetch(
                """
                SELECT m.id,
                       m.name,
                       m.metadata,
                       cl.tlx_scores,
                       cl.vad_scores,
                       1 - (m.embedding <=> $1::vector) AS similarity
                FROM memories m
                JOIN v3_cognitive_ledger cl ON m.id = cl.memory_id
                WHERE m.namespace_id = $2::uuid
                  AND m.node_type = 'PROJECT'
                  AND m.embedding IS NOT NULL
                  AND m.valid_to IS NULL
                  AND m.content_fts @@ websearch_to_tsquery('english', $3)
                ORDER BY m.embedding <=> $1::vector ASC
                LIMIT $4
                """,
                vector_json,
                str(ns_uuid),
                query_kw,
                top_k,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT m.id,
                       m.name,
                       m.metadata,
                       cl.tlx_scores,
                       cl.vad_scores,
                       1 - (m.embedding <=> $1::vector) AS similarity
                FROM memories m
                JOIN v3_cognitive_ledger cl ON m.id = cl.memory_id
                WHERE m.namespace_id = $2::uuid
                  AND m.node_type = 'PROJECT'
                  AND m.embedding IS NOT NULL
                  AND m.valid_to IS NULL
                ORDER BY m.embedding <=> $1::vector ASC
                LIMIT $3
                """,
                vector_json,
                str(ns_uuid),
                top_k,
            )

    results: list[dict[str, Any]] = []
    for r in rows:
        meta = {}
        raw_meta = r["metadata"]
        if isinstance(raw_meta, str):
            try:
                meta = json.loads(raw_meta)
            except Exception:
                pass
        elif isinstance(raw_meta, dict):
            meta = raw_meta

        slip_reason = meta.get("slip_reason") or ""
        if not slip_reason:
            tlx = {}
            raw_tlx = r["tlx_scores"]
            if isinstance(raw_tlx, str):
                try:
                    tlx = json.loads(raw_tlx)
                except Exception:
                    pass
            elif isinstance(raw_tlx, dict):
                tlx = raw_tlx
            slip_reason = tlx.get("slip_reason") or ""

        results.append(
            {
                "project": r["name"],
                "slip_reason": slip_reason,
                "similarity": float(r["similarity"] if r["similarity"] is not None else 0.0),
            }
        )

    return results


async def do_suggest_pl(
    engine: NCEEngine,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Suggest a project lead using HR's tools via the A2A client, degrading gracefully.

    Parameters
    ----------
    engine:
        NCEEngine instance.
    params:
        {
            "namespace_id": str | UUID,   # required
            "project_id":   str,          # required
            "a2a_client":   Any,          # optional A2A client
        }
    """
    raw_ns = params.get("namespace_id")
    if not raw_ns:
        return {"ok": False, "error": "do_suggest_pl: 'namespace_id' is required"}
    try:
        ns_uuid = UUID(str(raw_ns)) if not isinstance(raw_ns, UUID) else raw_ns
    except ValueError as exc:
        return {"ok": False, "error": f"do_suggest_pl: invalid namespace_id: {exc}"}

    project_id = str(params.get("project_id") or "").strip()
    if not project_id:
        return {"ok": False, "error": "do_suggest_pl: 'project_id' is required"}

    a2a_client = params.get("a2a_client")
    if not a2a_client:
        log.info("do_suggest_pl: a2a_client not provided — HR unavailable")
        return {"ok": False, "error": "HR unavailable"}

    try:
        result = await a2a_client.call_tool(
            "hr.suggest_pl",
            {
                "namespace_id": str(ns_uuid),
                "project_id": project_id,
            },
        )
        return {"ok": True, "suggestion": result}
    except Exception as exc:
        log.warning("do_suggest_pl A2A call failed: %s", exc)
        return {"ok": False, "error": f"HR unavailable: {exc}"}


async def do_record_project_outcome(
    engine: NCEEngine,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Record project outcome signals to memories and v3_cognitive_ledger idempotently.

    Parameters
    ----------
    engine:
        NCEEngine instance.
    params:
        {
            "namespace_id": str | UUID,   # required
            "project_id":   str,          # required — e.g. "PROJECT:XYZ"
            "description":  str,          # required — project description/BOM representation
            "slip_reason":  str,          # required — explanation of the slippage
            "margin_drift": float,        # optional
            "gate_dwell_time": int,       # optional
        }
    """
    raw_ns = params.get("namespace_id")
    if not raw_ns:
        return {"ok": False, "error": "do_record_project_outcome: 'namespace_id' is required"}
    try:
        ns_uuid = UUID(str(raw_ns)) if not isinstance(raw_ns, UUID) else raw_ns
    except ValueError as exc:
        return {"ok": False, "error": f"do_record_project_outcome: invalid namespace_id: {exc}"}

    project_id = str(params.get("project_id") or "").strip()
    if not project_id:
        return {"ok": False, "error": "do_record_project_outcome: 'project_id' is required"}

    description = str(params.get("description") or "").strip()
    if not description:
        return {"ok": False, "error": "do_record_project_outcome: 'description' is required"}

    slip_reason = str(params.get("slip_reason") or "").strip()
    if not slip_reason:
        return {"ok": False, "error": "do_record_project_outcome: 'slip_reason' is required"}

    margin_drift = params.get("margin_drift")
    gate_dwell_time = params.get("gate_dwell_time")

    # Embed the project description
    vector = await embed(description)
    vector_json = json.dumps(vector)

    memory_id = uuid.uuid4()
    payload_ref = memory_id.hex[:24]

    row_metadata: dict[str, Any] = {
        "project_id": project_id,
        "slip_reason": slip_reason,
        "description": description,
    }
    if margin_drift is not None:
        row_metadata["margin_drift"] = float(margin_drift)
    if gate_dwell_time is not None:
        row_metadata["gate_dwell_time"] = int(gate_dwell_time)

    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        # Enforce idempotency: delete pre-existing memories matching name/namespace_id
        # First select the memory IDs to delete from both tables (since v3_cognitive_ledger has no ON DELETE CASCADE FK)
        old_memory_ids = await conn.fetch(
            """
            SELECT id FROM memories
            WHERE namespace_id = $1::uuid
              AND name = $2
              AND node_type = 'PROJECT'
            """,
            str(ns_uuid),
            project_id,
        )
        if old_memory_ids:
            old_ids = [r["id"] for r in old_memory_ids]
            await conn.execute(
                "DELETE FROM v3_cognitive_ledger WHERE memory_id = ANY($1::uuid[])",
                old_ids,
            )
            await conn.execute(
                "DELETE FROM memories WHERE id = ANY($1::uuid[])",
                old_ids,
            )

        # Insert new memories row
        await conn.execute(
            """
            INSERT INTO memories (
                id, namespace_id, agent_id, content_fts,
                payload_ref, memory_type, assertion_type,
                embedding, pii_redacted, metadata, node_type, name
            ) VALUES (
                $1::uuid, $2::uuid, $3, to_tsvector('english', $4),
                $5, $6, $7, $8::vector, $9, $10::jsonb, $11, $12
            )
            """,
            str(memory_id),
            str(ns_uuid),
            "project.recall",
            description[:4000],
            payload_ref,
            "episodic",
            "observation",
            vector_json,
            False,
            json.dumps(row_metadata),
            "PROJECT",
            project_id,
        )

        # Insert into v3_cognitive_ledger
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
            json.dumps(row_metadata),
            json.dumps({}),
            "1.0",
        )

    return {"ok": True, "memory_id": str(memory_id)}
