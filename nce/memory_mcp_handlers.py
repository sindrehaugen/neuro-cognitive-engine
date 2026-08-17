"""
MCP tool handlers for memory operations (§1–§3). Extracted from server.py:call_tool().
Follows the same pattern as bridge_mcp_handlers.py — each handler receives the engine
and raw arguments dict, and returns a JSON string that call_tool() wraps in TextContent.

Caching, quota enforcement, and admin authorization are handled by call_tool() as
cross-cutting concerns — handlers focus purely on domain logic.

Uncle Bob SRP refactoring (2026-05-08):
  - All Pydantic imports moved to module top-level (single import block).
  - Private helpers _ok_response, _serialize isolate JSON formatting from routing.
  - Every handler now uses a Pydantic model for input validation (no raw dict access).
  - Handlers are thin routing facades: parse args → delegate to engine → format response.
"""

from __future__ import annotations

import json
from typing import Any

from nce.mcp_errors import mcp_handler
from nce.models import (
    ArtifactPayload,
    BoostMemoryRequest,
    ForgetMemoryRequest,
    GetRecentContextRequest,
    SemanticSearchRequest,
    ShredMemoryRequest,
    StoreMemoryRequest,
    UnredactMemoryRequest,
)
from nce.orchestrator import NCEEngine

# ── Private response formatters ───────────────────────────────────────────────


def _ok_response(payload_ref: str, **extras: Any) -> str:
    """Serialize a standard {"status": "ok", "payload_ref": ...} envelope."""
    return json.dumps({"status": "ok", "payload_ref": payload_ref, **extras}, default=str)


def _serialize(data: object) -> str:
    """Serialize a dict or list to a JSON string for MCP TextContent wrapping."""
    return json.dumps(data, default=str)


# ── MCP Tool Handlers ─────────────────────────────────────────────────────────


@mcp_handler
async def handle_store_memory(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """Persist a memory (conversation turn, document, or summary) to the Tri-Stack."""
    payload = StoreMemoryRequest(**arguments)
    result = await engine.store_memory(payload)
    payload_ref = result.get("payload_ref")
    if not isinstance(payload_ref, str) or not payload_ref:
        raise ValueError("Invalid engine response: missing or empty payload_ref")
    return _ok_response(
        payload_ref,
        contradiction=result.get("contradiction"),
    )


@mcp_handler
async def handle_store_artifact(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """Ingest large artifacts (media, PDF, log, etc.) into the Quad-Stack."""
    payload = ArtifactPayload(**arguments)
    mongo_id = await engine.store_artifact(payload)
    return _ok_response(mongo_id, storage="minio")


@mcp_handler
async def handle_store_media(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """[DEPRECATED] Alias for store_artifact. Use store_artifact instead."""
    import warnings

    warnings.warn(
        "handle_store_media is deprecated; use handle_store_artifact instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    result_json = await handle_store_artifact(engine, arguments)
    data = json.loads(result_json)
    data["deprecated"] = True
    data["replacement"] = "store_artifact"
    return json.dumps(data, default=str)


@mcp_handler
async def handle_semantic_search(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """Search stored memories by semantic similarity (pgvector cosine search)."""
    req = SemanticSearchRequest(**arguments)
    results = await engine.semantic_search(
        query=req.query,
        namespace_id=str(req.namespace_id),
        agent_id=req.agent_id or "default",
        limit=req.limit,
        offset=req.offset,
        as_of=req.as_of,
    )
    return _serialize(results)


@mcp_handler
async def handle_get_recent_context(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """Retrieve the most recent cached context for a user/session from Redis."""
    req = GetRecentContextRequest(**arguments)
    context = await engine.recall_recent(
        namespace_id=str(req.namespace_id),
        agent_id=req.agent_id or "default",
        limit=req.limit,
        as_of=req.as_of,
        offset=req.offset,
    )
    return _serialize({"context": context})


@mcp_handler
async def handle_boost_memory(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """Boost the salience of a memory for the calling agent."""
    req = BoostMemoryRequest(**arguments)
    res = await engine.boost_memory(
        memory_id=str(req.memory_id),
        agent_id=req.agent_id,
        namespace_id=str(req.namespace_id),
        factor=req.factor,
    )
    return _serialize(res)


@mcp_handler
async def handle_forget_memory(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """Set salience to 0.0 for the calling agent (soft-delete)."""
    req = ForgetMemoryRequest(**arguments)
    res = await engine.forget_memory(
        memory_id=str(req.memory_id),
        agent_id=req.agent_id,
        namespace_id=str(req.namespace_id),
    )
    return _serialize(res)


@mcp_handler
async def handle_shred_memory(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """[ADMIN] Provably forget a memory across every store, returning a deletion receipt."""
    req = ShredMemoryRequest(**arguments)
    result = await engine.shred_memory(
        memory_id=str(req.memory_id),
        namespace_id=str(req.namespace_id),
        agent_id=req.agent_id,
    )
    return _serialize(result)


@mcp_handler
async def handle_unredact_memory(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """Reverse pseudonymisation for a given memory (requires elevated permissions)."""
    req = UnredactMemoryRequest(**arguments)
    result = await engine.unredact_memory(
        memory_id=str(req.memory_id),
        namespace_id=str(req.namespace_id),
        agent_id=req.agent_id,
    )
    return _serialize(result)
