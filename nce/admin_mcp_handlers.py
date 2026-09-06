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
    from nce.mcp_args import model_kwargs
    from nce.models import ManageNamespaceRequest

    payload = ManageNamespaceRequest(**model_kwargs(arguments))
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
    from nce.mcp_args import model_kwargs
    from nce.models import ManageQuotasRequest

    req = ManageQuotasRequest(**model_kwargs(arguments))
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
    from nce.auth import set_namespace_context
    from nce.event_log import append_event
    from nce.signing import get_active_key, key_fingerprint, rotate_key
    from nce.system_namespace import get_system_namespace_id

    actor = admin_identity or "admin"

    async with engine.pg_pool.acquire(timeout=10.0) as conn:
        async with conn.transaction():
            # Signing keys are global (not namespace-scoped). The reserved
            # ``_system`` namespace seeded by migration 065 is what makes this
            # append_event possible; before it existed this handler could only
            # log, and a log line is not an immutable record.
            system_ns_id = await get_system_namespace_id(conn)
            await set_namespace_context(conn, system_ns_id)

            # Fingerprint the OUTGOING key BEFORE rotating, best-effort. A key
            # whose stored blob cannot be decrypted is precisely the situation
            # an operator rotates *out of* (2026-09-02: 26h of exactly that),
            # so a decryption failure must never block the remediation -- it is
            # recorded as an unknown fingerprint instead.
            old_key_id: str | None = None
            old_key_fp: str | None = None
            try:
                old_key_id, old_raw = await get_active_key(conn)
                old_key_fp = key_fingerprint(old_raw)
            except Exception as exc:
                log.warning("Could not fingerprint the outgoing signing key: %s", exc)

            new_key_id = await rotate_key(conn)

            # rotate_key() clears the key cache, so this loads the NEW key.
            new_key_fp: str | None = None
            try:
                _, new_raw = await get_active_key(conn)
                new_key_fp = key_fingerprint(new_raw)
            except Exception as exc:
                log.warning("Could not fingerprint the incoming signing key: %s", exc)

            # Emitted inside the SAME transaction as the rotation: an audit row
            # that could commit while the rotation rolled back would be worse
            # than no row at all. Fingerprints only -- never key material.
            await append_event(
                conn=conn,
                namespace_id=system_ns_id,
                agent_id=actor,
                event_type="signing_key_rotated",
                params={
                    "old_key_id": old_key_id,
                    "old_key_fingerprint": old_key_fp,
                    "new_key_id": new_key_id,
                    "new_key_fingerprint": new_key_fp,
                    "rotated_by": actor,
                },
                result_summary={"status": "ok"},
            )

    # Log at WARNING level so this always surfaces in operator logs too.
    log.warning(
        "SECURITY: signing key rotated by %s — new_key_id=%s new_key_fp=%s "
        "old_key_id=%s old_key_fp=%s",
        actor,
        new_key_id,
        new_key_fp,
        old_key_id,
        old_key_fp,
    )
    return json.dumps(
        {
            "status": "ok",
            "new_key_id": new_key_id,
            "new_key_fingerprint": new_key_fp,
            "old_key_id": old_key_id,
            "old_key_fingerprint": old_key_fp,
        }
    )


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
