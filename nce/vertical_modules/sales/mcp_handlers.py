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
from nce.vertical_modules.sales.commission import do_calculate_commission
from nce.vertical_modules.sales.lines import do_add_quote_line, do_get_quote_lines
from nce.vertical_modules.sales.signing import do_request_signature
from nce.vertical_modules.sales.write_routing import (
    do_create_customer,
    do_create_deal,
    do_create_lead,
    do_edit_deal,
)

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


@mcp_handler
async def handle_sales_request_signature(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """MCP tool: sales_request_signature — request e-signature for a sales quote.

    Surfaces Sales quote signing orchestration via C7 SignTransport.
    Under Option (b), the quote document is deterministically rendered directly
    from the frozen baseline stored in `sales_signed_baselines`.
    Actor tool, mutation=True, admin_only=True.

    Arguments
    ---------
    namespace_id (str): Required. Caller namespace UUID.
    quote_id (str): Required. The Sales QUOTE identifier.
    signer (dict): Required. Signer details with non-empty 'name' and valid 'email'.
    method (str, optional): Transport method ("manual", "email_code", "oneflow", "criipto", "signicat"). Defaults to "manual".
    idempotency_key (str, optional): Caller-supplied idempotency key.

    Returns
    -------
    JSON body of the created signing session dict with document_hash.
    """
    ns = require_namespace_id(arguments)
    quote_id = arguments.get("quote_id")
    if not isinstance(quote_id, str) or not quote_id.strip():
        raise ValueError("quote_id is required")

    if "doc_bytes" in arguments:
        raise ValueError(
            "doc_bytes parameter is not allowed; quote document is rendered from the frozen baseline"
        )

    params: dict[str, Any] = {
        "namespace_id": ns,
        "quote_id": quote_id.strip(),
    }
    if "signer" in arguments:
        params["signer"] = arguments["signer"]
    if "method" in arguments and isinstance(arguments["method"], str):
        params["method"] = arguments["method"].strip()
    if "idempotency_key" in arguments:
        params["idempotency_key"] = arguments["idempotency_key"]

    session = await do_request_signature(engine, params)
    return json.dumps(session)


@mcp_handler
async def handle_sales_create_customer(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """MCP tool: sales_create_customer — create a customer account through write routing.

    Governed mutation tool. Routes to native NCE graph / read-model or external D365
    based on active source mode. In 'nce' mode, enforces 'nce:' ID prefixing.

    Arguments
    ---------
    namespace_id (str): Required. Caller namespace UUID.
    customer_id (str): Required. Customer identifier (e.g. 'CUST-001' or 'nce:cust-1').
    name (str, optional): Customer/account name.
    source_id (str, optional): External or source identifier.

    Returns
    -------
    JSON body: result dict containing status, mode, and write payloads.
    """
    ns = require_namespace_id(arguments)
    customer_id = arguments.get("customer_id")
    if not isinstance(customer_id, str) or not customer_id.strip():
        raise ValueError("customer_id is required")

    params: dict[str, Any] = {
        "namespace_id": ns,
        "customer_id": customer_id.strip(),
    }
    if "name" in arguments and arguments["name"] is not None:
        params["name"] = str(arguments["name"]).strip()
    if "source_id" in arguments and arguments["source_id"] is not None:
        params["source_id"] = str(arguments["source_id"]).strip()

    result = await do_create_customer(engine, params)
    return json.dumps(result)


@mcp_handler
async def handle_sales_create_lead(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """MCP tool: sales_create_lead — create a sales lead through write routing.

    Governed mutation tool. Routes to native NCE graph / read-model or external D365
    based on active source mode. In 'nce' mode, enforces 'nce:' ID prefixing.

    Arguments
    ---------
    namespace_id (str): Required. Caller namespace UUID.
    lead_id (str): Required. Lead identifier (e.g. 'LEAD-001' or 'nce:lead-1').
    customer_id (str, optional): Customer identifier associated with this lead.
    name (str, optional): Lead name or topic.
    confidence (float, optional): Edge confidence weight (0.0 to 1.0).
    source_id (str, optional): External or source identifier.

    Returns
    -------
    JSON body: result dict containing status, mode, and write payloads.
    """
    ns = require_namespace_id(arguments)
    lead_id = arguments.get("lead_id")
    if not isinstance(lead_id, str) or not lead_id.strip():
        raise ValueError("lead_id is required")

    params: dict[str, Any] = {
        "namespace_id": ns,
        "lead_id": lead_id.strip(),
    }
    if "customer_id" in arguments and arguments["customer_id"] is not None:
        params["customer_id"] = str(arguments["customer_id"]).strip()
    if "name" in arguments and arguments["name"] is not None:
        params["name"] = str(arguments["name"]).strip()
    if "confidence" in arguments and arguments["confidence"] is not None:
        params["confidence"] = float(arguments["confidence"])
    if "source_id" in arguments and arguments["source_id"] is not None:
        params["source_id"] = str(arguments["source_id"]).strip()

    result = await do_create_lead(engine, params)
    return json.dumps(result)


@mcp_handler
async def handle_sales_create_deal(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """MCP tool: sales_create_deal — create a pipeline deal through write routing.

    Governed mutation tool. Routes to native NCE graph / read-model or external D365
    based on active source mode. In 'nce' mode, enforces 'nce:' ID prefixing.

    Arguments
    ---------
    namespace_id (str): Required. Caller namespace UUID.
    deal_id (str): Required. Deal identifier (e.g. 'DEAL-001' or 'nce:deal-1').
    customer_id (str): Required. Associated customer identifier.
    quote_id (str): Required. Associated quote identifier.
    opportunity_id (str, optional): Intermediate opportunity identifier.
    lead_id (str, optional): Originating lead identifier.
    name (str, optional): Deal name.
    confidence (float, optional): Edge confidence weight.
    source_id (str, optional): External or source identifier.

    Returns
    -------
    JSON body: result dict containing status, mode, and write payloads.
    """
    ns = require_namespace_id(arguments)
    deal_id = arguments.get("deal_id")
    customer_id = arguments.get("customer_id")
    quote_id = arguments.get("quote_id")

    if not (deal_id and isinstance(deal_id, str) and deal_id.strip()):
        raise ValueError("deal_id is required")
    if not (customer_id and isinstance(customer_id, str) and customer_id.strip()):
        raise ValueError("customer_id is required")
    if not (quote_id and isinstance(quote_id, str) and quote_id.strip()):
        raise ValueError("quote_id is required")

    params: dict[str, Any] = {
        "namespace_id": ns,
        "deal_id": deal_id.strip(),
        "customer_id": customer_id.strip(),
        "quote_id": quote_id.strip(),
    }
    if "opportunity_id" in arguments and arguments["opportunity_id"] is not None:
        params["opportunity_id"] = str(arguments["opportunity_id"]).strip()
    if "lead_id" in arguments and arguments["lead_id"] is not None:
        params["lead_id"] = str(arguments["lead_id"]).strip()
    if "name" in arguments and arguments["name"] is not None:
        params["name"] = str(arguments["name"]).strip()
    if "confidence" in arguments and arguments["confidence"] is not None:
        params["confidence"] = float(arguments["confidence"])
    if "source_id" in arguments and arguments["source_id"] is not None:
        params["source_id"] = str(arguments["source_id"]).strip()

    result = await do_create_deal(engine, params)
    return json.dumps(result)


@mcp_handler
async def handle_sales_edit_deal(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """MCP tool: sales_edit_deal — edit a pipeline deal through write routing.

    Governed mutation tool. Prevents editing non-native D365 records without an
    existing NCE mapping. Updates graph and read-model.

    Arguments
    ---------
    namespace_id (str): Required. Caller namespace UUID.
    deal_id (str): Required. Deal identifier to update.
    name (str, optional): Updated deal name.
    confidence (float, optional): Updated edge confidence.
    source_id (str, optional): External or source identifier.

    Returns
    -------
    JSON body: result dict containing status, mode, and write payloads.
    """
    ns = require_namespace_id(arguments)
    deal_id = arguments.get("deal_id")
    if not isinstance(deal_id, str) or not deal_id.strip():
        raise ValueError("deal_id is required")

    params: dict[str, Any] = {
        "namespace_id": ns,
        "deal_id": deal_id.strip(),
    }
    if "name" in arguments and arguments["name"] is not None:
        params["name"] = str(arguments["name"]).strip()
    if "confidence" in arguments and arguments["confidence"] is not None:
        params["confidence"] = float(arguments["confidence"])
    if "source_id" in arguments and arguments["source_id"] is not None:
        params["source_id"] = str(arguments["source_id"]).strip()

    result = await do_edit_deal(engine, params)
    return json.dumps(result)


@mcp_handler
async def handle_sales_calculate_commission(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """MCP tool: sales_calculate_commission — calculate DB-weighted sales commissions.

    Arguments
    ---------
    namespace_id : str (required)
    seller_id    : str (optional) — filter commission history by seller.
    deal_data    : dict (optional) — direct deal items calculation.

    Returns
    -------
    JSON body: dict containing commission calculation result.
    """
    ns = require_namespace_id(arguments)
    params: dict[str, Any] = {"namespace_id": ns}
    if "seller_id" in arguments and arguments["seller_id"] is not None:
        params["seller_id"] = str(arguments["seller_id"]).strip()
    if "deal_data" in arguments and isinstance(arguments["deal_data"], dict):
        params["deal_data"] = arguments["deal_data"]

    result = await do_calculate_commission(engine, params)
    return json.dumps(result)
