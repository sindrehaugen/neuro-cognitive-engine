"""
nce.vertical_modules.resources.material_flow
============================================
Material Flow Planner (Warehouse-to-Project Bridge) for Module 15 (Staff & Resources Engine).
Coordinates Inventory BOM kit/reserve, van scheduling (RS-2: van is VEHICLE + STOCK_LOCATION,
never a customer FUNCTIONAL_LOCATION), and Field Tech dispatch staging (Spec §80, §85, §115).
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any
from uuid import uuid4

from nce.db_utils import scoped_pg_session
from nce.vertical_modules.resources._guard import (
    ResourceNotFoundError,
    ResourceValidationError,
    require_resources_enabled,
)
from nce.vertical_modules.resources.allocations import _parse_datetime, do_reserve
from nce.vertical_modules.resources.registry import _extract_pool, _parse_uuid

log = logging.getLogger("nce.vertical_modules.resources.material_flow")


async def do_plan_material_flow(engine: Any, params: dict[str, Any]) -> dict[str, Any]:
    """
    Plan end-to-end material flow from warehouse staging to van stock to job site.
    Enforces RS-2: Van is VEHICLE (Resources) and STOCK_LOCATION (Inventory),
    and NEVER a customer FUNCTIONAL_LOCATION.
    """
    require_resources_enabled(params.get("namespace_metadata"))
    ns_id = _parse_uuid(params.get("namespace_id"), "namespace_id")

    van_res_id = _parse_uuid(params.get("van_resource_id"), "van_resource_id")
    target_date_raw = params.get("target_date") or params.get("starts_at")
    install_time = _parse_datetime(target_date_raw, "target_date/starts_at")

    demand_kind = str(params.get("demand_kind") or "project").strip()
    demand_id_raw = (
        params.get("demand_id") or params.get("project_id") or params.get("work_order_id")
    )
    demand_id = _parse_uuid(demand_id_raw, "demand_id") if demand_id_raw else None

    dest_loc_raw = params.get("destination_location_id") or params.get("functional_location_id")
    dest_loc_id = _parse_uuid(dest_loc_raw, "destination_location_id") if dest_loc_raw else None

    # RS-2 check: Destination must NOT be a van / vehicle
    if dest_loc_id and str(dest_loc_id) == str(van_res_id):
        raise ResourceValidationError(
            "RS-2 Violation: Destination cannot be a van or vehicle. A van is never a customer functional location."
        )

    raw_items = params.get("items") or []
    if not isinstance(raw_items, list):
        raise ResourceValidationError("items must be a list of kit/BOM items.")

    items: list[dict[str, Any]] = []
    for idx, it in enumerate(raw_items):
        if not isinstance(it, dict):
            continue
        sku = str(it.get("sku") or it.get("item_id") or f"ITEM-{idx + 1}")
        qty = max(1, int(it.get("quantity", 1)))
        items.append(
            {
                "item_id": str(it.get("item_id") or uuid4()),
                "sku": sku,
                "description": str(it.get("description") or sku),
                "quantity": qty,
                "staged": False,
            }
        )

    auto_reserve_van = bool(params.get("auto_reserve_van", False))

    pool = _extract_pool(engine)
    flow_id = uuid4()

    # Timeline calculation:
    # Staging/Kit at warehouse: 24h prior
    # Loading into van: 2h prior
    # Delivery window: 2h prior to install_time
    kit_by = install_time - timedelta(hours=24)
    load_at = install_time - timedelta(hours=2)
    depart_at = install_time - timedelta(hours=1)
    arrival_at = install_time

    async with scoped_pg_session(pool, ns_id) as conn:
        # 1. Verify van resource exists in resources table and is kind='vehicle'
        van_row = await conn.fetchrow(
            """
            SELECT id, kind, ref_id, display_name, attrs
            FROM resources
            WHERE id = $1 AND namespace_id = $2
            """,
            van_res_id,
            ns_id,
        )
        if not van_row:
            raise ResourceNotFoundError(
                f"Van resource {van_res_id} not found in namespace {ns_id}."
            )
        if van_row["kind"] != "vehicle":
            raise ResourceValidationError(
                f"Resource {van_res_id} has kind {van_row['kind']!r}; expected 'vehicle' (RS-2)."
            )

        # 2. Check if a corresponding van stock_location exists in Inventory
        stock_loc_row = await conn.fetchrow(
            """
            SELECT id, kind, name, vehicle_ref
            FROM stock_locations
            WHERE namespace_id = $1 AND (vehicle_ref = $2 OR name = $3)
            LIMIT 1
            """,
            ns_id,
            str(van_res_id),
            van_row["display_name"],
        )
        van_stock_location_id = str(stock_loc_row["id"]) if stock_loc_row else None

    # Staging stages
    stages = [
        {
            "stage": "pick_and_kit",
            "location_kind": "warehouse",
            "scheduled_time": kit_by.isoformat(),
            "status": "pending_pick",
            "items_count": len(items),
        },
        {
            "stage": "van_loading",
            "location_kind": "van_stock_location",
            "van_stock_location_id": van_stock_location_id,
            "scheduled_time": load_at.isoformat(),
            "status": "pending_load",
            "van_resource_id": str(van_res_id),
        },
        {
            "stage": "transit_and_delivery",
            "origin": "warehouse",
            "destination_location_id": str(dest_loc_id) if dest_loc_id else "customer_site",
            "departure_time": depart_at.isoformat(),
            "arrival_time": arrival_at.isoformat(),
            "status": "scheduled",
            "vehicle_resource_id": str(van_res_id),
        },
    ]

    van_allocation: dict[str, Any] | None = None
    if auto_reserve_van:
        # Reserve vehicle for transit window [depart_at, arrival_at + 4h install buffer]
        transit_end = arrival_at + timedelta(hours=4)
        van_allocation = await do_reserve(
            engine,
            {
                "namespace_id": ns_id,
                "resource_id": van_res_id,
                "demand_kind": demand_kind,
                "demand_id": demand_id,
                "functional_location_id": dest_loc_id,
                "starts_at": depart_at.isoformat(),
                "ends_at": transit_end.isoformat(),
                "status": "reserved",
                "attrs": {
                    "material_flow_id": str(flow_id),
                    "items_count": len(items),
                },
            },
        )

    return {
        "flow_id": str(flow_id),
        "namespace_id": str(ns_id),
        "demand_kind": demand_kind,
        "demand_id": str(demand_id) if demand_id else None,
        "destination_location_id": str(dest_loc_id) if dest_loc_id else None,
        "van_resource": {
            "id": str(van_row["id"]),
            "display_name": van_row["display_name"],
            "stock_location_id": van_stock_location_id,
        },
        "stages": stages,
        "items": items,
        "van_allocation": van_allocation,
        "install_time": install_time.isoformat(),
        "status": "planned" if not van_allocation else "staged",
        "rationale": "Material flow coordinated: warehouse pick, van loading, and site delivery scheduled.",
    }
