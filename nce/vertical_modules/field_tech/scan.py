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

from nce.db_utils import scoped_pg_session

log = logging.getLogger("nce.vertical_modules.field_tech.scan")

EVENT_TYPE_SERIAL_SCANNED: str = "field_tech_serial_scanned"
_NODE_TYPE_SCAN = "FIELD_TECH_SCAN"
_NODE_TYPE_ASSET = "ASSET"
_NODE_TYPE_BOM_LINE = "BOM_LINE"


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
            SELECT id FROM work_orders
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

        # 2. Insert scan node
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

        # 3. Insert seed ASSET node
        await conn.execute(
            """
            INSERT INTO kg_nodes (label, entity_type, namespace_id, change_origin)
            VALUES ($1, $2, $3::uuid, 'agent')
            ON CONFLICT (label, namespace_id) DO NOTHING
            """,
            asset_label,
            _NODE_TYPE_ASSET,
            ns_uuid,
        )

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

    return {
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
