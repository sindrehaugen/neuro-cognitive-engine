"""
nce/vertical_modules/vendors/certs.py
======================================
Certification modeling and watcher for the Vendors Engine (Batch 101).
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID

from bson import ObjectId

from nce.db_utils import scoped_mongo_session, scoped_pg_session
from nce.entity_resolution.ownership import assert_owner
from nce.events.bus import publish
from nce.events.emit import emit_graph_write

log = logging.getLogger("nce.vertical_modules.vendors.certs")

_VENDORS_ENGINE: str = "vendors"
_NODE_TYPE_CERT: str = "CERT"
_NODE_TYPE_CONTRACTOR: str = "CONTRACTOR"

# Retrieve NCE_VENDORS_CERT_EXPIRY_WARN_DAYS from config/env with fallback
try:
    from nce.config import NCE_VENDORS_CERT_EXPIRY_WARN_DAYS
except ImportError:
    import os

    try:
        NCE_VENDORS_CERT_EXPIRY_WARN_DAYS = int(
            os.environ.get("NCE_VENDORS_CERT_EXPIRY_WARN_DAYS", 30)
        )
    except ValueError:
        NCE_VENDORS_CERT_EXPIRY_WARN_DAYS = 30


async def do_upsert_cert(
    engine: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Upsert a contractor certification node in the graph and its metadata in MongoDB.

    Params:
        namespace_id (str | UUID): active namespace UUID
        contractor_id (str): contractor label (e.g. 'CONTRACTOR:XYZ')
        cert_name (str): certification identifier / name (e.g. 'SAFETY_101')
        expiry_date (str | date | datetime): cert expiry date (YYYY-MM-DD)
        name (str, optional): friendly cert name
    """
    ns_raw = params.get("namespace_id")
    if not ns_raw:
        raise ValueError("namespace_id is required")
    ns_uuid = UUID(str(ns_raw)) if not isinstance(ns_raw, UUID) else ns_raw

    contractor_id = params.get("contractor_id")
    if not contractor_id:
        raise ValueError("contractor_id is required")
    contractor_id_str = str(contractor_id).strip()

    cert_name = params.get("cert_name")
    if not cert_name:
        raise ValueError("cert_name is required")
    cert_name_str = str(cert_name).strip()

    expiry_raw = params.get("expiry_date")
    if not expiry_raw:
        raise ValueError("expiry_date is required")

    # Parse and normalise expiry_date
    if isinstance(expiry_raw, (date, datetime)):
        expiry_date_val = expiry_raw.date() if isinstance(expiry_raw, datetime) else expiry_raw
    else:
        # Support both full ISO timestamps and plain dates
        expiry_str = str(expiry_raw).strip()
        if "T" in expiry_str:
            expiry_str = expiry_str.split("T")[0]
        expiry_date_val = date.fromisoformat(expiry_str)

    expiry_date_str = expiry_date_val.isoformat()

    # Format contractor label (must start with CONTRACTOR:)
    contractor_label = (
        contractor_id_str
        if contractor_id_str.startswith("CONTRACTOR:")
        else f"CONTRACTOR:{contractor_id_str.upper()}"
    )

    # Format cert label (must start with CERT:)
    contractor_suffix = contractor_label.split(":", 1)[1]
    cert_label = f"CERT:{contractor_suffix}:{cert_name_str.upper()}"

    # 1. Check if the cert node already exists to preserve payload_ref
    existing_payload_ref: str | None = None
    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        row = await conn.fetchrow(
            "SELECT payload_ref FROM kg_nodes WHERE label = $1 AND namespace_id = $2::uuid",
            cert_label,
            ns_uuid,
        )
        if row:
            existing_payload_ref = row["payload_ref"]

    # 2. Write details to MongoDB episodes collection
    mongo_doc = {
        "contractor_id": contractor_label,
        "cert_name": cert_name_str,
        "expiry_date": expiry_date_str,
        "friendly_name": params.get("name") or cert_name_str,
    }

    payload_ref = existing_payload_ref
    if engine.mongo_client:
        try:
            async with scoped_mongo_session(engine.mongo_client, ns_uuid) as db:
                if existing_payload_ref and existing_payload_ref != "000000000000000000000000":
                    await db.episodes.replace_one(
                        {"_id": ObjectId(existing_payload_ref)},
                        mongo_doc,
                    )
                else:
                    res = await db.episodes.insert_one(mongo_doc)
                    payload_ref = str(res.inserted_id)
        except Exception as e:
            log.error("Failed to write certification to MongoDB: %s", e)
            if not payload_ref:
                payload_ref = "000000000000000000000000"
    else:
        if not payload_ref:
            payload_ref = "000000000000000000000000"

    # 3. Write to Postgres kg_nodes & kg_edges and emit event
    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        await assert_owner(conn, ns_uuid, _NODE_TYPE_CERT, _VENDORS_ENGINE)

        # Upsert the CERT node
        await conn.execute(
            """
            INSERT INTO kg_nodes (label, entity_type, namespace_id, change_origin, payload_ref)
            VALUES ($1, $2, $3::uuid, 'agent', $4)
            ON CONFLICT (label, namespace_id) DO UPDATE SET
                payload_ref = EXCLUDED.payload_ref,
                updated_at = NOW()
            """,
            cert_label,
            _NODE_TYPE_CERT,
            ns_uuid,
            payload_ref,
        )

        # Upsert the CONTRACTOR -[has]-> CERT edge
        await conn.execute(
            """
            INSERT INTO kg_edges (subject_label, predicate, object_label, confidence, namespace_id, change_origin)
            VALUES ($1, 'has', $2, 1.0, $3::uuid, 'agent')
            ON CONFLICT (subject_label, predicate, object_label, namespace_id) DO NOTHING
            """,
            contractor_label,
            cert_label,
            ns_uuid,
        )

        # Emit the graph-write event for the cert node
        await emit_graph_write(
            conn,
            namespace_id=ns_uuid,
            node_type=_NODE_TYPE_CERT,
            op="upserted",
            node_id=cert_label,
        )

    return {
        "ok": True,
        "cert_label": cert_label,
        "payload_ref": payload_ref,
        "contractor_label": contractor_label,
    }


async def do_check_cert_expiry(
    engine: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Scan all CERT nodes in the active namespace and idempotently publish cert.expiry events for expiring ones.

    Params:
        namespace_id (str | UUID): active namespace UUID
        reference_date (str | date | datetime, optional): base date to check against (defaults to today)
    """
    ns_raw = params.get("namespace_id")
    if not ns_raw:
        raise ValueError("namespace_id is required")
    ns_uuid = UUID(str(ns_raw)) if not isinstance(ns_raw, UUID) else ns_raw

    # Resolve reference date
    ref_date_raw = params.get("reference_date")
    if ref_date_raw:
        if isinstance(ref_date_raw, (date, datetime)):
            ref_date = ref_date_raw.date() if isinstance(ref_date_raw, datetime) else ref_date_raw
        else:
            ref_date = date.fromisoformat(str(ref_date_raw))
    else:
        ref_date = date.today()

    # Calculate warning date threshold
    warn_date = ref_date + timedelta(days=NCE_VENDORS_CERT_EXPIRY_WARN_DAYS)

    # 1. Fetch all CERT nodes and their linked contractor labels
    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        rows = await conn.fetch(
            """
            SELECT n.label AS cert_label, n.payload_ref, e.subject_label AS contractor_label
            FROM kg_nodes n
            LEFT JOIN kg_edges e ON e.object_label = n.label AND e.predicate = 'has' AND e.namespace_id = n.namespace_id
            WHERE n.entity_type = 'CERT' AND n.namespace_id = $1::uuid
            """,
            ns_uuid,
        )

    if not rows:
        return {"checked": 0, "expiring": 0, "published": 0}

    # 2. Bulk-fetch payloads from MongoDB episodes collection
    payload_refs = [
        r["payload_ref"]
        for r in rows
        if r["payload_ref"] and r["payload_ref"] != "000000000000000000000000"
    ]
    docs_by_ref: dict[str, dict[str, Any]] = {}

    if payload_refs and engine.mongo_client:
        try:
            async with scoped_mongo_session(engine.mongo_client, ns_uuid) as db:
                cursor = db.episodes.find({"_id": {"$in": [ObjectId(r) for r in payload_refs]}})
                docs = await cursor.to_list(length=None)
                docs_by_ref = {str(d["_id"]): d for d in docs}
        except Exception as e:
            log.error("Failed to bulk-fetch certification payloads from MongoDB: %s", e)

    checked = 0
    expiring = 0
    published = 0

    # 3. Check expiry and publish C4 events
    for row in rows:
        cert_label = row["cert_label"]
        payload_ref = row["payload_ref"]
        contractor_label = row["contractor_label"]

        doc = docs_by_ref.get(payload_ref) if payload_ref else None
        if not doc:
            continue

        expiry_date_str = doc.get("expiry_date")
        if not expiry_date_str:
            continue

        # Parse expiry date
        try:
            if isinstance(expiry_date_str, (date, datetime)):
                expiry_date = (
                    expiry_date_str.date()
                    if isinstance(expiry_date_str, datetime)
                    else expiry_date_str
                )
            else:
                expiry_date = date.fromisoformat(expiry_date_str)
        except Exception:
            log.warning("Invalid expiry_date format '%s' for cert %s", expiry_date_str, cert_label)
            continue

        checked += 1

        if expiry_date <= warn_date:
            expiring += 1

            # Idempotently publish cert.expiry C4 event
            async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
                already_published = await conn.fetchval(
                    """
                    SELECT EXISTS (
                        SELECT 1 FROM outbox_events
                        WHERE namespace_id = $1::uuid
                          AND event_type = 'cert.expiry'
                          AND aggregate_id = $2
                    )
                    """,
                    ns_uuid,
                    cert_label,
                )

                if not already_published:
                    await publish(
                        conn,
                        namespace_id=ns_uuid,
                        node_type="cert",
                        op="expiry",
                        aggregate_id=cert_label,
                        payload={
                            "cert_id": cert_label,
                            "contractor_id": contractor_label or "",
                            "cert_name": doc.get("cert_name") or cert_label.split(":")[-1],
                            "expiry_date": expiry_date.isoformat(),
                            "namespace_id": str(ns_uuid),
                        },
                    )
                    published += 1

    return {
        "checked": checked,
        "expiring": expiring,
        "published": published,
    }
