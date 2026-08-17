"""
MCP handlers for the Phase 1 Enterprise Query Catalog tools:
  suggest_queries, execute_query_template, describe_schema.

SRP pattern matching the rest of the handler layer:
  Each handler extracts typed arguments, delegates to CatalogManager,
  and serialises the result.  No DB logic lives here.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from nce.constants import MAX_TOP_K as _MAX_TOP_K
from nce.mcp_args import require_namespace_id as _require_namespace_id
from nce.mcp_errors import mcp_handler
from nce.orchestrator import NCEEngine
from nce.query_catalog import CatalogManager

_MAX_SCHEMA_LIMIT: int = 200  # upper bound for describe_schema limit parameter


@mcp_handler
async def handle_suggest_queries(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """Return ranked query template suggestions matching the agent's intent."""
    namespace_id = uuid.UUID(_require_namespace_id(arguments))
    intent: str = arguments.get("intent") or ""
    if not intent.strip():
        raise ValueError("intent must not be empty")
    top_k: int = max(1, min(int(arguments.get("top_k") or 5), _MAX_TOP_K))

    mgr = CatalogManager(pool=engine.pg_pool)
    suggestions = await mgr.suggest(intent=intent, namespace_id=namespace_id, limit=top_k)

    return json.dumps(
        {
            "status": "ok",
            "suggestions": [
                {
                    "slug": s.slug,
                    "description": s.description,
                    "tools": s.tools,
                    "param_schema": s.param_schema,
                    "confidence": round(s.confidence, 4),
                }
                for s in suggestions
            ],
        }
    )


@mcp_handler
async def handle_execute_query_template(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """Execute a query template by slug with the supplied slot values."""
    namespace_id = uuid.UUID(_require_namespace_id(arguments))
    slug: str = str(arguments.get("slug") or "").strip()
    if not slug:
        raise ValueError("slug is required")
    params: dict[str, Any] = dict(arguments.get("parameters") or {})

    mgr = CatalogManager(pool=engine.pg_pool)
    results = await mgr.execute(slug=slug, params=params, namespace_id=namespace_id)

    return json.dumps({"status": "ok", "results": results}, default=str)


@mcp_handler
async def handle_describe_schema(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """Return the live vocabulary schema (entity types + edge predicates)."""
    namespace_id = uuid.UUID(_require_namespace_id(arguments))
    limit: int = max(1, min(int(arguments.get("limit") or 50), _MAX_SCHEMA_LIMIT))

    mgr = CatalogManager(pool=engine.pg_pool)
    schema = await mgr.describe_schema(namespace_id=namespace_id, limit=limit)

    return json.dumps(
        {
            "status": "ok",
            "entity_types": schema.entity_types,
            "edge_predicates": schema.edge_predicates,
            "sampled_at": schema.sampled_at,
        }
    )
