# ruff: noqa: F401
"""Shared imports, constants and helpers for admin HTTP handlers."""

from __future__ import annotations

import json
import logging
import math
import os
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
# ``economy.py`` and ``inventory.py``; the ``_json_safe`` copies
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


def _require_namespace_id(raw: str | None) -> tuple[str | None, JSONResponse | None]:
    """Validate a route's required ``namespace_id``.

    Returns ``(namespace_id, None)`` on success or ``(None, error_response)``
    on failure. ``validate_agent_id`` only sanitises free text and never
    raises (see ``nce/auth.py``), so the actual UUID-shape check is the
    explicit ``uuid.UUID(...)`` parse below.
    """
    namespace_id = str(raw or "").strip()
    if not namespace_id:
        return None, JSONResponse(
            {"error": "Missing required field: namespace_id"}, status_code=422
        )
    namespace_id = validate_agent_id(namespace_id)
    try:
        uuid.UUID(namespace_id)
    except ValueError as exc:
        return None, JSONResponse({"error": f"Invalid namespace_id: {exc}"}, status_code=422)
    return namespace_id, None
