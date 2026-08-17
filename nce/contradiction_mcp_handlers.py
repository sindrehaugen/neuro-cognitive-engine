"""
MCP tool handlers for contradiction detection and resolution (§6). Extracted from
server.py:call_tool(). Follows the same pattern as bridge_mcp_handlers.py.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID

from nce.auth import require_scope, validate_agent_id
from nce.mcp_errors import mcp_handler
from nce.orchestrator import NCEEngine

log = logging.getLogger("nce.contradiction_mcp_handlers")

# Allowlisted resolution codes — must stay in sync with resolve_contradiction()
# downstream and the contradictions table query expectations.
_ALLOWED_RESOLUTIONS: frozenset[str] = frozenset(
    {
        "accepted_a",
        "accepted_b",
        "merged",
        "rejected",
        "superseded",
        "duplicate",
        "false_positive",
    }
)


def _require_uuid_arg(arguments: dict[str, Any], key: str) -> str:
    """Extract, validate, and return a UUID argument as a canonical string."""
    raw = arguments.get(key)
    if not raw:
        raise ValueError(f"{key} is required")
    return str(UUID(str(raw)))


def _optional_resolution(raw: Any) -> str | None:
    """Validate an optional resolution value against the allowlist."""
    if raw is None or raw == "":
        return None
    value = str(raw).strip().lower()
    if value not in _ALLOWED_RESOLUTIONS:
        raise ValueError(f"resolution must be one of {sorted(_ALLOWED_RESOLUTIONS)}, got {value!r}")
    return value


def _required_resolution(arguments: dict[str, Any]) -> str:
    """Extract and validate the mandatory resolution argument."""
    if "resolution" not in arguments:
        raise ValueError("resolution is required")
    value = _optional_resolution(arguments["resolution"])
    if value is None:
        raise ValueError("resolution is required and must not be empty")
    return value


def _optional_note(raw: Any) -> str | None:
    """Validate an optional free-text note, capped at 2048 chars."""
    if raw is None:
        return None
    note = str(raw)
    if len(note) > 2048:
        raise ValueError("note must be <= 2048 characters")
    return note


@mcp_handler
async def handle_list_contradictions(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """List contradictions detected in the knowledge graph.

    Required: namespace_id (UUID).
    Optional: resolution (allowlisted string), agent_id, limit (1–200), offset (>= 0).
    """
    namespace_id = _require_uuid_arg(arguments, "namespace_id")
    resolution = _optional_resolution(arguments.get("resolution"))

    agent_id: str | None = None
    raw_agent = arguments.get("agent_id")
    if raw_agent:
        agent_id = validate_agent_id(str(raw_agent))

    limit = max(1, min(int(arguments.get("limit", 50)), 200))
    offset = max(0, int(arguments.get("offset", 0)))

    res = await engine.list_contradictions(
        namespace_id=namespace_id,
        resolution=resolution,
        agent_id=agent_id,
        limit=limit,
        offset=offset,
    )
    return json.dumps(res, default=str)


@require_scope("admin")
@mcp_handler
async def handle_resolve_contradiction(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """Resolve a detected contradiction with a resolution decision. RLS-enforced via namespace_id.

    Required: contradiction_id (UUID), namespace_id (UUID), resolution (allowlisted),
              resolved_by (agent identifier).
    Optional: note (free text, max 2048 chars).
    """
    contradiction_id = _require_uuid_arg(arguments, "contradiction_id")
    namespace_id = _require_uuid_arg(arguments, "namespace_id")
    resolution = _required_resolution(arguments)

    raw_resolved_by = arguments.get("resolved_by")
    if not raw_resolved_by:
        raise ValueError("resolved_by is required")
    resolved_by = validate_agent_id(str(raw_resolved_by))

    note = _optional_note(arguments.get("note"))

    res = await engine.resolve_contradiction(
        contradiction_id=contradiction_id,
        namespace_id=namespace_id,
        resolution=resolution,
        resolved_by=resolved_by,
        note=note,
    )
    return json.dumps(res, default=str)
