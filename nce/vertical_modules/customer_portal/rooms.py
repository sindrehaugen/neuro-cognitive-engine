"""nce/vertical_modules/customer_portal/rooms.py
=============================================
Room-centric Domino's tracker, overview rollups, and asset register read projections.
Charter Phase 2: Reads over FUNCTIONAL_LOCATION, BOM_LINE.status, and ASSET lifecycle.

Enforces:
  1. Customer-scope principal boundary (IDOR denial on mismatched scope).
  2. Four messy-reality cases (delay, change order, partial delivery, frozen).
  3. Strict allow-list redaction (customer-redaction.json) eliminating margin, cost, and slip.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from nce.vertical_modules.customer_portal.auth import evaluate_customer_scope_access
from nce.vertical_modules.customer_portal.redaction import project_customer_safe

_CONFIG_PATH = Path(__file__).parent / "room-tracker-stages.json"


def load_room_tracker_stages() -> dict[str, Any]:
    """Load the Domino's stage mapping and messy reality configuration."""
    with open(_CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def compute_room_stage_and_progress(
    bom_lines: list[dict[str, Any]],
    assets: list[dict[str, Any]],
    messy_context: dict[str, Any] | None = None,
) -> tuple[str, int, str, str]:
    """Compute Domino's room stage, percent ready, customer status display, and narrative.

    Returns:
        tuple[stage_id, percent_ready, status_display, narrative]
    """
    config = load_room_tracker_stages()
    stages = {s["id"]: s for s in config["stages"]}
    stage_order = [s["id"] for s in config["stages"]]
    bom_map = config["bom_status_to_stage"]
    asset_map = config["asset_lifecycle_to_stage"]

    stage_weights = [0]

    for line in bom_lines:
        status = str(line.get("status", "planned")).lower()
        stg_id = bom_map.get(status, "planned")
        stg_cfg = stages.get(stg_id, stages["planned"])
        stage_weights.append(stg_cfg["weight_pct"])

    for asset in assets:
        lifecycle = str(asset.get("lifecycle", asset.get("status", "planned"))).lower()
        stg_id = asset_map.get(lifecycle, "planned")
        stg_cfg = stages.get(stg_id, stages["planned"])
        stage_weights.append(stg_cfg["weight_pct"])

    # Calculate average weight
    if len(stage_weights) > 1:
        avg_weight = sum(stage_weights[1:]) / (len(stage_weights) - 1)
    else:
        avg_weight = 10.0

    percent_ready = int(round(avg_weight))

    # Derive representative stage
    matched_stage_id = stage_order[0]
    for stg_id in stage_order:
        if percent_ready >= stages[stg_id]["weight_pct"]:
            matched_stage_id = stg_id

    stage_display = stages[matched_stage_id]["display_name"]
    narrative = stages[matched_stage_id]["description"]

    # Apply Messy Reality Overrides (Charter §4)
    if messy_context and "case" in messy_context:
        case = messy_context["case"]
        messy_rules = config.get("messy_reality_rules", {})
        if case in messy_rules:
            rule = messy_rules[case]
            stage_display = rule.get("customer_safe_status", stage_display)
            narrative = rule.get("customer_safe_narrative", narrative)

    return matched_stage_id, percent_ready, stage_display, narrative


async def do_room_tracker(engine: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Project Domino's tracker state for a given room.

    Enforces customer principal scope and filters through room_tracker allow-list.
    """
    cust_scope = params.get("customer_scope_id")
    target_scope = params.get("target_scope_id", cust_scope)
    if not evaluate_customer_scope_access(cust_scope, target_scope):
        raise PermissionError(
            f"IDOR attempt: scope {cust_scope} denied access to scope {target_scope}"
        )

    room_id = params.get("room_id", "")
    room_name = params.get("room_name", f"Room {room_id}")
    site_id = params.get("site_id", "")
    site_name = params.get("site_name", f"Site {site_id}")

    bom_lines = params.get("bom_lines", [])
    assets = params.get("assets", [])
    messy_context = params.get("messy_context")

    stage_id, percent_ready, status_display, narrative = compute_room_stage_and_progress(
        bom_lines=bom_lines,
        assets=assets,
        messy_context=messy_context,
    )

    raw_response = {
        "room_id": room_id,
        "room_name": room_name,
        "site_id": site_id,
        "site_name": site_name,
        "stage": stage_id,
        "percent_ready": percent_ready,
        "status": status_display,
        "summary": narrative,
        "last_updated_at": params.get("last_updated_at", "2026-09-05T20:00:00Z"),
        "estimated_readiness": params.get("estimated_readiness", "2026-10-15"),
        # Include raw params for allow-list redaction verification
        **{k: v for k, v in params.items() if k not in ("bom_lines", "assets", "messy_context")},
    }

    return project_customer_safe(raw_response, "room_tracker")


async def do_room_overview(engine: Any, params: dict[str, Any]) -> dict[str, Any]:
    cust_scope = params.get("customer_scope_id")
    target_scope = params.get("target_scope_id", cust_scope)
    if not evaluate_customer_scope_access(cust_scope, target_scope):
        raise PermissionError(
            f"IDOR attempt: scope {cust_scope} denied access to scope {target_scope}"
        )

    site_id = params.get("site_id", "")
    site_name = params.get("site_name", "")
    rooms_input = params.get("rooms", [])

    projected_rooms = []
    total_pct = 0

    for r in rooms_input:
        r_params = {
            "namespace_id": params.get("namespace_id"),
            "customer_scope_id": cust_scope,
            "site_id": site_id,
            "site_name": site_name,
            **r,
        }
        room_proj = await do_room_tracker(engine, r_params)
        projected_rooms.append(room_proj)
        total_pct += room_proj.get("percent_ready", 0)

    total_rooms = len(projected_rooms)
    overall_pct = int(round(total_pct / total_rooms)) if total_rooms > 0 else 0

    raw_overview = {
        "site_id": site_id,
        "site_name": site_name,
        "total_rooms": total_rooms,
        "overall_percent_ready": overall_pct,
        "rooms": projected_rooms,
        **{k: v for k, v in params.items() if k not in ("rooms",)},
    }

    return project_customer_safe(raw_overview, "room_overview")


async def do_asset_register(engine: Any, params: dict[str, Any]) -> dict[str, Any]:
    cust_scope = params.get("customer_scope_id")
    target_scope = params.get("target_scope_id", cust_scope)
    if not evaluate_customer_scope_access(cust_scope, target_scope):
        raise PermissionError(
            f"IDOR attempt: scope {cust_scope} denied access to scope {target_scope}"
        )

    room_id = params.get("room_id", "")
    raw_assets = params.get("assets", [])

    projected_assets = [project_customer_safe(asset, "asset_register") for asset in raw_assets]

    return {
        "room_id": room_id,
        "customer_scope_id": str(cust_scope),
        "total_assets": len(projected_assets),
        "assets": projected_assets,
    }
