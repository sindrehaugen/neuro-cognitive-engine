"""
nce/vertical_modules/vendors/mcp_handlers.py
============================================
MCP tool handlers for the Vendors vertical module (M4.W3).
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from nce.mcp_args import require_namespace_id
from nce.mcp_errors import mcp_handler
from nce.vertical_modules.vendors.feed import (
    do_check_tier_at_risk,
    do_detect_reliability_degradation,
)
from nce.vertical_modules.vendors.frontier import do_calibrate_weights, do_reliability_radar
from nce.vertical_modules.vendors.matching import do_match_contractor
from nce.vertical_modules.vendors.performance import (
    do_compute_performance,
    do_recall_similar_jobs,
)
from nce.vertical_modules.vendors.registry import do_get_vendor
from nce.vertical_modules.vendors.scorecard import do_compute_scorecard
from nce.vertical_modules.vendors.tiers import do_get_tier_status

if TYPE_CHECKING:
    from nce.orchestrator import NCEEngine

log = logging.getLogger("nce.vertical_modules.vendors.mcp_handlers")


@mcp_handler
async def handle_vendors_get_vendor(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """MCP tool: vendors_get_vendor — fetch a single vendor.

    Requires ``namespace_id`` and ``vendor_id`` in *arguments*.
    """
    require_namespace_id(arguments)
    result = await do_get_vendor(engine, arguments)
    return json.dumps(result, default=str)


@mcp_handler
async def handle_vendors_compute_scorecard(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """MCP tool: vendors_compute_scorecard — compute scorecard for a vendor.

    Requires ``namespace_id`` and ``vendor_id`` in *arguments*.
    """
    require_namespace_id(arguments)
    result = await do_compute_scorecard(engine, arguments)
    return json.dumps(result, default=str)


@mcp_handler
async def handle_vendors_get_tier_status(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """MCP tool: vendors_get_tier_status — get current kickback tier status for a vendor.

    Requires ``namespace_id`` and ``vendor_id`` in *arguments*.
    """
    require_namespace_id(arguments)
    result = await do_get_tier_status(engine, arguments)
    return json.dumps(result, default=str)


@mcp_handler
async def handle_vendors_detect_reliability_degradation(
    engine: NCEEngine, arguments: dict[str, Any]
) -> str:
    """MCP tool: vendors_detect_reliability_degradation — detect reliability degradation for a vendor.

    Requires ``namespace_id`` and ``vendor_id`` in *arguments*.
    """
    require_namespace_id(arguments)
    result = await do_detect_reliability_degradation(engine, arguments)
    return json.dumps(result, default=str)


@mcp_handler
async def handle_vendors_check_tier_at_risk(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """MCP tool: vendors_check_tier_at_risk — check if kickback tier is at risk for a vendor.

    Requires ``namespace_id`` and ``vendor_id`` in *arguments*.
    """
    require_namespace_id(arguments)
    result = await do_check_tier_at_risk(engine, arguments)
    return json.dumps(result, default=str)


@mcp_handler
async def handle_vendors_match_contractor(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """MCP tool: vendors_match_contractor — match and rank contractors for a job.

    Requires ``namespace_id`` and ``job`` in *arguments*.
    """
    require_namespace_id(arguments)
    job = arguments.get("job")
    if not isinstance(job, dict):
        raise ValueError("job parameter must be a dictionary")
    result = await do_match_contractor(engine, arguments)
    return json.dumps(result, default=str)


@mcp_handler
async def handle_vendors_compute_performance(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """MCP tool: vendors_compute_performance — compute performance score for a contractor.

    Requires ``namespace_id`` and ``contractor_id`` in *arguments*.
    """
    require_namespace_id(arguments)
    result = await do_compute_performance(engine, arguments)
    return json.dumps(result, default=str)


@mcp_handler
async def handle_vendors_recall_similar_jobs(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """MCP tool: vendors_recall_similar_jobs — recall similar contractor jobs.

    Requires ``namespace_id`` and ``query`` in *arguments*.
    """
    require_namespace_id(arguments)
    result = await do_recall_similar_jobs(engine, arguments)
    return json.dumps(result, default=str)


@mcp_handler
async def handle_vendors_reliability_radar(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """MCP tool: vendors_reliability_radar — analyze supplier-risk and contractor-burnout signals.

    Requires ``namespace_id`` in *arguments*.
    """
    require_namespace_id(arguments)
    result = await do_reliability_radar(engine, arguments)
    return json.dumps(result, default=str)


@mcp_handler
async def handle_vendors_calibrate_weights(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """MCP tool: vendors_calibrate_weights — recalibrate vendor scorecard weights dynamically.

    Requires ``namespace_id`` in *arguments*.
    """
    require_namespace_id(arguments)
    result = await do_calibrate_weights(engine, arguments)
    return json.dumps(result, default=str)
