"""
nce/vertical_modules/support/mcp_handlers.py
============================================
MCP tool handlers for Module 10 (Support Engine):
  - handle_support_query_ticket: Watcher; read-only, cacheable.
  - handle_support_open_ticket: Actor; mutation, admin_only.
  - handle_support_sla_clock: Watcher; read-only, cacheable.
  - handle_support_health_score: Watcher; read-only, cacheable.
  - handle_support_troubleshoot: Watcher; read-only, cacheable.
  - handle_support_resolve_ticket: Actor; mutation, admin_only.

Flags mirror the Support Engine contract:
| Tool                     | cacheable | admin_only | mutation |
|--------------------------|-----------|------------|----------|
| support_query_ticket     | Y         | N          | N        |
| support_open_ticket      | N         | Y          | Y        |
| support_sla_clock        | Y         | N          | N        |
| support_health_score     | Y         | N          | N        |
| support_troubleshoot     | Y         | N          | N        |
| support_resolve_ticket   | N         | Y          | Y        |

Opt-In Guard (Charter §5.5 & Pattern)
--------------------------------------
Applied at handler boundary: _check_support_enabled calls require_support_enabled
and raises McpError(-32005) when disabled.

Domain Refusals
---------------
TicketNotFoundError, TicketAlreadyResolvedError, InvalidTicketStatusError
are mapped to McpError(-32005) with structured machine-readable reason data.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from nce.mcp_args import require_namespace_id
from nce.mcp_errors import MCP_SCOPE_FORBIDDEN, McpError, mcp_handler
from nce.vertical_modules.support._guard import (
    SupportDisabledError,
    require_support_enabled,
)
from nce.vertical_modules.support.health import do_health_score, do_record_touchpoint
from nce.vertical_modules.support.sla import do_sla_clock
from nce.vertical_modules.support.tickets import (
    InvalidTicketStatusError,
    TicketAlreadyResolvedError,
    TicketNotFoundError,
    do_open_ticket,
    do_query_ticket,
    do_resolve_ticket,
)
from nce.vertical_modules.support.triage import do_triage_ticket
from nce.vertical_modules.support.troubleshoot import do_troubleshoot

log = logging.getLogger("nce.vertical_modules.support.mcp_handlers")

_MCP_SUPPORT_DISABLED_CODE: int = MCP_SCOPE_FORBIDDEN  # -32005
_MCP_BUSINESS_REFUSED_CODE: int = MCP_SCOPE_FORBIDDEN  # -32005


def _extract_pool(engine_or_pool: Any) -> Any:
    """Extract an asyncpg pool or pool-like object from engine or pool."""
    if hasattr(engine_or_pool, "pg_pool") and (
        "pg_pool" in getattr(engine_or_pool, "__dict__", {})
        or hasattr(type(engine_or_pool), "pg_pool")
    ):
        return engine_or_pool.pg_pool
    return engine_or_pool


async def _check_support_enabled(engine: Any, arguments: dict[str, Any]) -> str:
    """Check namespace opt-in; raise McpError(-32005) if not enabled.

    Returns the validated canonical namespace_id UUID string.
    """
    namespace_id = require_namespace_id(arguments)
    pool = _extract_pool(engine)
    try:
        await require_support_enabled(pool, namespace_id)
    except SupportDisabledError as exc:
        raise McpError(
            _MCP_SUPPORT_DISABLED_CODE,
            "Support vertical is not enabled for this namespace",
            data={"reason": "support_disabled", "detail": str(exc)},
        ) from exc
    return namespace_id


# ---------------------------------------------------------------------------
# MCP Handlers
# ---------------------------------------------------------------------------


@mcp_handler
async def handle_support_query_ticket(engine: Any, arguments: dict[str, Any]) -> str:
    """MCP tool: support_query_ticket — retrieve single ticket or list tickets.

    Watcher; read-only, cacheable. Requires ``namespace_id``.
    Optional ``ticket_id`` returns single ticket with SLA clock.
    """
    await _check_support_enabled(engine, arguments)
    try:
        result = await do_query_ticket(engine, dict(arguments))
    except TicketNotFoundError as exc:
        raise McpError(
            _MCP_BUSINESS_REFUSED_CODE,
            str(exc),
            data={"reason": "ticket_not_found", "ticket_id": exc.ticket_id},
        ) from exc
    return json.dumps({"ok": True, **result}, default=str)


@mcp_handler
async def handle_support_open_ticket(engine: Any, arguments: dict[str, Any]) -> str:
    """MCP tool: support_open_ticket — open a new service ticket and init SLA clock.

    Actor; mutation, admin_only. Requires ``namespace_id``, ``summary``.
    """
    await _check_support_enabled(engine, arguments)
    result = await do_open_ticket(engine, dict(arguments))
    return json.dumps({"ok": True, **result}, default=str)


@mcp_handler
async def handle_support_sla_clock(engine: Any, arguments: dict[str, Any]) -> str:
    """MCP tool: support_sla_clock — query and evaluate running SLA clock.

    Watcher; read-only, cacheable. Requires ``namespace_id``, ``ticket_id``.
    """
    await _check_support_enabled(engine, arguments)
    try:
        result = await do_sla_clock(engine, dict(arguments))
    except TicketNotFoundError as exc:
        raise McpError(
            _MCP_BUSINESS_REFUSED_CODE,
            str(exc),
            data={"reason": "ticket_not_found", "ticket_id": exc.ticket_id},
        ) from exc
    return json.dumps({"ok": True, **result}, default=str)


@mcp_handler
async def handle_support_health_score(engine: Any, arguments: dict[str, Any]) -> str:
    """MCP tool: support_health_score — compute and upsert rolling customer health.

    Watcher; read-only, cacheable. Requires ``namespace_id``, ``customer_id``.
    """
    await _check_support_enabled(engine, arguments)
    result = await do_health_score(engine, dict(arguments))
    return json.dumps({"ok": True, **result}, default=str)


@mcp_handler
async def handle_support_troubleshoot(engine: Any, arguments: dict[str, Any]) -> str:
    """MCP tool: support_troubleshoot — AI Troubleshooter cognitive recall.

    Watcher; read-only, cacheable. Requires ``namespace_id``, and ``symptom_text``
    or ``ticket_id``.
    """
    await _check_support_enabled(engine, arguments)
    try:
        result = await do_troubleshoot(engine, dict(arguments))
    except TicketNotFoundError as exc:
        raise McpError(
            _MCP_BUSINESS_REFUSED_CODE,
            str(exc),
            data={"reason": "ticket_not_found", "ticket_id": exc.ticket_id},
        ) from exc
    return json.dumps({"ok": True, **result}, default=str)


@mcp_handler
async def handle_support_resolve_ticket(engine: Any, arguments: dict[str, Any]) -> str:
    """MCP tool: support_resolve_ticket — resolve ticket and record in cognitive ledger.

    Actor; mutation, admin_only. Requires ``namespace_id``, ``ticket_id``,
    and ``resolution_text``.
    """
    await _check_support_enabled(engine, arguments)
    try:
        result = await do_resolve_ticket(engine, dict(arguments))
    except TicketNotFoundError as exc:
        raise McpError(
            _MCP_BUSINESS_REFUSED_CODE,
            str(exc),
            data={"reason": "ticket_not_found", "ticket_id": exc.ticket_id},
        ) from exc
    except TicketAlreadyResolvedError as exc:
        raise McpError(
            _MCP_BUSINESS_REFUSED_CODE,
            str(exc),
            data={
                "reason": "ticket_already_resolved",
                "ticket_id": exc.ticket_id,
                "status": exc.status,
            },
        ) from exc
    except InvalidTicketStatusError as exc:
        raise McpError(
            _MCP_BUSINESS_REFUSED_CODE,
            str(exc),
            data={
                "reason": "invalid_ticket_status",
                "ticket_id": exc.ticket_id,
                "status": exc.status,
            },
        ) from exc
    return json.dumps({"ok": True, **result}, default=str)


@mcp_handler
async def handle_support_triage_ticket(engine: Any, arguments: dict[str, Any]) -> str:
    """MCP tool: support_triage_ticket — triage ticket priority, urgency, and routing.

    Advisor; read-only, cacheable. Requires ``namespace_id``, and ``ticket_id``.
    """
    await _check_support_enabled(engine, arguments)
    try:
        result = await do_triage_ticket(engine, dict(arguments))
    except TicketNotFoundError as exc:
        raise McpError(
            _MCP_BUSINESS_REFUSED_CODE,
            str(exc),
            data={"reason": "ticket_not_found", "ticket_id": exc.ticket_id},
        ) from exc
    return json.dumps({"ok": True, **result}, default=str)


@mcp_handler
async def handle_support_record_touchpoint(engine: Any, arguments: dict[str, Any]) -> str:
    """MCP tool: support_record_touchpoint — record ÉT-spørsmål touchpoint and update health.

    Actor; mutation. Requires ``namespace_id``, ``customer_id``, and ``answer``.
    """
    await _check_support_enabled(engine, arguments)
    result = await do_record_touchpoint(engine, dict(arguments))
    return json.dumps({"ok": True, **result}, default=str)
