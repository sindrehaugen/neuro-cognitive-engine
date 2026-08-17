"""
MCP tool handlers for admin operations (§8). Extracted from server.py:call_tool().
Follows the same pattern as bridge_mcp_handlers.py — each handler receives the engine
and raw arguments dict, and returns a JSON string that call_tool() wraps in TextContent.

RBAC is enforced by the ``@require_scope("admin")`` decorator on every handler.
The decorator validates ``admin_api_key`` against ``NCE_ADMIN_API_KEY`` (constant-time),
strips auth keys from arguments before they reach ``extra='forbid'`` domain models, and
forwards ``admin_identity`` as a keyword argument to handlers that declare it.

On scope violation the decorator raises :class:`nce.auth.ScopeError`, which
:func:`call_tool` lets propagate unchanged so the MCP framework produces a JSON-RPC
error response (code ``-32005`` — scope forbidden).

Rate limiting is enforced by the ``@admin_rate_limit(limit=10, period=60)`` decorator.
If the limit is exceeded, it raises :class:`nce.auth.RateLimitError`.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from nce.auth import admin_rate_limit, require_scope
from nce.mcp_errors import mcp_handler
from nce.orchestrator import NCEEngine

log = logging.getLogger("nce.admin_mcp_handlers")


@require_scope("admin")
@admin_rate_limit(limit=10, period=60)
@mcp_handler
async def handle_manage_namespace(
    engine: NCEEngine,
    arguments: dict[str, Any],
    admin_identity: str | None = None,
) -> str:
    """[ADMIN] Manage namespaces: create, list, grant, revoke, update_metadata."""
    from nce.models import ManageNamespaceRequest

    payload = ManageNamespaceRequest(**arguments)
    res = await engine.manage_namespace(payload, admin_identity=admin_identity)
    return json.dumps(res, default=str)


@require_scope("admin")
@admin_rate_limit(limit=10, period=60)
@mcp_handler
async def handle_verify_memory(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """Verify the integrity and causal provenance of a memory."""
    from nce.temporal import parse_as_of

    as_of_dt = parse_as_of(arguments.get("as_of")) if "as_of" in arguments else None
    res = await engine.verify_memory(memory_id=arguments["memory_id"], as_of=as_of_dt)
    return json.dumps(res)


@require_scope("admin")
@admin_rate_limit(limit=10, period=60)
@mcp_handler
async def handle_trigger_consolidation(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """[ADMIN] Manually trigger a sleep-consolidation run for a namespace."""
    from nce.temporal import parse_as_of

    since_dt = (
        parse_as_of(arguments.get("since_timestamp")) if "since_timestamp" in arguments else None
    )
    res = await engine.trigger_consolidation(
        namespace_id=arguments["namespace_id"], since_timestamp=since_dt
    )
    return json.dumps(res)


@require_scope("admin")
@admin_rate_limit(limit=10, period=60)
@mcp_handler
async def handle_consolidation_status(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """[ADMIN] Check the status of a consolidation run."""
    res = await engine.consolidation_status(run_id=arguments["run_id"])
    return json.dumps(res)


@require_scope("admin")
@admin_rate_limit(limit=10, period=60)
@mcp_handler
async def handle_manage_quotas(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """[ADMIN] Manage resource quotas for a namespace."""
    from nce.models import ManageQuotasRequest

    req = ManageQuotasRequest(**arguments)
    res = await engine.manage_quotas(req)
    return json.dumps(res, default=str)


@require_scope("admin")
@admin_rate_limit(limit=10, period=60)
@mcp_handler
async def handle_rotate_signing_key(
    engine: NCEEngine,
    arguments: dict[str, Any],
    admin_identity: str | None = None,
) -> str:
    """[ADMIN] Generate a new active signing key and retire the current one."""
    from nce.signing import rotate_key

    async with engine.pg_pool.acquire(timeout=10.0) as conn:
        async with conn.transaction():
            new_key_id = await rotate_key(conn)

    # Signing keys are global (not namespace-scoped), so append_event cannot be
    # used here until a designated system/security namespace is provisioned.
    # Log at WARNING level so this always surfaces in operator logs.
    log.warning(
        "SECURITY: signing key rotated by %s — new_key_id=%s",
        admin_identity or "admin",
        new_key_id,
    )
    return json.dumps({"status": "ok", "new_key_id": new_key_id})


@require_scope("admin")
@admin_rate_limit(limit=10, period=60)
@mcp_handler
async def handle_get_health(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """[ADMIN] Comprehensive health check for all database and cognitive layers."""
    res = await engine.check_health()
    return json.dumps(res)


@require_scope("admin")
@admin_rate_limit(limit=30, period=60)
@mcp_handler
async def handle_list_dlq(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """[ADMIN] List dead-letter queue entries.

    Args:
        task_name (optional): Filter by task function name.
        status (optional): Filter by status (pending, replayed, purged).
        limit (optional): Max rows (default 50, max 200).
        offset (optional): Pagination offset (default 0).
    """
    from nce.dead_letter_queue import list_dead_letters

    entries = await list_dead_letters(
        engine.pg_pool,
        task_name=arguments.get("task_name"),
        status=arguments.get("status"),
        limit=int(arguments.get("limit", 50)),
        offset=int(arguments.get("offset", 0)),
    )
    return json.dumps({"entries": entries, "count": len(entries)}, default=str)


@require_scope("admin")
@admin_rate_limit(limit=10, period=60)
@mcp_handler
async def handle_replay_dlq(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """[ADMIN] Mark a dead-letter queue entry as replayed.

    Args:
        dlq_id: UUID of the DLQ entry to replay.
    """
    from nce.dead_letter_queue import replay_dead_letter

    dlq_id: str = arguments["dlq_id"]
    result = await replay_dead_letter(engine.pg_pool, dlq_id)
    return json.dumps(result, default=str)


@require_scope("admin")
@admin_rate_limit(limit=10, period=60)
@mcp_handler
async def handle_purge_dlq(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """[ADMIN] Permanently remove a dead-letter queue entry.

    Args:
        dlq_id: UUID of the DLQ entry to purge.
    """
    from nce.dead_letter_queue import purge_dead_letter

    dlq_id: str = arguments["dlq_id"]
    await purge_dead_letter(engine.pg_pool, dlq_id)
    return json.dumps({"status": "ok", "id": dlq_id})
