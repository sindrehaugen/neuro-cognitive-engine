"""
nce/vertical_modules/vendors/registry.py
========================================
Vendor registry operations for Vendors Axis (Batch 094).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
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


async def do_seed_vendors(
    engine: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Seed VENDOR identities from sales_read_model and Nettailer sources through C1.

    Queries supplier accounts from tenant-isolated ``sales_read_model`` and
    manufacturers/suppliers from ``product_catalog`` / ``product_prices``.
    Resolves candidates through C1 entity resolution primitive to deduplicate
    and merge into existing VENDOR identities.

    Params:
        namespace_id (str | UUID): active namespace UUID (required)
        sources (list[str], optional): sources to seed from, subset of
            ['sales_read_model', 'nettailer'] (default: all)
        dry_run (bool, optional): if True, discovers and resolves candidates without writing
        limit (int, optional): maximum total candidates to process
    """
    ns_raw = params.get("namespace_id")
    if not ns_raw:
        raise ValueError("namespace_id is required")
    try:
        ns_uuid = UUID(str(ns_raw)) if not isinstance(ns_raw, UUID) else ns_raw
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"Invalid namespace_id: {exc}") from exc

    sources_in = params.get("sources")
    if sources_in is None:
        sources = ["sales_read_model", "nettailer"]
    elif isinstance(sources_in, list):
        sources = [str(s).strip() for s in sources_in]
    else:
        sources = [str(sources_in).strip()]

    dry_run = bool(params.get("dry_run", False))
    limit_val = params.get("limit")
    limit: int | None = None
    if limit_val is not None:
        try:
            limit = int(limit_val)
            if limit < 0:
                limit = None
        except (ValueError, TypeError):
            limit = None

    srm_candidates: list[dict[str, Any]] = []
    net_candidates: list[dict[str, Any]] = []

    # 1. Query sales_read_model suppliers if enabled
    if "sales_read_model" in sources and getattr(engine, "pg_pool", None):
        try:
            async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
                srm_rows = await conn.fetch(
                    """
                    SELECT id, entity, source_id, name, source_json
                    FROM sales_read_model
                    WHERE namespace_id = $1::uuid
                      AND is_deleted = false
                      AND (
                          entity = 'suppliers'
                          OR (
                              entity = 'accounts'
                              AND (
                                  source_json->>'customertypecode' IN ('11', 'supplier', 'vendor')
                                  OR lower(source_json->>'accountclassificationcode') IN ('supplier', 'vendor')
                                  OR lower(source_json->>'relationship_type') IN ('supplier', 'vendor')
                                  OR source_json ? 'supplier'
                                  OR (source_json->>'is_supplier')::boolean = true
                              )
                          )
                      )
                    ORDER BY id ASC
                    """,
                    str(ns_uuid),
                )
                for row in srm_rows:
                    raw_json = row["source_json"]
                    source_json = (
                        json.loads(raw_json) if isinstance(raw_json, str) else dict(raw_json or {})
                    )
                    name = (
                        row["name"]
                        or source_json.get("name")
                        or source_json.get("accountname")
                        or ""
                    ).strip()
                    if not name:
                        continue

                    raw_orgnr = (
                        source_json.get("organizationnumber")
                        or source_json.get("orgnr")
                        or source_json.get("accountnumber")
                        or source_json.get("vatnumber")
                    )
                    if raw_orgnr:
                        clean_orgnr = re.sub(r"[^A-Za-z0-9]", "", str(raw_orgnr)).strip().upper()
                    else:
                        clean_source_id = (
                            re.sub(r"[^A-Za-z0-9_]", "", str(row["source_id"])).strip().upper()
                        )
                        clean_orgnr = (
                            f"SRM_{clean_source_id}" if clean_source_id else f"SRM_{row['id']}"
                        )

                    source_id = f"sales_read_model:{row['source_id']}"
                    feed_fields = {
                        "source": "sales_read_model",
                        "entity": row["entity"],
                        "source_id": row["source_id"],
                    }
                    for k, v in source_json.items():
                        if k not in ("name", "organizationnumber", "orgnr"):
                            feed_fields[k] = v

                    srm_candidates.append(
                        {
                            "source": "sales_read_model",
                            "name": name,
                            "orgnr": clean_orgnr,
                            "source_id": source_id,
                            "feed_fields": feed_fields,
                        }
                    )
        except Exception as e:
            log.warning("do_seed_vendors: error querying sales_read_model: %s", e)

    # 2. Query Nettailer catalog manufacturers and prices suppliers if enabled
    if "nettailer" in sources and getattr(engine, "pg_pool", None):
        net_map: dict[str, dict[str, Any]] = {}
        try:
            async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
                mfr_rows = await conn.fetch(
                    """
                    SELECT DISTINCT manufacturer
                    FROM product_catalog
                    WHERE is_deleted = false
                      AND manufacturer IS NOT NULL
                      AND trim(manufacturer) != ''
                    ORDER BY manufacturer ASC
                    """
                )
                for r in mfr_rows:
                    mfr_name = (r["manufacturer"] or "").strip()
                    if not mfr_name:
                        continue
                    if mfr_name not in net_map:
                        net_map[mfr_name] = {
                            "name": mfr_name,
                            "is_manufacturer": True,
                            "is_supplier": False,
                        }
                    else:
                        net_map[mfr_name]["is_manufacturer"] = True
        except Exception as e:
            log.warning("do_seed_vendors: error querying product_catalog: %s", e)

        try:
            async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
                supp_rows = await conn.fetch(
                    """
                    SELECT DISTINCT supplier
                    FROM product_prices
                    WHERE namespace_id = $1::uuid
                      AND supplier IS NOT NULL
                      AND trim(supplier) != ''
                    ORDER BY supplier ASC
                    """,
                    str(ns_uuid),
                )
                for r in supp_rows:
                    supp_name = (r["supplier"] or "").strip()
                    if not supp_name:
                        continue
                    if supp_name not in net_map:
                        net_map[supp_name] = {
                            "name": supp_name,
                            "is_manufacturer": False,
                            "is_supplier": True,
                        }
                    else:
                        net_map[supp_name]["is_supplier"] = True
        except Exception as e:
            log.warning("do_seed_vendors: error querying product_prices: %s", e)

        for item in net_map.values():
            name = item["name"]
            clean_slug = re.sub(r"[^A-Z0-9]", "", name.upper())[:32]
            if not clean_slug:
                clean_slug = hashlib.sha256(name.encode("utf-8")).hexdigest()[:16].upper()
            fallback_orgnr = f"NET_{clean_slug}"
            source_id = f"nettailer:{name}"
            feed_fields = {
                "source": "nettailer",
                "is_manufacturer": item["is_manufacturer"],
                "is_supplier": item["is_supplier"],
            }
            net_candidates.append(
                {
                    "source": "nettailer",
                    "name": name,
                    "orgnr": fallback_orgnr,
                    "source_id": source_id,
                    "feed_fields": feed_fields,
                }
            )

    all_candidates = srm_candidates + net_candidates
    if limit is not None:
        all_candidates = all_candidates[:limit]

    seeded_vendors: list[dict[str, Any]] = []
    srm_seeded = 0
    net_seeded = 0

    # 3. Resolve each candidate through C1 and upsert
    for cand in all_candidates:
        cand_source = cand["source"]
        cand_name = cand["name"]
        cand_orgnr = cand["orgnr"]
        cand_source_id = cand["source_id"]
        cand_feed = cand["feed_fields"]

        # Run C1 resolve to see if an existing VENDOR node matches
        action = "create"
        final_orgnr = cand_orgnr

        if getattr(engine, "pg_pool", None):
            try:
                async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
                    matches = await resolve(
                        conn,
                        namespace_id=ns_uuid,
                        candidate={"orgnr": cand_orgnr, "name": cand_name},
                        keys=["orgnr", "name"],
                        node_type=_NODE_TYPE_VENDOR,
                    )
                    if matches:
                        top_match = matches[0]
                        if top_match.score >= 0.8:
                            row = await conn.fetchrow(
                                "SELECT label FROM kg_nodes WHERE id = $1 AND namespace_id = $2::uuid",
                                top_match.node_id,
                                str(ns_uuid),
                            )
                            if row and row["label"]:
                                lbl = row["label"]
                                matched_orgnr = lbl.split(":", 1)[-1]
                                final_orgnr = matched_orgnr
                                action = "update"

                    if action == "create":
                        # Also check if exact label exists in kg_nodes
                        label_check = f"VENDOR:{final_orgnr.upper()}"
                        existing_node = await conn.fetchrow(
                            "SELECT id FROM kg_nodes WHERE label = $1 AND namespace_id = $2::uuid",
                            label_check,
                            str(ns_uuid),
                        )
                        if existing_node:
                            action = "update"
            except Exception as e:
                log.warning("do_seed_vendors: C1 resolution lookup warning: %s", e)

        if dry_run:
            seeded_vendors.append(
                {
                    "source": cand_source,
                    "name": cand_name,
                    "orgnr": final_orgnr,
                    "label": f"VENDOR:{final_orgnr.upper()}",
                    "source_id": cand_source_id,
                    "action": action,
                    "dry_run": True,
                }
            )
        else:
            try:
                upsert_res = await do_upsert_vendor(
                    engine,
                    {
                        "namespace_id": ns_uuid,
                        "orgnr": final_orgnr,
                        "name": cand_name,
                        "source_id": cand_source_id,
                        "feed_fields": cand_feed,
                        "source_type": "feed",
                    },
                )
                seeded_vendors.append(
                    {
                        "source": cand_source,
                        "name": cand_name,
                        "orgnr": final_orgnr,
                        "label": upsert_res["label"],
                        "payload_ref": upsert_res.get("payload_ref"),
                        "source_id": cand_source_id,
                        "action": action,
                    }
                )
                if cand_source == "sales_read_model":
                    srm_seeded += 1
                elif cand_source == "nettailer":
                    net_seeded += 1
            except Exception as e:
                log.error("do_seed_vendors: failed to upsert candidate %s: %s", cand_name, e)

    return {
        "ok": True,
        "namespace_id": str(ns_uuid),
        "dry_run": dry_run,
        "sales_read_model_candidates": len(srm_candidates),
        "sales_read_model_seeded": srm_seeded,
        "nettailer_candidates": len(net_candidates),
        "nettailer_seeded": net_seeded,
        "total_candidates": len(all_candidates),
        "total_seeded": len(seeded_vendors) if not dry_run else 0,
        "vendors": seeded_vendors,
    }
