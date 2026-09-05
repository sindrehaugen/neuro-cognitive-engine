"""
nce/vertical_modules/field_tech/mcp_handlers.py
================================================
MCP tool handlers for Module 12 (Field Tech Engine):
  - handle_field_tech_dispatch: Advisor; read-only, cacheable.
  - handle_field_tech_partner_view: Advisor; read-only, cacheable (partner-scoped).
  - handle_field_tech_create_work_order: Actor; mutation, admin_only.
  - handle_field_tech_assign: Actor; mutation, admin_only.
  - handle_field_tech_complete_checklist: Actor; mutation.
  - handle_field_tech_scan_serial: Actor; mutation.
  - handle_field_tech_log_time: Actor; mutation.
  - handle_field_tech_attach_photo: Capture; mutation.
  - handle_field_tech_sync: Offline reconcile; mutation.
  - handle_field_tech_record_outcome: Ledger append; mutation, admin_only.

Flags mirror the Field Tech Engine contract:
| Tool                           | cacheable | admin_only | mutation | AI-role |
|--------------------------------|-----------|------------|----------|---------|
| field_tech_dispatch            | Y         | N          | N        | Advisor |
| field_tech_partner_view        | Y         | N          | N        | Advisor |
| field_tech_create_work_order   | N         | Y          | Y        | Actor   |
| field_tech_assign              | N         | Y          | Y        | Actor   |
| field_tech_complete_checklist  | N         | N          | Y        | Actor   |
| field_tech_scan_serial         | N         | N          | Y        | Actor   |
| field_tech_log_time            | N         | N          | Y        | Actor   |
| field_tech_attach_photo        | N         | N          | Y        | Capture |
| field_tech_sync                | N         | N          | Y        | Sync    |
| field_tech_record_outcome      | N         | Y          | Y        | Ledger  |
"""

from __future__ import annotations

import json
import logging
from typing import Any

from nce.mcp_args import require_namespace_id
from nce.mcp_errors import MCP_SCOPE_FORBIDDEN, McpError, mcp_handler
from nce.vertical_modules.field_tech._guard import (
    FieldTechDisabledError,
    require_field_tech_enabled,
)
from nce.vertical_modules.field_tech.checklist import (
    ChecklistIncompleteError,
    ChecklistNotFoundError,
    do_complete_checklist,
)
from nce.vertical_modules.field_tech.dispatch import do_dispatch
from nce.vertical_modules.field_tech.outcome import do_record_outcome
from nce.vertical_modules.field_tech.partner_view import do_partner_view
from nce.vertical_modules.field_tech.photo import do_attach_photo
from nce.vertical_modules.field_tech.scan import do_scan_serial
from nce.vertical_modules.field_tech.sync import do_sync
from nce.vertical_modules.field_tech.time_entry import do_log_time
from nce.vertical_modules.field_tech.work_orders import (
    WorkOrderInvalidTransitionError,
    WorkOrderNotFoundError,
    do_assign,
    do_create_work_order,
)

log = logging.getLogger("nce.vertical_modules.field_tech.mcp_handlers")

_MCP_DISABLED_CODE: int = MCP_SCOPE_FORBIDDEN  # -32005
_MCP_REFUSED_CODE: int = MCP_SCOPE_FORBIDDEN  # -32005


def _extract_pool(engine_or_pool: Any) -> Any:
    if hasattr(engine_or_pool, "pg_pool") and (
        "pg_pool" in getattr(engine_or_pool, "__dict__", {})
        or hasattr(type(engine_or_pool), "pg_pool")
    ):
        return engine_or_pool.pg_pool
    return engine_or_pool


async def _check_field_tech_enabled(engine: Any, arguments: dict[str, Any]) -> str:
    namespace_id = require_namespace_id(arguments)
    pool = _extract_pool(engine)
    try:
        await require_field_tech_enabled(pool, namespace_id)
    except FieldTechDisabledError as exc:
        raise McpError(
            _MCP_DISABLED_CODE,
            "Field Tech vertical is not enabled for this namespace",
            data={"reason": "field_tech_disabled", "detail": str(exc)},
        ) from exc
    return namespace_id


# ---------------------------------------------------------------------------
# MCP Handlers
# ---------------------------------------------------------------------------


@mcp_handler
async def handle_field_tech_dispatch(engine: Any, arguments: dict[str, Any]) -> str:
    """MCP tool: field_tech_dispatch — AI dispatch advisor ranking candidate technicians."""
    namespace_id = await _check_field_tech_enabled(engine, arguments)
    params = dict(arguments)
    params["namespace_id"] = namespace_id
    try:
        res = await do_dispatch(engine, params)
    except ValueError as exc:
        raise McpError(-32602, str(exc)) from exc
    return json.dumps(res)


@mcp_handler
async def handle_field_tech_partner_view(engine: Any, arguments: dict[str, Any]) -> str:
    """MCP tool: field_tech_partner_view — partner-scoped, field-redacted work order projection."""
    namespace_id = await _check_field_tech_enabled(engine, arguments)
    params = dict(arguments)
    params["namespace_id"] = namespace_id
    try:
        res = await do_partner_view(engine, params)
    except ValueError as exc:
        raise McpError(-32602, str(exc)) from exc
    return json.dumps(res)


@mcp_handler
async def handle_field_tech_create_work_order(engine: Any, arguments: dict[str, Any]) -> str:
    """MCP tool: field_tech_create_work_order — create a work order from project or ticket."""
    namespace_id = await _check_field_tech_enabled(engine, arguments)
    params = dict(arguments)
    params["namespace_id"] = namespace_id
    try:
        res = await do_create_work_order(engine, params)
    except ValueError as exc:
        raise McpError(-32602, str(exc)) from exc
    return json.dumps(res)


@mcp_handler
async def handle_field_tech_assign(engine: Any, arguments: dict[str, Any]) -> str:
    """MCP tool: field_tech_assign — assign a work order to an employee or contractor."""
    namespace_id = await _check_field_tech_enabled(engine, arguments)
    params = dict(arguments)
    params["namespace_id"] = namespace_id
    try:
        res = await do_assign(engine, params)
    except WorkOrderNotFoundError as exc:
        raise McpError(_MCP_REFUSED_CODE, str(exc), data={"reason": "not_found"}) from exc
    except WorkOrderInvalidTransitionError as exc:
        raise McpError(_MCP_REFUSED_CODE, str(exc), data={"reason": "invalid_transition"}) from exc
    except ValueError as exc:
        raise McpError(-32602, str(exc)) from exc
    return json.dumps(res)


@mcp_handler
async def handle_field_tech_complete_checklist(engine: Any, arguments: dict[str, Any]) -> str:
    """MCP tool: field_tech_complete_checklist — record ISO9001 quality checklist verification."""
    namespace_id = await _check_field_tech_enabled(engine, arguments)
    params = dict(arguments)
    params["namespace_id"] = namespace_id
    try:
        res = await do_complete_checklist(engine, params)
    except ChecklistIncompleteError as exc:
        raise McpError(
            _MCP_REFUSED_CODE, str(exc), data={"reason": "checklist_incomplete"}
        ) from exc
    except ChecklistNotFoundError as exc:
        raise McpError(_MCP_REFUSED_CODE, str(exc), data={"reason": "not_found"}) from exc
    except ValueError as exc:
        raise McpError(-32602, str(exc)) from exc
    return json.dumps(res)


@mcp_handler
async def handle_field_tech_scan_serial(engine: Any, arguments: dict[str, Any]) -> str:
    """MCP tool: field_tech_scan_serial — record equipment S/N scan seeding Assets register edge."""
    namespace_id = await _check_field_tech_enabled(engine, arguments)
    params = dict(arguments)
    params["namespace_id"] = namespace_id
    try:
        res = await do_scan_serial(engine, params)
    except ValueError as exc:
        raise McpError(-32602, str(exc)) from exc
    return json.dumps(res)


@mcp_handler
async def handle_field_tech_log_time(engine: Any, arguments: dict[str, Any]) -> str:
    """MCP tool: field_tech_log_time — record GPS or manual labor time entry with dedup."""
    namespace_id = await _check_field_tech_enabled(engine, arguments)
    params = dict(arguments)
    params["namespace_id"] = namespace_id
    try:
        res = await do_log_time(engine, params)
    except ValueError as exc:
        raise McpError(-32602, str(exc)) from exc
    return json.dumps(res)


@mcp_handler
async def handle_field_tech_attach_photo(engine: Any, arguments: dict[str, Any]) -> str:
    """MCP tool: field_tech_attach_photo — attach documentation photo to work order."""
    namespace_id = await _check_field_tech_enabled(engine, arguments)
    params = dict(arguments)
    params["namespace_id"] = namespace_id
    try:
        res = await do_attach_photo(engine, params)
    except ValueError as exc:
        raise McpError(-32602, str(exc)) from exc
    return json.dumps(res)


@mcp_handler
async def handle_field_tech_sync(engine: Any, arguments: dict[str, Any]) -> str:
    """MCP tool: field_tech_sync — reconcile offline client mutation queue with server-sequence ordering."""
    namespace_id = await _check_field_tech_enabled(engine, arguments)
    params = dict(arguments)
    params["namespace_id"] = namespace_id
    try:
        res = await do_sync(engine, params)
    except ValueError as exc:
        raise McpError(-32602, str(exc)) from exc
    return json.dumps(res)


@mcp_handler
async def handle_field_tech_record_outcome(engine: Any, arguments: dict[str, Any]) -> str:
    """MCP tool: field_tech_record_outcome — append WO outcome to v3_cognitive_ledger."""
    namespace_id = await _check_field_tech_enabled(engine, arguments)
    params = dict(arguments)
    params["namespace_id"] = namespace_id
    try:
        res = await do_record_outcome(engine, params)
    except ValueError as exc:
        raise McpError(-32602, str(exc)) from exc
    return json.dumps(res)
