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
  ``handle_sales_add_quote_line`` — the MANUAL-PICK origination path for
  BOM_LINE (Batch 132d); delegates to ``sales.lines.do_add_quote_line``.
  ``handle_sales_get_quote_lines`` — the cross-engine READ of a quote's
  BOM_LINE rows (Batch 132f); the seam System Design's from_quote flow uses.

Registered in ``nce/tool_registry.py`` via:
  ``_h(sales_mcp_handlers, "handle_sales_ping")``
  ``_h(sales_mcp_handlers, "handle_sales_get_signed_baseline")``
  ``_h(sales_mcp_handlers, "handle_sales_add_quote_line")``
  ``_h(sales_mcp_handlers, "handle_sales_get_quote_lines")``
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
from nce.vertical_modules.sales.lines import do_add_quote_line, do_get_quote_lines

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


@mcp_handler
async def handle_sales_add_quote_line(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """MCP tool: sales_add_quote_line — add one manually picked line to a quote.

    The manual-pick origination path for BOM_LINE: a human picks an article and
    it becomes exactly one row, written through the sales-owned
    ``content:create:manual`` transition. Delegates every decision to
    ``nce.vertical_modules.sales.lines.do_add_quote_line`` — this handler is
    argument extraction and nothing else.

    Provenance is NOT caller-writable. There is no origin_kind argument; a key
    of that name in *arguments* is never read and cannot reach the store. The
    writer module's own flow-to-origin mapping is the only producer.

    Idempotent on (quote_id, line_ref): replaying the same pick returns the
    existing row instead of creating a second one.

    Arguments
    ---------
    namespace_id (str): Required. Caller namespace UUID.
    quote_id (str): Required. The Sales QUOTE identifier.
    line_ref (str): Required. Line reference, unique within the quote.
    qty (str): Required. Quantity. A decimal string keeps NUMERIC exact.
    unit_price (str): Required. Unit price. A decimal string keeps NUMERIC exact.
    line_total (str): Optional. Defaults to qty multiplied by unit_price.
    currency (str): Optional. ISO-4217 code; defaults to NOK.
    origin_ref (str): Optional. Free-text pointer to what the human picked.

    Returns
    -------
    JSON body: the created (or already-existing) bom_line_content row, exactly
    as ``nce.bom_lines.create_bom_line`` returns it.

    The ``@mcp_handler`` decorator maps a missing/invalid-argument ``ValueError``
    to an ``McpError(-32602)`` at the call-site.
    """
    ns = require_namespace_id(arguments)
    ns_uuid = UUID(ns)

    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        row = await do_add_quote_line(
            conn,
            ns_uuid,
            quote_id=arguments.get("quote_id"),
            line_ref=arguments.get("line_ref"),
            qty=arguments.get("qty"),
            unit_price=arguments.get("unit_price"),
            line_total=arguments.get("line_total"),
            currency=arguments.get("currency"),
            origin_ref=arguments.get("origin_ref"),
        )

    return json.dumps(row)


@mcp_handler
async def handle_sales_get_quote_lines(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """MCP tool: sales_get_quote_lines — read every BOM_LINE on one quote.

    The cross-engine (A2A) READ seam that System Design's
    ``system_design.from_quote._read_quote_lines`` resolves to. Read-only: it
    writes nothing and takes no writer_engine or origin_kind from anybody.
    Delegates to ``nce.vertical_modules.sales.lines.do_get_quote_lines``, which
    reads through the namespace_id-scoped store query.

    Arguments
    ---------
    namespace_id (str): Required. Caller namespace UUID.
    quote_id (str): Required. The Sales QUOTE identifier.

    Returns
    -------
    JSON body: a list of bom_line_content rows, ordered by line_ref, exactly as
    the store holds them. An unknown quote_id yields ``[]``, not an error.

    KNOWN LIMITATION (ledger defect D37): the store has no SKU, manufacturer,
    part number or functional-location column, so those fields are simply
    absent from each row. Callers apply their own defaults; nothing here
    fabricates one.

    The ``@mcp_handler`` decorator maps a missing/invalid-argument ``ValueError``
    to an ``McpError(-32602)`` at the call-site.
    """
    ns = require_namespace_id(arguments)
    rows = await do_get_quote_lines(engine, UUID(ns), arguments.get("quote_id"))
    return json.dumps(rows)
