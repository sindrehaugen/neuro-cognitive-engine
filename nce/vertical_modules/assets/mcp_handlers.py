"""
nce/vertical_modules/assets/mcp_handlers.py
=============================================
MCP tool handlers for the Assets vertical module.

Wave 1 (Batch 141, Module 9.Wave 1, ``lifecycle-core``) shipped only a
skeleton ping. Wave 3 (Batch 143, Module 9.Wave 3, ``assets-surface``) adds
the DB-aware read/write surface this file's own Wave-1 docstring named as
later-wave work: ``do_get_asset`` / ``do_list_assets`` / ``do_advance_lifecycle``,
each dual-surfaced as an MCP tool here and a REST route in
``nce/admin_handlers/assets.py`` (the "one core function" pattern —
``docs/vertical_engines/VERTICAL_MODULE_PATTERN.md``, "Dual-surface exposure").

Public entry-points:
  ``handle_assets_ping`` — liveness probe; verifies the namespace_id is
  present and returns a simple OK payload.

  ``do_get_asset`` / ``handle_assets_get`` — fetch one ``assets`` register
  row by ``asset_id``. Watcher-style read; cacheable.

  ``do_list_assets`` / ``handle_assets_list`` — list ``assets`` register rows
  for a namespace, optionally filtered by ``functional_location_id`` and/or
  ``lifecycle_state``. Watcher-style read; cacheable.

  ``do_advance_lifecycle`` / ``handle_assets_advance_lifecycle`` — the
  DB-aware wrapper this module's Wave-1 docstring deferred: reads an asset's
  current ``lifecycle_state``, calls the pure 14-state machine
  (:func:`nce.vertical_modules.assets.lifecycle.advance`), and persists the
  new state on a legal transition. Actor; mutation. Named
  ``assets_advance_lifecycle`` and flagged ``cacheable=False, admin_only=False,
  mutation=True`` to match the MCP tools table in
  ``docs/vertical_engines/09-assets-engine.md``.

Two scope points, named rather than left to be discovered
-----------------------------------------------------------
- **No warranty resolution.** ``advance()`` accepts an optional
  ``warranty_months`` duration (resolved, per the engine doc, from Product's
  warranty terms — cross-module work this wave's ``Files:`` list does not
  reach). ``do_advance_lifecycle`` calls ``advance()`` with no duration, so
  ``warranty_set``/``warranty_until`` are never populated on a
  ``VERIFIED``-entering transition and are deliberately excluded from this
  wrapper's return dict. Migration 054's ``assets`` table also has no
  ``warranty_until`` column to persist one into.
- **No ledger write.** ``docs/vertical_engines/09-assets-engine.md`` says
  ``do_advance_lifecycle`` "logs transition to ledger" as part of the FULL
  engine (its Build-phase B4). This wave's ``Files:`` list includes no
  ledger/event-log module, so no ``v3_cognitive_ledger``/``event_log`` row is
  written here — only the ``assets.lifecycle_state`` column.
- **Parameter name is ``target_state``, not the doc's ``event``.** The pure
  state machine this wraps (Wave 1, ``lifecycle.py``) was built and tested
  with the parameter name ``target_state``; this wrapper matches the already-
  shipped code, not the earlier doc's aspirational wording.

Registered in ``nce/tool_registry.py`` via:
  ``_h(assets_mcp_handlers, "handle_assets_ping")``
  ``_h(assets_mcp_handlers, "handle_assets_get")``
  ``_h(assets_mcp_handlers, "handle_assets_list")``
  ``_h(assets_mcp_handlers, "handle_assets_advance_lifecycle")``
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any
from uuid import UUID

from nce.db_utils import scoped_pg_session
from nce.mcp_args import require_namespace_id
from nce.mcp_errors import mcp_handler
from nce.vertical_modules.assets.lifecycle import advance

if TYPE_CHECKING:
    from nce.orchestrator import NCEEngine

log = logging.getLogger("nce.vertical_modules.assets.mcp_handlers")


@mcp_handler
async def handle_assets_ping(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """MCP tool: assets_ping — liveness probe for the Assets vertical.

    Requires ``namespace_id`` in *arguments*. Returns ``{"ok": true, "engine":
    "assets"}`` on success; the ``@mcp_handler`` decorator converts a
    missing-namespace ``ValueError`` into an ``McpError(-32602)`` at call-site.
    """
    require_namespace_id(arguments)
    return json.dumps({"ok": True, "engine": "assets"})


# ---------------------------------------------------------------------------
# Wave 3 (Batch 143) — assets-surface: get / list / advance-lifecycle
# ---------------------------------------------------------------------------

# Columns read back for both do_get_asset and do_list_assets. Selecting the
# same tuple for both keeps a single row-shaping helper (_row_to_asset_dict)
# correct for either caller.
_ASSET_COLUMNS: tuple[str, ...] = (
    "id",
    "bom_line_id",
    "serial",
    "functional_location_id",
    "lifecycle_state",
    "change_origin",
    "created_at",
    "updated_at",
)
_ASSET_SELECT_LIST: str = ", ".join(_ASSET_COLUMNS)

# Hard safety cap on do_list_assets — not a caller-facing knob (this wave adds
# no pagination parameter; a namespace register is expected to be small
# relative to this cap, and no wave brief line asked for cursor pagination).
_LIST_ROW_CAP: int = 500


def _require_asset_id(params: dict[str, Any]) -> str:
    """Coerce a required ``asset_id`` to its canonical UUID string.

    Mirrors ``nce.mcp_args.require_namespace_id``'s contract for the same
    reason: a validated, canonical identifier goes into every SQL statement
    below, never a caller-supplied raw string.
    """
    raw = params.get("asset_id")
    if not raw:
        raise ValueError("'asset_id' is required")
    try:
        return str(UUID(str(raw)))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"invalid asset_id: {exc}") from exc


def _row_to_asset_dict(row: Any) -> dict[str, Any]:
    """Convert one ``assets`` row into a JSON-safe dict.

    Normalises the UUID primary key to ``asset_id`` (str) — matching
    ``seed.py``'s own ``str(row["id"])`` convention so a caller cannot tell
    whether a row came from ``do_seed_asset_from_bom`` or this wave's reads —
    and both timestamps to ISO-8601 strings. Doing this once, here, means
    neither the MCP surface (``json.dumps(..., default=str)``) nor the REST
    surface (a bare ``JSONResponse``) needs its own UUID/datetime handling.
    """
    return {
        "asset_id": str(row["id"]),
        "bom_line_id": row["bom_line_id"],
        "serial": row["serial"],
        "functional_location_id": row["functional_location_id"],
        "lifecycle_state": row["lifecycle_state"],
        "change_origin": row["change_origin"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
    }


async def do_get_asset(engine: NCEEngine, params: dict[str, Any]) -> dict[str, Any]:
    """Fetch one ``assets`` register row by ``asset_id``.

    Parameters
    ----------
    params:
        ``namespace_id`` (str, required) · ``asset_id`` (str, required) — the
        ``assets.id`` UUID (NOT the BOM line id and NOT the ``serial``).

    Returns
    -------
    dict
        ``{"ok": True, "asset": {...} | None}``. ``asset`` is ``None`` when
        no row matches — an absent asset is a normal outcome, never an error
        (mirrors ``product.mcp_handlers.do_get_product``'s
        ``{"product": None, ...}`` convention).
    """
    namespace_id = require_namespace_id(params)
    asset_id = _require_asset_id(params)

    async with scoped_pg_session(engine.pg_pool, namespace_id) as conn:
        row = await conn.fetchrow(
            f"""
            SELECT {_ASSET_SELECT_LIST}
            FROM assets
            WHERE namespace_id = $1::uuid AND id = $2::uuid
            """,
            namespace_id,
            asset_id,
        )

    return {"ok": True, "asset": _row_to_asset_dict(row) if row is not None else None}


async def do_list_assets(engine: NCEEngine, params: dict[str, Any]) -> dict[str, Any]:
    """List ``assets`` register rows for a namespace, most-recent first.

    Parameters
    ----------
    params:
        ``namespace_id``           (str, required)
        ``functional_location_id`` (str, optional) — filter to one room.
        ``lifecycle_state``        (str, optional) — filter to one state.

    Returns
    -------
    dict
        ``{"ok": True, "items": [{...}, ...]}``, capped at
        :data:`_LIST_ROW_CAP` rows.
    """
    namespace_id = require_namespace_id(params)
    functional_location_id = str(params.get("functional_location_id") or "").strip() or None
    lifecycle_state = str(params.get("lifecycle_state") or "").strip() or None

    conditions: list[str] = ["namespace_id = $1::uuid"]
    args: list[Any] = [namespace_id]
    if functional_location_id is not None:
        args.append(functional_location_id)
        conditions.append(f"functional_location_id = ${len(args)}")
    if lifecycle_state is not None:
        args.append(lifecycle_state)
        conditions.append(f"lifecycle_state = ${len(args)}")

    query = (
        f"SELECT {_ASSET_SELECT_LIST} FROM assets "
        f"WHERE {' AND '.join(conditions)} "
        f"ORDER BY created_at DESC LIMIT {_LIST_ROW_CAP}"
    )

    async with scoped_pg_session(engine.pg_pool, namespace_id) as conn:
        rows = await conn.fetch(query, *args)

    return {"ok": True, "items": [_row_to_asset_dict(r) for r in rows]}


async def do_advance_lifecycle(engine: NCEEngine, params: dict[str, Any]) -> dict[str, Any]:
    """Advance one asset's ``lifecycle_state`` via the pure 14-state machine.

    Reads the asset's current ``lifecycle_state``, calls
    :func:`nce.vertical_modules.assets.lifecycle.advance`, and — only on a
    legal, state-changing transition — ``UPDATE``s the row. An idempotent
    self-transition (``target_state == current``) and an illegal transition
    both leave the row untouched, matching ``advance()``'s own contract.

    See the module docstring's "Two scope points" section: no warranty
    resolution, no ledger write.

    Parameters
    ----------
    params:
        ``namespace_id`` (str, required) · ``asset_id`` (str, required) ·
        ``target_state`` (str, required) — e.g. ``"VERIFIED"``.

    Returns
    -------
    dict
        - Asset absent: ``{"ok": False, "not_found": True, "asset_id": str,
          "error": str}``.
        - Legal transition (incl. idempotent no-op): ``{"ok": True,
          "not_found": False, "changed": bool, "asset_id": str,
          "previous_state": str, "new_state": str, "error": None}``.
        - Illegal transition (business refusal, never raised):
          ``{"ok": False, "not_found": False, "changed": False,
          "asset_id": str, "previous_state": str, "new_state": str,
          "error": str}``.

    Raises
    ------
    ValueError
        ``namespace_id``/``asset_id``/``target_state`` missing or malformed —
        a request-shape defect, distinct from the business-rule refusals
        above, which are returned, never raised.
    """
    namespace_id = require_namespace_id(params)
    asset_id = _require_asset_id(params)
    target_state = str(params.get("target_state") or "").strip()
    if not target_state:
        raise ValueError("'target_state' is required")

    async with scoped_pg_session(engine.pg_pool, namespace_id) as conn:
        row = await conn.fetchrow(
            "SELECT lifecycle_state FROM assets WHERE namespace_id = $1::uuid AND id = $2::uuid",
            namespace_id,
            asset_id,
        )
        if row is None:
            return {
                "ok": False,
                "not_found": True,
                "asset_id": asset_id,
                "error": f"asset {asset_id!r} not found in this namespace",
            }

        current_state: str = row["lifecycle_state"]
        transition = advance({"lifecycle_state": current_state}, target_state)

        if transition["ok"] and transition["changed"]:
            await conn.execute(
                """
                UPDATE assets
                SET lifecycle_state = $1, updated_at = NOW()
                WHERE namespace_id = $2::uuid AND id = $3::uuid
                """,
                transition["new_state"],
                namespace_id,
                asset_id,
            )

    return {
        "ok": transition["ok"],
        "not_found": False,
        "changed": transition["changed"],
        "asset_id": asset_id,
        "previous_state": current_state,
        "new_state": transition["new_state"],
        "error": transition["error"],
    }


# ---------------------------------------------------------------------------
# MCP surface — thin adapters over the do_* cores above
# ---------------------------------------------------------------------------


@mcp_handler
async def handle_assets_get(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """MCP tool: assets_get — fetch one asset register row (Watcher, read-only).

    Requires ``namespace_id`` and ``asset_id``. Thin adapter — all logic
    lives in :func:`do_get_asset`.
    """
    result = await do_get_asset(engine, dict(arguments))
    return json.dumps(result, default=str)


@mcp_handler
async def handle_assets_list(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """MCP tool: assets_list — list asset register rows (Watcher, read-only).

    Requires ``namespace_id``; optionally filters by ``functional_location_id``
    and/or ``lifecycle_state``. Thin adapter — all logic lives in
    :func:`do_list_assets`.
    """
    result = await do_list_assets(engine, dict(arguments))
    return json.dumps(result, default=str)


@mcp_handler
async def handle_assets_advance_lifecycle(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """MCP tool: assets_advance_lifecycle — advance the 14-state lifecycle (Actor).

    Requires ``namespace_id``, ``asset_id``, and ``target_state``. Thin
    adapter — all logic lives in :func:`do_advance_lifecycle`.
    """
    result = await do_advance_lifecycle(engine, dict(arguments))
    return json.dumps(result, default=str)
