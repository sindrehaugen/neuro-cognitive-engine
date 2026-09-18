"""
nce/vertical_modules/legal_entities/mcp_handlers.py
=======================================================
MCP tool wrapper for the C15 Legal-Entity Register's national business
registry feed (Wave F-8). The register's own CRUD tools
(``legal_entities_upsert_legal_entities`` etc.) are auto-mounted by the
C12 resource surface from ``resources.py``'s ``LEGAL_ENTITY_SPEC`` — this
file exists only for the one hand-written tool the resource surface
does not generate.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from nce.mcp_errors import mcp_handler
from nce.vertical_modules.legal_entities.brreg_feed import do_enrich_legal_entity_from_registry

if TYPE_CHECKING:
    from nce.orchestrator import NCEEngine


@mcp_handler
async def handle_legal_entities_enrich_from_registry(
    engine: NCEEngine, arguments: dict[str, Any]
) -> str:
    """MCP tool: legal_entities_enrich_from_registry — pull one legal
    entity's current company data from Norway's BRREG Enhetsregisteret
    and merge it into the entity's metadata (Operator/cron pull).

    Requires ``namespace_id`` and ``entity_id``. Thin adapter — all logic
    lives in :func:`do_enrich_legal_entity_from_registry`.
    """
    result = await do_enrich_legal_entity_from_registry(engine, dict(arguments))
    return json.dumps(result, default=str)
