"""
nce/vertical_modules/sites/mcp_handlers.py
===============================================
MCP tool wrappers for C17 Site Master Data's feed adapters: the address
registry enrichment (Wave F-9).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from nce.mcp_args import require_namespace_id as _require_namespace_id
from nce.mcp_errors import mcp_handler
from nce.vertical_modules.sites.address_registry import do_enrich_site_from_address_registry

if TYPE_CHECKING:
    from nce.orchestrator import NCEEngine


@mcp_handler
async def handle_sites_enrich_address_from_registry(
    engine: NCEEngine, arguments: dict[str, Any]
) -> str:
    """MCP tool: sites_enrich_address_from_registry — validate/geocode a
    site's address against Kartverket's cadastre and merge the result
    into the site's own address/coordinate fields (Operator).

    Requires ``namespace_id`` and ``site_id``. Optional ``query``
    overrides the site's own stored address text. Thin adapter — all
    logic lives in :func:`do_enrich_site_from_address_registry`.
    """
    namespace_id = _require_namespace_id(arguments)
    result = await do_enrich_site_from_address_registry(
        engine, {**dict(arguments), "namespace_id": namespace_id}
    )
    return json.dumps(result, default=str)
