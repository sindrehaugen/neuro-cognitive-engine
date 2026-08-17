"""
MCP tool handlers for GraphRAG operations (§5). Extracted from server.py:call_tool().
Follows the same pattern as bridge_mcp_handlers.py — each handler receives the engine
and raw arguments dict, and returns a JSON string that call_tool() wraps in TextContent.

Uncle Bob SRP refactoring (2026-05-08):
  - Moved Pydantic import to module top-level (single import block).
  - Handler delegates the validated GraphSearchRequest directly to the engine — no
    field-by-field destructuring.
  - Eliminated legacy raw-dict access ``arguments.get("user_id")``.  The
    ``agent_id`` field on GraphSearchRequest is Pydantic-validated and flows
    through to the GraphOrchestrator as ``user_id``.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from nce.mcp_errors import mcp_handler
from nce.models import GraphSearchRequest
from nce.orchestrator import NCEEngine

log = logging.getLogger("nce.graph_mcp_handlers")


@mcp_handler
async def handle_graph_search(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """GraphRAG traversal over the Knowledge Graph with temporal and user scoping."""
    req = GraphSearchRequest(**arguments)
    result = await engine.graph_search(req)
    return json.dumps(result, default=str)


@mcp_handler
async def handle_neuromorphic_search(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """GraphRAG spreading activation traversal over the Knowledge Graph."""
    if engine._graph_traverser is None:
        raise RuntimeError("Engine not connected — call connect() first")

    # Validate baseline parameters against GraphSearchRequest
    search_keys = {
        "namespace_id",
        "agent_id",
        "query",
        "max_depth",
        "anchor_top_k",
        "as_of",
        "max_edges_per_node",
        "edge_limit",
        "edge_offset",
    }
    search_args = {k: v for k, v in arguments.items() if k in search_keys}
    req = GraphSearchRequest(**search_args)

    # Extract additional neuromorphic parameters
    telemetry_severity = arguments.get("telemetry_severity")
    if telemetry_severity is not None:
        telemetry_severity = float(telemetry_severity)

    theta = float(arguments.get("theta", 0.5))
    decay = float(arguments.get("decay", 0.85))
    alpha = float(arguments.get("alpha", 1.0))

    ticks = arguments.get("ticks")
    if ticks is not None:
        ticks = int(ticks)

    subgraph = await engine._graph_traverser.neuromorphic_search(
        query=req.query,
        namespace_id=str(req.namespace_id),
        max_depth=req.max_depth,
        anchor_top_k=req.anchor_top_k,
        user_id=req.agent_id,
        private=bool(req.agent_id),
        as_of=req.as_of,
        max_edges_per_node=req.max_edges_per_node,
        edge_limit=req.edge_limit,
        edge_offset=req.edge_offset,
        telemetry_severity=telemetry_severity,
        theta=theta,
        decay=decay,
        alpha=alpha,
        ticks=ticks,
    )
    return json.dumps(subgraph.to_dict(), default=str)
