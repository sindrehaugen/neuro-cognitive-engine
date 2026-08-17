"""
nce/vertical_modules/vendors/registry.py
========================================
Vendor registry operations for Vendors Axis (Batch 094).
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from bson import ObjectId

from nce.db_utils import scoped_mongo_session, scoped_pg_session
from nce.entity_resolution.ownership import assert_owner
from nce.entity_resolution.resolver import resolve
from nce.events.emit import emit_graph_write

log = logging.getLogger("nce.vertical_modules.vendors.registry")

_VENDORS_ENGINE: str = "vendors"
_NODE_TYPE_VENDOR: str = "VENDOR"


async def do_upsert_vendor(
    engine: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Upsert a single VENDOR node and merge its fields in MongoDB.

    Keyed on orgnr, uses C1 entity resolution primitive to check for duplicates,
    merging feed and admin fields.

    Params:
        namespace_id (str | UUID): active namespace UUID
        orgnr (str): organization number
        name (str): vendor name
        feed_fields (dict, optional): feed fields to merge
        admin_fields (dict, optional): admin-entered fields to merge
        source_id (str, optional): vendors_source_id for retirement tracking
        source_type (str, optional): 'feed' or 'admin' (default 'feed')
    """
    ns_raw = params.get("namespace_id")
    if not ns_raw:
        raise ValueError("namespace_id is required")
    ns_uuid = UUID(str(ns_raw)) if not isinstance(ns_raw, UUID) else ns_raw

    orgnr = params.get("orgnr")
    if not orgnr:
        raise ValueError("orgnr is required")
    orgnr_str = str(orgnr).strip()

    name = params.get("name")
    if not name:
        raise ValueError("name is required")
    name_str = str(name).strip()

    source_id = params.get("source_id")
    source_type = params.get("source_type", "feed")

    # 1. Scoped PG session to assert ownership and run C1 resolve
    existing_payload_ref: str | None = None
    existing_vendors_source_id: str | None = None

    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        await assert_owner(conn, ns_uuid, _NODE_TYPE_VENDOR, _VENDORS_ENGINE)

        # Query C1 resolve over orgnr/name keys
        matches = await resolve(
            conn,
            namespace_id=ns_uuid,
            candidate={"orgnr": orgnr_str, "name": name_str},
            keys=["orgnr", "name"],
            node_type=_NODE_TYPE_VENDOR,
        )

        # Average similarity of keys against n.label must be high enough.
        # Since orgnr is unique, if top match matches orgnr, we reuse it.
        if matches:
            top_match = matches[0]
            if top_match.score >= 0.2:
                # Retrieve existing node details to confirm orgnr matches
                row = await conn.fetchrow(
                    "SELECT id, label, payload_ref, vendors_source_id FROM kg_nodes WHERE id = $1",
                    top_match.node_id,
                )
                if row:
                    lbl = row["label"]
                    matched_orgnr = lbl.split(":")[-1]
                    if matched_orgnr == orgnr_str:
                        existing_payload_ref = row["payload_ref"]
                        existing_vendors_source_id = row["vendors_source_id"]

    # 2. Scoped Mongo session (outside PG transaction to avoid blocking)
    new_feed = params.get("feed_fields") or {}
    new_admin = params.get("admin_fields") or {}

    # Extract flat fields (if passed directly in params)
    other_fields = {
        k: v
        for k, v in params.items()
        if k
        not in (
            "namespace_id",
            "orgnr",
            "name",
            "source_id",
            "source_type",
            "feed_fields",
            "admin_fields",
        )
    }
    if other_fields:
        if source_type == "admin":
            new_admin = {**other_fields, **new_admin}
        else:
            new_feed = {**other_fields, **new_feed}

    existing_doc: dict[str, Any] = {}
    if existing_payload_ref and engine.mongo_client:
        try:
            async with scoped_mongo_session(engine.mongo_client, ns_uuid) as db:
                doc = await db.episodes.find_one({"_id": ObjectId(existing_payload_ref)})
                if doc:
                    existing_doc = doc
        except Exception as e:
            log.warning("Failed to fetch MongoDB payload for ref %s: %s", existing_payload_ref, e)

    # Idempotent merge: start with existing, override with new
    merged_feed = {**(existing_doc.get("feed_fields") or {}), **new_feed}
    merged_admin = {**(existing_doc.get("admin_fields") or {}), **new_admin}

    # admin wins over feed
    merged_all = {**merged_feed, **merged_admin}

    # Always keep name and orgnr updated at root
    mongo_doc = {
        "orgnr": orgnr_str,
        "name": name_str,
        "feed_fields": merged_feed,
        "admin_fields": merged_admin,
        "merged_fields": merged_all,
    }

    # Save to MongoDB
    payload_ref = existing_payload_ref
    if engine.mongo_client:
        try:
            async with scoped_mongo_session(engine.mongo_client, ns_uuid) as db:
                if existing_payload_ref:
                    await db.episodes.replace_one(
                        {"_id": ObjectId(existing_payload_ref)},
                        mongo_doc,
                    )
                else:
                    res = await db.episodes.insert_one(mongo_doc)
                    payload_ref = str(res.inserted_id)
        except Exception as e:
            log.error("Failed to write to MongoDB: %s", e)
            if not payload_ref:
                payload_ref = "000000000000000000000000"
    else:
        # Fallback when mongo is not connected
        if not payload_ref:
            payload_ref = "000000000000000000000000"

    # 3. Scoped PG session to write the kg_node and emit event
    label = f"VENDOR:{orgnr_str.upper()}"
    final_vendors_source_id = source_id or existing_vendors_source_id

    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        # Re-assert ownership in this transaction
        await assert_owner(conn, ns_uuid, _NODE_TYPE_VENDOR, _VENDORS_ENGINE)

        await conn.execute(
            """
            INSERT INTO kg_nodes (label, entity_type, namespace_id, change_origin, payload_ref, vendors_source_id)
            VALUES ($1, $2, $3::uuid, 'agent', $4, $5)
            ON CONFLICT (label, namespace_id) DO UPDATE
                SET payload_ref = COALESCE(EXCLUDED.payload_ref, kg_nodes.payload_ref),
                    vendors_source_id = COALESCE(EXCLUDED.vendors_source_id, kg_nodes.vendors_source_id),
                    updated_at = NOW()
            """,
            label,
            _NODE_TYPE_VENDOR,
            str(ns_uuid),
            payload_ref,
            final_vendors_source_id,
        )

        await emit_graph_write(
            conn,
            namespace_id=ns_uuid,
            node_type=_NODE_TYPE_VENDOR,
            op="upserted",
            node_id=label,
        )

    return {"ok": True, "label": label, "payload_ref": payload_ref}


async def do_get_vendor(
    engine: Any,
    params: dict[str, Any],
) -> dict[str, Any] | None:
    """Fetch a single vendor: canonical identity + current scorecard + tier/ytd-progress.

    Params:
        namespace_id (str | UUID): active namespace UUID
        vendor_id (str): label, ID or vendors_source_id of the VENDOR node
    """
    ns_raw = params.get("namespace_id")
    if not ns_raw:
        raise ValueError("namespace_id is required")
    ns_uuid = UUID(str(ns_raw)) if not isinstance(ns_raw, UUID) else ns_raw

    vendor_id = params.get("vendor_id")
    if not vendor_id:
        raise ValueError("vendor_id is required")
    vendor_id_str = str(vendor_id).strip()

    row = None
    scorecard_row = None
    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        row = await conn.fetchrow(
            """
            SELECT id, label, payload_ref, vendors_source_id 
            FROM kg_nodes 
            WHERE (label = $1 OR vendors_source_id = $1 OR id::text = $1)
              AND namespace_id = $2 
              AND entity_type = 'VENDOR'
            """,
            vendor_id_str,
            ns_uuid,
        )
        if row:
            scorecard_row = await conn.fetchrow(
                """
                SELECT on_time_pct, defect_rma_rate, substitution_rate, reliability, 
                       current_tier, ytd_progress, sample_n, computed_at
                FROM vendor_scorecards
                WHERE vendor_id = $1 AND namespace_id = $2
                """,
                row["label"],
                ns_uuid,
            )

    if not row:
        return None

    mongo_doc: dict[str, Any] = {}
    if row["payload_ref"] and engine.mongo_client:
        try:
            async with scoped_mongo_session(engine.mongo_client, ns_uuid) as db:
                doc = await db.episodes.find_one({"_id": ObjectId(row["payload_ref"])})
                if doc:
                    mongo_doc = doc
        except Exception as e:
            log.warning("Failed to fetch MongoDB payload for ref %s: %s", row["payload_ref"], e)

    scorecard_data = None
    if scorecard_row:
        scorecard_data = {
            "on_time_pct": float(scorecard_row["on_time_pct"])
            if scorecard_row["on_time_pct"] is not None
            else None,
            "defect_rma_rate": float(scorecard_row["defect_rma_rate"])
            if scorecard_row["defect_rma_rate"] is not None
            else None,
            "substitution_rate": float(scorecard_row["substitution_rate"])
            if scorecard_row["substitution_rate"] is not None
            else None,
            "reliability": float(scorecard_row["reliability"])
            if scorecard_row["reliability"] is not None
            else None,
            "current_tier": scorecard_row["current_tier"],
            "ytd_progress": float(scorecard_row["ytd_progress"])
            if scorecard_row["ytd_progress"] is not None
            else None,
            "sample_n": scorecard_row["sample_n"],
            "computed_at": scorecard_row["computed_at"],
        }

    return {
        "id": str(row["id"]),
        "label": row["label"],
        "vendors_source_id": row["vendors_source_id"],
        "name": mongo_doc.get("name"),
        "orgnr": mongo_doc.get("orgnr"),
        "feed_fields": mongo_doc.get("feed_fields") or {},
        "admin_fields": mongo_doc.get("admin_fields") or {},
        "merged_fields": mongo_doc.get("merged_fields") or {},
        "scorecard": scorecard_data,
    }
