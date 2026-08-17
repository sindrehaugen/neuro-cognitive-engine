"""
nce/vertical_modules/system_design/mcp_handlers.py
===================================================
MCP tool handlers for the System Design vertical module.

Phase 1a — skeleton only.  No domain logic, no graph writes, no external
systems.  Later waves bolt ``do_*`` functions onto this spine.

Public entry-points:
  ``handle_system_design_ping`` — liveness probe; verifies the namespace_id
  is present and returns a simple OK payload.
  ``handle_system_design_publish_design_docs`` — export a DESIGN and its
  DESIGN_LINE/FUNCTIONAL_LOCATION tree to Lucid (W11, Phase 1b, EXPORT ONLY).

Registered in ``nce/tool_registry.py`` via:
  ``_h(system_design_mcp_handlers, "handle_system_design_ping")``
  ``_h(system_design_mcp_handlers, "handle_system_design_publish_design_docs")``
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from nce.mcp_args import require_namespace_id
from nce.mcp_errors import mcp_handler
from nce.vertical_modules.system_design.lucid import do_publish_design_docs

if TYPE_CHECKING:
    from nce.orchestrator import NCEEngine

log = logging.getLogger("nce.vertical_modules.system_design.mcp_handlers")


@mcp_handler
async def handle_system_design_ping(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """MCP tool: system_design_ping — liveness probe for the System Design vertical.

    Requires ``namespace_id`` in *arguments*.  Returns ``{"ok": true, "engine":
    "system_design"}`` on success; the ``@mcp_handler`` decorator converts a
    missing-namespace ``ValueError`` into an ``McpError(-32602)`` at call-site.
    """
    require_namespace_id(arguments)
    return json.dumps({"ok": True, "engine": "system_design"})


@mcp_handler
async def handle_system_design_publish_design_docs(
    engine: NCEEngine, arguments: dict[str, Any]
) -> str:
    """MCP tool: system_design_publish_design_docs — export a DESIGN to Lucid.

    **EXPORT ONLY** (spec correction, Wave 11 — Lucid import is cut).

    Requires ``namespace_id`` and ``design_id`` in *arguments*.
    Returns ``{"lucid_url": str}`` on success, ``{"lucid_url": null}`` when
    Lucid credentials are unset (clean no-op — Phase 1b is not a gate).

    The ``@mcp_handler`` decorator converts a missing-namespace
    ``ValueError`` into an ``McpError(-32602)`` at call-site.
    """
    require_namespace_id(arguments)
    if not arguments.get("design_id"):
        raise ValueError("design_id is required")
    result = await do_publish_design_docs(engine, arguments)
    return json.dumps(result)
