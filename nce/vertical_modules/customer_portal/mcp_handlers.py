"""nce/vertical_modules/customer_portal/mcp_handlers.py
======================================================
MCP tool handlers for Module 17: Customer Portal Engine (ML17-B5).

The 9 MCP Tools:
  1. customer_portal_room_tracker             — Advisor (customer-scoped); read-only, cacheable
  2. customer_portal_room_overview            — Read-projection; read-only, cacheable
  3. customer_portal_asset_register           — Read-projection; read-only, cacheable
  4. customer_portal_list_documents           — Read-projection; read-only, cacheable
  5. customer_portal_sla_status               — Watcher (customer-scoped); read-only, cacheable
  6. customer_portal_list_invoices            — Read-projection; read-only, cacheable
  7. customer_portal_advisor_answer           — Advisor (sandboxed, customer-scoped); read-only
  8. customer_portal_raise_service_request    — Actor (hand-off -> Support); mutation
  9. customer_portal_register_expansion_interest — Actor (hand-off -> Sales); mutation
"""

from __future__ import annotations

import json
import logging
from typing import Any

from nce.auth import resolve_customer_scope
from nce.mcp_args import require_namespace_id
from nce.mcp_errors import mcp_handler
from nce.vertical_modules.customer_portal.actions import (
    do_raise_service_request,
    do_register_expansion_interest,
)
from nce.vertical_modules.customer_portal.advisor import do_advisor_answer
from nce.vertical_modules.customer_portal.documents import do_list_documents
from nce.vertical_modules.customer_portal.invoices import do_list_invoices
from nce.vertical_modules.customer_portal.rooms import (
    do_asset_register,
    do_room_overview,
    do_room_tracker,
)
from nce.vertical_modules.customer_portal.sla import do_sla_status

log = logging.getLogger("nce.vertical_modules.customer_portal.mcp_handlers")


async def _resolve_portal_mcp_scope(engine: Any, params: dict[str, Any]) -> str:
    """Resolve, validate, and audit customer_scope_id for internal MCP operator surface.

    Raises ValueError if customer_scope_id is missing, nil-UUID, or invalid.
    Emits WORM `customer_scope_impersonated` audit event.
    """
    require_namespace_id(params)
    scope = await resolve_customer_scope(
        None,
        params,
        namespace_id=params.get("namespace_id"),
        engine=engine,
        reason="mcp_customer_portal_operator",
    )
    if not scope:
        raise ValueError("customer_scope_id is required for customer_portal MCP operations")
    params["customer_scope_id"] = scope
    return scope


@mcp_handler
async def handle_customer_portal_room_tracker(
    engine: Any,
    params: dict[str, Any],
) -> str:
    """Project Domino's tracker progression and room status safely."""
    await _resolve_portal_mcp_scope(engine, params)
    result = await do_room_tracker(engine, params)
    return json.dumps(result, default=str)


@mcp_handler
async def handle_customer_portal_room_overview(
    engine: Any,
    params: dict[str, Any],
) -> str:
    """Project overall customer rooms readiness rollup."""
    await _resolve_portal_mcp_scope(engine, params)
    result = await do_room_overview(engine, params)
    return json.dumps(result, default=str)


@mcp_handler
async def handle_customer_portal_asset_register(
    engine: Any,
    params: dict[str, Any],
) -> str:
    """Project room-centric asset register with commercial values redacted."""
    await _resolve_portal_mcp_scope(engine, params)
    result = await do_asset_register(engine, params)
    return json.dumps(result, default=str)


@mcp_handler
async def handle_customer_portal_list_documents(
    engine: Any,
    params: dict[str, Any],
) -> str:
    """List granted, unexpired, and unrevoked documents for customer scope."""
    await _resolve_portal_mcp_scope(engine, params)
    result = await do_list_documents(engine, params)
    return json.dumps(result, default=str)


@mcp_handler
async def handle_customer_portal_sla_status(
    engine: Any,
    params: dict[str, Any],
) -> str:
    """Project SLA self-service clock and tier status without internal MRR/costs."""
    await _resolve_portal_mcp_scope(engine, params)
    result = await do_sla_status(engine, params)
    return json.dumps(result, default=str)


@mcp_handler
async def handle_customer_portal_list_invoices(
    engine: Any,
    params: dict[str, Any],
) -> str:
    """List customer invoices with margin, internal cost, and rebate stripped."""
    await _resolve_portal_mcp_scope(engine, params)
    result = await do_list_invoices(engine, params)
    return json.dumps(result, default=str)


@mcp_handler
async def handle_customer_portal_advisor_answer(
    engine: Any,
    params: dict[str, Any],
) -> str:
    """Sandboxed AI customer advisor answering room progress and intake inquiries."""
    await _resolve_portal_mcp_scope(engine, params)
    result = await do_advisor_answer(engine, params)
    return json.dumps(result, default=str)


@mcp_handler
async def handle_customer_portal_raise_service_request(
    engine: Any,
    params: dict[str, Any],
) -> str:
    """Inbound customer service request intake with Contract-B gating and Support hand-off."""
    await _resolve_portal_mcp_scope(engine, params)
    result = await do_raise_service_request(engine, params)
    return json.dumps(result, default=str)


@mcp_handler
async def handle_customer_portal_register_expansion_interest(
    engine: Any,
    params: dict[str, Any],
) -> str:
    """Inbound customer re-buy or expansion interest hand-off to Sales lead queue."""
    await _resolve_portal_mcp_scope(engine, params)
    result = await do_register_expansion_interest(engine, params)
    return json.dumps(result, default=str)
