"""
Admin HTTP handlers for the System Design vertical module.

Exports:
  ``api_system_design_publish_design_docs`` (W11)
      POST /api/system-design/publish-design-docs
  ``api_system_design_get_topology`` (W13a)
      GET /api/system-design/topology
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
)
from nce.entity_resolution.ownership import OwnershipError
from nce.vertical_modules.system_design.enrichment import do_enrich_design_lines
from nce.vertical_modules.system_design.from_quote import do_design_from_quote
from nce.vertical_modules.system_design.geometry import VersionConflictError
from nce.vertical_modules.system_design.lucid import do_publish_design_docs
from nce.vertical_modules.system_design.mcp_handlers import (
    author_device_topology_from_arguments,
    author_functional_location_from_arguments,
    retire_planned_from_arguments,
)
from nce.vertical_modules.system_design.read import do_get_topology
from nce.vertical_modules.system_design.retire import RetireDeniedError
from nce.vertical_modules.system_design.sow import do_generate_sow
from nce.vertical_modules.system_design.to_quote import do_design_to_quote
from nce.vertical_modules.system_design.validation_queries import validate_design_graph

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


def _ownership_denied_response(exc: OwnershipError) -> JSONResponse:
    """HTTP form of a deny-by-default ``assert_owner`` refusal.

    403, not 500. ``OwnershipError`` is not a ``ValueError``, so without this it
    falls through to the generic handler and a *correct, expected* authorisation
    refusal is reported as a server fault — with the refusal text in ``detail``,
    where a caller cannot act on it and an operator sees a phantom 5xx.

    The message is fixed and caller-vetted (it names a node type and an engine,
    never tenant data), so it is safe to return in production, which is why this
    does not go through ``admin_error_response``'s dev-only detail path.
    """
    return JSONResponse(
        {
            "error": "Not permitted to write this node type",
            "reason": "ownership_denied",
            "detail": str(exc),
        },
        status_code=403,
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
        return _ownership_denied_response(exc)
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
        return _ownership_denied_response(exc)
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

    try:
        result = await validate_design_graph(
            admin_state.engine,
            {"namespace_id": namespace_id, "design_id": design_id},
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
        return _ownership_denied_response(exc)
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
