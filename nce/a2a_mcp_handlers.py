"""
MCP tool handlers for Agent-to-Agent (A2A) sharing (§7). Extracted from server.py:call_tool().

Clean Code SRP: Each handler is a thin orchestrator that:
  1. Extracts and validates arguments via typed helpers
  2. Delegates to a single domain function
  3. Serialises the response
Transport logic (args → typed objects) and domain logic (typed objects → result)
are fully separated. All dependencies are module-level imports.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from nce.a2a import (
    A2AGrantRequest,
    A2AGrantResponse,
    create_grant,
    enforce_scope,
    inspect_grant,
    list_grants,
    revoke_grant,
    update_grant_scopes,
    verify_grant_status,
    verify_token,
)
from nce.auth import NamespaceContext
from nce.mcp_errors import mcp_handler
from nce.mcp_utils import build_caller_context as _build_caller_context
from nce.mcp_utils import parse_scopes as _parse_scopes
from nce.models import A2AQuerySharedRequest
from nce.orchestrator import NCEEngine

log = logging.getLogger("nce.a2a_mcp_handlers")

_MAX_SHARING_TOKEN_LEN: int = 4_096

# ---------------------------------------------------------------------------
# Private helpers — argument extraction (transport concern)
# ---------------------------------------------------------------------------


def _build_grant_request(arguments: dict[str, Any]) -> A2AGrantRequest:
    """Build a typed A2AGrantRequest from raw MCP arguments."""
    return A2AGrantRequest(
        target_namespace_id=arguments.get("target_namespace_id"),
        target_agent_id=arguments.get("target_agent_id"),
        scopes=_parse_scopes(arguments.get("scopes", [])),
        expires_in_seconds=int(arguments.get("expires_in_seconds", 3600)),
        can_delegate=bool(arguments.get("can_delegate", False)),
    )


# ---------------------------------------------------------------------------
# Handlers — each calls exactly one domain function (SRP)
# ---------------------------------------------------------------------------


@mcp_handler
async def handle_a2a_create_grant(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """Create an A2A sharing grant — generates a token for cross-namespace access."""
    caller_ctx = _build_caller_context(arguments)
    grant_request = _build_grant_request(arguments)

    async with engine.pg_pool.acquire(timeout=10.0) as conn:
        response: A2AGrantResponse = await create_grant(conn, caller_ctx, grant_request)

    return json.dumps(
        {
            "grant_id": str(response.grant_id),
            "sharing_token": response.sharing_token,
            "expires_at": response.expires_at.isoformat(),
        }
    )


@mcp_handler
async def handle_a2a_revoke_grant(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """Revoke an active A2A sharing grant."""
    caller_ctx = _build_caller_context(arguments)

    raw_gid = arguments.get("grant_id")
    if not raw_gid:
        raise ValueError("grant_id is required")
    try:
        grant_id = uuid.UUID(str(raw_gid).strip())
    except (ValueError, AttributeError):
        raise ValueError("grant_id must be a valid UUID")

    async with engine.pg_pool.acquire(timeout=10.0) as conn:
        revoked = await revoke_grant(conn, grant_id, caller_ctx)

    return json.dumps({"revoked": revoked})


@mcp_handler
async def handle_a2a_list_grants(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """List all active A2A sharing grants owned by this namespace."""
    caller_ctx = _build_caller_context(arguments)
    include_inactive = bool(arguments.get("include_inactive", False))

    async with engine.pg_pool.acquire(timeout=10.0) as conn:
        grants = await list_grants(conn, caller_ctx, include_inactive=include_inactive)

    return json.dumps({"status": "ok", "grants": grants}, default=str)


@mcp_handler
async def handle_a2a_query_shared(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """Execute a semantic search against another agent's memories using an A2A token."""
    req = A2AQuerySharedRequest(**arguments)
    if len(req.sharing_token) > _MAX_SHARING_TOKEN_LEN:
        raise ValueError(f"sharing_token exceeds maximum length ({_MAX_SHARING_TOKEN_LEN} chars)")
    consumer_ctx = NamespaceContext(
        namespace_id=req.consumer_namespace_id,
        agent_id=req.consumer_agent_id,
    )

    async with engine.pg_pool.acquire(timeout=10.0) as conn:
        verified = await verify_token(conn, req.sharing_token, consumer_ctx)

        resource_id = req.resource_id or str(verified.owner_namespace_id)
        if req.resource_type != "namespace" and not req.resource_id:
            raise ValueError(f"resource_id is required when resource_type={req.resource_type!r}")

        enforce_scope(
            verified.scopes,
            req.resource_type,
            resource_id,
            str(verified.owner_namespace_id),
        )

        # Append signed event on the owner's log
        owner_ctx = NamespaceContext(
            namespace_id=verified.owner_namespace_id,
            agent_id=verified.owner_agent_id,
        )
        from nce.a2a import _append_a2a_event

        await _append_a2a_event(
            conn,
            owner_ctx=owner_ctx,
            event_type="a2a_shared_query",
            params={
                "consumer_namespace_id": str(consumer_ctx.namespace_id),
                "consumer_agent_id": consumer_ctx.agent_id,
                "grant_id": str(verified.grant_id),
                "query": req.query,
            },
        )

    if verified.owner_namespace_id == consumer_ctx.namespace_id:
        log.warning(
            "A2A self-access: consumer_namespace=%s used token from same namespace "
            "(owner_namespace=%s) — verify this is intentional",
            consumer_ctx.namespace_id,
            verified.owner_namespace_id,
        )

    results = await engine.semantic_search(
        namespace_id=str(verified.owner_namespace_id),
        agent_id=verified.owner_agent_id,
        query=req.query,
        limit=req.top_k,
        offset=0,
    )

    # Hydrate owner's original signature + key ID alongside shared memories
    if results:
        memory_ids = [res["memory_id"] for res in results]
        async with engine.pg_pool.acquire(timeout=10.0) as conn:
            rows = await conn.fetch(
                """
                SELECT id, signature, signature_key_id
                FROM memories
                WHERE id = ANY($1) AND namespace_id = $2
                """,
                memory_ids,
                verified.owner_namespace_id,
            )
            sig_map = {
                row["id"]: {
                    "signature": row["signature"].hex() if row["signature"] else None,
                    "signature_key_id": row["signature_key_id"],
                }
                for row in rows
            }
            for res in results:
                info = sig_map.get(res["memory_id"], {})
                res["signature"] = info.get("signature")
                res["signature_key_id"] = info.get("signature_key_id")

    return json.dumps({"results": results}, default=str)


@mcp_handler
async def handle_a2a_verify_grant_status(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """Verify the validity, scopes, and expiration of an A2A grant."""
    caller_ctx = _build_caller_context(arguments)
    sharing_token = arguments.get("sharing_token")
    grant_id_str = arguments.get("grant_id")

    grant_id = None
    if grant_id_str is not None:
        try:
            grant_id = uuid.UUID(str(grant_id_str).strip())
        except (ValueError, AttributeError):
            raise ValueError("grant_id must be a valid UUID")

    async with engine.pg_pool.acquire(timeout=10.0) as conn:
        res = await verify_grant_status(
            conn=conn,
            ctx=caller_ctx,
            sharing_token=sharing_token,
            grant_id=grant_id,
        )
    return json.dumps(res)


@mcp_handler
async def handle_a2a_update_grant_scopes(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """Dynamically mutate the scopes on an active grant owned by this namespace."""
    owner_ctx = _build_caller_context(arguments)
    raw_gid = arguments.get("grant_id")
    if not raw_gid:
        raise ValueError("grant_id is required")
    try:
        grant_id = uuid.UUID(str(raw_gid).strip())
    except (ValueError, AttributeError):
        raise ValueError("grant_id must be a valid UUID")

    scopes = _parse_scopes(arguments.get("scopes", []))
    mode = arguments.get("mode", "replace")
    if mode not in ("replace", "append"):
        raise ValueError("mode must be 'replace' or 'append'")

    async with engine.pg_pool.acquire(timeout=10.0) as conn:
        res = await update_grant_scopes(
            conn=conn,
            owner_ctx=owner_ctx,
            grant_id=grant_id,
            scopes=scopes,
            mode=mode,
        )
    return json.dumps(res)


@mcp_handler
async def handle_a2a_inspect_grant(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """Retrieve single grant metadata for audit audit trails (excludes token_hash)."""
    owner_ctx = _build_caller_context(arguments)
    raw_gid = arguments.get("grant_id")
    if not raw_gid:
        raise ValueError("grant_id is required")
    try:
        grant_id = uuid.UUID(str(raw_gid).strip())
    except (ValueError, AttributeError):
        raise ValueError("grant_id must be a valid UUID")

    async with engine.pg_pool.acquire(timeout=10.0) as conn:
        res = await inspect_grant(
            conn=conn,
            owner_ctx=owner_ctx,
            grant_id=grant_id,
        )
    return json.dumps(res)
