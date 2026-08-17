"""
nce/vertical_modules/vendors/contractors.py
===========================================
Contractor registry operations for Vendors Axis (Batch 099).
"""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID

from nce.db_utils import scoped_pg_session

log = logging.getLogger("nce.vertical_modules.vendors.contractors")

_VENDORS_ENGINE: str = "vendors"
_NODE_TYPE_CONTRACTOR: str = "CONTRACTOR"


async def do_upsert_contractor(
    engine: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Upsert a contractor profile in PostgreSQL table and graph (CONTRACTOR node).

    Params:
        namespace_id (str | UUID): active namespace UUID
        contractor_id (str): contractor label (e.g. 'CONTRACTOR:XYZ') or raw string
        partner_scope_id (str | UUID): external scope ID for the contractor
        profile (dict, optional): profile metadata
        rates (dict, optional): billing rates metadata
        skills (list[str], optional): skills list
        availability (dict, optional): availability metadata
        performance_score (float, optional): performance score
    """
    ns_raw = params.get("namespace_id")
    if not ns_raw:
        raise ValueError("namespace_id is required")
    ns_uuid = UUID(str(ns_raw)) if not isinstance(ns_raw, UUID) else ns_raw

    contractor_id = params.get("contractor_id")
    if not contractor_id:
        raise ValueError("contractor_id is required")
    contractor_id_str = str(contractor_id).strip()

    partner_scope_raw = params.get("partner_scope_id")
    if not partner_scope_raw:
        raise ValueError("partner_scope_id is required")
    partner_scope_uuid = (
        UUID(str(partner_scope_raw))
        if not isinstance(partner_scope_raw, UUID)
        else partner_scope_raw
    )

    profile = params.get("profile") or {}
    rates = params.get("rates") or {}
    skills = params.get("skills") or []
    availability = params.get("availability") or {}
    performance_score = params.get("performance_score")

    # Scoped PG session sets namespace_id GUC
    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        from nce.db_utils import set_external_scope
        from nce.entity_resolution.ownership import assert_owner
        from nce.events.emit import emit_graph_write

        # 1. Set external scope GUC so RLS check on contractor_profiles passes
        await set_external_scope(conn, partner_scope_uuid)

        # 2. Assert owner for CONTRACTOR node
        await assert_owner(conn, ns_uuid, _NODE_TYPE_CONTRACTOR, _VENDORS_ENGINE)

        # 3. Format contractor node label (must start with CONTRACTOR:)
        label = (
            contractor_id_str
            if contractor_id_str.startswith("CONTRACTOR:")
            else f"CONTRACTOR:{contractor_id_str.upper()}"
        )

        # 4. Upsert CONTRACTOR node in kg_nodes
        await conn.execute(
            """
            INSERT INTO kg_nodes (label, entity_type, namespace_id, change_origin)
            VALUES ($1, 'CONTRACTOR', $2::uuid, 'agent')
            ON CONFLICT (label, namespace_id) DO NOTHING
            """,
            label,
            str(ns_uuid),
        )

        # 5. Upsert contractor_profiles table
        await conn.execute(
            """
            INSERT INTO contractor_profiles (
                contractor_id, namespace_id, partner_scope_id, profile, rates, skills, availability, performance_score, updated_at
            ) VALUES ($1, $2::uuid, $3::uuid, $4::jsonb, $5::jsonb, $6::text[], $7::jsonb, $8, NOW())
            ON CONFLICT (contractor_id, namespace_id) DO UPDATE SET
                partner_scope_id = EXCLUDED.partner_scope_id,
                profile = EXCLUDED.profile,
                rates = EXCLUDED.rates,
                skills = EXCLUDED.skills,
                availability = EXCLUDED.availability,
                performance_score = EXCLUDED.performance_score,
                updated_at = NOW()
            """,
            label,
            ns_uuid,
            partner_scope_uuid,
            json.dumps(profile),
            json.dumps(rates),
            skills,
            json.dumps(availability),
            performance_score,
        )

        await emit_graph_write(
            conn,
            namespace_id=ns_uuid,
            node_type=_NODE_TYPE_CONTRACTOR,
            op="upserted",
            node_id=label,
        )

    return {"ok": True, "contractor_id": label, "partner_scope_id": str(partner_scope_uuid)}


async def do_get_contractor(
    engine: Any,
    params: dict[str, Any],
) -> dict[str, Any] | None:
    """Fetch a single contractor profile from PostgreSQL.

    Params:
        namespace_id (str | UUID): active namespace UUID
        contractor_id (str): contractor label or ID
        partner_scope_id (str | UUID, optional): If provided, sets the external scope GUC.
    """
    ns_raw = params.get("namespace_id")
    if not ns_raw:
        raise ValueError("namespace_id is required")
    ns_uuid = UUID(str(ns_raw)) if not isinstance(ns_raw, UUID) else ns_raw

    contractor_id = params.get("contractor_id")
    if not contractor_id:
        raise ValueError("contractor_id is required")
    contractor_id_str = str(contractor_id).strip()

    partner_scope_raw = params.get("partner_scope_id")

    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        from nce.db_utils import set_external_scope

        if partner_scope_raw:
            partner_scope_uuid = (
                UUID(str(partner_scope_raw))
                if not isinstance(partner_scope_raw, UUID)
                else partner_scope_raw
            )
            await set_external_scope(conn, partner_scope_uuid)

        row = await conn.fetchrow(
            """
            SELECT contractor_id, namespace_id, partner_scope_id, profile, rates, skills, availability, performance_score, updated_at
            FROM contractor_profiles
            WHERE contractor_id = $1 AND namespace_id = $2
            """,
            contractor_id_str,
            ns_uuid,
        )

        if not row:
            return None

        # Parse json fields cleanly
        def parse_json(val: Any) -> Any:
            if isinstance(val, str):
                return json.loads(val)
            return val

        return {
            "contractor_id": row["contractor_id"],
            "namespace_id": str(row["namespace_id"]),
            "partner_scope_id": str(row["partner_scope_id"]),
            "profile": parse_json(row["profile"]),
            "rates": parse_json(row["rates"]),
            "skills": row["skills"],
            "availability": parse_json(row["availability"]),
            "performance_score": float(row["performance_score"])
            if row["performance_score"] is not None
            else None,
            "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
        }
