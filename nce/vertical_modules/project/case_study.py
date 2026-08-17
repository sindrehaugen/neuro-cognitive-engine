"""
nce/vertical_modules/project/case_study.py
===========================================
Domain core: generates a CASE_STUDY node and PROJECT -[generates]-> CASE_STUDY edge when terminal.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any
from uuid import UUID

from nce.db_utils import scoped_pg_session
from nce.entity_resolution.ownership import assert_owner
from nce.events.emit import emit_graph_write

if TYPE_CHECKING:
    from nce.orchestrator import NCEEngine

log = logging.getLogger("nce.vertical_modules.project.case_study")

_PROJECT_ENGINE: str = "project"
_NODE_TYPE_PROJECT: str = "PROJECT_PROJECT"
_NODE_TYPE_CASE_STUDY: str = "PROJECT_CASE_STUDY"
_PREDICATE_GENERATES: str = "generates"
_PREDICATE_IN_PHASE: str = "in_phase"


async def do_generate_case_study_edge(
    engine: NCEEngine,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Generate a CASE_STUDY seed node and the generates edge when a project is G6.

    Parameters
    ----------
    engine:
        NCEEngine instance.
    params:
        {
            "namespace_id": str | UUID,   # required
            "project_id":   str,          # required — e.g. "PROJECT:Q123"
            "confidence":   float,        # optional (default 1.0)
        }
    """
    raw_ns = params.get("namespace_id")
    if not raw_ns:
        raise ValueError("do_generate_case_study_edge: 'namespace_id' is required")
    ns_uuid = UUID(str(raw_ns)) if not isinstance(raw_ns, UUID) else raw_ns

    project_id = str(params.get("project_id") or "").strip()
    if not project_id:
        raise ValueError("do_generate_case_study_edge: 'project_id' is required")

    confidence_val = params.get("confidence")
    confidence = float(confidence_val) if confidence_val is not None else 1.0

    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        # Check if project exists
        project_exists = await conn.fetchval(
            """
            SELECT 1 FROM kg_nodes
            WHERE label = $1
              AND entity_type = $2
              AND namespace_id = $3::uuid
            """,
            project_id,
            _NODE_TYPE_PROJECT,
            str(ns_uuid),
        )
        if not project_exists:
            return {"ok": False, "error": f"Project '{project_id}' not found"}

        # Read current phase of the project
        row = await conn.fetchrow(
            """
            SELECT object_label
            FROM   kg_edges
            WHERE  subject_label = $1
              AND  predicate      = $2
              AND  namespace_id   = $3::uuid
            LIMIT 1
            """,
            project_id,
            _PREDICATE_IN_PHASE,
            str(ns_uuid),
        )
        if not row:
            return {"ok": False, "error": f"Project '{project_id}' is not in any phase"}

        gate_label = row["object_label"]
        parts = gate_label.split(":")
        current_phase = parts[-1] if len(parts) >= 3 else ""

        # Only seed when project is in terminal G6 handover state
        if current_phase != "G6":
            return {
                "ok": False,
                "reason": "in_flight",
                "current_phase": current_phase,
            }

        # Parse quote ID to generate case study label
        prefix = "PROJECT:"
        if project_id.upper().startswith(prefix):
            quote_id = project_id[len(prefix) :]
        else:
            quote_id = project_id
        case_study_label = f"CASE_STUDY:{quote_id.upper()}"

        # Assert ownership of CASE_STUDY node type before writing it
        await assert_owner(conn, ns_uuid, _NODE_TYPE_CASE_STUDY, _PROJECT_ENGINE)

        # Upsert CASE_STUDY node
        await conn.execute(
            """
            INSERT INTO kg_nodes (label, entity_type, namespace_id)
            VALUES ($1, $2, $3::uuid)
            ON CONFLICT (label, namespace_id) DO UPDATE
                SET entity_type = EXCLUDED.entity_type,
                    updated_at  = NOW()
            """,
            case_study_label,
            _NODE_TYPE_CASE_STUDY,
            str(ns_uuid),
        )
        await emit_graph_write(
            conn,
            namespace_id=ns_uuid,
            node_type=_NODE_TYPE_CASE_STUDY,
            op="upserted",
            node_id=case_study_label,
        )

        # Upsert PROJECT -[generates]-> CASE_STUDY edge
        await conn.execute(
            """
            INSERT INTO kg_edges
                (subject_label, predicate, object_label, confidence, namespace_id)
            VALUES ($1, $2, $3, $4, $5::uuid)
            ON CONFLICT (subject_label, predicate, object_label, namespace_id) DO UPDATE
                SET confidence = EXCLUDED.confidence,
                    updated_at = NOW()
            """,
            project_id,
            _PREDICATE_GENERATES,
            case_study_label,
            confidence,
            str(ns_uuid),
        )

        return {
            "ok": True,
            "case_study_label": case_study_label,
            "edge": f"{project_id} -[{_PREDICATE_GENERATES}]-> {case_study_label}",
        }
