"""
nce/vertical_modules/resources/mcp_handlers.py
==============================================
MCP Tool Handlers for Module 15: Staff & Resources Engine (ML15-B7).

The 9 MCP Tools:
  1. resources_resolve_capacity  — Watcher/Advisor; read-only, cacheable
  2. resources_plan_allocation   — Advisor; read-only
  3. resources_detect_conflicts  — Watcher; read-only, cacheable
  4. resources_forecast_demand   — Advisor; read-only, cacheable
  5. resources_field_schedule    — Read-model; read-only, cacheable
  6. resources_reserve           — Actor; mutation
  7. resources_release           — Actor; mutation
  8. resources_plan_material_flow — Actor; mutation, admin_only
  9. resources_plan_travel       — Actor; mutation
"""

from __future__ import annotations

import json
import logging
from typing import Any

from nce.mcp_args import require_namespace_id
from nce.mcp_errors import MCP_SCOPE_FORBIDDEN, McpError, mcp_handler
from nce.vertical_modules.resources._guard import (
    ResourcesDisabledError,
    require_resources_enabled,
)
from nce.vertical_modules.resources.allocations import (
    do_detect_conflicts,
    do_release,
    do_reserve,
)
from nce.vertical_modules.resources.capacity import do_resolve_capacity
from nce.vertical_modules.resources.field_schedule import do_field_schedule
from nce.vertical_modules.resources.forecast import do_forecast_demand
from nce.vertical_modules.resources.material_flow import do_plan_material_flow
from nce.vertical_modules.resources.planner import do_plan_allocation
from nce.vertical_modules.resources.travel import do_plan_travel

log = logging.getLogger("nce.vertical_modules.resources.mcp_handlers")


def _check_resources_enabled(params: dict[str, Any]) -> None:
    try:
        require_resources_enabled(params.get("namespace_metadata"))
    except ResourcesDisabledError as exc:
        raise McpError(MCP_SCOPE_FORBIDDEN, str(exc)) from exc


@mcp_handler
async def handle_resources_resolve_capacity(
    engine: Any,
    params: dict[str, Any],
) -> str:
    """Resolve capacity calendar and utilization for resources in a namespace."""
    require_namespace_id(params)
    _check_resources_enabled(params)
    result = await do_resolve_capacity(engine, params)
    return json.dumps(result, default=str)


@mcp_handler
async def handle_resources_plan_allocation(
    engine: Any,
    params: dict[str, Any],
) -> str:
    """AI allocation advisor: multi-objective skill matching & cognitive recall from ledger."""
    require_namespace_id(params)
    _check_resources_enabled(params)
    result = await do_plan_allocation(engine, params)
    return json.dumps(result, default=str)


@mcp_handler
async def handle_resources_detect_conflicts(
    engine: Any,
    params: dict[str, Any],
) -> str:
    """Detect overlapping double-bookings and schedule clashes across resources."""
    require_namespace_id(params)
    _check_resources_enabled(params)
    result = await do_detect_conflicts(engine, params)
    return json.dumps(result, default=str)


@mcp_handler
async def handle_resources_forecast_demand(
    engine: Any,
    params: dict[str, Any],
) -> str:
    """Forecast staff & resource demand vs capacity across planning horizons; hire/contractor signals."""
    require_namespace_id(params)
    _check_resources_enabled(params)
    result = await do_forecast_demand(engine, params)
    return json.dumps(result, default=str)


@mcp_handler
async def handle_resources_field_schedule(
    engine: Any,
    params: dict[str, Any],
) -> str:
    """Field webapp mobile read model: composed technician schedule, travel, lodging, van stock, and work orders."""
    require_namespace_id(params)
    _check_resources_enabled(params)
    result = await do_field_schedule(engine, params)
    return json.dumps(result, default=str)


@mcp_handler
async def handle_resources_reserve(
    engine: Any,
    params: dict[str, Any],
) -> str:
    """Reserve time window for a resource; DB-enforced against double-booking via btree_gist (RS-3)."""
    require_namespace_id(params)
    _check_resources_enabled(params)
    result = await do_reserve(engine, params)
    return json.dumps(result, default=str)


@mcp_handler
async def handle_resources_release(
    engine: Any,
    params: dict[str, Any],
) -> str:
    """Release an active resource allocation, freeing capacity and lifting exclusion."""
    require_namespace_id(params)
    _check_resources_enabled(params)
    result = await do_release(engine, params)
    return json.dumps(result, default=str)


@mcp_handler
async def handle_resources_plan_material_flow(
    engine: Any,
    params: dict[str, Any],
) -> str:
    """Coordinate material staging: warehouse pick, van loading (RS-2), transport, and delivery."""
    require_namespace_id(params)
    _check_resources_enabled(params)
    result = await do_plan_material_flow(engine, params)
    return json.dumps(result, default=str)


@mcp_handler
async def handle_resources_plan_travel(
    engine: Any,
    params: dict[str, Any],
) -> str:
    """Plan or book technician travel & lodging behind Contract-B spend gate (RS-5) with Norwegian diett."""
    require_namespace_id(params)
    _check_resources_enabled(params)
    result = await do_plan_travel(engine, params)
    return json.dumps(result, default=str)
