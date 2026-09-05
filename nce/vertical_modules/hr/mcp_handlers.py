"""
nce/vertical_modules/hr/mcp_handlers.py
=======================================
MCP tool handlers for Module 13 (HR Engine):
  - handle_hr_get_employee: Watcher; read-only, cacheable (access-scoped).
  - handle_hr_match_skills: Advisor; read-only, cacheable (RL-1: NEVER ranking).
  - handle_hr_capacity: Advisor; read-only, cacheable.
  - handle_hr_cert_status: Watcher; read-only, cacheable.
  - handle_hr_register_absence: Actor; mutation.
  - handle_hr_build_onboarding_quest: Actor; mutation, admin_only.
  - handle_hr_log_one_on_one: Actor; mutation, admin_only (RL-3 PII redaction).
  - handle_hr_coach: Advisor; read-only, cacheable (RL-1: strictly individual).

Flags mirror the HR Engine contract:
| Tool                       | cacheable | admin_only | mutation | AI-role |
|----------------------------|-----------|------------|----------|---------|
| hr_get_employee            | Y         | N          | N        | Watcher |
| hr_match_skills            | Y         | N          | N        | Advisor |
| hr_capacity                | Y         | N          | N        | Advisor |
| hr_cert_status             | Y         | N          | N        | Watcher |
| hr_register_absence        | N         | N          | Y        | Actor   |
| hr_build_onboarding_quest  | N         | Y          | Y        | Actor   |
| hr_log_one_on_one          | N         | Y          | Y        | Actor   |
| hr_coach                   | Y         | N          | N        | Advisor |
"""

from __future__ import annotations

import json
import logging
from typing import Any

from nce.mcp_args import require_namespace_id
from nce.mcp_errors import MCP_SCOPE_FORBIDDEN, McpError, mcp_handler
from nce.vertical_modules.hr._guard import (
    HrDisabledError,
    HrRankingProhibitedError,
    require_hr_enabled,
)
from nce.vertical_modules.hr.absences import do_register_absence
from nce.vertical_modules.hr.capacity import do_capacity
from nce.vertical_modules.hr.certs import do_cert_status
from nce.vertical_modules.hr.coaching import do_coach, do_log_one_on_one
from nce.vertical_modules.hr.onboarding import do_build_onboarding_quest
from nce.vertical_modules.hr.profile import do_get_employee
from nce.vertical_modules.hr.skills import do_match_skills

log = logging.getLogger("nce.vertical_modules.hr.mcp_handlers")

_MCP_DISABLED_CODE: int = MCP_SCOPE_FORBIDDEN  # -32005


def _extract_pool(engine_or_pool: Any) -> Any:
    if hasattr(engine_or_pool, "pg_pool") and (
        "pg_pool" in getattr(engine_or_pool, "__dict__", {})
        or hasattr(type(engine_or_pool), "pg_pool")
    ):
        return engine_or_pool.pg_pool
    return engine_or_pool


async def _check_hr_enabled(engine: Any, arguments: dict[str, Any]) -> str:
    namespace_id = require_namespace_id(arguments)
    pool = _extract_pool(engine)
    try:
        await require_hr_enabled(pool, namespace_id)
    except HrDisabledError as exc:
        raise McpError(
            _MCP_DISABLED_CODE,
            "HR vertical is not enabled for this namespace",
            data={"reason": "hr_disabled", "detail": str(exc)},
        ) from exc
    return namespace_id


# ---------------------------------------------------------------------------
# MCP Handlers
# ---------------------------------------------------------------------------


@mcp_handler
async def handle_hr_get_employee(engine: Any, arguments: dict[str, Any]) -> str:
    """MCP tool: hr_get_employee — retrieve profile card, skills, and certifications."""
    namespace_id = await _check_hr_enabled(engine, arguments)
    params = dict(arguments)
    params["namespace_id"] = namespace_id
    try:
        res = await do_get_employee(engine, params)
    except ValueError as exc:
        raise McpError(-32602, str(exc)) from exc
    return json.dumps(res)


@mcp_handler
async def handle_hr_match_skills(engine: Any, arguments: dict[str, Any]) -> str:
    """MCP tool: hr_match_skills — match candidates against requirement criteria with rationale (RL-1 NEVER ranking)."""
    namespace_id = await _check_hr_enabled(engine, arguments)
    params = dict(arguments)
    params["namespace_id"] = namespace_id
    try:
        res = await do_match_skills(engine, params)
    except HrRankingProhibitedError as exc:
        raise McpError(MCP_SCOPE_FORBIDDEN, str(exc)) from exc
    except ValueError as exc:
        raise McpError(-32602, str(exc)) from exc
    return json.dumps(res)


@mcp_handler
async def handle_hr_capacity(engine: Any, arguments: dict[str, Any]) -> str:
    """MCP tool: hr_capacity — compute workload and utilization over forecast horizon."""
    namespace_id = await _check_hr_enabled(engine, arguments)
    params = dict(arguments)
    params["namespace_id"] = namespace_id
    try:
        res = await do_capacity(engine, params)
    except ValueError as exc:
        raise McpError(-32602, str(exc)) from exc
    return json.dumps(res)


@mcp_handler
async def handle_hr_cert_status(engine: Any, arguments: dict[str, Any]) -> str:
    """MCP tool: hr_cert_status — check certification validity and impending expirations."""
    namespace_id = await _check_hr_enabled(engine, arguments)
    params = dict(arguments)
    params["namespace_id"] = namespace_id
    try:
        res = await do_cert_status(engine, params)
    except ValueError as exc:
        raise McpError(-32602, str(exc)) from exc
    return json.dumps(res)


@mcp_handler
async def handle_hr_register_absence(engine: Any, arguments: dict[str, Any]) -> str:
    """MCP tool: hr_register_absence — register employee absence or leave event."""
    namespace_id = await _check_hr_enabled(engine, arguments)
    params = dict(arguments)
    params["namespace_id"] = namespace_id
    try:
        res = await do_register_absence(engine, params)
    except ValueError as exc:
        raise McpError(-32602, str(exc)) from exc
    return json.dumps(res)


@mcp_handler
async def handle_hr_build_onboarding_quest(engine: Any, arguments: dict[str, Any]) -> str:
    """MCP tool: hr_build_onboarding_quest — generate structured 90-day onboarding checklist."""
    namespace_id = await _check_hr_enabled(engine, arguments)
    params = dict(arguments)
    params["namespace_id"] = namespace_id
    try:
        res = await do_build_onboarding_quest(engine, params)
    except ValueError as exc:
        raise McpError(-32602, str(exc)) from exc
    return json.dumps(res)


@mcp_handler
async def handle_hr_log_one_on_one(engine: Any, arguments: dict[str, Any]) -> str:
    """MCP tool: hr_log_one_on_one — record 1-on-1 coaching session with PII redaction."""
    namespace_id = await _check_hr_enabled(engine, arguments)
    params = dict(arguments)
    params["namespace_id"] = namespace_id
    try:
        res = await do_log_one_on_one(engine, params)
    except ValueError as exc:
        raise McpError(-32602, str(exc)) from exc
    return json.dumps(res)


@mcp_handler
async def handle_hr_coach(engine: Any, arguments: dict[str, Any]) -> str:
    """MCP tool: hr_coach — individual skill advancement advisor (RL-1: NEVER ranking)."""
    namespace_id = await _check_hr_enabled(engine, arguments)
    params = dict(arguments)
    params["namespace_id"] = namespace_id
    try:
        res = await do_coach(engine, params)
    except HrRankingProhibitedError as exc:
        raise McpError(MCP_SCOPE_FORBIDDEN, str(exc)) from exc
    except ValueError as exc:
        raise McpError(-32602, str(exc)) from exc
    return json.dumps(res)
