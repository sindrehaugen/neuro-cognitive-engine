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
_NODE_TYPE_CERTIFICATION: str = "CERTIFICATION"
_NODE_TYPE_CONTRACTOR: str = "CONTRACTOR"
_OP_EXPIRED: str = "EXPIRED"

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


def validate_contractor_regulatory_compliance(
    contractor_meta: dict[str, Any],
    certs: list[dict[str, Any]] | list[Any],
    regulatory_scope: str = "electrical_lv",
    require_iso: list[str] | None = None,
    ref_date: date | None = None,
) -> dict[str, Any]:
    """Verify contractor company regulatory registrations and ISO management systems.

    Verifies:
    - electrical_lv / electrical_hv: contractor registered in DSB Elvirksomhetsregisteret
      and has a designated Faglig Ansvarlig (FEK § 3 / § 5 / § 7).
    - ekom: contractor registered in NKOM ENA register or holds active NKOM_ENA cert.
    - dnv_marine: contractor is DNV approved supplier or holds active DNV_AOSS cert.
    - require_iso: verifies each requested ISO management system standard (e.g. ISO 9001,
      ISO 14001, ISO 45001) is active and not expired.
    """
    if ref_date is None:
        ref_date = date.today()
    elif isinstance(ref_date, datetime):
        ref_date = ref_date.date()
    elif isinstance(ref_date, str):
        try:
            ref_date = date.fromisoformat(ref_date.split("T")[0])
        except Exception:
            ref_date = date.today()

    norm_scope = str(regulatory_scope or "electrical_lv").strip().lower()
    reasons: list[str] = []
    verified_requirements: list[str] = []
    missing_requirements: list[str] = []

    def _is_cert_active(c: dict[str, Any]) -> bool:
        st = str(c.get("status") or "active").strip().lower()
        if st in ("expired", "revoked", "suspended", "inactive"):
            return False
        v_raw = c.get("valid_to") or c.get("expiry_date") or c.get("expires_at")
        if v_raw:
            try:
                v_d = (
                    v_raw.date()
                    if isinstance(v_raw, datetime)
                    else (
                        v_raw
                        if isinstance(v_raw, date)
                        else date.fromisoformat(str(v_raw).split("T")[0])
                    )
                )
                if v_d < ref_date:
                    return False
            except Exception:
                pass
        return True

    parsed_certs: list[dict[str, Any]] = []
    for c in certs or []:
        if isinstance(c, str):
            parsed_certs.append({"name": c, "status": "active"})
        elif isinstance(c, dict):
            parsed_certs.append(c)

    def _has_active_cert(cert_keys: list[str]) -> bool:
        normalized_keys = {k.strip().upper().replace("-", "_").replace(" ", "_") for k in cert_keys}
        for pc in parsed_certs:
            nm = str(
                pc.get("name") or pc.get("cert_name") or pc.get("code") or pc.get("cert_id") or ""
            )
            norm_nm = nm.strip().upper().replace("-", "_").replace(" ", "_")
            if any(k in norm_nm or norm_nm in k for k in normalized_keys):
                if _is_cert_active(pc):
                    return True
        return False

    # 1. Electrical scope: DSB Elvirksomhetsregisteret + Faglig Ansvarlig
    if norm_scope in ("electrical_lv", "electrical_hv"):
        if contractor_meta.get("dsb_elvirksomhet_registered") is True:
            verified_requirements.append("DSB Elvirksomhetsregisteret")
        else:
            missing_requirements.append("DSB Elvirksomhetsregisteret")
            reasons.append(
                "Contractor is not registered in DSB Elvirksomhetsregisteret (Forskrift om elektroforetak / FEK § 3)"
            )

        fa = contractor_meta.get("faglig_ansvarlig")
        if fa and str(fa).strip():
            verified_requirements.append(f"Faglig Ansvarlig ({fa})")
        else:
            missing_requirements.append("Faglig Ansvarlig")
            reasons.append("Contractor lacks designated Faglig Ansvarlig (FEK § 5 / § 7)")

    # 2. Ekom scope: NKOM ENA registered or active cert
    elif norm_scope == "ekom":
        if contractor_meta.get("nkom_ena_registered") is True or _has_active_cert(
            ["NKOM_ENA", "ENA", "EKOM"]
        ):
            verified_requirements.append("NKOM Ekomautorisasjon (ENA)")
        else:
            missing_requirements.append("NKOM Ekomautorisasjon (ENA)")
            reasons.append(
                "Contractor is not NKOM ENA registered and lacks active NKOM_ENA certificate (Ekomforskriften § 9-1)"
            )

    # 3. Maritime scope: DNV Approved Supplier or active DNV_AOSS cert
    elif norm_scope == "dnv_marine":
        if contractor_meta.get("dnv_approved_supplier") is True or _has_active_cert(
            ["DNV_AOSS", "DNV_APPROVED", "DNV"]
        ):
            verified_requirements.append("DNV Approved Supplier (AOSS)")
        else:
            missing_requirements.append("DNV Approved Supplier (AOSS)")
            reasons.append(
                "Contractor is not DNV approved supplier and lacks active DNV_AOSS certificate (DNV-CP-0484)"
            )

    # 4. ISO management system standards (e.g. ISO 9001, ISO 14001, ISO 45001)
    if require_iso:
        for iso_item in require_iso:
            iso_str = str(iso_item).strip().upper()
            iso_key = iso_str.replace("-", "_").replace(" ", "_")
            in_meta = (
                contractor_meta.get(iso_key.lower()) is True
                or contractor_meta.get(iso_key) is True
                or iso_key
                in [str(x).upper().replace("-", "_") for x in contractor_meta.get("iso_certs", [])]
                or iso_key
                in [
                    str(x).upper().replace("-", "_")
                    for x in contractor_meta.get("iso_certifications", [])
                ]
            )
            in_certs = _has_active_cert([iso_str, iso_key])
            if in_meta or in_certs:
                verified_requirements.append(iso_str)
            else:
                missing_requirements.append(iso_str)
                reasons.append(f"Missing or expired management system certification: {iso_str}")

    return {
        "compliant": len(reasons) == 0,
        "regulatory_scope": regulatory_scope,
        "reasons": reasons,
        "verified_requirements": verified_requirements,
        "missing_requirements": missing_requirements,
    }


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

            # Idempotently publish CERTIFICATION.EXPIRED C4 event (Wave V-2)
            async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
                already_published = await conn.fetchval(
                    """
                    SELECT EXISTS (
                        SELECT 1 FROM outbox_events
                        WHERE namespace_id = $1::uuid
                          AND event_type IN ('CERTIFICATION.EXPIRED', 'cert.expiry')
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
                        node_type=_NODE_TYPE_CERTIFICATION,
                        op=_OP_EXPIRED,
                        aggregate_id=cert_label,
                        payload={
                            "cert_id": cert_label,
                            "contractor_id": contractor_label or "",
                            "employee_id": contractor_label or "",
                            "resource_id": contractor_label or "",
                            "cert_name": doc.get("cert_name") or cert_label.split(":")[-1],
                            "status": "expired",
                            "valid_to": expiry_date.isoformat(),
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
