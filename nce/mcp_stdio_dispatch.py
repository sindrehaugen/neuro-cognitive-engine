"""MCP stdio tool dispatch (handler routing and error envelopes)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from mcp.types import TextContent

# Handler module imports are kept here so tests can use
# ``patch.object(dispatch.memory_mcp_handlers, "handle_foo", mock)``
# (same module objects that ``_h()`` in tool_registry resolves at call time).
from nce import (
    NCEEngine,
    a2a_mcp_handlers,  # noqa: F401
    admin_mcp_handlers,  # noqa: F401
    bridge_mcp_handlers,  # noqa: F401
    catalog_mcp_handlers,  # noqa: F401
    code_mcp_handlers,  # noqa: F401
    contradiction_mcp_handlers,  # noqa: F401
    graph_mcp_handlers,  # noqa: F401
    memory_mcp_handlers,  # noqa: F401
    migration_mcp_handlers,  # noqa: F401
    replay_mcp_handlers,  # noqa: F401
    snapshot_mcp_handlers,  # noqa: F401
)
from nce.auth import RateLimitError, ScopeError, enforce_mcp_tool_auth
from nce.config import cfg
from nce.constants import MCP_CACHE_TTL_S as _MCP_CACHE_TTL_S
from nce.mcp_args import bump_cache_generation, purge_document_cache
from nce.mcp_errors import (
    McpError,
    UnknownToolError,
    client_visible_detail,
    internal_error_data,
)
from nce.mcp_stdio_rpc import (
    MCP_QUOTA_EXCEEDED_PREFIX,
    _check_admin,
    _consume_quota_for_mcp_tool,
    _jsonrpc_error_response,
    _try_cached_mcp_tool_response,
)
from nce.observability import instrument_tool_call
from nce.quotas import QuotaExceededError, null_reservation
from nce.tool_governance import GOVERNANCE, GovernanceUnavailable
from nce.tool_registry import TOOL_REGISTRY

log = logging.getLogger("nce-mcp")


_concurrency_semaphore: asyncio.Semaphore | None = None


def get_concurrency_semaphore() -> asyncio.Semaphore:
    global _concurrency_semaphore
    if _concurrency_semaphore is None:
        _concurrency_semaphore = asyncio.Semaphore(cfg.NCE_MAX_CONCURRENT_TOOLS)
    return _concurrency_semaphore


async def execute_call_tool(
    engine: NCEEngine | None,
    name: str,
    arguments: dict[str, Any],
) -> list[TextContent]:
    if engine is None:
        return _jsonrpc_error_response(
            -32603,
            "Internal error",
            detail="Engine not initialized",
        )

    # Governance gate (audit Domain 1, CWE-636/1188): last-known-good interceptor.
    # A positive hit returns the strict scope error; an unavailable registry
    # fails CLOSED with the same -32005 code (never -32603, never un-revokes).
    try:
        if await GOVERNANCE.is_disabled(engine.redis_client, name):
            return _jsonrpc_error_response(
                -32005,
                "Scope forbidden",
                detail=f"Tool '{name}' has been disabled by the administrator.",
            )
    except GovernanceUnavailable:
        return _jsonrpc_error_response(
            -32005,
            "Scope forbidden",
            detail="Tool governance registry unavailable; dispatch blocked.",
        )

    q_res = null_reservation()

    async with instrument_tool_call(name):
        try:
            try:
                enforce_mcp_tool_auth(name, arguments)
            except ScopeError as exc:
                return _jsonrpc_error_response(
                    -32005,
                    "Scope forbidden",
                    detail=client_visible_detail(exc.reason),
                )

            # --- Registry lookup — unknown tools fail fast before quota is consumed ---
            spec = TOOL_REGISTRY.get(name)
            if spec is None:
                raise UnknownToolError(name)

            # Migration gate: disabled tools return a plain message, no error envelope.
            if spec.migration and cfg.NCE_DISABLE_MIGRATION_MCP:
                return [
                    TextContent(
                        type="text",
                        text="Migration tools are disabled (NCE_DISABLE_MIGRATION_MCP=true).",
                    )
                ]

            # --- API response cache (before quota — FIX-020) ---
            cached_payload, cache_key = await _try_cached_mcp_tool_response(engine, name, arguments)
            if cached_payload is not None:
                return cached_payload

            async with get_concurrency_semaphore():
                # Quota is incremented only on cache miss, immediately before the tool runs.
                # Never increment on cache hit — see FIX-020.
                q_res = await _consume_quota_for_mcp_tool(
                    engine.pg_pool, name, arguments, engine.redis_client
                )

                # --- Handler call (quota is rolled back on any exception) ---
                try:
                    if spec.admin_only:
                        _check_admin(arguments)
                    result_text = await spec.handler(engine, arguments)
                    # Post-success: bump the generation counter so stale cached reads
                    # become unreachable.  Must run AFTER the handler so failed mutations
                    # do not cause unnecessary cache invalidation.
                    #
                    # D3: and therefore NEVER raised.  Everything below this line runs
                    # after the handler has already committed, so a Redis failure here
                    # would turn a landed write into -32603 -- inviting a client that
                    # retries on internal errors to write it twice.  The stale window
                    # degrades to TTL expiry, which is strictly better than a false
                    # failure.  Semantics deliberately mirror the REST twin,
                    # admin_handlers/_shared.py's bump_mcp_cache_generation, whose
                    # docstring argues this same case: "failing the HTTP response would
                    # invite the caller to retry a write that already landed."  The
                    # purge_document_cache call below was ALREADY guarded this way --
                    # the guard existed three lines under the call that needed it most.
                    if spec.mutation:
                        try:
                            await bump_cache_generation(engine.redis_client)
                        except Exception as bump_exc:  # noqa: BLE001 - see above
                            log.warning(
                                "%s: MCP cache generation bump failed after a committed "
                                "mutation; cacheable reads may serve stale data for up "
                                "to MCP_CACHE_TTL_S: %s",
                                name,
                                bump_exc,
                            )

                        doc_id = arguments.get("memory_id") or arguments.get("snapshot_id")
                        if name in ("forget_memory", "delete_snapshot") and doc_id:
                            ns_id = arguments.get("namespace_id")
                            if ns_id:
                                try:
                                    await purge_document_cache(
                                        engine.redis_client,
                                        namespace_id=str(ns_id),
                                        memory_id=str(doc_id),
                                    )
                                except Exception as exc:
                                    log.warning(
                                        "%s: document cache purge failed: %s",
                                        name,
                                        exc,
                                    )
                    if spec.cacheable and cache_key:
                        # Same rule, same reason: the handler has returned, so a
                        # failure to populate the cache must not become the caller's
                        # error.  A missing cache entry costs one recomputation.
                        try:
                            await engine.redis_client.setex(
                                cache_key, _MCP_CACHE_TTL_S, result_text
                            )
                        except Exception as cache_exc:  # noqa: BLE001 - see above
                            log.warning("%s: MCP response cache write failed: %s", name, cache_exc)
                    return [TextContent(type="text", text=result_text)]
                except BaseException:
                    # BaseException catches asyncio.CancelledError (Python ≥ 3.8) so
                    # quota is rolled back even when the task is cancelled mid-call.
                    try:
                        await q_res.rollback()
                    except Exception as roll_exc:
                        log.warning(
                            "Quota rollback failed (not masking original exception): %s", roll_exc
                        )
                    raise

        except McpError as e:
            return _jsonrpc_error_response(e.code, e.message, data=e.data)
        except ScopeError as e:
            return _jsonrpc_error_response(
                -32005,
                "Scope forbidden",
                data={"reason": "scope_forbidden", "required_scope": e.required_scope},
                detail=client_visible_detail(e.reason or str(e)),
            )
        except RateLimitError as e:
            return _jsonrpc_error_response(
                -32029,
                "Rate limit exceeded",
                data={"reason": "rate_limited"},
                detail=client_visible_detail(str(e)),
            )
        except QuotaExceededError as e:
            return _jsonrpc_error_response(
                -32013,
                "Resource quota exceeded",
                data={"reason": "quota_exceeded"},
                detail=client_visible_detail(str(e)),
            )
        except (ValueError, TypeError) as e:
            msg = str(e)
            if msg.startswith(MCP_QUOTA_EXCEEDED_PREFIX):
                return _jsonrpc_error_response(
                    -32013,
                    "Resource quota exceeded",
                    data={"reason": "quota_exceeded"},
                    detail=client_visible_detail(msg),
                )
            if msg.startswith("Rate limit exceeded"):
                return _jsonrpc_error_response(
                    -32029,
                    "Rate limit exceeded",
                    data={"reason": "rate_limited"},
                    detail=client_visible_detail(msg),
                )
            return _jsonrpc_error_response(
                -32602,
                "Invalid params",
                data={"reason": "invalid_params"},
                detail=client_visible_detail(msg),
            )
        except Exception as e:
            log.exception("Unhandled error in tool '%s'", name)
            return _jsonrpc_error_response(
                -32603,
                "Internal error",
                data=internal_error_data(e),
            )
