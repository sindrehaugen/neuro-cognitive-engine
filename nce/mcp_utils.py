"""Shared MCP transport utilities.

Thin helpers that convert raw JSON-RPC arguments into typed domain objects.
Kept in a dedicated module so multiple handler modules can share them
without depending on each other's domain logic.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from nce.a2a import A2AScope
from nce.auth import NamespaceContext, validate_agent_id
from nce.mcp_args import extract_namespace_id

_MAX_SCOPES_INPUT_BYTES: int = 65_536  # 64 KB
_MAX_SCOPES_LIST_ITEMS: int = 256


def build_caller_context(arguments: dict[str, Any]) -> NamespaceContext:
    """Extract the caller's identity from raw MCP arguments."""
    ns_str = extract_namespace_id(arguments)  # raises ValueError on invalid UUID
    if ns_str is None:
        raise ValueError("namespace_id is required")
    return NamespaceContext(
        namespace_id=uuid.UUID(ns_str),
        agent_id=validate_agent_id(str(arguments.get("agent_id") or "default")),
    )


def parse_scopes(raw_scopes: Any) -> list[A2AScope]:
    """Parse A2A scopes from JSON string or list-of-dicts."""
    if isinstance(raw_scopes, str):
        if len(raw_scopes.encode()) > _MAX_SCOPES_INPUT_BYTES:
            raise ValueError(f"raw_scopes exceeds maximum size ({_MAX_SCOPES_INPUT_BYTES} bytes)")
        try:
            raw_scopes = json.loads(raw_scopes)
        except json.JSONDecodeError as exc:
            raise ValueError(f"raw_scopes is not valid JSON: {exc}") from exc
    if not isinstance(raw_scopes, list):
        raise ValueError(f"scopes must be a list, got {type(raw_scopes).__name__!r}")
    if len(raw_scopes) > _MAX_SCOPES_LIST_ITEMS:
        raise ValueError(f"scopes list exceeds maximum length ({_MAX_SCOPES_LIST_ITEMS} items)")
    return [A2AScope.model_validate(s) for s in raw_scopes]
