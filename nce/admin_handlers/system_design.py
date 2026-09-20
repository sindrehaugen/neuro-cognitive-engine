"""
Admin HTTP handlers for the System Design vertical module.

Exports:
  ``api_system_design_publish_design_docs`` (W11)
      POST /api/system-design/publish-design-docs
  ``api_system_design_get_topology`` (W13a)
      GET /api/system-design/topology
  ``api_system_design_inspect_signal_flow`` (Wave SD-5)
  ``api_system_design_procurement_view`` (Wave SD-6)
      GET /api/system-design/signal-flow
  ``api_system_design_author_topology`` (W13b)
      POST /api/system-design/topology
  ``api_system_design_author_functional_location`` (W13b)
      POST /api/system-design/functional-location
  ``api_system_design_validate_design_graph`` (W13c)
      POST /api/system-design/validate
  ``api_system_design_delete_planned`` (W17)
      DELETE /api/system-design/planned — 🔴 SOFT-RETIRES by default; the
      method and the path are Copper's contract and the name is a deliberate
      mismatch with the behaviour.  See below.

Thin REST wrappers — they delegate to ``do_publish_design_docs`` (lucid.py),
``do_get_topology`` (read.py), the two ``do_author_*`` cores (devices.py /
graph.py) and ``validate_design_graph`` (validation_queries.py), and hold no
domain logic of their own.  The Lucid path is EXPORT ONLY — Lucid import is cut
(spec correction, Wave 11).

Why the validator is a POST that does not bump the cache (W13c)
---------------------------------------------------------------
``POST /api/system-design/validate`` is a pure read spelled POST: that is the
row Copper's contract table pins, and the body is where W16+'s richer inputs
will go.  Because it mutates nothing it performs **no** cache-generation bump —
bumping on a read would invalidate every cacheable MCP entry in the namespace on
every canvas keystroke.  Its MCP twin is ``mutation=False`` for the same reason.

Cache invalidation (W13b)
-------------------------
The two authoring routes are the REST twins of ``mutation=True`` MCP tools, so
each calls ``bump_mcp_cache_generation`` after its core returns.  Without it a
write performed over HTTP leaves the cacheable ``system_design_get_topology``
MCP entry readable for the full ``MCP_CACHE_TTL_S`` — silently, with nothing in
any log.  The bump is deliberately keyed to the *route*, not to a tool-name
prefix: a prefix filter is what under-scoped six of nineteen routes the last
time this defect was fixed.  ``DELETE /api/system-design/planned`` (W17) is on
the same rule and bumps for the same reason — more sharply, because the entry it
would leave readable is a device the caller just removed.

Why the W17 route reads a JSON BODY and not the query string
-------------------------------------------------------------
``DELETE`` with a body is unusual (RFC 9110 gives it no defined semantics, but
does not forbid it) and it is the right call here for one concrete reason: a
query string has no types.  ``?permanent=false`` arrives as the STRING
``"false"``, which is truthy, and ``permanent`` is the flag that separates a
reversible status change from an irreversible delete.  ``permanent_of`` in the
shared adapter refuses any non-boolean outright rather than coercing, so a
stringified flag is a 422 rather than a deletion — but the body is what keeps a
correct client from ever meeting that refusal.  ``node_labels`` is a list, which
a query string also cannot express without a second convention.
"""

from __future__ import annotations

import logging
from typing import Any

from nce.admin_handlers._shared import (
    _MISSING_NAMESPACE_QUERY_PARAM,
    JSONResponse,
    _require_namespace_id,
    admin_error_response,
    admin_state,
    bump_mcp_cache_generation,
    ownership_denied_response,
)
from nce.entity_resolution.ownership import OwnershipError
from nce.vertical_modules.system_design.capability_sync import do_sync_device_capabilities
from nce.vertical_modules.system_design.enrichment import do_enrich_design_lines
from nce.vertical_modules.system_design.from_quote import do_design_from_quote
from nce.vertical_modules.system_design.geometry import VersionConflictError
from nce.vertical_modules.system_design.lucid import do_publish_design_docs
from nce.vertical_modules.system_design.mcp_handlers import (
    author_device_topology_from_arguments,
    author_functional_location_from_arguments,
    retire_planned_from_arguments,
)
from nce.vertical_modules.system_design.procurement_view import do_get_procurement_view
from nce.vertical_modules.system_design.read import do_get_topology
from nce.vertical_modules.system_design.retire import RetireDeniedError
from nce.vertical_modules.system_design.signal_distribution import do_get_signal_rules
from nce.vertical_modules.system_design.signal_flow import do_inspect_signal_flow
from nce.vertical_modules.system_design.sow import do_generate_sow
from nce.vertical_modules.system_design.standards import do_get_standards
from nce.vertical_modules.system_design.to_quote import do_design_to_quote
from nce.vertical_modules.system_design.validate import do_validate_design
from nce.vertical_modules.system_design.validation_queries import (
    validate_design_graph as validate_design_graph,
)

log = logging.getLogger("nce.admin_handlers.system_design")


async def api_system_design_publish_design_docs(request) -> JSONResponse:
    """POST /api/system-design/publish-design-docs

    JSON body:
        namespace_id (str, required): Active namespace UUID.
        design_id    (str, required): Design identifier to export.

    Response (JSON):
        {"status": "ok", "lucid_url": str | null}

    Returns ``lucid_url: null`` when Lucid credentials are unset (clean no-op).
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=422)

    namespace_id, ns_err = _require_namespace_id(body.get("namespace_id"))
    if ns_err is not None:
        return ns_err

    design_id = str(body.get("design_id") or "").strip()
    if not design_id:
        return JSONResponse({"error": "Missing required field: design_id"}, status_code=422)

    try:
        result = await do_publish_design_docs(
            admin_state.engine,
            {"namespace_id": namespace_id, "design_id": design_id},
        )
    except Exception as exc:
        log.exception("api_system_design_publish_design_docs: unexpected error")
        return admin_error_response(
            "Failed to publish system design documents", exc, status_code=500
        )

    # Mirror the MCP dispatch loop's post-mutation invalidation
    # (system_design_publish_design_docs is a mutation=True tool).
    await bump_mcp_cache_generation(
        admin_state.engine, route="api_system_design_publish_design_docs"
    )

    return JSONResponse({"status": "ok", "lucid_url": result.get("lucid_url")})


async def api_system_design_get_topology(request) -> JSONResponse:
    """GET /api/system-design/topology

    Query parameters:
        namespace_id (str, required): Active namespace UUID.
        design_id    (str, required): Design identifier to read.
        statuses     (str, repeatable, optional): **LIVE lifecycle filter**
            since M6.W16b — no longer accepted-and-ignored.  Each repetition of
            the parameter adds one status; the read is narrowed SQL-side to the
            devices, racks and cables whose stored status is one of them.  A node
            with no lifecycle state row, or a null status, never matches.
            🔴 REST/MCP asymmetry, deliberate and unresolved: a bare ``?statuses=``
            reaches this route as ``[""]`` — one empty status, which matches
            nothing — whereas MCP's ``statuses: []`` means *no filter*.  Over REST,
            omit the parameter entirely for no filter.

    Response (JSON):
        {"status": "ok", "topology": { ... }}

    Read-only — no cache-generation bump (``mutation=False``); the matching MCP
    tool is ``system_design_get_topology``.
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    namespace_id, ns_err = _require_namespace_id(
        request.query_params.get("namespace_id"),
        missing_error=_MISSING_NAMESPACE_QUERY_PARAM,
    )
    if ns_err is not None:
        return ns_err

    design_id = str(request.query_params.get("design_id") or "").strip()
    if not design_id:
        return JSONResponse({"error": "Missing required query param: design_id"}, status_code=422)

    try:
        result = await do_get_topology(
            admin_state.engine,
            {
                "namespace_id": namespace_id,
                "design_id": design_id,
                # LIVE since M6.W16b: passed through to read.py, which filters
                # devices/racks/cables SQL-side.  ``getlist`` returns [""] for a
                # bare ``?statuses=``, which is truthy and therefore forwarded as
                # a one-element filter matching nothing — see the docstring.
                "statuses": request.query_params.getlist("statuses") or None,
            },
        )
    except Exception as exc:
        return admin_error_response(
            "Failed to read system design topology",
            exc,
            status_code=500,
            log_event="api_system_design_get_topology: unexpected error",
        )

    return JSONResponse({"status": "ok", "topology": result})


async def api_system_design_inspect_signal_flow(request) -> JSONResponse:
    """GET /api/system-design/signal-flow

    Query parameters:
        namespace_id (str, required): Active namespace UUID.
        design_id    (str, required): Design identifier to inspect.
        node_label   (str, optional): Target DEVICE, PORT, or CABLE node label.
        direction    (str, optional): Traversal direction ("upstream"|"downstream"|"both").
        max_depth    (int, optional): Max traversal hop depth (default 32).

    Response (JSON):
        {"status": "ok", "signal_flow": { ... }}

    Read-only — no cache-generation bump (``mutation=False``); the matching MCP
    tool is ``system_design_inspect_signal_flow``.
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    namespace_id, ns_err = _require_namespace_id(
        request.query_params.get("namespace_id"),
        missing_error=_MISSING_NAMESPACE_QUERY_PARAM,
    )
    if ns_err is not None:
        return ns_err

    design_id = str(request.query_params.get("design_id") or "").strip()
    if not design_id:
        return JSONResponse({"error": "Missing required query param: design_id"}, status_code=422)

    node_label = request.query_params.get("node_label")
    direction = request.query_params.get("direction")
    max_depth = request.query_params.get("max_depth")

    params: dict[str, Any] = {
        "namespace_id": namespace_id,
        "design_id": design_id,
    }
    if node_label:
        params["node_label"] = node_label.strip()
    if direction:
        params["direction"] = direction.strip()
    if max_depth:
        params["max_depth"] = max_depth.strip()

    try:
        result = await do_inspect_signal_flow(admin_state.engine, params)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response(
            "Failed to inspect system design signal flow",
            exc,
            status_code=500,
            log_event="api_system_design_inspect_signal_flow: unexpected error",
        )

    return JSONResponse({"status": "ok", "signal_flow": result})


async def api_system_design_procurement_view(request) -> JSONResponse:
    """GET /api/system-design/procurement-view

    Query parameters:
        namespace_id    (str, required): Active namespace UUID.
        design_id       (str, required): Design identifier.
        require_frozen  (bool, optional): "true"|"1" to enforce frozen design.
        design_version  (int, optional): Frozen design version number.
        required_by_day (int, optional): Delivery deadline in days.

    Response (JSON):
        {"status": "ok", "procurement_view": { ... }}

    Read-only — no cache-generation bump (``mutation=False``); matching MCP
    tool is ``system_design_procurement_view``.
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    namespace_id, ns_err = _require_namespace_id(
        request.query_params.get("namespace_id"),
        missing_error=_MISSING_NAMESPACE_QUERY_PARAM,
    )
    if ns_err is not None:
        return ns_err

    design_id = str(request.query_params.get("design_id") or "").strip()
    if not design_id:
        return JSONResponse({"error": "Missing required query param: design_id"}, status_code=422)

    params: dict[str, Any] = {
        "namespace_id": namespace_id,
        "design_id": design_id,
    }
    rf_raw = request.query_params.get("require_frozen")
    if rf_raw:
        params["require_frozen"] = rf_raw.lower() in ("true", "1", "yes")

    dv_raw = request.query_params.get("design_version")
    if dv_raw:
        try:
            params["design_version"] = int(dv_raw)
        except ValueError:
            return JSONResponse(
                {"error": "Query param 'design_version' must be an integer"}, status_code=422
            )

    rbd_raw = request.query_params.get("required_by_day")
    if rbd_raw:
        try:
            params["required_by_day"] = int(rbd_raw)
        except ValueError:
            return JSONResponse(
                {"error": "Query param 'required_by_day' must be an integer"}, status_code=422
            )

    try:
        result = await do_get_procurement_view(admin_state.engine, params)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response(
            "Failed to get system design procurement view",
            exc,
            status_code=500,
            log_event="api_system_design_procurement_view: unexpected error",
        )

    return JSONResponse({"status": "ok", "procurement_view": result})


# ---------------------------------------------------------------------------
# W13b — the two authoring routes.
#
# Both delegate to the SAME adapter coroutine the matching MCP tool uses
# (``nce/vertical_modules/system_design/mcp_handlers.py``), so validation,
# argument forwarding, the actor record and the ``expected_version`` rejection
# cannot drift between the two surfaces.  This module contributes only the HTTP
# translation: body parsing, status codes, and the cache-generation bump the MCP
# dispatch loop performs for itself.
# ---------------------------------------------------------------------------


def _version_conflict_response(exc: VersionConflictError) -> JSONResponse:
    """HTTP form of a stale ``expected_version`` (Rev 2 §2, LIVE since W14).

    **409, not 422.**  That is the status HTTP defines for this exact case —
    the request conflicts with the current state of the target resource — and
    it is deliberately different from the 422 every other failure on this route
    returns.  A caller must be able to tell "you are behind, re-read and retry"
    (retryable; the request was well formed) from "your argument is malformed"
    (permanent; retrying changes nothing).  Collapsing both onto 422 makes a
    correct client either spin on a hopeless request or abandon a winnable one.

    ``reason`` is the same machine-readable discriminator the MCP surface puts
    in ``error.data.reason``, read from the one definition on
    ``VersionConflictError``, so the two surfaces cannot drift.  The expected
    and actual versions are included so a client can re-drive its state machine
    without a second round trip.
    """
    return JSONResponse(
        {
            "error": str(exc),
            "reason": exc.reason,
            "parameter": "expected_version",
            "expected_version": exc.expected,
            "actual_version": exc.actual,
        },
        status_code=409,
    )


async def _read_json_body(request) -> tuple[dict[str, Any] | None, JSONResponse | None]:
    """Parse a required JSON object body, or return the 422 to send instead."""
    try:
        body = await request.json()
    except Exception:
        return None, JSONResponse({"error": "Invalid JSON body"}, status_code=422)
    if not isinstance(body, dict):
        return None, JSONResponse({"error": "JSON body must be an object"}, status_code=422)
    return body, None


async def api_system_design_author_topology(request) -> JSONResponse:
    """POST /api/system-design/topology

    JSON body (mirrors the ``system_design_author_topology`` MCP tool):
        namespace_id (str, required), design_id (str, required),
        devices (list, required — each may carry an optional ``geometry``
        object, and each of its ``ports`` may too; each may also carry W16's
        ``status`` / ``revision`` / ``salience``, which its ``ports`` may NOT —
        a PORT has no lifecycle status), connections (list, optional — each may
        carry an optional ``cable_geometry`` and W16's ``cable_status`` /
        ``cable_revision`` / ``cable_salience`` for the CABLE node it names),
        racks (list, optional — each may carry an optional ``geometry`` and
        W16's ``status`` / ``revision`` / ``salience``),
        source_id (str, optional),
        actor (str, optional — the human's UPN; never invented when omitted),
        expected_version (int, optional — LIVE since W14).

    The whole body is forwarded to the shared adapter unchanged, so the W16
    keys need no translation here and get none: this route interprets no item
    in those lists. A state row is written only for a node that is genuinely new
    to the call or that the caller sent a lifecycle key for, so a re-author
    naming none leaves a pre-existing node with no state row — which is what
    lets W17 deny on absence for the whole legacy estate.

    W16 shares this route's existing failure vocabulary rather than adding to
    it: a malformed or misplaced lifecycle key, and a status outside the node
    type's vocabulary, both arrive as ``ValueError`` and therefore 422. Only the
    stale-token case is 409.

    Response (JSON):
        200 {"status": "ok", "authored": {"nodes", "edges", "capabilities",
             "state", "geometry"}, "version": int}
        409 on a stale ``expected_version`` — see ``_version_conflict_response``.
        422 on a missing/malformed argument (including a non-integer or
            negative ``expected_version``, which is a malformed argument and
            NOT a conflict).
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    body, body_err = await _read_json_body(request)
    if body_err is not None:
        return body_err
    assert body is not None  # narrowed by body_err

    namespace_id, ns_err = _require_namespace_id(body.get("namespace_id"))
    if ns_err is not None:
        return ns_err

    arguments = dict(body)
    arguments["namespace_id"] = namespace_id

    try:
        result = await author_device_topology_from_arguments(admin_state.engine, arguments)
    except VersionConflictError as exc:
        return _version_conflict_response(exc)
    except OwnershipError as exc:
        return ownership_denied_response(exc)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response(
            "Failed to author system design topology",
            exc,
            status_code=500,
            log_event="api_system_design_author_topology: unexpected error",
        )

    # Mirror the MCP dispatch loop's post-mutation invalidation: this route's
    # twin tool is mutation=True, and system_design_get_topology is cacheable.
    await bump_mcp_cache_generation(admin_state.engine, route="api_system_design_author_topology")

    return JSONResponse(
        {
            "status": "ok",
            "authored": result.get("authored", {}),
            # The design's NEW version — the token for the caller's next write.
            # Returning it here is what lets a REST client stay in the
            # optimistic-concurrency loop without a re-read, and it keeps this
            # route's payload the same shape as its MCP twin's.
            "version": result.get("version"),
        }
    )


async def api_system_design_author_functional_location(request) -> JSONResponse:
    """POST /api/system-design/functional-location

    JSON body (mirrors the ``system_design_author_functional_location`` MCP tool):
        namespace_id (str, required), namespace_slug (str, required),
        design_id (str, required), site_name (str, required),
        buildings (list, required — each building, each of its ``floors`` and
        each floor's ``rooms`` may carry an optional ``geometry`` object;
        ``positions`` are bare strings and cannot),
        design_lines (list, optional), source_id (str, optional),
        actor (str, optional — the human's UPN; never invented when omitted),
        expected_version (int, optional — LIVE since W14).

    Response (JSON):
        200 {"status": "ok", "authored": {"nodes", "edges", "geometry"},
             "version": int}
        409 on a stale ``expected_version`` — see ``_version_conflict_response``.
        422 on a missing/malformed argument.
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    body, body_err = await _read_json_body(request)
    if body_err is not None:
        return body_err
    assert body is not None  # narrowed by body_err

    namespace_id, ns_err = _require_namespace_id(body.get("namespace_id"))
    if ns_err is not None:
        return ns_err

    arguments = dict(body)
    arguments["namespace_id"] = namespace_id

    try:
        result = await author_functional_location_from_arguments(admin_state.engine, arguments)
    except VersionConflictError as exc:
        return _version_conflict_response(exc)
    except OwnershipError as exc:
        return ownership_denied_response(exc)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response(
            "Failed to author system design functional location",
            exc,
            status_code=500,
            log_event="api_system_design_author_functional_location: unexpected error",
        )

    await bump_mcp_cache_generation(
        admin_state.engine, route="api_system_design_author_functional_location"
    )

    return JSONResponse(
        {
            "status": "ok",
            "authored": result.get("authored", {}),
            "version": result.get("version"),
        }
    )


# ---------------------------------------------------------------------------
# W13c — the design-graph validator route.
# ---------------------------------------------------------------------------


async def api_system_design_validate_design_graph(request) -> JSONResponse:
    """POST /api/system-design/validate

    JSON body (mirrors the ``system_design_validate_design_graph`` MCP tool):
        namespace_id (str, required): Active namespace UUID.
        design_id    (str, required): Design identifier to validate.

    Response (JSON):
        200 {"status": "ok", "validation": {"passed": bool, "reasons": [str]}}
        422 on a missing/malformed argument.

    ``validation`` is ``validate_design_graph``'s return value **verbatim**.
    Two of its behaviours are deliberate and are not this adapter's to
    reinterpret: an unknown signal format does not fail the design, and the
    power/heat budget is informational — it always contributes its totals to
    ``reasons`` and never sets ``passed=False``.  A non-empty ``reasons`` is
    therefore not a failure signal; ``passed`` is.

    Read-only — no cache-generation bump (the twin MCP tool is
    ``mutation=False``).
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    body, body_err = await _read_json_body(request)
    if body_err is not None:
        return body_err
    assert body is not None  # narrowed by body_err

    namespace_id, ns_err = _require_namespace_id(body.get("namespace_id"))
    if ns_err is not None:
        return ns_err

    design_id = str(body.get("design_id") or "").strip()
    if not design_id:
        return JSONResponse({"error": "Missing required field: design_id"}, status_code=422)

    payload: dict[str, Any] = {"namespace_id": namespace_id, "design_id": design_id}
    if "decisions" in body and body["decisions"] is not None:
        payload["decisions"] = body["decisions"]
    if "source_id" in body and body["source_id"] is not None:
        payload["source_id"] = body["source_id"]

    try:
        result = await do_validate_design(
            admin_state.engine,
            payload,
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response(
            "Failed to validate system design graph",
            exc,
            status_code=500,
            log_event="api_system_design_validate_design_graph: unexpected error",
        )

    if payload.get("decisions"):
        await bump_mcp_cache_generation(
            admin_state.engine,
            route="api_system_design_validate_design_graph",
        )

    return JSONResponse({"status": "ok", "validation": result})


# ---------------------------------------------------------------------------
# W17 — the retire route.  THE FIRST DELETE PATH IN THIS CODEBASE.
#
# 🔴 The METHOD IS A DELIBERATE MISMATCH WITH THE DEFAULT BEHAVIOUR.
# ``DELETE /api/system-design/planned`` is Copper's pinned contract row, so the
# verb and the path stay — but the default is a SOFT RETIRE and nothing is
# removed without ``permanent: true`` in the body.  Stated in the first line of
# the handler docstring, in the tool docstring, and in ``retire.py``.
# ---------------------------------------------------------------------------


def _retire_denied_response(exc: RetireDeniedError) -> JSONResponse:
    """HTTP form of a refusal to retire (W17).

    **409, not 422 and not 403.**  Not 422: the request was well formed — the
    labels parsed, they belonged to the design, the flags were the right types.
    Not 403: 403 says *you* may not do this, which is a fact about the caller;
    this says *these nodes, in this state,* may not be done to, which is a fact
    about the graph, and the same caller gets a different answer once the nodes'
    status changes.  409 is the status HTTP defines for exactly that — a
    conflict with the current state of the target resource.

    It shares 409 with the stale-token refusal on the authoring routes, and
    ``reason`` is what tells them apart: ``"version_conflict"`` means re-read and
    retry the same request, ``"retire_denied"`` means this request will never
    succeed as written.  Both read their reason from the one definition on their
    exception class so the two surfaces cannot drift.

    ``denials`` is every refused node, not the first: a canvas that selected
    forty devices needs forty answers in one response.
    """
    return JSONResponse(
        {
            "error": str(exc),
            "reason": exc.reason,
            "denials": exc.denials,
        },
        status_code=409,
    )


async def api_system_design_delete_planned(request) -> JSONResponse:
    """DELETE /api/system-design/planned — 🔴 SOFT-RETIRES by default.

    **The verb is a deliberate mismatch with the behaviour.**  ``DELETE`` and
    this path are Copper's published contract and are not adjustable, but the
    default writes the node's retired lifecycle status (``'decommissioning'``
    for a DEVICE or a CABLE, ``'deprecated'`` for a RACK — migration 061's
    vocabularies are disjoint and a RACK has no ``'decommissioning'``) and
    floors its salience.  **Nothing is removed.**  A genuine transactional
    delete — the node, its edges, its PORT children and all three side-table
    rows — happens only on an explicit ``permanent: true``, which additionally
    requires ``actor``.

    Only ``'planned'`` nodes are touchable.  No state row, or a NULL status,
    is a **denial** — and absence is the normal state of everything authored
    before W16, which is what keeps this route away from real installed
    equipment.  ``active`` deletion is out of scope.  One denied node denies the
    whole call and nothing is changed.

    JSON body (mirrors the ``system_design_delete_planned`` MCP tool; a body
    rather than a query string — see the module docstring for why
    ``?permanent=false`` would be a trap):
        namespace_id (str, required), design_id (str, required),
        node_labels (list[str], required, non-empty — canonical labels as
        returned by ``GET /api/system-design/topology``; DEVICE / RACK / CABLE
        only, all belonging to ``design_id``),
        permanent (bool, optional, default false — must be a JSON boolean; a
        string is refused rather than coerced),
        actor (str, optional in general — Rev 2 §1 — but REQUIRED when
        ``permanent`` is true),
        expected_version (int, optional — LIVE since W14).

    Response (JSON):
        200 {"status": "ok", "permanent": bool, "retired": [...],
             "removed": {...} | null, "version": int}
        403 when this engine does not own the node type here (deny-by-default).
        409 on a stale ``expected_version`` (``reason: version_conflict``) or on
            a node that is not retirable (``reason: retire_denied``, with the
            full ``denials`` list).
        422 on a missing/malformed argument — including a ``permanent`` that is
            not a JSON boolean, a label outside ``design_id``, a PORT label, and
            a missing ``actor`` on the permanent path.
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    body, body_err = await _read_json_body(request)
    if body_err is not None:
        return body_err
    assert body is not None  # narrowed by body_err

    namespace_id, ns_err = _require_namespace_id(body.get("namespace_id"))
    if ns_err is not None:
        return ns_err

    arguments = dict(body)
    arguments["namespace_id"] = namespace_id

    try:
        result = await retire_planned_from_arguments(admin_state.engine, arguments)
    except VersionConflictError as exc:
        return _version_conflict_response(exc)
    except RetireDeniedError as exc:
        return _retire_denied_response(exc)
    except OwnershipError as exc:
        return ownership_denied_response(exc)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response(
            "Failed to retire system design nodes",
            exc,
            status_code=500,
            log_event="api_system_design_delete_planned: unexpected error",
        )

    # Mirror the MCP dispatch loop's post-mutation invalidation. Sharper here
    # than on the authoring routes: without it the cacheable
    # system_design_get_topology entry keeps serving a device the caller just
    # removed, for the full MCP_CACHE_TTL_S, with nothing in any log.
    await bump_mcp_cache_generation(admin_state.engine, route="api_system_design_delete_planned")

    return JSONResponse(
        {
            "status": "ok",
            "permanent": result.get("permanent"),
            "retired": result.get("retired", []),
            "removed": result.get("removed"),
            # The design's NEW version — the token for the caller's next write.
            "version": result.get("version"),
        }
    )


# ---------------------------------------------------------------------------
# M6.W26 (Batch 230a) -- the commercial half of the design loop, REST twins of
# the four MCP tools registered in the same batch.
#
# Cache generation: from_quote and to_quote write graph rows, so a write over
# HTTP would otherwise leave the cacheable ``system_design_get_topology`` MCP
# entry readable for the full MCP_CACHE_TTL_S. enrich_design_lines writes no
# graph row -- it queues Product enrichment -- so its bump is not strictly
# required; it bumps anyway because a spurious bump costs one cache miss while a
# missing one serves stale topology. generate_sow is a read and does not bump.
# ---------------------------------------------------------------------------


async def api_system_design_from_quote(request) -> JSONResponse:
    """POST /api/system-design/from-quote

    JSON body (mirrors the ``system_design_from_quote`` MCP tool):
        namespace_id (str, required), quote_id (str, required),
        design_id (str, optional -- defaults to ``DESIGN-<quote_id>``),
        namespace_slug (str, optional), source_id (str, optional).

    Response (JSON):
        200 the core's result, unchanged.
        422 on a missing/malformed argument.
        503 when the engine is not connected.
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)
    body, body_err = await _read_json_body(request)
    if body_err is not None:
        return body_err
    assert body is not None
    namespace_id, ns_err = _require_namespace_id(body.get("namespace_id"))
    if ns_err is not None:
        return ns_err
    if not body.get("quote_id"):
        return JSONResponse({"error": "quote_id is required"}, status_code=422)
    arguments = dict(body)
    arguments["namespace_id"] = namespace_id
    try:
        result = await do_design_from_quote(admin_state.engine, arguments)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response("Failed to realise the quote into a design", exc)
    await bump_mcp_cache_generation(admin_state.engine, route="api_system_design_from_quote")
    return JSONResponse(result)


async def api_system_design_to_quote(request) -> JSONResponse:
    """POST /api/system-design/to-quote

    JSON body (mirrors the ``system_design_to_quote`` MCP tool):
        namespace_id (str, required), design_id (str, required),
        source_id (str, optional).

    Sales still owns pricing and signing: this returns the lines, it does not
    price or freeze them.

    Response (JSON):
        200 the core's result, unchanged.
        422 on a missing/malformed argument.
        503 when the engine is not connected.
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)
    body, body_err = await _read_json_body(request)
    if body_err is not None:
        return body_err
    assert body is not None
    namespace_id, ns_err = _require_namespace_id(body.get("namespace_id"))
    if ns_err is not None:
        return ns_err
    if not body.get("design_id"):
        return JSONResponse({"error": "design_id is required"}, status_code=422)
    arguments = dict(body)
    arguments["namespace_id"] = namespace_id
    try:
        result = await do_design_to_quote(admin_state.engine, arguments)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response("Failed to derive quote lines from the design", exc)
    await bump_mcp_cache_generation(admin_state.engine, route="api_system_design_to_quote")
    return JSONResponse(result)


async def api_system_design_generate_sow(request) -> JSONResponse:
    """POST /api/system-design/sow

    JSON body (mirrors the ``system_design_generate_sow`` MCP tool):
        namespace_id (str, required), design_id (str, required),
        version_number (int, optional -- overrides the derived version and marks
        the result frozen).

    POST, but a PURE READ: it writes nothing and therefore does not bump the MCP
    cache generation. ``version_number`` is derived deterministically from the
    design state, so re-issuing against an unchanged design returns the same
    version.

    Response (JSON):
        200 the core's result, unchanged.
        422 on a missing/malformed argument.
        503 when the engine is not connected.
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)
    body, body_err = await _read_json_body(request)
    if body_err is not None:
        return body_err
    assert body is not None
    namespace_id, ns_err = _require_namespace_id(body.get("namespace_id"))
    if ns_err is not None:
        return ns_err
    if not body.get("design_id"):
        return JSONResponse({"error": "design_id is required"}, status_code=422)
    arguments = dict(body)
    arguments["namespace_id"] = namespace_id
    try:
        result = await do_generate_sow(admin_state.engine, arguments)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response("Failed to generate the statement of work", exc)
    return JSONResponse(result)


async def api_system_design_enrich_design_lines(request) -> JSONResponse:
    """POST /api/system-design/enrich-design-lines

    JSON body (mirrors the ``system_design_enrich_design_lines`` MCP tool):
        namespace_id (str, required), design_id (str, required),
        missing_fields (list[str], optional -- defaults to ``["etim_specs"]``).

    SIDE-EFFECTING: writes no graph row but QUEUES Product enrichment, once per
    unique referenced product. See the block comment above on why it bumps the
    cache generation anyway.

    Response (JSON):
        200 the core's result, unchanged.
        422 on a missing/malformed argument.
        503 when the engine is not connected.
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)
    body, body_err = await _read_json_body(request)
    if body_err is not None:
        return body_err
    assert body is not None
    namespace_id, ns_err = _require_namespace_id(body.get("namespace_id"))
    if ns_err is not None:
        return ns_err
    if not body.get("design_id"):
        return JSONResponse({"error": "design_id is required"}, status_code=422)
    arguments = dict(body)
    arguments["namespace_id"] = namespace_id
    try:
        result = await do_enrich_design_lines(admin_state.engine, arguments)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response("Failed to enrich the design lines", exc)
    await bump_mcp_cache_generation(
        admin_state.engine, route="api_system_design_enrich_design_lines"
    )
    return JSONResponse(result)


async def api_system_design_get_standards(request) -> JSONResponse:
    """GET /api/system-design/standards

    Query parameters:
        category    (str, optional): Standard category key.
        standard_id (str, optional): Standard ID.
        search      (str, optional): Text search query.

    Response (JSON):
        {"categories": [...], "standards": [...], "total": int}
    """
    category = request.query_params.get("category")
    standard_id = request.query_params.get("standard_id")
    search = request.query_params.get("search")
    result = do_get_standards(
        admin_state.engine,
        {"category": category, "standard_id": standard_id, "search": search},
    )
    return JSONResponse(result)


async def api_system_design_get_signal_rules(request) -> JSONResponse:
    """GET /api/system-design/signal-rules

    Query parameters:
        containment      (str, optional): in_wall, surface, floor_box, none.
        distance_m       (float, optional): Cable distance in meters.
        altmode          (bool, optional): USB-C DisplayPort Alt-Mode supported.
        allow_usbc       (bool, optional): USB-C cabling permitted.
        wants_wireless   (bool, optional): Wireless screen presentation requested.
        wants_charging   (bool, optional): Laptop power delivery required.
        laptop_watt      (float, optional): Required charging wattage.
        vendor_ecosystem (str, optional): Room vendor ecosystem.

    Response (JSON):
        {"roles": [...], "rules": [...]} or evaluated recommendation dict.
    """
    params = dict(request.query_params)
    result = do_get_signal_rules(admin_state.engine, params)
    return JSONResponse(result)


async def api_system_design_sync_device_capabilities(request) -> JSONResponse:
    """POST /api/system-design/capabilities/sync

    JSON body:
        namespace_id (str, required): Active namespace UUID.
        device_label (str, required): Target DEVICE node label.
        product_id   (str, optional): Catalog product UUID.
        mfr_part_no  (str, optional): Manufacturer part number.
        manufacturer (str, optional): Manufacturer name.
        port_specs   (list[dict], optional): Explicit port overrides.
        extra        (dict, optional): Additional metadata.

    Response (JSON):
        {"status": "synced", "device_label": str, ...}
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=422)

    namespace_id, ns_err = _require_namespace_id(body.get("namespace_id"))
    if ns_err is not None:
        return ns_err

    device_label = str(body.get("device_label") or "").strip()
    if not device_label:
        return JSONResponse({"error": "device_label is required"}, status_code=422)

    arguments = dict(body)
    arguments["namespace_id"] = namespace_id
    try:
        result = await do_sync_device_capabilities(admin_state.engine, arguments)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        log.exception("api_system_design_sync_device_capabilities: unexpected error")
        return admin_error_response("Failed to sync device capabilities", exc)

    await bump_mcp_cache_generation(
        admin_state.engine, route="api_system_design_sync_device_capabilities"
    )
    return JSONResponse(result)


# ---------------------------------------------------------------------------
# Wave C-1: Functional Location Tree REST Handlers
# ---------------------------------------------------------------------------


async def api_system_design_list_functional_locations(request) -> JSONResponse:
    """GET /api/system-design/functional-locations

    Query parameters:
        namespace_id (str, required): Active namespace UUID.
        q            (str, optional): Search query matching label or name.
        kind         (str, optional): Filter by kind (site, building, floor, room, desk, vessel).
        as_built     (bool, optional): Filter by as-built vs design-intent.
        limit        (int, optional): Max records to return (default 50).

    Response (JSON):
        {"status": "ok", "items": [...], "count": int}
    """
    from nce.db_utils import scoped_pg_session
    from nce.vertical_modules.system_design.fl_tree import search_fl_nodes

    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    namespace_id, ns_err = _require_namespace_id(
        request.query_params.get("namespace_id"),
        missing_error=_MISSING_NAMESPACE_QUERY_PARAM,
    )
    if ns_err is not None:
        return ns_err

    q = request.query_params.get("q") or request.query_params.get("query")
    kind = request.query_params.get("kind")
    as_built_raw = request.query_params.get("as_built")
    as_built = as_built_raw.lower() in ("true", "1") if as_built_raw is not None else None
    try:
        limit = int(request.query_params.get("limit") or 50)
    except ValueError:
        limit = 50

    try:
        if getattr(admin_state.engine, "pg_pool", None):
            async with scoped_pg_session(admin_state.engine.pg_pool, namespace_id) as conn:
                items = await search_fl_nodes(
                    conn, namespace_id, q=q, kind=kind, as_built=as_built, limit=limit
                )
        else:
            items = await search_fl_nodes(
                None, namespace_id, q=q, kind=kind, as_built=as_built, limit=limit
            )
    except Exception as exc:
        log.exception("api_system_design_list_functional_locations: unexpected error")
        return admin_error_response("Failed to search functional locations", exc)

    return JSONResponse({"status": "ok", "items": items, "count": len(items)})


async def api_system_design_get_functional_location(request) -> JSONResponse:
    """GET /api/system-design/functional-locations/{id}

    Path parameters:
        id (str, required): Functional location UUID or label.
    Query parameters:
        namespace_id (str, required): Active namespace UUID.

    Response (JSON):
        {"status": "ok", "node": {...}}
    """
    from nce.db_utils import scoped_pg_session
    from nce.vertical_modules.system_design.fl_tree import (
        FLNodeNotFoundError,
        get_fl_node,
    )

    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    namespace_id, ns_err = _require_namespace_id(
        request.query_params.get("namespace_id"),
        missing_error=_MISSING_NAMESPACE_QUERY_PARAM,
    )
    if ns_err is not None:
        return ns_err

    node_id = str(request.path_params.get("id") or "").strip()
    if not node_id:
        return JSONResponse({"error": "Missing node id"}, status_code=422)

    try:
        if getattr(admin_state.engine, "pg_pool", None):
            async with scoped_pg_session(admin_state.engine.pg_pool, namespace_id) as conn:
                node = await get_fl_node(conn, namespace_id, node_id)
        else:
            node = await get_fl_node(None, namespace_id, node_id)
    except FLNodeNotFoundError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    except Exception as exc:
        log.exception("api_system_design_get_functional_location: unexpected error")
        return admin_error_response("Failed to get functional location", exc)

    return JSONResponse({"status": "ok", "node": node})


async def api_system_design_get_fl_children(request) -> JSONResponse:
    """GET /api/system-design/functional-locations/{id}/children

    Path parameters:
        id (str, required): Parent functional location UUID or label.
    Query parameters:
        namespace_id (str, required): Active namespace UUID.
        recursive    (bool, optional): If true, return all descendants.

    Response (JSON):
        {"status": "ok", "children": [...], "count": int}
    """
    from nce.db_utils import scoped_pg_session
    from nce.vertical_modules.system_design.fl_tree import (
        FLNodeNotFoundError,
        get_fl_children,
    )

    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    namespace_id, ns_err = _require_namespace_id(
        request.query_params.get("namespace_id"),
        missing_error=_MISSING_NAMESPACE_QUERY_PARAM,
    )
    if ns_err is not None:
        return ns_err

    node_id = str(request.path_params.get("id") or "").strip()
    if not node_id:
        return JSONResponse({"error": "Missing node id"}, status_code=422)

    recursive_raw = request.query_params.get("recursive")
    recursive = recursive_raw.lower() in ("true", "1") if recursive_raw is not None else False

    try:
        if getattr(admin_state.engine, "pg_pool", None):
            async with scoped_pg_session(admin_state.engine.pg_pool, namespace_id) as conn:
                children = await get_fl_children(conn, namespace_id, node_id, recursive=recursive)
        else:
            children = await get_fl_children(None, namespace_id, node_id, recursive=recursive)
    except FLNodeNotFoundError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    except Exception as exc:
        log.exception("api_system_design_get_fl_children: unexpected error")
        return admin_error_response("Failed to get functional location children", exc)

    return JSONResponse({"status": "ok", "children": children, "count": len(children)})


async def api_system_design_get_fl_ancestors(request) -> JSONResponse:
    """GET /api/system-design/functional-locations/{id}/ancestors

    Path parameters:
        id (str, required): Target functional location UUID or label.
    Query parameters:
        namespace_id (str, required): Active namespace UUID.

    Response (JSON):
        {"status": "ok", "ancestors": [...], "count": int}
    """
    from nce.db_utils import scoped_pg_session
    from nce.vertical_modules.system_design.fl_tree import (
        FLNodeNotFoundError,
        get_fl_ancestors,
    )

    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    namespace_id, ns_err = _require_namespace_id(
        request.query_params.get("namespace_id"),
        missing_error=_MISSING_NAMESPACE_QUERY_PARAM,
    )
    if ns_err is not None:
        return ns_err

    node_id = str(request.path_params.get("id") or "").strip()
    if not node_id:
        return JSONResponse({"error": "Missing node id"}, status_code=422)

    try:
        if getattr(admin_state.engine, "pg_pool", None):
            async with scoped_pg_session(admin_state.engine.pg_pool, namespace_id) as conn:
                ancestors = await get_fl_ancestors(conn, namespace_id, node_id)
        else:
            ancestors = await get_fl_ancestors(None, namespace_id, node_id)
    except FLNodeNotFoundError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    except Exception as exc:
        log.exception("api_system_design_get_fl_ancestors: unexpected error")
        return admin_error_response("Failed to get functional location ancestors", exc)

    return JSONResponse({"status": "ok", "ancestors": ancestors, "count": len(ancestors)})


async def api_system_design_get_fl_path(request) -> JSONResponse:
    """GET /api/system-design/functional-locations/{id}/path

    Path parameters:
        id (str, required): Target functional location UUID or label.
    Query parameters:
        namespace_id (str, required): Active namespace UUID.

    Response (JSON):
        {"status": "ok", "path_nodes": [...], "path_string": str, "depth": int}
    """
    from nce.db_utils import scoped_pg_session
    from nce.vertical_modules.system_design.fl_tree import (
        FLNodeNotFoundError,
        get_fl_path,
    )

    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    namespace_id, ns_err = _require_namespace_id(
        request.query_params.get("namespace_id"),
        missing_error=_MISSING_NAMESPACE_QUERY_PARAM,
    )
    if ns_err is not None:
        return ns_err

    node_id = str(request.path_params.get("id") or "").strip()
    if not node_id:
        return JSONResponse({"error": "Missing node id"}, status_code=422)

    try:
        if getattr(admin_state.engine, "pg_pool", None):
            async with scoped_pg_session(admin_state.engine.pg_pool, namespace_id) as conn:
                path_info = await get_fl_path(conn, namespace_id, node_id)
        else:
            path_info = await get_fl_path(None, namespace_id, node_id)
    except FLNodeNotFoundError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    except Exception as exc:
        log.exception("api_system_design_get_fl_path: unexpected error")
        return admin_error_response("Failed to get functional location path", exc)

    return JSONResponse({"status": "ok", **path_info})


async def api_system_design_move_functional_location(request) -> JSONResponse:
    """POST /api/system-design/functional-locations/{id}/move

    Path parameters:
        id (str, required): Target functional location UUID or label.
    JSON Body:
        namespace_id (str, required): Active namespace UUID.
        new_parent_id (str, required): New parent UUID or label.
        actor (str, optional): Identity of the actor initiating the move.

    Response (JSON):
        {"status": "ok", "moved": true, ...}
    """
    from nce.db_utils import scoped_pg_session
    from nce.vertical_modules.system_design.fl_tree import (
        CycleDetectedError,
        FLNodeNotFoundError,
        InvalidMoveError,
        move_fl_node,
    )

    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=422)

    namespace_id, ns_err = _require_namespace_id(body.get("namespace_id"))
    if ns_err is not None:
        return ns_err

    node_id = str(request.path_params.get("id") or "").strip()
    new_parent_id = str(body.get("new_parent_id") or "").strip()
    if not node_id or not new_parent_id:
        return JSONResponse({"error": "node_id and new_parent_id are required"}, status_code=422)

    actor = body.get("actor")

    try:
        if getattr(admin_state.engine, "pg_pool", None):
            async with scoped_pg_session(admin_state.engine.pg_pool, namespace_id) as conn:
                result = await move_fl_node(conn, namespace_id, node_id, new_parent_id, actor=actor)
        else:
            result = await move_fl_node(None, namespace_id, node_id, new_parent_id, actor=actor)
    except FLNodeNotFoundError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    except (CycleDetectedError, InvalidMoveError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    except Exception as exc:
        log.exception("api_system_design_move_functional_location: unexpected error")
        return admin_error_response("Failed to move functional location", exc)

    await bump_mcp_cache_generation(
        admin_state.engine, route="api_system_design_move_functional_location"
    )
    return JSONResponse({"status": "ok", **result})


async def api_system_design_merge_functional_locations(request) -> JSONResponse:
    """POST /api/system-design/functional-locations/{id}/merge

    Path parameters:
        id (str, required): Survivor functional location UUID or label.
    JSON Body:
        namespace_id (str, required): Active namespace UUID.
        absorbed_id (str, required): Absorbed functional location UUID or label.
        reversible (bool, optional): Default true.
        actor (str, optional): Identity of the actor initiating the merge.

    Response (JSON):
        {"status": "ok", "merged": true, "audit": {...}}
    """
    from nce.db_utils import scoped_pg_session
    from nce.vertical_modules.system_design.fl_tree import (
        FLNodeNotFoundError,
        MergeConflictError,
        merge_fl_nodes,
    )

    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=422)

    namespace_id, ns_err = _require_namespace_id(body.get("namespace_id"))
    if ns_err is not None:
        return ns_err

    survivor_id = str(request.path_params.get("id") or "").strip()
    absorbed_id = str(body.get("absorbed_id") or "").strip()
    if not survivor_id or not absorbed_id:
        return JSONResponse({"error": "survivor_id and absorbed_id are required"}, status_code=422)

    reversible = bool(body.get("reversible", True))
    actor = body.get("actor")

    try:
        if getattr(admin_state.engine, "pg_pool", None):
            async with scoped_pg_session(admin_state.engine.pg_pool, namespace_id) as conn:
                result = await merge_fl_nodes(
                    conn, namespace_id, survivor_id, absorbed_id, reversible=reversible, actor=actor
                )
        else:
            result = await merge_fl_nodes(
                None, namespace_id, survivor_id, absorbed_id, reversible=reversible, actor=actor
            )
    except FLNodeNotFoundError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    except MergeConflictError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    except Exception as exc:
        log.exception("api_system_design_merge_functional_locations: unexpected error")
        return admin_error_response("Failed to merge functional locations", exc)

    await bump_mcp_cache_generation(
        admin_state.engine, route="api_system_design_merge_functional_locations"
    )
    return JSONResponse({"status": "ok", **result})


async def api_system_design_promote_functional_location(request) -> JSONResponse:
    """POST /api/system-design/functional-locations/{id}/promote

    Path parameters:
        id (str, required): Target functional location UUID or label.
    JSON Body:
        namespace_id (str, required): Active namespace UUID.
        actor (str, optional): Identity of the actor initiating promotion.

    Response (JSON):
        {"status": "ok", "as_built": true, ...}
    """
    from nce.db_utils import scoped_pg_session
    from nce.vertical_modules.system_design.fl_tree import (
        FLNodeNotFoundError,
        promote_fl_node,
    )

    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=422)

    namespace_id, ns_err = _require_namespace_id(body.get("namespace_id"))
    if ns_err is not None:
        return ns_err

    node_id = str(request.path_params.get("id") or "").strip()
    if not node_id:
        return JSONResponse({"error": "Missing node id"}, status_code=422)

    actor = body.get("actor")

    try:
        if getattr(admin_state.engine, "pg_pool", None):
            async with scoped_pg_session(admin_state.engine.pg_pool, namespace_id) as conn:
                result = await promote_fl_node(conn, namespace_id, node_id, actor=actor)
        else:
            result = await promote_fl_node(None, namespace_id, node_id, actor=actor)
    except FLNodeNotFoundError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    except Exception as exc:
        log.exception("api_system_design_promote_functional_location: unexpected error")
        return admin_error_response("Failed to promote functional location", exc)

    await bump_mcp_cache_generation(
        admin_state.engine, route="api_system_design_promote_functional_location"
    )
    return JSONResponse({"status": "ok", **result})


# ---------------------------------------------------------------------------
# Room Categories & FL Metadata REST Endpoints (Wave C-2)
# ---------------------------------------------------------------------------


async def api_system_design_list_room_categories(request) -> JSONResponse:
    """GET /api/system-design/room-categories

    Query parameters:
        q (str, optional): Search keyword matching category ID, name, or description.
        min_capacity (int, optional): Minimum seat capacity filter.
        max_capacity (int, optional): Maximum seat capacity filter.

    Response (JSON):
        {"status": "ok", "room_categories": [...], "count": int}
    """
    from nce.vertical_modules.system_design.room_categories import do_get_room_categories

    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    q = request.query_params.get("q")
    min_cap = request.query_params.get("min_capacity")
    max_cap = request.query_params.get("max_capacity")

    try:
        min_c = int(min_cap) if min_cap is not None else None
        max_c = int(max_cap) if max_cap is not None else None
    except ValueError:
        return JSONResponse({"error": "Invalid capacity parameter"}, status_code=422)

    categories = do_get_room_categories(
        admin_state.engine,
        {"q": q, "min_capacity": min_c, "max_capacity": max_c},
    )
    return JSONResponse({"status": "ok", "room_categories": categories, "count": len(categories)})


async def api_system_design_get_room_category(request) -> JSONResponse:
    """GET /api/system-design/room-categories/{id}

    Path parameters:
        id (str, required): Room category ID (e.g. BOARDROOM, HUDDLE).

    Response (JSON):
        {"status": "ok", "category": {...}}
    """
    from nce.vertical_modules.system_design.room_categories import (
        RoomCategoryNotFoundError,
        do_get_room_category,
    )

    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    category_id = str(request.path_params.get("id") or "").strip()
    if not category_id:
        return JSONResponse({"error": "Missing category id"}, status_code=422)

    try:
        category = do_get_room_category(admin_state.engine, category_id)
    except RoomCategoryNotFoundError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)

    return JSONResponse({"status": "ok", "category": category})


async def api_system_design_get_fl_room_category(request) -> JSONResponse:
    """GET /api/system-design/functional-locations/{id}/room-category

    Path parameters:
        id (str, required): Functional location UUID or label.
    Query parameters:
        namespace_id (str, required): Active tenant namespace UUID.

    Response (JSON):
        {"status": "ok", "fl_id": str, "fl_label": str, "category_id": str | None, "category": dict | None}
    """
    from nce.vertical_modules.system_design.fl_tree import FLNodeNotFoundError
    from nce.vertical_modules.system_design.room_categories import do_get_fl_room_category

    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    namespace_id, ns_err = _require_namespace_id(
        request.query_params.get("namespace_id"),
        missing_error=_MISSING_NAMESPACE_QUERY_PARAM,
    )
    if ns_err is not None:
        return ns_err

    node_id = str(request.path_params.get("id") or "").strip()
    if not node_id:
        return JSONResponse({"error": "Missing functional location id"}, status_code=422)

    try:
        result = await do_get_fl_room_category(admin_state.engine, namespace_id, {"fl_id": node_id})
    except FLNodeNotFoundError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    except Exception as exc:
        log.exception("api_system_design_get_fl_room_category: unexpected error")
        return admin_error_response("Failed to get room category", exc)

    return JSONResponse({"status": "ok", **result})


async def api_system_design_set_fl_room_category(request) -> JSONResponse:
    """POST /api/system-design/functional-locations/{id}/room-category

    Path parameters:
        id (str, required): Functional location UUID or label.
    JSON Body:
        namespace_id (str, required): Active tenant namespace UUID.
        category_id (str, required): Room category ID (e.g. BOARDROOM).
        actor (str, optional): Actor initiating change.

    Response (JSON):
        {"status": "ok", "fl_id": str, "fl_label": str, "category_id": str, "actor": str | None}
    """
    from nce.vertical_modules.system_design.fl_tree import FLNodeNotFoundError
    from nce.vertical_modules.system_design.room_categories import (
        RoomCategoryNotFoundError,
        do_set_fl_room_category,
    )

    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=422)

    namespace_id, ns_err = _require_namespace_id(body.get("namespace_id"))
    if ns_err is not None:
        return ns_err

    node_id = str(request.path_params.get("id") or "").strip()
    if not node_id:
        return JSONResponse({"error": "Missing functional location id"}, status_code=422)

    category_id = str(body.get("category_id") or "").strip()
    if not category_id:
        return JSONResponse({"error": "category_id is required"}, status_code=422)

    actor = body.get("actor")
    params = {"fl_id": node_id, "category_id": category_id, "actor": actor}

    try:
        result = await do_set_fl_room_category(admin_state.engine, namespace_id, params)
    except FLNodeNotFoundError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    except RoomCategoryNotFoundError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    except Exception as exc:
        log.exception("api_system_design_set_fl_room_category: unexpected error")
        return admin_error_response("Failed to set room category", exc)

    await bump_mcp_cache_generation(
        admin_state.engine, route="api_system_design_set_fl_room_category"
    )
    return JSONResponse({"status": "ok", **result})


async def api_system_design_list_fl_responsible(request) -> JSONResponse:
    """GET /api/system-design/functional-locations/{id}/responsible

    Path parameters:
        id (str, required): Functional location UUID or label.
    Query parameters:
        namespace_id (str, required): Active tenant namespace UUID.

    Response (JSON):
        {"status": "ok", "responsible": [...], "count": int}
    """
    from nce.vertical_modules.system_design.fl_tree import FLNodeNotFoundError
    from nce.vertical_modules.system_design.room_categories import do_list_fl_responsible

    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    namespace_id, ns_err = _require_namespace_id(
        request.query_params.get("namespace_id"),
        missing_error=_MISSING_NAMESPACE_QUERY_PARAM,
    )
    if ns_err is not None:
        return ns_err

    node_id = str(request.path_params.get("id") or "").strip()
    if not node_id:
        return JSONResponse({"error": "Missing functional location id"}, status_code=422)

    try:
        results = await do_list_fl_responsible(admin_state.engine, namespace_id, {"fl_id": node_id})
    except FLNodeNotFoundError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    except Exception as exc:
        log.exception("api_system_design_list_fl_responsible: unexpected error")
        return admin_error_response("Failed to list responsible personnel", exc)

    return JSONResponse({"status": "ok", "responsible": results, "count": len(results)})


async def api_system_design_assign_fl_responsible(request) -> JSONResponse:
    """POST /api/system-design/functional-locations/{id}/responsible

    Path parameters:
        id (str, required): Functional location UUID or label.
    JSON Body:
        namespace_id (str, required): Active tenant namespace UUID.
        employee_id (str, required): Employee identifier.
        role (str, optional): Role designation (default: primary).
        actor (str, optional): Actor initiating assignment.

    Response (JSON):
        {"status": "ok", "fl_id": str, "fl_label": str, "employee_id": str, "role": str, "actor": str | None}
    """
    from nce.vertical_modules.system_design.fl_tree import FLNodeNotFoundError
    from nce.vertical_modules.system_design.room_categories import (
        InvalidResponsibleRoleError,
        do_assign_fl_responsible,
    )

    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=422)

    namespace_id, ns_err = _require_namespace_id(body.get("namespace_id"))
    if ns_err is not None:
        return ns_err

    node_id = str(request.path_params.get("id") or "").strip()
    if not node_id:
        return JSONResponse({"error": "Missing functional location id"}, status_code=422)

    employee_id = str(body.get("employee_id") or "").strip()
    if not employee_id:
        return JSONResponse({"error": "employee_id is required"}, status_code=422)

    role = str(body.get("role") or "primary").strip()
    actor = body.get("actor")

    params = {
        "fl_id": node_id,
        "employee_id": employee_id,
        "role": role,
        "actor": actor,
    }

    try:
        result = await do_assign_fl_responsible(admin_state.engine, namespace_id, params)
    except FLNodeNotFoundError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    except InvalidResponsibleRoleError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        log.exception("api_system_design_assign_fl_responsible: unexpected error")
        return admin_error_response("Failed to assign responsible personnel", exc)

    await bump_mcp_cache_generation(
        admin_state.engine, route="api_system_design_assign_fl_responsible"
    )
    return JSONResponse({"status": "ok", **result})


async def api_system_design_unassign_fl_responsible(request) -> JSONResponse:
    """DELETE /api/system-design/functional-locations/{id}/responsible/{employee_id}

    Path parameters:
        id (str, required): Functional location UUID or label.
        employee_id (str, required): Employee identifier.
    Query / Body parameters:
        namespace_id (str, required): Active tenant namespace UUID.
        actor (str, optional): Actor initiating removal.

    Response (JSON):
        {"status": "ok", "unassigned": bool, ...}
    """
    from nce.vertical_modules.system_design.fl_tree import FLNodeNotFoundError
    from nce.vertical_modules.system_design.room_categories import do_unassign_fl_responsible

    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    raw_ns = request.query_params.get("namespace_id")
    actor = None
    if not raw_ns:
        try:
            body = await request.json()
            raw_ns = body.get("namespace_id")
            actor = body.get("actor")
        except Exception:
            pass

    namespace_id, ns_err = _require_namespace_id(
        raw_ns, missing_error=_MISSING_NAMESPACE_QUERY_PARAM
    )
    if ns_err is not None:
        return ns_err

    node_id = str(request.path_params.get("id") or "").strip()
    employee_id = str(request.path_params.get("employee_id") or "").strip()
    if not node_id or not employee_id:
        return JSONResponse(
            {"error": "Missing functional location id or employee id"}, status_code=422
        )

    params = {
        "fl_id": node_id,
        "employee_id": employee_id,
        "actor": actor,
    }

    try:
        result = await do_unassign_fl_responsible(admin_state.engine, namespace_id, params)
    except FLNodeNotFoundError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    except Exception as exc:
        log.exception("api_system_design_unassign_fl_responsible: unexpected error")
        return admin_error_response("Failed to unassign responsible personnel", exc)

    await bump_mcp_cache_generation(
        admin_state.engine, route="api_system_design_unassign_fl_responsible"
    )
    return JSONResponse({"status": "ok", **result})


async def api_system_design_my_responsible_fls(request) -> JSONResponse:
    """GET /api/system-design/functional-locations/my-responsible

    Query parameters:
        namespace_id (str, required): Active tenant namespace UUID.

    Authentication & Security (C16 Principal Mapping):
        Resolves calling principal identity via current_principal(request).
        If caller is unbound, non-employee tier, or missing employee_id,
        returns an empty list [] per C16 security contract.

    Response (JSON):
        {"status": "ok", "functional_locations": [...], "count": int}
    """
    from nce.principal_bindings import current_principal
    from nce.vertical_modules.system_design.room_categories import do_list_my_responsible_fls

    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    namespace_id, ns_err = _require_namespace_id(
        request.query_params.get("namespace_id"),
        missing_error=_MISSING_NAMESPACE_QUERY_PARAM,
    )
    if ns_err is not None:
        return ns_err

    try:
        principal = await current_principal(request)
        results = await do_list_my_responsible_fls(
            admin_state.engine, namespace_id, principal=principal
        )
    except Exception as exc:
        log.exception("api_system_design_my_responsible_fls: unexpected error")
        return admin_error_response("Failed to retrieve assigned functional locations", exc)

    return JSONResponse({"status": "ok", "functional_locations": results, "count": len(results)})


# ---------------------------------------------------------------------------
# Wave C-3: DESIGN Versions & Room Specifications REST Endpoints
# ---------------------------------------------------------------------------


async def api_system_design_list_designs(request) -> JSONResponse:
    """GET /api/system-design/designs

    Query parameters:
        namespace_id (str, required): Active tenant namespace UUID.
        functional_location_id (str, optional): FL UUID or label filter.
        fl_id (str, optional): Alias for functional_location_id.
        is_active (bool, optional): Active design filter.
        q / query (str, optional): Search keyword.
        limit (int, optional): Max results (default 50).
        offset (int, optional): Pagination offset (default 0).
    """
    from nce.db_utils import scoped_pg_session
    from nce.vertical_modules.system_design.design_versions import list_designs

    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    namespace_id, ns_err = _require_namespace_id(
        request.query_params.get("namespace_id"),
        missing_error=_MISSING_NAMESPACE_QUERY_PARAM,
    )
    if ns_err is not None:
        return ns_err

    fl_id = request.query_params.get("functional_location_id") or request.query_params.get("fl_id")
    is_active_val = request.query_params.get("is_active")
    is_active = None
    if is_active_val is not None:
        is_active = is_active_val.lower() in ("true", "1", "yes")

    query = request.query_params.get("query") or request.query_params.get("q")
    try:
        limit = int(request.query_params.get("limit", 50))
    except (ValueError, TypeError):
        limit = 50
    try:
        offset = int(request.query_params.get("offset", 0))
    except (ValueError, TypeError):
        offset = 0

    try:
        if getattr(admin_state.engine, "pg_pool", None):
            async with scoped_pg_session(admin_state.engine.pg_pool, namespace_id) as conn:
                designs = await list_designs(
                    conn,
                    namespace_id,
                    functional_location_id=str(fl_id) if fl_id else None,
                    is_active=is_active,
                    query=str(query) if query else None,
                    limit=limit,
                    offset=offset,
                )
        else:
            designs = await list_designs(
                None,
                namespace_id,
                functional_location_id=str(fl_id) if fl_id else None,
                is_active=is_active,
                query=str(query) if query else None,
                limit=limit,
                offset=offset,
            )
    except Exception as exc:
        log.exception("api_system_design_list_designs: unexpected error")
        return admin_error_response("Failed to list designs", exc)

    return JSONResponse({"status": "ok", "designs": designs, "count": len(designs)})


async def api_system_design_create_design(request) -> JSONResponse:
    """POST /api/system-design/designs

    JSON body:
        namespace_id (str, required)
        design_id / id (str, required)
        name (str, required)
        functional_location_id / fl_id (str, required)
        version (int, optional)
        revision (str, optional)
        room_spec (dict, optional)
        is_active (bool, optional)
        metadata (dict, optional)
        source_id (str, optional)
    """
    from nce.db_utils import scoped_pg_session
    from nce.vertical_modules.system_design.design_versions import (
        InvalidDesignError,
        InvalidRoomSpecError,
        create_design,
    )

    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=422)

    namespace_id, ns_err = _require_namespace_id(body.get("namespace_id"))
    if ns_err is not None:
        return ns_err

    design_id = str(body.get("design_id") or body.get("id") or "").strip()
    name = str(body.get("name") or "").strip()
    fl_id = str(body.get("functional_location_id") or body.get("fl_id") or "").strip()
    if not design_id or not name or not fl_id:
        return JSONResponse(
            {"error": "design_id, name, and functional_location_id are required"},
            status_code=422,
        )

    version = int(body.get("version", 1))
    revision = body.get("revision")
    room_spec = body.get("room_spec")
    is_active = bool(body.get("is_active", False))
    metadata = body.get("metadata")
    source_id = body.get("source_id")

    try:
        if getattr(admin_state.engine, "pg_pool", None):
            async with scoped_pg_session(admin_state.engine.pg_pool, namespace_id) as conn:
                created = await create_design(
                    conn,
                    namespace_id,
                    design_id=design_id,
                    name=name,
                    functional_location_id=fl_id,
                    version=version,
                    revision=str(revision) if revision else None,
                    room_spec=room_spec,
                    is_active=is_active,
                    metadata=metadata,
                    source_id=str(source_id) if source_id else None,
                )
        else:
            created = await create_design(
                None,
                namespace_id,
                design_id=design_id,
                name=name,
                functional_location_id=fl_id,
                version=version,
                revision=str(revision) if revision else None,
                room_spec=room_spec,
                is_active=is_active,
                metadata=metadata,
                source_id=str(source_id) if source_id else None,
            )
    except (InvalidDesignError, InvalidRoomSpecError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        log.exception("api_system_design_create_design: unexpected error")
        return admin_error_response("Failed to create design", exc)

    await bump_mcp_cache_generation(admin_state.engine, route="api_system_design_create_design")
    return JSONResponse({"status": "ok", "design": created}, status_code=201)


async def api_system_design_get_design(request) -> JSONResponse:
    """GET /api/system-design/designs/{id}"""
    from nce.db_utils import scoped_pg_session
    from nce.vertical_modules.system_design.design_versions import (
        DesignNotFoundError,
        get_design,
    )

    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    namespace_id, ns_err = _require_namespace_id(
        request.query_params.get("namespace_id"),
        missing_error=_MISSING_NAMESPACE_QUERY_PARAM,
    )
    if ns_err is not None:
        return ns_err

    design_id = str(request.path_params.get("id") or "").strip()
    if not design_id:
        return JSONResponse({"error": "Missing design id"}, status_code=422)

    try:
        if getattr(admin_state.engine, "pg_pool", None):
            async with scoped_pg_session(admin_state.engine.pg_pool, namespace_id) as conn:
                design = await get_design(conn, namespace_id, design_id)
        else:
            design = await get_design(None, namespace_id, design_id)
    except DesignNotFoundError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    except Exception as exc:
        log.exception("api_system_design_get_design: unexpected error")
        return admin_error_response("Failed to get design", exc)

    return JSONResponse({"status": "ok", "design": design})


async def api_system_design_update_design(request) -> JSONResponse:
    """PATCH /api/system-design/designs/{id}"""
    from nce.db_utils import scoped_pg_session
    from nce.vertical_modules.system_design.design_versions import (
        DesignNotFoundError,
        InvalidDesignError,
        InvalidRoomSpecError,
        update_design,
    )

    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=422)

    namespace_id, ns_err = _require_namespace_id(body.get("namespace_id"))
    if ns_err is not None:
        return ns_err

    design_id = str(request.path_params.get("id") or "").strip()
    if not design_id:
        return JSONResponse({"error": "Missing design id"}, status_code=422)

    name = body.get("name")
    revision = body.get("revision")
    room_spec = body.get("room_spec")
    metadata = body.get("metadata")
    is_active_val = body.get("is_active")
    is_active = bool(is_active_val) if is_active_val is not None else None

    try:
        if getattr(admin_state.engine, "pg_pool", None):
            async with scoped_pg_session(admin_state.engine.pg_pool, namespace_id) as conn:
                updated = await update_design(
                    conn,
                    namespace_id,
                    design_id,
                    name=str(name) if name is not None else None,
                    revision=str(revision) if revision is not None else None,
                    room_spec=room_spec,
                    metadata=metadata,
                    is_active=is_active,
                )
        else:
            updated = await update_design(
                None,
                namespace_id,
                design_id,
                name=str(name) if name is not None else None,
                revision=str(revision) if revision is not None else None,
                room_spec=room_spec,
                metadata=metadata,
                is_active=is_active,
            )
    except DesignNotFoundError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    except (InvalidDesignError, InvalidRoomSpecError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        log.exception("api_system_design_update_design: unexpected error")
        return admin_error_response("Failed to update design", exc)

    await bump_mcp_cache_generation(admin_state.engine, route="api_system_design_update_design")
    return JSONResponse({"status": "ok", "design": updated})


async def api_system_design_set_active_design(request) -> JSONResponse:
    """POST /api/system-design/designs/{id}/set-active"""
    from nce.db_utils import scoped_pg_session
    from nce.vertical_modules.system_design.design_versions import (
        DesignNotFoundError,
        set_active_design,
    )

    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    raw_ns = request.query_params.get("namespace_id")
    if not raw_ns:
        try:
            body = await request.json()
            raw_ns = body.get("namespace_id")
        except Exception:
            pass

    namespace_id, ns_err = _require_namespace_id(
        raw_ns, missing_error=_MISSING_NAMESPACE_QUERY_PARAM
    )
    if ns_err is not None:
        return ns_err

    design_id = str(request.path_params.get("id") or "").strip()
    if not design_id:
        return JSONResponse({"error": "Missing design id"}, status_code=422)

    try:
        if getattr(admin_state.engine, "pg_pool", None):
            async with scoped_pg_session(admin_state.engine.pg_pool, namespace_id) as conn:
                active = await set_active_design(conn, namespace_id, design_id)
        else:
            active = await set_active_design(None, namespace_id, design_id)
    except DesignNotFoundError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    except Exception as exc:
        log.exception("api_system_design_set_active_design: unexpected error")
        return admin_error_response("Failed to set active design", exc)

    await bump_mcp_cache_generation(admin_state.engine, route="api_system_design_set_active_design")
    return JSONResponse({"status": "ok", "design": active})


async def api_system_design_get_room_spec(request) -> JSONResponse:
    """GET /api/system-design/designs/{id}/room-spec"""
    from nce.db_utils import scoped_pg_session
    from nce.vertical_modules.system_design.design_versions import (
        DesignNotFoundError,
        get_room_spec,
    )

    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    namespace_id, ns_err = _require_namespace_id(
        request.query_params.get("namespace_id"),
        missing_error=_MISSING_NAMESPACE_QUERY_PARAM,
    )
    if ns_err is not None:
        return ns_err

    design_id = str(request.path_params.get("id") or "").strip()
    if not design_id:
        return JSONResponse({"error": "Missing design id"}, status_code=422)

    try:
        if getattr(admin_state.engine, "pg_pool", None):
            async with scoped_pg_session(admin_state.engine.pg_pool, namespace_id) as conn:
                spec = await get_room_spec(conn, namespace_id, design_id)
        else:
            spec = await get_room_spec(None, namespace_id, design_id)
    except DesignNotFoundError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    except Exception as exc:
        log.exception("api_system_design_get_room_spec: unexpected error")
        return admin_error_response("Failed to get room spec", exc)

    return JSONResponse({"status": "ok", "room_spec": spec})


async def api_system_design_set_room_spec(request) -> JSONResponse:
    """PUT /api/system-design/designs/{id}/room-spec"""
    from nce.db_utils import scoped_pg_session
    from nce.vertical_modules.system_design.design_versions import (
        DesignNotFoundError,
        InvalidRoomSpecError,
        set_room_spec,
    )

    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=422)

    namespace_id, ns_err = _require_namespace_id(body.get("namespace_id"))
    if ns_err is not None:
        return ns_err

    design_id = str(request.path_params.get("id") or "").strip()
    if not design_id:
        return JSONResponse({"error": "Missing design id"}, status_code=422)

    room_spec = body.get("room_spec")
    if room_spec is None or not isinstance(room_spec, dict):
        return JSONResponse({"error": "room_spec must be a dictionary"}, status_code=422)

    try:
        if getattr(admin_state.engine, "pg_pool", None):
            async with scoped_pg_session(admin_state.engine.pg_pool, namespace_id) as conn:
                updated_spec = await set_room_spec(conn, namespace_id, design_id, room_spec)
        else:
            updated_spec = await set_room_spec(None, namespace_id, design_id, room_spec)
    except DesignNotFoundError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    except InvalidRoomSpecError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        log.exception("api_system_design_set_room_spec: unexpected error")
        return admin_error_response("Failed to set room spec", exc)

    await bump_mcp_cache_generation(admin_state.engine, route="api_system_design_set_room_spec")
    return JSONResponse({"status": "ok", "room_spec": updated_spec})


async def api_system_design_list_fl_designs(request) -> JSONResponse:
    """GET /api/system-design/functional-locations/{id}/designs"""
    from nce.db_utils import scoped_pg_session
    from nce.vertical_modules.system_design.design_versions import list_designs

    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    namespace_id, ns_err = _require_namespace_id(
        request.query_params.get("namespace_id"),
        missing_error=_MISSING_NAMESPACE_QUERY_PARAM,
    )
    if ns_err is not None:
        return ns_err

    fl_id = str(request.path_params.get("id") or "").strip()
    if not fl_id:
        return JSONResponse({"error": "Missing functional location id"}, status_code=422)

    is_active_val = request.query_params.get("is_active")
    is_active = None
    if is_active_val is not None:
        is_active = is_active_val.lower() in ("true", "1", "yes")

    try:
        limit = int(request.query_params.get("limit", 50))
    except (ValueError, TypeError):
        limit = 50
    try:
        offset = int(request.query_params.get("offset", 0))
    except (ValueError, TypeError):
        offset = 0

    try:
        if getattr(admin_state.engine, "pg_pool", None):
            async with scoped_pg_session(admin_state.engine.pg_pool, namespace_id) as conn:
                designs = await list_designs(
                    conn,
                    namespace_id,
                    functional_location_id=fl_id,
                    is_active=is_active,
                    limit=limit,
                    offset=offset,
                )
        else:
            designs = await list_designs(
                None,
                namespace_id,
                functional_location_id=fl_id,
                is_active=is_active,
                limit=limit,
                offset=offset,
            )
    except Exception as exc:
        log.exception("api_system_design_list_fl_designs: unexpected error")
        return admin_error_response("Failed to list functional location designs", exc)

    return JSONResponse({"status": "ok", "designs": designs, "count": len(designs)})


async def api_system_design_get_fl_active_design(request) -> JSONResponse:
    """GET /api/system-design/functional-locations/{id}/active-design"""
    from nce.db_utils import scoped_pg_session
    from nce.vertical_modules.system_design.design_versions import get_active_design_for_fl

    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    namespace_id, ns_err = _require_namespace_id(
        request.query_params.get("namespace_id"),
        missing_error=_MISSING_NAMESPACE_QUERY_PARAM,
    )
    if ns_err is not None:
        return ns_err

    fl_id = str(request.path_params.get("id") or "").strip()
    if not fl_id:
        return JSONResponse({"error": "Missing functional location id"}, status_code=422)

    try:
        if getattr(admin_state.engine, "pg_pool", None):
            async with scoped_pg_session(admin_state.engine.pg_pool, namespace_id) as conn:
                active = await get_active_design_for_fl(conn, namespace_id, fl_id)
        else:
            active = await get_active_design_for_fl(None, namespace_id, fl_id)
    except Exception as exc:
        log.exception("api_system_design_get_fl_active_design: unexpected error")
        return admin_error_response("Failed to get active design for functional location", exc)

    return JSONResponse({"status": "ok", "active_design": active})


# ---------------------------------------------------------------------------
# Wave C-4: Solution Design Intake Queue (DESIGN_REQUEST / losningsdesign-ko)
# ---------------------------------------------------------------------------


async def api_system_design_list_requests(request) -> JSONResponse:
    """GET /api/system-design/requests"""
    from nce.db_utils import scoped_pg_session
    from nce.vertical_modules.system_design.design_requests import list_design_requests

    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    namespace_id, ns_err = _require_namespace_id(
        request.query_params.get("namespace_id"),
        missing_error=_MISSING_NAMESPACE_QUERY_PARAM,
    )
    if ns_err is not None:
        return ns_err

    status = request.query_params.get("status")
    owner_id = request.query_params.get("owner_id")
    quote_id = request.query_params.get("quote_id")
    functional_location_id = request.query_params.get(
        "functional_location_id"
    ) or request.query_params.get("fl_id")
    priority = request.query_params.get("priority")
    query = request.query_params.get("query")

    try:
        limit = int(request.query_params.get("limit", 50))
    except (ValueError, TypeError):
        limit = 50
    try:
        offset = int(request.query_params.get("offset", 0))
    except (ValueError, TypeError):
        offset = 0

    try:
        if getattr(admin_state.engine, "pg_pool", None):
            async with scoped_pg_session(admin_state.engine.pg_pool, namespace_id) as conn:
                requests = await list_design_requests(
                    conn,
                    namespace_id,
                    status=status,
                    owner_id=owner_id,
                    quote_id=quote_id,
                    functional_location_id=functional_location_id,
                    priority=priority,
                    query=query,
                    limit=limit,
                    offset=offset,
                )
        else:
            requests = await list_design_requests(
                None,
                namespace_id,
                status=status,
                owner_id=owner_id,
                quote_id=quote_id,
                functional_location_id=functional_location_id,
                priority=priority,
                query=query,
                limit=limit,
                offset=offset,
            )
    except Exception as exc:
        log.exception("api_system_design_list_requests: unexpected error")
        return admin_error_response("Failed to list design requests", exc)

    return JSONResponse({"status": "ok", "requests": requests, "count": len(requests)})


async def api_system_design_create_request(request) -> JSONResponse:
    """POST /api/system-design/requests"""
    from nce.db_utils import scoped_pg_session
    from nce.vertical_modules.system_design.design_requests import (
        InvalidDesignRequestPayloadError,
        create_design_request,
    )

    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    namespace_id, ns_err = _require_namespace_id(
        body.get("namespace_id") or request.query_params.get("namespace_id"),
        missing_error=_MISSING_NAMESPACE_QUERY_PARAM,
    )
    if ns_err is not None:
        return ns_err

    title = str(body.get("title") or "").strip()
    if not title:
        return JSONResponse({"error": "Missing required field: title"}, status_code=422)

    try:
        if getattr(admin_state.engine, "pg_pool", None):
            async with scoped_pg_session(admin_state.engine.pg_pool, namespace_id) as conn:
                created = await create_design_request(
                    conn,
                    namespace_id,
                    title=title,
                    quote_id=body.get("quote_id"),
                    functional_location_id=body.get("functional_location_id") or body.get("fl_id"),
                    description=body.get("description"),
                    priority=body.get("priority", "normal"),
                    owner_id=body.get("owner_id"),
                    room_spec=body.get("room_spec"),
                    metadata=body.get("metadata"),
                    request_id=body.get("request_id") or body.get("id"),
                )
        else:
            created = await create_design_request(
                None,
                namespace_id,
                title=title,
                quote_id=body.get("quote_id"),
                functional_location_id=body.get("functional_location_id") or body.get("fl_id"),
                description=body.get("description"),
                priority=body.get("priority", "normal"),
                owner_id=body.get("owner_id"),
                room_spec=body.get("room_spec"),
                metadata=body.get("metadata"),
                request_id=body.get("request_id") or body.get("id"),
            )
    except InvalidDesignRequestPayloadError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        log.exception("api_system_design_create_request: unexpected error")
        return admin_error_response("Failed to create design request", exc)

    await bump_mcp_cache_generation(admin_state.engine, route="api_system_design_create_request")
    return JSONResponse({"status": "ok", "request": created}, status_code=201)


async def api_system_design_get_request(request) -> JSONResponse:
    """GET /api/system-design/requests/{id}"""
    from nce.db_utils import scoped_pg_session
    from nce.vertical_modules.system_design.design_requests import (
        DesignRequestNotFoundError,
        get_design_request,
    )

    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    namespace_id, ns_err = _require_namespace_id(
        request.query_params.get("namespace_id"),
        missing_error=_MISSING_NAMESPACE_QUERY_PARAM,
    )
    if ns_err is not None:
        return ns_err

    request_id = str(request.path_params.get("id") or "").strip()
    if not request_id:
        return JSONResponse({"error": "Missing design request id"}, status_code=422)

    try:
        if getattr(admin_state.engine, "pg_pool", None):
            async with scoped_pg_session(admin_state.engine.pg_pool, namespace_id) as conn:
                req = await get_design_request(conn, namespace_id, request_id)
        else:
            req = await get_design_request(None, namespace_id, request_id)
    except DesignRequestNotFoundError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    except Exception as exc:
        log.exception("api_system_design_get_request: unexpected error")
        return admin_error_response("Failed to get design request", exc)

    return JSONResponse({"status": "ok", "request": req})


async def api_system_design_update_request(request) -> JSONResponse:
    """PATCH /api/system-design/requests/{id}"""
    from nce.db_utils import scoped_pg_session
    from nce.vertical_modules.system_design.design_requests import (
        DesignRequestNotFoundError,
        InvalidDesignRequestPayloadError,
        InvalidDesignRequestStatusError,
        update_design_request,
    )

    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    namespace_id, ns_err = _require_namespace_id(
        body.get("namespace_id") or request.query_params.get("namespace_id"),
        missing_error=_MISSING_NAMESPACE_QUERY_PARAM,
    )
    if ns_err is not None:
        return ns_err

    request_id = str(request.path_params.get("id") or "").strip()
    if not request_id:
        return JSONResponse({"error": "Missing design request id"}, status_code=422)

    try:
        if getattr(admin_state.engine, "pg_pool", None):
            async with scoped_pg_session(admin_state.engine.pg_pool, namespace_id) as conn:
                updated = await update_design_request(
                    conn,
                    namespace_id,
                    request_id,
                    title=body.get("title"),
                    description=body.get("description"),
                    status=body.get("status"),
                    priority=body.get("priority"),
                    owner_id=body.get("owner_id"),
                    room_spec=body.get("room_spec"),
                    metadata=body.get("metadata"),
                )
        else:
            updated = await update_design_request(
                None,
                namespace_id,
                request_id,
                title=body.get("title"),
                description=body.get("description"),
                status=body.get("status"),
                priority=body.get("priority"),
                owner_id=body.get("owner_id"),
                room_spec=body.get("room_spec"),
                metadata=body.get("metadata"),
            )
    except DesignRequestNotFoundError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    except (InvalidDesignRequestStatusError, InvalidDesignRequestPayloadError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        log.exception("api_system_design_update_request: unexpected error")
        return admin_error_response("Failed to update design request", exc)

    await bump_mcp_cache_generation(admin_state.engine, route="api_system_design_update_request")
    return JSONResponse({"status": "ok", "request": updated})


async def api_system_design_assign_request(request) -> JSONResponse:
    """POST /api/system-design/requests/{id}/assign"""
    from nce.db_utils import scoped_pg_session
    from nce.vertical_modules.system_design.design_requests import (
        DesignRequestNotFoundError,
        InvalidDesignRequestPayloadError,
        assign_design_request,
    )

    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    namespace_id, ns_err = _require_namespace_id(
        body.get("namespace_id") or request.query_params.get("namespace_id"),
        missing_error=_MISSING_NAMESPACE_QUERY_PARAM,
    )
    if ns_err is not None:
        return ns_err

    request_id = str(request.path_params.get("id") or "").strip()
    if not request_id:
        return JSONResponse({"error": "Missing design request id"}, status_code=422)

    owner_id = str(body.get("owner_id") or "").strip()
    if not owner_id:
        return JSONResponse({"error": "Missing required field: owner_id"}, status_code=422)

    try:
        if getattr(admin_state.engine, "pg_pool", None):
            async with scoped_pg_session(admin_state.engine.pg_pool, namespace_id) as conn:
                assigned = await assign_design_request(conn, namespace_id, request_id, owner_id)
        else:
            assigned = await assign_design_request(None, namespace_id, request_id, owner_id)
    except DesignRequestNotFoundError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    except InvalidDesignRequestPayloadError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        log.exception("api_system_design_assign_request: unexpected error")
        return admin_error_response("Failed to assign design request", exc)

    await bump_mcp_cache_generation(admin_state.engine, route="api_system_design_assign_request")
    return JSONResponse({"status": "ok", "request": assigned})


async def api_system_design_complete_request(request) -> JSONResponse:
    """POST /api/system-design/requests/{id}/complete"""
    from nce.db_utils import scoped_pg_session
    from nce.vertical_modules.system_design.design_requests import (
        DesignRequestNotFoundError,
        InvalidDesignRequestPayloadError,
        complete_design_request,
    )

    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    namespace_id, ns_err = _require_namespace_id(
        body.get("namespace_id") or request.query_params.get("namespace_id"),
        missing_error=_MISSING_NAMESPACE_QUERY_PARAM,
    )
    if ns_err is not None:
        return ns_err

    request_id = str(request.path_params.get("id") or "").strip()
    if not request_id:
        return JSONResponse({"error": "Missing design request id"}, status_code=422)

    design_id = str(body.get("design_id") or "").strip()
    if not design_id:
        return JSONResponse({"error": "Missing required field: design_id"}, status_code=422)

    try:
        if getattr(admin_state.engine, "pg_pool", None):
            async with scoped_pg_session(admin_state.engine.pg_pool, namespace_id) as conn:
                completed = await complete_design_request(conn, namespace_id, request_id, design_id)
        else:
            completed = await complete_design_request(None, namespace_id, request_id, design_id)
    except DesignRequestNotFoundError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    except InvalidDesignRequestPayloadError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        log.exception("api_system_design_complete_request: unexpected error")
        return admin_error_response("Failed to complete design request", exc)

    await bump_mcp_cache_generation(admin_state.engine, route="api_system_design_complete_request")
    return JSONResponse({"status": "ok", "request": completed})


async def api_system_design_fulfill_request(request) -> JSONResponse:
    """POST /api/system-design/requests/{id}/fulfill"""
    from nce.vertical_modules.system_design.design_requests import (
        DesignRequestNotFoundError,
        InvalidDesignRequestPayloadError,
        InvalidDesignRequestStatusError,
        fulfill_design_request_from_quote,
    )

    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    try:
        body = await request.json()
    except Exception:
        body = {}

    namespace_id, ns_err = _require_namespace_id(
        body.get("namespace_id") or request.query_params.get("namespace_id"),
        missing_error=_MISSING_NAMESPACE_QUERY_PARAM,
    )
    if ns_err is not None:
        return ns_err

    request_id = str(request.path_params.get("id") or "").strip()
    if not request_id:
        return JSONResponse({"error": "Missing design request id"}, status_code=422)

    try:
        result = await fulfill_design_request_from_quote(
            admin_state.engine,
            namespace_id,
            request_id,
            design_id=body.get("design_id"),
            namespace_slug=body.get("namespace_slug"),
            source_id=body.get("source_id"),
        )
    except DesignRequestNotFoundError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    except (
        InvalidDesignRequestStatusError,
        InvalidDesignRequestPayloadError,
        ValueError,
    ) as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        log.exception("api_system_design_fulfill_request: unexpected error")
        return admin_error_response("Failed to fulfill design request", exc)

    await bump_mcp_cache_generation(admin_state.engine, route="api_system_design_fulfill_request")
    return JSONResponse({"status": "ok", "result": result})


async def api_system_design_cancel_request(request) -> JSONResponse:
    """POST /api/system-design/requests/{id}/cancel"""
    from nce.db_utils import scoped_pg_session
    from nce.vertical_modules.system_design.design_requests import (
        DesignRequestNotFoundError,
        cancel_design_request,
    )

    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    try:
        body = await request.json()
    except Exception:
        body = {}

    namespace_id, ns_err = _require_namespace_id(
        body.get("namespace_id") or request.query_params.get("namespace_id"),
        missing_error=_MISSING_NAMESPACE_QUERY_PARAM,
    )
    if ns_err is not None:
        return ns_err

    request_id = str(request.path_params.get("id") or "").strip()
    if not request_id:
        return JSONResponse({"error": "Missing design request id"}, status_code=422)

    reason = body.get("reason")

    try:
        if getattr(admin_state.engine, "pg_pool", None):
            async with scoped_pg_session(admin_state.engine.pg_pool, namespace_id) as conn:
                cancelled = await cancel_design_request(
                    conn, namespace_id, request_id, reason=reason
                )
        else:
            cancelled = await cancel_design_request(None, namespace_id, request_id, reason=reason)
    except DesignRequestNotFoundError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    except Exception as exc:
        log.exception("api_system_design_cancel_request: unexpected error")
        return admin_error_response("Failed to cancel design request", exc)

    await bump_mcp_cache_generation(admin_state.engine, route="api_system_design_cancel_request")
    return JSONResponse({"status": "ok", "request": cancelled})
