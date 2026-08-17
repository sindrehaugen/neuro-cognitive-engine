"""
nce/vertical_modules/project/mcp_handlers.py
============================================
MCP tool handlers for the Project vertical module.

Phase 1 (P1) — first surface.  No DB, no external HTTP, no graph writes.
Phase 2 (P2) — Sales→Project bridge: ``handle_project_convert_signed_quote``.
Wave 4a (M7.W4a) — phase-transition Actor: ``handle_project_advance_phase``.

Public entry-points:
  ``handle_project_can_enter_phase`` — thin adapter over the pure W1
  ``phase_gates.can_enter_phase`` state-machine.  Returns a JSON string
  with ``{"ok": bool, "missing_criteria": list[str]}``.

  ``handle_project_convert_signed_quote`` — thin adapter over the P2
  ``convert.do_convert_signed_quote`` bridge.  Actor / autonomous-by-tier;
  ``mutation=True, admin_only=True``.  Returns a JSON string with
  ``{"project_id": str, "gate": str, "bom_lines_linked": int,
  "degraded": bool, "degraded_reasons": list[str], "degraded_detail": str | None,
  "baseline": ...}``.

  ``handle_project_advance_phase`` — thin adapter over ``advance.do_advance_phase``.
  Actor; ``mutation=True, admin_only=True``.  Returns a JSON string with one of:
  ``{"ok": True, "phase": str}`` /
  ``{"ok": True, "phase": str, "noop": True}`` /
  ``{"ok": False, "missing_criteria": [...], "current_phase": str}`` /
  ``{"ok": False, "error": str}``.

Registered in ``nce/tool_registry.py`` via:
  ``_h(project_mcp_handlers, "handle_project_can_enter_phase")``
  ``_h(project_mcp_handlers, "handle_project_convert_signed_quote")``
  ``_h(project_mcp_handlers, "handle_project_advance_phase")``
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from nce.mcp_args import require_namespace_id
from nce.mcp_errors import mcp_handler
from nce.vertical_modules.project.advance import do_advance_phase
from nce.vertical_modules.project.convert import do_convert_signed_quote
from nce.vertical_modules.project.phase_gates import can_enter_phase
from nce.vertical_modules.project.recall import do_suggest_pl

if TYPE_CHECKING:
    from nce.orchestrator import NCEEngine

log = logging.getLogger("nce.vertical_modules.project.mcp_handlers")


@mcp_handler
async def handle_project_can_enter_phase(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """MCP tool: project_can_enter_phase — phase-gate readiness check.

    Requires ``namespace_id``, ``project`` (dict with ``current_phase`` and
    ``criteria_met``), and ``target_phase`` (str) in *arguments*.

    Returns ``{"ok": bool, "missing_criteria": list[str]}`` on success.
    The ``@mcp_handler`` decorator converts a missing-namespace ``ValueError``
    into an ``McpError(-32602)`` at call-site.

    This is a pure read — no DB, no HTTP, no side effects.  The handler is a
    thin adapter; all gate logic lives in ``phase_gates.can_enter_phase``.
    """
    require_namespace_id(arguments)
    project: dict[str, Any] = dict(arguments.get("project") or {})
    target_phase: str = str(arguments.get("target_phase") or "")
    result = can_enter_phase(project, target_phase)
    return json.dumps(result, default=str)


@mcp_handler
async def handle_project_convert_signed_quote(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """MCP tool: project_convert_signed_quote — Sales→Project bridge (P2).

    Requires ``namespace_id``, ``quote_id``, ``signed_by``, and
    ``signature_ref`` in *arguments*.

    Reads the Sales-frozen baseline via the W2 A2A seam, then materialises
    ``PROJECT_PROJECT`` + ``PROJECT_GATE@G0`` + ``PROJECT_TASK`` nodes and
    ``PROJECT -[contains]-> BOM_LINE`` edges onto existing BOM_LINE nodes.

    Returns
    ``{"project_id": str, "gate": str, "bom_lines_linked": int, "degraded": bool,
    "degraded_reasons": list[str], "degraded_detail": str | None, "baseline": {...}}``.

    ``degraded`` is True when the conversion succeeded structurally but is
    incomplete — notably ``no_bom_lines_in_graph``, meaning the project was
    created with an empty bill of materials because no ``BOM_LINE`` nodes
    exist in NCE for the quote.  Do not read a non-error result as proof the
    project is fully populated.

    Actor / Autonomous-by-tier (mutation=True, admin_only=True).  The handler
    is a thin adapter; all conversion logic lives in ``convert.do_convert_signed_quote``.

    Idempotent on ``quote_id``: a re-run returns the same ``project_id`` and
    creates no duplicate nodes or edges.
    """
    require_namespace_id(arguments)
    params: dict[str, Any] = dict(arguments)
    result = await do_convert_signed_quote(engine, params)
    return json.dumps(result, default=str)


@mcp_handler
async def handle_project_advance_phase(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """MCP tool: project_advance_phase — phase-transition Actor (M7.W4a).

    Requires ``namespace_id``, ``project_id``, ``target_phase``, and
    ``actor`` in *arguments*.  Optionally accepts ``criteria_met``
    (list[str]; default ``[]``).

    Calls ``advance.do_advance_phase`` which (1) reads the current phase
    from the graph, (2) validates the transition via the pure
    ``can_enter_phase`` gate, and (3) on success atomically upserts the new
    ``PROJECT_GATE`` node, moves the ``in_phase`` edge, and appends one
    ``project_phase_advanced`` row to the WORM ``event_log``.

    Returns one of:
    - ``{"ok": True, "phase": str}``                 — transition succeeded.
    - ``{"ok": True, "phase": str, "noop": True}``   — already in target phase.
    - ``{"ok": False, "missing_criteria": [...], "current_phase": str}``
    - ``{"ok": False, "error": str}``

    Actor / admin-only (``mutation=True, admin_only=True``).  The handler
    is a thin adapter; all domain logic lives in ``advance.do_advance_phase``.
    """
    require_namespace_id(arguments)
    params: dict[str, Any] = dict(arguments)
    result = await do_advance_phase(engine, params)
    return json.dumps(result, default=str)


@mcp_handler
async def handle_project_suggest_pl(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """MCP tool: project_suggest_pl — Suggest project lead (Module 7.Wave 11).

    Requires ``namespace_id`` and ``project_id`` in *arguments*.
    """
    require_namespace_id(arguments)
    params: dict[str, Any] = dict(arguments)
    result = await do_suggest_pl(engine, params)
    return json.dumps(result, default=str)
