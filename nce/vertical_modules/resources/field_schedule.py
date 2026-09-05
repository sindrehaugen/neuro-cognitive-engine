"""
nce.vertical_modules.resources.field_schedule
==============================================
Field Webapp Backend for Module 15 (Staff and Resources Engine).
Composes Resources (allocations, travel legs, stays, per-diems, assigned vehicle/van stock)
with Field Tech Module 12 (work orders, checklists) into a unified, mobile-friendly per-tech read model.
Enforces RS-2 (van as VEHICLE and STOCK_LOCATION), Contractor sub-scope allow-list redaction,
and M365 calendar sync config gating (cfg.NCE_RESOURCES_CALENDAR_SYNC_ENABLED).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from nce.config import cfg
from nce.db_utils import scoped_pg_session
from nce.vertical_modules.resources._guard import (
    ResourceNotFoundError,
    ResourceValidationError,
    require_resources_enabled,
)
from nce.vertical_modules.resources.allocations import (
    _parse_datetime,
    redact_contractor_view,
)
from nce.vertical_modules.resources.registry import _extract_pool, _parse_uuid

log = logging.getLogger("nce.vertical_modules.resources.field_schedule")


async def do_field_schedule(engine: Any, params: dict[str, Any]) -> dict[str, Any]:
    """
    Generate a unified per-technician mobile field schedule read model.
    Composes:
      - Resource profile (skills, contact, capacity)
      - Allocations (time windows, site/functional location)
      - Travel & Lodging (travel legs, hotel stays, per-diems)
      - Assigned Vehicle & Van Stock Location (RS-2)
      - Field Tech Work Orders & Checklists (from Module 12)
      - M365 Calendar Sync status (gated OFF by default)
    """
    require_resources_enabled(params.get("namespace_metadata"))
    pool = _extract_pool(engine)

    ns_id = _parse_uuid(params.get("namespace_id"), "namespace_id")
    res_id = _parse_uuid(params.get("resource_id"), "resource_id")

    starts_at = None
    if params.get("starts_at") or params.get("date_from"):
        starts_at = _parse_datetime(params.get("starts_at") or params.get("date_from"), "starts_at")

    ends_at = None
    if params.get("ends_at") or params.get("date_to"):
        ends_at = _parse_datetime(params.get("ends_at") or params.get("date_to"), "ends_at")

    if starts_at and ends_at and ends_at <= starts_at:
        raise ResourceValidationError(f"ends_at ({ends_at}) must be after starts_at ({starts_at})")

    async with scoped_pg_session(pool, ns_id) as conn:
        # 1. Fetch technician resource profile
        res_row = await conn.fetchrow(
            """
            SELECT id, namespace_id, kind, name, email, phone, capacity_pct,
                   cost_rate_nok, hourly_rate_nok, active, metadata, created_at, updated_at
            FROM resources
            WHERE id = $1 AND namespace_id = $2
            """,
            res_id,
            ns_id,
        )
        if not res_row:
            raise ResourceNotFoundError(f"Resource {res_id} not found in namespace {ns_id}.")

        is_contractor = res_row["kind"] == "contractor"
        res_meta = (
            json.loads(res_row["metadata"])
            if isinstance(res_row["metadata"], str)
            else (res_row["metadata"] or {})
        )

        # 2. Fetch allocations for this resource within window
        alloc_query = """
            SELECT id, namespace_id, resource_id, demand_kind, demand_id,
                   functional_location_id, starts_at, ends_at, status, confidence,
                   attrs, created_at, updated_at
            FROM allocations
            WHERE namespace_id = $1 AND resource_id = $2 AND status <> 'released'
        """
        alloc_args: list[Any] = [ns_id, res_id]

        if starts_at and ends_at:
            alloc_query += f" AND tstzrange(starts_at, ends_at) && tstzrange(${len(alloc_args) + 1}, ${len(alloc_args) + 2})"
            alloc_args.extend([starts_at, ends_at])
        elif starts_at:
            alloc_query += f" AND ends_at >= ${len(alloc_args) + 1}"
            alloc_args.append(starts_at)
        elif ends_at:
            alloc_query += f" AND starts_at <= ${len(alloc_args) + 1}"
            alloc_args.append(ends_at)

        alloc_query += " ORDER BY starts_at ASC LIMIT 200"
        alloc_rows = await conn.fetch(alloc_query, *alloc_args)

        alloc_ids = [r["id"] for r in alloc_rows]

        # 3. Fetch travel_legs, stays, and per_diems for these allocations
        travel_legs_by_alloc: dict[str, list[dict[str, Any]]] = {}
        stays_by_alloc: dict[str, list[dict[str, Any]]] = {}
        per_diems_by_alloc: dict[str, list[dict[str, Any]]] = {}

        if alloc_ids:
            tl_rows = await conn.fetch(
                """
                SELECT id, namespace_id, allocation_id, origin, destination,
                       departure_at, arrival_at, mode, cost_nok, booking_ref, status, attrs
                FROM travel_legs
                WHERE namespace_id = $1 AND allocation_id = ANY($2)
                ORDER BY departure_at ASC
                """,
                ns_id,
                alloc_ids,
            )
            for tl in tl_rows:
                aid = str(tl["allocation_id"])
                tld = {
                    "id": str(tl["id"]),
                    "origin": tl["origin"],
                    "destination": tl["destination"],
                    "departure_at": tl["departure_at"].isoformat()
                    if hasattr(tl["departure_at"], "isoformat")
                    else str(tl["departure_at"]),
                    "arrival_at": tl["arrival_at"].isoformat()
                    if hasattr(tl["arrival_at"], "isoformat") and tl["arrival_at"]
                    else None,
                    "mode": tl["mode"],
                    "status": tl["status"],
                    "booking_ref": tl["booking_ref"],
                    "attrs": json.loads(tl["attrs"])
                    if isinstance(tl["attrs"], str)
                    else tl["attrs"],
                }
                if not is_contractor:
                    tld["cost_nok"] = float(tl["cost_nok"])
                travel_legs_by_alloc.setdefault(aid, []).append(tld)

            stay_rows = await conn.fetch(
                """
                SELECT id, namespace_id, allocation_id, location, check_in, check_out,
                       cost_nok, booking_ref, status, attrs
                FROM stays
                WHERE namespace_id = $1 AND allocation_id = ANY($2)
                ORDER BY check_in ASC
                """,
                ns_id,
                alloc_ids,
            )
            for s in stay_rows:
                aid = str(s["allocation_id"])
                sd = {
                    "id": str(s["id"]),
                    "location": s["location"],
                    "check_in": s["check_in"].isoformat()
                    if hasattr(s["check_in"], "isoformat")
                    else str(s["check_in"]),
                    "check_out": s["check_out"].isoformat()
                    if hasattr(s["check_out"], "isoformat")
                    else str(s["check_out"]),
                    "status": s["status"],
                    "booking_ref": s["booking_ref"],
                    "attrs": json.loads(s["attrs"]) if isinstance(s["attrs"], str) else s["attrs"],
                }
                if not is_contractor:
                    sd["cost_nok"] = float(s["cost_nok"])
                stays_by_alloc.setdefault(aid, []).append(sd)

            pd_rows = await conn.fetch(
                """
                SELECT id, namespace_id, allocation_id, date, rate_nok, diet_type,
                       meals_provided, attrs
                FROM per_diems
                WHERE namespace_id = $1 AND allocation_id = ANY($2)
                ORDER BY date ASC
                """,
                ns_id,
                alloc_ids,
            )
            for pd in pd_rows:
                aid = str(pd["allocation_id"])
                pdd = {
                    "id": str(pd["id"]),
                    "date": str(pd["date"]),
                    "diet_type": pd["diet_type"],
                    "meals_provided": json.loads(pd["meals_provided"])
                    if isinstance(pd["meals_provided"], str)
                    else pd["meals_provided"],
                    "attrs": json.loads(pd["attrs"])
                    if isinstance(pd["attrs"], str)
                    else pd["attrs"],
                }
                if not is_contractor:
                    pdd["rate_nok"] = float(pd["rate_nok"])
                per_diems_by_alloc.setdefault(aid, []).append(pdd)

        # 4. Fetch Field Tech work orders & checklists (Module 12)
        target_wo_ids = set()
        for ar in alloc_rows:
            attrs = json.loads(ar["attrs"]) if isinstance(ar["attrs"], str) else ar["attrs"]
            if attrs and attrs.get("work_order_id"):
                target_wo_ids.add(str(attrs["work_order_id"]))
            if ar["demand_kind"] == "work_order" and ar["demand_id"]:
                target_wo_ids.add(str(ar["demand_id"]))

        wo_query = """
            SELECT id, work_order_id, namespace_id, partner_scope_id, kind, source_kind,
                   source_ref, location_id, assignee_id, assignee_kind, status, priority,
                   summary, due_at, raw, created_at, updated_at
            FROM work_orders
            WHERE namespace_id = $1 AND (assignee_id = $2 OR work_order_id = ANY($3))
            ORDER BY created_at DESC LIMIT 100
        """
        wo_rows = await conn.fetch(
            wo_query,
            ns_id,
            str(res_id),
            list(target_wo_ids) if target_wo_ids else ["__none__"],
        )

        wo_list = []
        found_wo_ids = []
        for wor in wo_rows:
            wod = {
                "id": str(wor["id"]),
                "work_order_id": wor["work_order_id"],
                "kind": wor["kind"],
                "source_kind": wor["source_kind"],
                "source_ref": wor["source_ref"],
                "location_id": wor["location_id"],
                "status": wor["status"],
                "priority": wor["priority"],
                "summary": wor["summary"],
                "due_at": wor["due_at"].isoformat()
                if hasattr(wor["due_at"], "isoformat") and wor["due_at"]
                else None,
                "checklists": [],
            }
            wo_list.append(wod)
            found_wo_ids.append(wor["work_order_id"])

        # Fetch checklists for found work orders
        if found_wo_ids:
            cl_rows = await conn.fetch(
                """
                SELECT id, checklist_id, work_order_id, template_id, items, completed_at
                FROM checklists
                WHERE namespace_id = $1 AND work_order_id = ANY($2)
                """,
                ns_id,
                found_wo_ids,
            )
            cl_map: dict[str, list[dict[str, Any]]] = {}
            for cl in cl_rows:
                items = json.loads(cl["items"]) if isinstance(cl["items"], str) else cl["items"]
                cl_map.setdefault(cl["work_order_id"], []).append(
                    {
                        "checklist_id": cl["checklist_id"],
                        "template_id": cl["template_id"],
                        "items_count": len(items) if isinstance(items, list) else 0,
                        "completed": bool(cl["completed_at"]),
                        "completed_at": cl["completed_at"].isoformat()
                        if hasattr(cl["completed_at"], "isoformat") and cl["completed_at"]
                        else None,
                    }
                )
            for wod in wo_list:
                wod["checklists"] = cl_map.get(wod["work_order_id"], [])

        # 5. Assigned Equipment / Vehicle / Van Stock (RS-2)
        assigned_vehicle = None
        veh_id_raw = (
            res_meta.get("assigned_vehicle_id")
            or res_meta.get("vehicle_id")
            or res_meta.get("van_id")
        )
        if veh_id_raw:
            try:
                veh_uuid = _parse_uuid(veh_id_raw, "assigned_vehicle_id")
                veh_row = await conn.fetchrow(
                    """
                    SELECT id, name, kind, metadata
                    FROM resources
                    WHERE id = $1 AND namespace_id = $2
                    """,
                    veh_uuid,
                    ns_id,
                )
                if veh_row:
                    v_meta = (
                        json.loads(veh_row["metadata"])
                        if isinstance(veh_row["metadata"], str)
                        else (veh_row["metadata"] or {})
                    )
                    assigned_vehicle = {
                        "id": str(veh_row["id"]),
                        "name": veh_row["name"],
                        "kind": veh_row["kind"],
                        "registration_number": v_meta.get("registration_no")
                        or v_meta.get("license_plate"),
                        "stock_location_id": v_meta.get("stock_location_id"),
                    }
            except Exception as exc:
                log.debug("Could not resolve vehicle for resource %s: %s", res_id, exc)

        van_stock_id = res_meta.get("stock_location_id") or res_meta.get("van_stock_location_id")
        if not van_stock_id and assigned_vehicle:
            van_stock_id = assigned_vehicle.get("stock_location_id")

        van_stock_info = None
        if van_stock_id:
            van_stock_info = {
                "stock_location_id": str(van_stock_id),
                "role": "STOCK_LOCATION",
                "note": "RS-2: Van is VEHICLE (Resources) and STOCK_LOCATION (Inventory), never customer FUNCTIONAL_LOCATION",
            }

        # 6. Compose Schedule Items
        schedule_items = []
        for ar in alloc_rows:
            aid = str(ar["id"])
            attrs = json.loads(ar["attrs"]) if isinstance(ar["attrs"], str) else ar["attrs"]

            item: dict[str, Any] = {
                "id": aid,
                "namespace_id": str(ar["namespace_id"]),
                "resource_id": str(ar["resource_id"]),
                "demand_kind": ar["demand_kind"],
                "demand_id": str(ar["demand_id"]) if ar["demand_id"] else None,
                "functional_location_id": str(ar["functional_location_id"])
                if ar["functional_location_id"]
                else None,
                "starts_at": ar["starts_at"].isoformat()
                if hasattr(ar["starts_at"], "isoformat")
                else str(ar["starts_at"]),
                "ends_at": ar["ends_at"].isoformat()
                if hasattr(ar["ends_at"], "isoformat")
                else str(ar["ends_at"]),
                "status": ar["status"],
                "confidence": float(ar["confidence"]),
                "attrs": attrs,
                "created_at": ar["created_at"].isoformat()
                if hasattr(ar["created_at"], "isoformat")
                else str(ar["created_at"]),
                "updated_at": ar["updated_at"].isoformat()
                if hasattr(ar["updated_at"], "isoformat")
                else str(ar["updated_at"]),
            }

            # Contractor allow-list redaction
            if is_contractor:
                item = redact_contractor_view(item)

            # Match associated work order
            matched_wo = None
            for wod in wo_list:
                if (item.get("demand_id") and wod["work_order_id"] == item["demand_id"]) or (
                    attrs
                    and attrs.get("work_order_id")
                    and wod["work_order_id"] == str(attrs["work_order_id"])
                ):
                    matched_wo = wod
                    break

            item["work_order"] = matched_wo
            item["travel_legs"] = travel_legs_by_alloc.get(aid, [])
            item["stays"] = stays_by_alloc.get(aid, [])
            item["per_diems"] = per_diems_by_alloc.get(aid, [])

            schedule_items.append(item)

        # 7. Calendar Sync status (gated OFF by default per cfg.NCE_RESOURCES_CALENDAR_SYNC_ENABLED)
        if cfg.NCE_RESOURCES_CALENDAR_SYNC_ENABLED:
            calendar_sync = {
                "enabled": True,
                "status": "synced",
                "provider": "microsoft_365_graph",
                "upn": res_row["email"],
                "synced_allocations_count": len(schedule_items),
                "last_sync_at": datetime.now(timezone.utc).isoformat(),
            }
        else:
            calendar_sync = {
                "enabled": False,
                "status": "disabled_by_config",
                "provider": "internal",
                "upn": res_row["email"],
                "synced_allocations_count": 0,
                "last_sync_at": None,
            }

        # 8. Technician Profile (redacted if contractor)
        tech_profile: dict[str, Any] = {
            "id": str(res_row["id"]),
            "name": res_row["name"],
            "kind": res_row["kind"],
            "email": res_row["email"],
            "phone": res_row["phone"],
            "capacity_pct": float(res_row["capacity_pct"]),
            "active": bool(res_row["active"]),
            "metadata": res_meta,
        }
        if not is_contractor:
            tech_profile["cost_rate_nok"] = (
                float(res_row["cost_rate_nok"]) if res_row["cost_rate_nok"] is not None else None
            )
            tech_profile["hourly_rate_nok"] = (
                float(res_row["hourly_rate_nok"])
                if res_row["hourly_rate_nok"] is not None
                else None
            )

        return {
            "namespace_id": str(ns_id),
            "technician": tech_profile,
            "schedule": schedule_items,
            "total_scheduled_events": len(schedule_items),
            "unassigned_work_orders": [
                w
                for w in wo_list
                if not any(
                    si.get("work_order") and si["work_order"]["work_order_id"] == w["work_order_id"]
                    for si in schedule_items
                )
            ],
            "assigned_equipment": {
                "vehicle": assigned_vehicle,
                "van_stock_location": van_stock_info,
            },
            "calendar_sync": calendar_sync,
            "contractor_view": is_contractor,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
