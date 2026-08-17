"""
nce/vertical_modules/sales/mcp_handlers.py
============================================
MCP tool handlers for the Sales vertical module.

Phase 1a — skeleton only. No domain logic, no database writes, no external
systems. Later waves bolt ``do_*`` functions onto this spine.

Public entry-points:
  ``handle_sales_ping`` — liveness probe; verifies the namespace_id
  is present and returns a simple OK payload.
  ``handle_sales_get_signed_baseline`` — cross-engine read of the Sales-frozen
  signed baseline for a quote (the A2A seam that Project consumes).

Registered in ``nce/tool_registry.py`` via:
  ``_h(sales_mcp_handlers, "handle_sales_ping")``
  ``_h(sales_mcp_handlers, "handle_sales_get_signed_baseline")``
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any
from uuid import UUID

from nce.db_utils import scoped_pg_session
from nce.mcp_args import require_namespace_id
from nce.mcp_errors import mcp_handler
from nce.vertical_modules.sales.baseline import get_signed_baseline

if TYPE_CHECKING:
    from nce.orchestrator import NCEEngine

log = logging.getLogger("nce.vertical_modules.sales.mcp_handlers")


@mcp_handler
async def handle_sales_ping(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """MCP tool: sales_ping — liveness probe for the Sales vertical.

    Requires ``namespace_id`` in *arguments*.  Returns ``{"ok": true, "engine":
    "sales"}`` on success; the ``@mcp_handler`` decorator converts a
    missing-namespace ``ValueError`` into an ``McpError(-32602)`` at call-site.
    """
    require_namespace_id(arguments)
    return json.dumps({"ok": True, "engine": "sales"})


@mcp_handler
async def handle_sales_get_signed_baseline(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """MCP tool: sales_get_signed_baseline — read the Sales-frozen signed baseline.

    This is the cross-engine (A2A) read seam that Project's
    ``project.baseline._read_signed_baseline`` resolves to at runtime.  Sales
    owns and freezes ``SIGNED_BASELINE`` exactly once (see ``sales.signing``);
    every other engine reads it *only* through this tool.

    Arguments
    ---------
    namespace_id : str  (required)
    quote_id     : str  (required) — the Sales QUOTE identifier.

    Returns
    -------
    JSON body that is either the frozen baseline row, or ``null`` when Sales has
    no baseline for *quote_id* (Project degrades gracefully to
    ``sales_available: false`` — no fabrication)::

        {
            "id": str,
            "quote_id": str,
            "signed_margin_pct": float,   # 0-1
            "signed_total_nok": float,
            "signed_at": str              # ISO-8601
        }

    The ``@mcp_handler`` decorator maps a missing/invalid-argument ``ValueError``
    to an ``McpError(-32602)`` at the call-site.
    """
    ns = require_namespace_id(arguments)
    quote_id = arguments.get("quote_id")
    if not isinstance(quote_id, str) or not quote_id.strip():
        raise ValueError("quote_id is required")

    ns_uuid = UUID(ns)
    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        baseline = await get_signed_baseline(conn, ns_uuid, quote_id)

    # Contract: the JSON body IS the baseline row (or null). No wrapper —
    # Project's A2A seam parses this directly into ``dict | None``.
    return json.dumps(baseline)
