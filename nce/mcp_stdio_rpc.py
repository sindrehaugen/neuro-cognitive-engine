"""JSON-RPC helpers and admin/quota utilities for MCP stdio."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from mcp.types import TextContent

from nce import quotas as _quotas
from nce.mcp_args import build_cache_key
from nce.mcp_errors import MCP_AUTH_FAILED, McpError, merge_client_error_data
from nce.tool_registry import CACHEABLE_TOOLS

log = logging.getLogger("nce-mcp")

if TYPE_CHECKING:
    from nce.orchestrator import NCEEngine

# MCP / JSON-RPC client-visible prefix when ``consume_for_tool`` hits a hard limit.
MCP_QUOTA_EXCEEDED_PREFIX = "Resource quota exceeded (-32013)"


async def _try_cached_mcp_tool_response(
    eng: NCEEngine,
    tool_name: str,
    arguments: dict[str, Any],
) -> tuple[list[TextContent] | None, str | None]:
    """Return cached MCP tool payloads before quota runs; misses return (None, redis_key).

    Naming note: callers treat a non-empty first element as cache hit."""
    if tool_name not in CACHEABLE_TOOLS:
        return None, None

    try:
        gen_raw = await eng.redis_client.get("mcp_cache_generation")
        gen_val = int(gen_raw.decode()) if gen_raw else 0
        ns_id = arguments.get("namespace_id")
        cache_key = build_cache_key(
            tool_name=tool_name,
            arguments=arguments,
            generation=gen_val,
            namespace_id=ns_id,
        )
        cached_raw = await eng.redis_client.get(cache_key)
        if not cached_raw:
            return None, cache_key

        ns_for_log = str(ns_id)[:8] if ns_id is not None else "global"
        log.info("API Cache hit for tool %s (ns=%s)", tool_name, ns_for_log)
        return (
            [TextContent(type="text", text=cached_raw.decode())],
            cache_key,
        )
    except Exception as exc:
        log.warning("Redis read failure in cache handler (treating as cache miss): %s", exc)
        return None, None


async def _consume_quota_for_mcp_tool(
    pg_pool,
    tool_name: str,
    arguments: dict[str, Any],
    redis_client,
) -> Any:
    return await _quotas.consume_for_tool(pg_pool, tool_name, arguments, redis_client=redis_client)


def _check_admin(arguments: dict[str, Any]) -> None:
    """Validate admin privileges via :func:`nce.auth._validate_scope`.

    Raises:
        McpError(-32001): When the caller is not authorised.
    """
    from nce.auth import ScopeError, _validate_scope

    try:
        _validate_scope("admin", arguments)
    except ScopeError as exc:
        raise McpError(
            MCP_AUTH_FAILED,
            "Admin authentication required",
            data={"reason": "unauthorized"},
        ) from exc


# ── JSON-RPC 2.0 Error Response Helper ─────────────────────────────────────
# Standard error codes per JSON-RPC 2.0:
#   -32600  Invalid Request
#   -32601  Method not found
#   -32602  Invalid params
#   -32603  Internal error
# MCP extended codes (server-errors -32000..-32099):
#   -32001  Admin authentication required
#   -32005  Scope forbidden
#   -32013  Resource quota exceeded
#   -32029  Rate limit exceeded


def _jsonrpc_error_response(
    code: int,
    message: str,
    *,
    detail: str | None = None,
    data: dict[str, Any] | None = None,
) -> list[TextContent]:
    """Return a JSON-RPC 2.0 error response as ``TextContent``.

    Args:
        code: Standard JSON-RPC 2.0 or MCP extended error code.
        message: Short human-readable error summary.
        detail: Optional detail string included under ``error.data.detail``.
        data: Optional extra fields merged into ``error.data``.

    Returns:
        A single-element ``TextContent`` list containing the serialized
        JSON-RPC error object.  The MCP SDK treats this as a *successful*
        tool result; the error payload is for the *client* to interpret.
    """
    error: dict[str, Any] = {"code": code, "message": message}
    error_data = merge_client_error_data(data, detail=detail)
    if error_data:
        error["data"] = error_data
    return [
        TextContent(
            type="text",
            text=json.dumps({"jsonrpc": "2.0", "error": error}),
        )
    ]
