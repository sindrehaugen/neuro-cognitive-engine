"""
nce/vertical_modules/vendors/partner_view.py
============================================
Partner access and redacted view operation (Batch 100).
"""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID

from bson import ObjectId

from nce.db_utils import scoped_mongo_session, scoped_pg_session, set_external_scope
from nce.redaction.redactor import project

log = logging.getLogger("nce.vertical_modules.vendors.partner_view")


async def do_partner_view(
    engine: Any,
    params: dict[str, Any],
) -> dict[str, Any] | None:
    """Fetch and return a redacted partner-safe view of a target node (VENDOR, CONTRACTOR, etc.).

    Params:
        namespace_id (str | UUID): active namespace UUID
        node_id (str): label, ID or external identifier of the target node
        partner_scope_id (str | UUID, optional): sets the external scope GUC
    """
    ns_raw = params.get("namespace_id")
    if not ns_raw:
        raise ValueError("namespace_id is required")
    ns_uuid = UUID(str(ns_raw)) if not isinstance(ns_raw, UUID) else ns_raw

    node_id = params.get("node_id") or params.get("contractor_id") or params.get("vendor_id")
    if not node_id:
        raise ValueError("node_id is required")
    node_id_str = str(node_id).strip()

    partner_scope_raw = params.get("partner_scope_id")
    partner_scope_uuid = None
    if partner_scope_raw:
        partner_scope_uuid = (
            UUID(str(partner_scope_raw))
            if not isinstance(partner_scope_raw, UUID)
            else partner_scope_raw
        )

    row = None
    contractor_row = None

    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        # If partner_scope_id is provided, set the external scope GUC
        if partner_scope_uuid:
            await set_external_scope(conn, partner_scope_uuid)

        # Query the node from kg_nodes
        row = await conn.fetchrow(
            """
            SELECT id, label, entity_type, payload_ref, namespace_id
            FROM kg_nodes
            WHERE (id::text = $1 OR label = $1)
              AND namespace_id = $2
            """,
            node_id_str,
            ns_uuid,
        )

        if not row:
            # Maybe it's a contractor queried directly by ID
            contractor_row = await conn.fetchrow(
                """
                SELECT contractor_id, namespace_id, partner_scope_id, profile, skills, availability, performance_score
                FROM contractor_profiles
                WHERE contractor_id = $1 AND namespace_id = $2
                """,
                node_id_str,
                ns_uuid,
            )

    if not row and not contractor_row:
        return None

    node_dict: dict[str, Any] = {}

    if row:
        node_dict = {
            "id": str(row["id"]),
            "label": row["label"],
            "node_type": row["entity_type"],
            "namespace_id": str(row["namespace_id"]),
        }

        # If it is a CONTRACTOR, fetch its profile fields from PostgreSQL table
        if row["entity_type"] == "CONTRACTOR":
            async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
                if partner_scope_uuid:
                    await set_external_scope(conn, partner_scope_uuid)
                contractor_row = await conn.fetchrow(
                    """
                    SELECT profile, skills, availability, performance_score
                    FROM contractor_profiles
                    WHERE contractor_id = $1 AND namespace_id = $2
                    """,
                    row["label"],
                    ns_uuid,
                )
            if not contractor_row:
                return None

        # Retrieve MongoDB document if payload_ref exists
        mongo_doc: dict[str, Any] = {}
        if row["payload_ref"] and engine.mongo_client:
            try:
                async with scoped_mongo_session(engine.mongo_client, ns_uuid) as db:
                    doc = await db.episodes.find_one({"_id": ObjectId(row["payload_ref"])})
                    if doc:
                        mongo_doc = doc
            except Exception as e:
                log.warning("Failed to fetch MongoDB payload for ref %s: %s", row["payload_ref"], e)

        # Merge MongoDB fields
        if "merged_fields" in mongo_doc:
            node_dict.update(mongo_doc["merged_fields"])
        else:
            for k, v in mongo_doc.items():
                if k not in ("_id", "feed_fields", "admin_fields", "merged_fields"):
                    node_dict[k] = v

    if contractor_row:
        # If we got contractor_row (either directly or via the CONTRACTOR entity type)
        def parse_json(val: Any) -> Any:
            if isinstance(val, str):
                return json.loads(val)
            return val

        # If we didn't have row (direct contractor_profiles fetch)
        if not node_dict:
            node_dict = {
                "id": contractor_row["contractor_id"],
                "label": contractor_row["contractor_id"],
                "node_type": "CONTRACTOR",
                "namespace_id": str(contractor_row["namespace_id"]),
            }

        profile_data = parse_json(contractor_row["profile"])
        if isinstance(profile_data, dict):
            node_dict.update(profile_data)

        # Merge skills, availability, performance_score
        node_dict["skills"] = contractor_row["skills"]
        node_dict["availability"] = parse_json(contractor_row["availability"])
        if contractor_row["performance_score"] is not None:
            node_dict["performance_score"] = float(contractor_row["performance_score"])

    # Redact using the C8 "partner" allow-list
    return project(node_dict, "partner")
