# ruff: noqa: F401
"""Shared imports, constants and helpers for admin HTTP handlers."""

from __future__ import annotations

import json
import logging
import math
import os
import sys
import uuid
from collections.abc import AsyncGenerator
from datetime import datetime, timezone
from typing import Any

from starlette.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse

from nce import admin_state
from nce.admin_http_support import (
    admin_client_error,
    admin_error_response,
    admin_validation_error,
    mask_uri_password,
    sanitize_admin_reason,
    serialize_pg_row,
    update_dotenv,
)
from nce.admin_routes import (
    ADMIN_MAX_LIST_LIMIT,
    ADMIN_MAX_ROWS_SKIP,
    ADMIN_NAMESPACES_DEFAULT_LIMIT,
    clamp_bounded_int,
    fetch_event_llm_payload_uri,
    fetch_fleet_overview_page,
    fetch_namespace_bridge_subscriptions,
    fetch_pg_rls_snapshot,
    fetch_recent_open_contradictions,
    fetch_salience_map_points,
    offset_from_page_limit,
    parse_optional_bigint_bounds,
    parse_optional_half_life_days,
    parse_optional_uuid,
    parse_page_limit_common,
    parse_salience_top_k,
    sanitize_event_type_filter,
    sanitize_optional_agent_filter,
    sanitize_resource_type_filter,
    sanitize_slug_prefix_filter,
    sanitize_task_name_filter,
    validate_dlq_status,
)
from nce.auth import set_namespace_context, validate_agent_id
from nce.background_task_manager import create_tracked_task
from nce.config import cfg
from nce.event_log import verify_merkle_chain
from nce.mcp_args import bump_cache_generation
from nce.notifications import dispatcher
from nce.observability import MERKLE_CHAIN_VALID
from nce.signing import admin_signing_keys_status
from nce.temporal import parse_as_of

UTC = timezone.utc
logger = logging.getLogger("nce-admin")


# ---------------------------------------------------------------------------
# Response serialisation / request validation helpers
#
# Shared by the vertical-module admin handlers that echo core results straight
# into a ``JSONResponse``. These lived as per-module private copies in
# ``economy.py`` / ``inventory.py`` / ``assets.py``; the ``_json_safe`` copies
# had DRIFTED -- inventory's dropped the non-finite-float half while its
# docstring still claimed to mirror economy's -- so they are defined once here.
# ---------------------------------------------------------------------------


def _neutralise_non_finite(value: Any) -> Any:
    """Recursively replace non-finite ``float``\\ s (``nan``/``inf``/``-inf``) with their
    string form, so ``json.dumps`` never has to fall back to emitting the bare
    ``NaN``/``Infinity`` tokens that Starlette's ``JSONResponse.render`` (``allow_nan=False``)
    rejects with a ``ValueError``.

    ``json.dumps``'s ``default=`` hook (used below in :func:`_json_safe` for ``Decimal``) is
    **never** invoked for ``float`` -- floats are natively handled -- so a non-finite float
    silently sails through ``_json_safe`` unconverted and only blows up later, inside
    ``JSONResponse``'s own encoder. At that point it is indistinguishable from a genuine
    domain-validation ``ValueError`` and gets misreported as an invalid request instead of
    what it actually is: a correct computation that merely echoed a non-finite value.
    Converting here, before ``json.dumps`` ever sees the value, avoids that exception
    entirely -- the same treatment ``Decimal`` already gets via ``default=str``.

    Non-finite ``Decimal``\\ s need no special case: ``Decimal`` is not natively
    serialisable, so ``default=str`` already renders ``Decimal("NaN")`` as ``"NaN"``.
    """
    if isinstance(value, float):
        if not math.isfinite(value):
            return str(value)
        return value
    if isinstance(value, dict):
        return {key: _neutralise_non_finite(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_neutralise_non_finite(item) for item in value]
    return value


def _json_safe(value: Any) -> Any:
    """Round-trip *value* through a ``Decimal``-aware ``json.dumps`` so every
    ``Decimal`` becomes its exact string form before Starlette's own JSON
    encoder (which has no ``default=`` hook here) ever sees it. Non-finite
    ``float`` values are neutralised the same way (see
    :func:`_neutralise_non_finite`) so a caller-echoed NaN/Infinity can never reach
    Starlette's ``allow_nan=False`` encoder and be mis-filed as a domain-validation error.

    Money must never be coerced through ``float`` (money-module briefing #2; see also
    ``economy/ngaap.py``'s module docstring), and neither must an exact stock quantity
    (``inventory_items`` is ``NUMERIC(18,3)`` -- see ``inventory/stock.py``) -- this is
    the route layer's job, not the core's.
    """
    return json.loads(json.dumps(_neutralise_non_finite(value), default=str))


#: The two "missing namespace_id" dialects already in use across the admin
#: surface. Routes reading a JSON body answer the first; routes reading the
#: query string answer the second. The split predates this helper
#: (``product.py``, ``vendors.py``, ``sales.py`` all speak the query dialect on
#: GETs) and is carried through rather than flattened, so folding a route onto
#: the helper stays a pure gain of UUID validation instead of also being an
#: unannounced change to that route's response body.
_MISSING_NAMESPACE_FIELD = "Missing required field: namespace_id"
_MISSING_NAMESPACE_QUERY_PARAM = "Missing required query param: namespace_id"


def _require_namespace_id(
    raw: str | None,
    *,
    missing_error: str = _MISSING_NAMESPACE_FIELD,
) -> tuple[str | None, JSONResponse | None]:
    """Validate a route's required ``namespace_id``.

    Returns ``(namespace_id, None)`` on success or ``(None, error_response)``
    on failure. ``validate_agent_id`` only sanitises free text and never
    raises (see ``nce/auth.py``), so the actual UUID-shape check is the
    explicit ``uuid.UUID(...)`` parse below.

    Args:
        raw:            the value as it arrived, from a body field or a query
                        parameter. ``None``/blank counts as absent.
        missing_error:  the 422 message for an *absent* value. Pass
                        :data:`_MISSING_NAMESPACE_QUERY_PARAM` on query-string
                        routes; the default suits body routes. The *invalid*
                        message is deliberately not parameterised -- every
                        surface already agrees on it.
    """
    namespace_id = str(raw or "").strip()
    if not namespace_id:
        return None, JSONResponse({"error": missing_error}, status_code=422)
    namespace_id = validate_agent_id(namespace_id)
    try:
        uuid.UUID(namespace_id)
    except ValueError as exc:
        return None, JSONResponse({"error": f"Invalid namespace_id: {exc}"}, status_code=422)
    return namespace_id, None


_ROUTE_PREFIX_TO_ENGINE: dict[str, str] = {
    "api_agreements_": "agreements",
    "api_assets_": "assets",
    "api_business_insights_": "business_insights",
    "api_customer_portal_": "customer_portal",
    "api_diagnostics_": "diagnostics",
    "api_diag_": "diagnostics",
    "api_admin_d365_": "dynamics365",
    "api_d365_": "dynamics365",
    "api_dynamics365_": "dynamics365",
    "api_economy_": "economy",
    "api_entity_resolution_": "entity_resolution",
    "api_merge_queue_": "entity_resolution",
    "api_field_tech_": "field_tech",
    "api_hr_": "hr",
    "api_inventory_": "inventory",
    "api_marketing_": "marketing",
    "api_netbox_": "netbox",
    "api_pricing_": "pricing",
    "api_procurement_": "procurement",
    "api_product_": "product",
    "api_project_": "project",
    "api_resources_": "resources",
    "api_sales_": "sales",
    "api_support_": "support",
    "api_system_design_": "system_design",
    "api_vendors_": "vendors",
    "api_a2a_": "a2a",
    "api_admin_memory_": "memory",
    "api_admin_embedding_migration_": "migration",
    "api_settings_": "global",
    "api_admin_settings_": "global",
}


def _resolve_engine_name(route: str = "", explicit: str | None = None) -> str | None:
    if explicit:
        return None if explicit == "global" else explicit
    if route:
        for prefix, eng in _ROUTE_PREFIX_TO_ENGINE.items():
            if route.startswith(prefix):
                return None if eng == "global" else eng
    try:
        frame = sys._getframe(2)
        caller_mod = frame.f_globals.get("__name__", "")
        if caller_mod.startswith("nce.admin_handlers."):
            mod_name = caller_mod.split(".")[-1]
            if mod_name == "d365":
                return "dynamics365"
            if mod_name not in ("_shared", "settings", "fleet"):
                return mod_name
    except Exception:
        pass
    return None


async def bump_mcp_cache_generation(
    engine: Any,
    *,
    route: str = "",
    engine_name: str | None = None,
) -> None:
    """Invalidate cached MCP tool responses after a REST-surface mutation.

    MCP tool results are cached in Redis under a per-engine generation counter
    (``mcp_cache_generation:{engine}``) or the global counter (``mcp_cache_generation``).
    ``nce/mcp_stdio_dispatch.py`` bumps it after every successful ``mutation=True`` tool
    call — that bump is what makes stale ``cacheable=True`` entries unreachable.

    REST routes in this package call the same ``do_*`` cores directly and never
    reach that dispatch loop, so without this call a mutation performed over
    HTTP leaves pre-mutation MCP cache entries readable for the full
    ``MCP_CACHE_TTL_S`` (300 s) — silently, with nothing in any log.

    Call this **after** the core has returned without raising, so a mutation
    that never committed does not discard a still-valid cache.  Semantics
    deliberately mirror the dispatch loop exactly: it bumps whenever the
    handler returns normally, without inspecting whether the write actually
    changed a row.  Matching that keeps the two surfaces equivalent — the
    property this helper exists to restore.

    A Redis failure here is logged, never raised: the mutation has already
    committed, and failing the HTTP response would invite the caller to retry
    a write that already landed.  The stale window degrades to TTL expiry,
    which is exactly the pre-existing behaviour.

    Args:
        engine:      the connected ``NCEEngine`` (usually ``admin_state.engine``).
        route:       route name, for failure logging and engine inference.
        engine_name: optional explicit engine domain (e.g. ``"assets"``). If omitted,
                     inferred from ``route`` or the caller's module.
    """
    redis_client = getattr(engine, "redis_client", None)
    if redis_client is None:
        return
    resolved_engine = _resolve_engine_name(route=route, explicit=engine_name)
    try:
        await bump_cache_generation(redis_client, engine=resolved_engine)
    except Exception as exc:  # noqa: BLE001 - never fail a committed mutation
        logger.warning(
            "MCP cache generation bump failed after %s; cacheable MCP reads may "
            "serve stale data for up to MCP_CACHE_TTL_S: %s",
            route,
            exc,
        )
