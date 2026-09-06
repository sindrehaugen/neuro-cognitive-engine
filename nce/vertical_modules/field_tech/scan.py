"""
nce/vertical_modules/field_tech/scan.py
=======================================
Serial Number scanning domain logic for Module 12 (Field Tech Engine):
  - do_scan_serial: creates FIELD_TECH_SCAN node and seeds the canonical
    BOM_LINE -[installed_as]-> ASSET boundary edge handed to Assets (Module 9).

Strict Tenant Predicate Discipline (Charter Â§4.4)
-------------------------------------------------
EVERY query against kg_nodes / kg_edges / work_orders carries explicit WHERE namespace_id = $N::uuid predicates.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from nce.bom_lines import update_bom_line_status
from nce.db_utils import scoped_pg_session
from nce.entity_resolution.ownership import assert_owner

log = logging.getLogger("nce.vertical_modules.field_tech.scan")

EVENT_TYPE_SERIAL_SCANNED: str = "field_tech_serial_scanned"
_NODE_TYPE_SCAN = "FIELD_TECH_SCAN"
_NODE_TYPE_ASSET = "ASSET"
_NODE_TYPE_BOM_LINE = "BOM_LINE"
_OWNER_ENGINE = "field_tech"


def _extract_pool(engine_or_pool: Any) -> Any:
    if hasattr(engine_or_pool, "pg_pool") and (
        "pg_pool" in getattr(engine_or_pool, "__dict__", {})
        or hasattr(type(engine_or_pool), "pg_pool")
    ):
        return engine_or_pool.pg_pool
    return engine_or_pool


def _parse_uuid(val: Any, field_name: str) -> UUID:
    if not val:
        raise ValueError(f"{field_name} is required")
    if isinstance(val, UUID):
        return val
    try:
        return UUID(str(val))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"Invalid {field_name} UUID: {val!r}") from exc


async def do_scan_serial(engine: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Record an equipment serial-number scan during installation or service."""
    pool = _extract_pool(engine)
    ns_uuid = _parse_uuid(params.get("namespace_id"), "namespace_id")

    work_order_id = str(params.get("work_order_id") or "").strip()
    if not work_order_id:
        raise ValueError("work_order_id is required")

    bom_line_id = str(params.get("bom_line_id") or "").strip()
    if not bom_line_id:
        raise ValueError("bom_line_id is required")

    serial = str(params.get("serial") or params.get("scanned_serial") or "").strip()
    if not serial:
        raise ValueError("serial is required")

    async with scoped_pg_session(pool, ns_uuid) as conn:
        # 1. Assert work order exists in namespace
        wo = await conn.fetchrow(
            """
            SELECT id, location_id FROM work_orders
            WHERE work_order_id = $1 AND namespace_id = $2::uuid
            """,
            work_order_id,
            ns_uuid,
        )
        if wo is None:
            raise ValueError(f"Work order {work_order_id!r} not found in namespace")

        scan_label = f"{_NODE_TYPE_SCAN}:{serial.upper()}"
        bom_label = (
            bom_line_id if bom_line_id.startswith("BOM_LINE:") else f"BOM_LINE:{bom_line_id}"
        )
        asset_label = f"{_NODE_TYPE_ASSET}:{serial.upper()}"
        wo_label = f"WORK_ORDER:{work_order_id}"

        # 2. Insert scan node (Contract-A guarded)
        await assert_owner(conn, ns_uuid, _NODE_TYPE_SCAN, _OWNER_ENGINE)
        await conn.execute(
            """
            INSERT INTO kg_nodes (label, entity_type, namespace_id, change_origin)
            VALUES ($1, $2, $3::uuid, 'agent')
            ON CONFLICT (label, namespace_id) DO NOTHING
            """,
            scan_label,
            _NODE_TYPE_SCAN,
            ns_uuid,
        )

        # 3. Route ASSET node & relational registration to Assets engine via I-0 registry (Contract A)
        raw_bom = (
            bom_line_id[len("BOM_LINE:") :] if bom_line_id.startswith("BOM_LINE:") else bom_line_id
        )
        seed_params = {
            "namespace_id": str(ns_uuid),
            "bom_line_id": raw_bom,
            "serial": serial,
            "functional_location_id": params.get("functional_location_id")
            or params.get("location_id")
            or (wo["location_id"] if wo and "location_id" in wo and wo["location_id"] else None),
        }
        modules = getattr(engine, "modules", None)
        if modules is not None:
            try:
                for_ns = getattr(modules, "for_namespace", None)
                scoped_reg = for_ns(str(ns_uuid)) if callable(for_ns) else modules
                if "assets" in scoped_reg:
                    assets_mod = scoped_reg["assets"]
                    if hasattr(assets_mod, "do_seed_asset_from_bom"):
                        await assets_mod.do_seed_asset_from_bom(engine, seed_params)
            except Exception as exc:
                log.debug("Assets registry seed invocation skipped: %s", exc)
        else:
            try:
                from nce.vertical_modules.assets.seed import do_seed_asset_from_bom

                await do_seed_asset_from_bom(engine, seed_params)
            except Exception as exc:
                log.debug("Direct do_seed_asset_from_bom invocation skipped: %s", exc)

        # 4. Insert seed edge: BOM_LINE -[installed_as]-> ASSET
        await conn.execute(
            """
            INSERT INTO kg_edges (subject_label, predicate, object_label, confidence, namespace_id, change_origin)
            VALUES ($1, 'installed_as', $2, 1.0, $3::uuid, 'agent')
            ON CONFLICT (subject_label, predicate, object_label, namespace_id) DO NOTHING
            """,
            bom_label,
            asset_label,
            ns_uuid,
        )

        # 5. Insert audit edge: WORK_ORDER -[scanned]-> FIELD_TECH_SCAN
        await conn.execute(
            """
            INSERT INTO kg_edges (subject_label, predicate, object_label, confidence, namespace_id, change_origin)
            VALUES ($1, 'scanned', $2, 1.0, $3::uuid, 'agent')
            ON CONFLICT (subject_label, predicate, object_label, namespace_id) DO NOTHING
            """,
            wo_label,
            scan_label,
            ns_uuid,
        )

        # 6. Advance BOM_LINE status to INSTALLED (Wave FT-1)
        target_quote_id = params.get("quote_id")
        target_line_ref = params.get("line_ref")
        if not (target_quote_id and target_line_ref):
            clean_id = (
                bom_line_id[len("BOM_LINE:") :]
                if bom_line_id.startswith("BOM_LINE:")
                else bom_line_id
            )
            if ":" in clean_id:
                target_quote_id, target_line_ref = clean_id.split(":", 1)
            else:
                try:
                    bl_row = await conn.fetchrow(
                        """
                        SELECT quote_id, line_ref FROM bom_line_content
                        WHERE namespace_id = $1::uuid
                          AND (bom_line_label = $2 OR bom_line_label = 'BOM_LINE:' || $2 OR line_ref = $2)
                        LIMIT 1
                        """,
                        ns_uuid,
                        bom_line_id,
                    )
                    if bl_row:
                        target_quote_id = bl_row["quote_id"]
                        target_line_ref = bl_row["line_ref"]
                except Exception as exc:
                    log.debug("Failed to query bom_line_content in do_scan_serial: %s", exc)

        if target_quote_id and target_line_ref:
            try:
                await update_bom_line_status(
                    conn,
                    ns_uuid,
                    writer_engine="field_tech",
                    quote_id=target_quote_id,
                    line_ref=target_line_ref,
                    status="INSTALLED",
                )
            except Exception as exc:
                log.warning("Could not update BOM_LINE status to INSTALLED: %s", exc)

        # 7. Promote FUNCTIONAL_LOCATION from design-intent to as-built (Wave FT-1)
        fl_val = (
            params.get("functional_location_id")
            or params.get("location_id")
            or (wo["location_id"] if wo and "location_id" in wo and wo["location_id"] else None)
        )
        asbuilt_label: str | None = None
        if fl_val:
            loc_str = str(fl_val).strip()
            if loc_str.startswith("FL:") or loc_str.startswith("FUNCTIONAL_LOCATION:"):
                intent_label = loc_str
            else:
                intent_label = f"FUNCTIONAL_LOCATION:{loc_str}"
            asbuilt_label = f"AsBuilt:{intent_label}"

            # Promoted_to_asbuilt edge: intent -> as-built
            await conn.execute(
                """
                INSERT INTO kg_edges (subject_label, predicate, object_label, confidence, namespace_id, change_origin)
                VALUES ($1, 'promoted_to_asbuilt', $2, 1.0, $3::uuid, 'agent')
                ON CONFLICT (subject_label, predicate, object_label, namespace_id) DO NOTHING
                """,
                intent_label,
                asbuilt_label,
                ns_uuid,
            )

            # Reverse confirmation edge: as-built -> intent
            await conn.execute(
                """
                INSERT INTO kg_edges (subject_label, predicate, object_label, confidence, namespace_id, change_origin)
                VALUES ($1, 'as_built_confirms', $2, 1.0, $3::uuid, 'agent')
                ON CONFLICT (subject_label, predicate, object_label, namespace_id) DO NOTHING
                """,
                asbuilt_label,
                intent_label,
                ns_uuid,
            )

            # Link asset to as-built location
            await conn.execute(
                """
                INSERT INTO kg_edges (subject_label, predicate, object_label, confidence, namespace_id, change_origin)
                VALUES ($1, 'lives_in', $2, 1.0, $3::uuid, 'agent')
                ON CONFLICT (subject_label, predicate, object_label, namespace_id) DO NOTHING
                """,
                asset_label,
                asbuilt_label,
                ns_uuid,
            )

    res: dict[str, Any] = {
        "status": "scanned",
        "work_order_id": work_order_id,
        "bom_line_id": bom_line_id,
        "serial": serial.upper(),
        "scan_label": scan_label,
        "asset_label": asset_label,
        "seed_edge": {
            "subject": bom_label,
            "predicate": "installed_as",
            "object": asset_label,
            "raw": f"{bom_label} -[installed_as]-> {asset_label}",
        },
    }
    if asbuilt_label:
        res["asbuilt_location"] = asbuilt_label
    return res
