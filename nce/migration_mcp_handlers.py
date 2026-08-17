"""
MCP tool handlers for embedding model migrations (§11). Extracted from server.py:call_tool().
Follows the same pattern as bridge_mcp_handlers.py — each handler receives the engine
and raw arguments dict, and returns a JSON string that call_tool() wraps in TextContent.

RBAC is enforced by the ``@require_scope("admin")`` decorator on every handler.
The decorator validates ``admin_api_key`` against ``NCE_ADMIN_API_KEY`` (constant-time),
strips auth keys from arguments before they reach ``extra='forbid'`` domain models, and
forwards ``admin_identity`` as a keyword argument to handlers that declare it.

Pre-flight WORM audit logging: every migration mutation handler (start_migration,
commit_migration, abort_migration) writes an irrefutable ``append_event`` audit record
on a **separate** PG connection with its own transaction BEFORE the migration
orchestrator is invoked.  If the audit write fails, the migration is rejected —
the audit gate is the security boundary.  The audit connection is independent of
the migration transaction, guaranteeing the audit record survives even if the
migration transaction rolls back.

On scope violation the decorator raises :class:`nce.auth.ScopeError`, which
:func:`call_tool` lets propagate unchanged so the MCP framework produces a JSON-RPC
error response (code ``-32005`` — scope forbidden).
"""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID

from nce.auth import require_scope
from nce.mcp_errors import mcp_handler
from nce.migration_gate import _SYSTEM_NAMESPACE  # noqa: F401  (re-export for back-compat)
from nce.migration_gate import audit_migration_action as _audit_migration_action
from nce.orchestrator import NCEEngine

log = logging.getLogger("nce.migration_mcp_handlers")


# ---------------------------------------------------------------------------
# Pre-flight audit helper — relocated to nce.migration_gate (the shared
# enforcement chokepoint) so the engine, the MCP tools, and the admin HTTP API
# all use one implementation.  Re-exported here under its historical name for
# the remaining handlers (abort) that emit audit events directly.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


@require_scope("admin")
@mcp_handler
async def handle_start_migration(
    engine: NCEEngine,
    arguments: dict[str, Any],
    admin_identity: str | None = None,
) -> str:
    """[ADMIN] Start a re-embedding migration to a new model.

    Input validation only — the dimension preflight and pre-flight WORM audit live
    in :meth:`NCEEngine.start_migration` (the shared engine chokepoint) so the admin
    HTTP route is gated identically.  This handler must not re-implement the gate.
    """
    target_model_id = arguments.get("target_model_id")
    if not target_model_id or not str(target_model_id).strip():
        raise ValueError("target_model_id is required")
    target_model_id = str(target_model_id).strip()
    if len(target_model_id) > 128:
        raise ValueError(f"target_model_id too long ({len(target_model_id)} chars, max 128)")

    res = await engine.start_migration(target_model_id, admin_identity=admin_identity)
    return json.dumps(res, default=str)


@require_scope("admin")
@mcp_handler
async def handle_migration_status(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """[ADMIN] Check the status of a running migration."""
    raw_mid = arguments.get("migration_id")
    if not raw_mid:
        raise ValueError("migration_id is required")
    try:
        migration_id = str(UUID(str(raw_mid).strip()))
    except (ValueError, AttributeError):
        raise ValueError("migration_id must be a valid UUID")

    res = await engine.migration_status(migration_id)
    return json.dumps(res, default=str)


@require_scope("admin")
@mcp_handler
async def handle_validate_migration(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """[ADMIN] Validate the results of a completed migration."""
    raw_mid = arguments.get("migration_id")
    if not raw_mid:
        raise ValueError("migration_id is required")
    try:
        migration_id = str(UUID(str(raw_mid).strip()))
    except (ValueError, AttributeError):
        raise ValueError("migration_id must be a valid UUID")

    res = await engine.validate_migration(migration_id)
    return json.dumps(res, default=str)


@require_scope("admin")
@mcp_handler
async def handle_commit_migration(
    engine: NCEEngine,
    arguments: dict[str, Any],
    admin_identity: str | None = None,
) -> str:
    """[ADMIN] Commit a validated migration, switching the active model.

    Input validation only — the neighbor-overlap quality gate, the audited
    ``force`` escape, and the pre-flight WORM audit live in
    :meth:`NCEEngine.commit_migration` (the shared engine chokepoint) so the admin
    HTTP route is gated identically.  This handler must not re-implement the gate.

    Before the v2→v1 schema swap the engine evaluates a neighbor-overlap quality
    gate: a random sample of migrated memories is compared (k-NN Jaccard) between
    the old and new embedding spaces.  Below ``NCE_REEMBED_GATE_MIN_OVERLAP`` (or on
    a degenerate/empty sample) the commit is refused and the score is surfaced.

    Pass ``force=true`` to override a failing gate.  The override is allowed but is
    unconditionally recorded as a WORM ``migration_commit_forced`` audit event
    carrying the gate score before the commit proceeds — it cannot be silently
    applied.
    """
    raw_mid = arguments.get("migration_id")
    if not raw_mid:
        raise ValueError("migration_id is required")
    try:
        migration_id = str(UUID(str(raw_mid).strip()))
    except (ValueError, AttributeError):
        raise ValueError("migration_id must be a valid UUID")

    force: bool = bool(arguments.get("force", False))

    res = await engine.commit_migration(
        migration_id,
        force=force,
        admin_identity=admin_identity,
    )
    return json.dumps(res, default=str)


@require_scope("admin")
@mcp_handler
async def handle_abort_migration(
    engine: NCEEngine,
    arguments: dict[str, Any],
    admin_identity: str | None = None,
) -> str:
    """[ADMIN] Abort an in-progress migration."""
    raw_mid = arguments.get("migration_id")
    if not raw_mid:
        raise ValueError("migration_id is required")
    try:
        migration_id = str(UUID(str(raw_mid).strip()))
    except (ValueError, AttributeError):
        raise ValueError("migration_id must be a valid UUID")

    # Pre-flight WORM audit — written BEFORE the abort transaction.
    await _audit_migration_action(
        engine.pg_pool,
        event_type="migration_abort_requested",
        admin_identity=admin_identity,
        migration_id=migration_id,
        target_model_id=None,
    )

    res = await engine.abort_migration(migration_id)
    return json.dumps(res, default=str)
