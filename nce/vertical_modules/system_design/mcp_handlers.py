"""
nce/vertical_modules/system_design/mcp_handlers.py
===================================================
MCP tool handlers for the System Design vertical module.

Phase 1a — skeleton only.  No domain logic, no graph writes, no external
systems.  Later waves bolt ``do_*`` functions onto this spine.

Public entry-points:
  ``handle_system_design_ping`` — liveness probe; verifies the namespace_id
  is present and returns a simple OK payload.
  ``handle_system_design_publish_design_docs`` — export a DESIGN and its
  DESIGN_LINE/FUNCTIONAL_LOCATION tree to Lucid (W11, Phase 1b, EXPORT ONLY).
  ``handle_system_design_get_topology`` — read a DESIGN's topology (W13a).
  ``handle_system_design_author_topology`` — write device topology (W13b;
  geometry + optimistic concurrency added in W14; per-node lifecycle status in
  W16).
  ``handle_system_design_author_functional_location`` — write the FL tree (W13b;
  geometry + optimistic concurrency added in W14).
  ``handle_system_design_validate_design_graph`` — run the five design-quality
  checks over a DESIGN's graph (W13c).
  ``handle_system_design_delete_planned`` — retire planned nodes (W17).  🔴 The
  name is a deliberate mismatch with the behaviour: the **default is a SOFT
  RETIRE** and nothing is removed unless ``permanent=true``.  Copper's contract
  pins both the tool name and the ``DELETE`` route, so neither is renamed and
  the mismatch is stated instead.  This is the codebase's FIRST delete path.

Registered in ``nce/tool_registry.py`` via:
  ``_h(system_design_mcp_handlers, "handle_system_design_ping")``
  ``_h(system_design_mcp_handlers, "handle_system_design_publish_design_docs")``
  ``_h(system_design_mcp_handlers, "handle_system_design_get_topology")``
  ``_h(system_design_mcp_handlers, "handle_system_design_author_topology")``
  ``_h(system_design_mcp_handlers, "handle_system_design_author_functional_location")``
  ``_h(system_design_mcp_handlers, "handle_system_design_validate_design_graph")``
  ``_h(system_design_mcp_handlers, "handle_system_design_delete_planned")``

Adapter discipline (uncle-bob-craft, W13b)
------------------------------------------
The two W13b handlers are **adapters and nothing else**.  They validate the
JSON-RPC argument bag, open a namespace-scoped session, and call
``do_author_device_topology`` / ``do_author_functional_location`` with their
signatures **unchanged**.  No domain rule lives here: every label, edge,
ownership assertion and upsert stays in ``devices.py`` / ``graph.py``, which
this module imports and never modifies.  Dependencies point inward — the domain
core has no idea an MCP surface exists.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any
from uuid import UUID

from nce.db_utils import scoped_pg_session
from nce.event_log import append_event
from nce.mcp_args import require_namespace_id
from nce.mcp_errors import McpError, mcp_handler
from nce.vertical_modules.system_design.capability_sync import do_sync_device_capabilities
from nce.vertical_modules.system_design.devices import do_author_device_topology
from nce.vertical_modules.system_design.enrichment import do_enrich_design_lines
from nce.vertical_modules.system_design.from_quote import do_design_from_quote
from nce.vertical_modules.system_design.geometry import (
    BIGINT_MAX,
    VersionConflictError,
    bump_design_version,
    do_author_functional_location_geometry,
    do_author_geometry,
)
from nce.vertical_modules.system_design.graph import do_author_functional_location
from nce.vertical_modules.system_design.lucid import do_publish_design_docs
from nce.vertical_modules.system_design.procurement_view import do_get_procurement_view
from nce.vertical_modules.system_design.propose import do_propose_design
from nce.vertical_modules.system_design.read import do_get_topology
from nce.vertical_modules.system_design.retire import (
    RetireDeniedError,
    do_retire_planned,
)
from nce.vertical_modules.system_design.signal_distribution import do_get_signal_rules
from nce.vertical_modules.system_design.signal_flow import do_inspect_signal_flow
from nce.vertical_modules.system_design.sow import do_generate_sow
from nce.vertical_modules.system_design.standards import do_get_standards
from nce.vertical_modules.system_design.to_quote import do_design_to_quote
from nce.vertical_modules.system_design.validate import do_validate_design
from nce.vertical_modules.system_design.validation_queries import (
    validate_design_graph as validate_design_graph,
)

if TYPE_CHECKING:
    from nce.orchestrator import NCEEngine

log = logging.getLogger("nce.vertical_modules.system_design.mcp_handlers")

# ---------------------------------------------------------------------------
# W14 — ``expected_version`` is LIVE (Rev 2 §2).
#
# W13b declared the parameter and refused it, because a client that passes a
# concurrency token, gets a silent success and believes it holds a lock it does
# not hold is strictly worse off than one whose request was refused.  W14
# creates the storage row (``system_design_geometry``, migration 060), so the
# refusal is gone and the parameter now performs a real compare-and-swap.
#
# A STALE TOKEN IS ITS OWN ERROR, NOT A VALIDATION FAILURE.
# ``geometry.VersionConflictError`` is neither a ``ValueError`` nor an
# ``McpError``, so neither ``@mcp_handler``'s generic "Invalid parameters"
# branch nor the REST routes' ``except ValueError`` can swallow it.  Each
# surface translates it into its own vocabulary:
#
#   * MCP  — code :data:`VERSION_CONFLICT_MCP_CODE` (-32040), in the JSON-RPC
#     server-defined range -32000..-32099, and deliberately NOT ``-32602``
#     (Invalid params).  "You are behind, re-read and retry" is a retryable
#     state fact about the server; "your argument is malformed" is a permanent
#     fault in the request.  A client that cannot tell them apart either
#     retries a request that will never succeed or gives up on one that would.
#   * REST — **409 Conflict**, the status HTTP defines for exactly this ("the
#     request could not be completed due to a conflict with the current state
#     of the target resource"), and deliberately NOT the 422 the surrounding
#     validation failures use, for the same reason.
#
# Both read ``reason`` from ``VersionConflictError.reason`` so the two can never
# drift apart, exactly as the W13b rejection did.
#
# The code constant lives here rather than in ``nce/mcp_errors.py`` only
# because that file is outside this wave's ``Files:`` list; W13b set the same
# precedent with its reason string.  Promoting it to the central registry is
# reported to the orchestrator, not absorbed here.
# ---------------------------------------------------------------------------
VERSION_CONFLICT_MCP_CODE: int = -32040

# ---------------------------------------------------------------------------
# W17 — ``retire.RetireDeniedError`` is a CONFLICT, not a bad argument.
#
# Same range and the same reasoning as -32040, but a DIFFERENT code, so a client
# can tell the two apart without parsing prose: -32040 means "your version token
# is stale, re-read and retry"; -32041 means "your arguments were fine and these
# specific nodes are not in a retirable state".  Those need different client
# behaviour — the first is retried after a re-read, the second is shown to a
# human — and a client that cannot distinguish them retries a request that can
# never succeed.
#
# The REST twin is 409 for BOTH, discriminated by ``reason``; see
# ``retire.RetireDeniedError`` for why that refusal is neither 422 nor 403.
#
# Like VERSION_CONFLICT_MCP_CODE this constant lives here rather than in
# ``nce/mcp_errors.py`` only because that file is outside this wave's ``Files:``
# list.  Promoting BOTH to the central registry is reported to the orchestrator,
# not absorbed here.
# ---------------------------------------------------------------------------
RETIRE_DENIED_MCP_CODE: int = -32041


# ---------------------------------------------------------------------------
# W13b — authoring audit event (Rev 2 §1, ``actor``).
#
# The MCP/HMAC key authenticates the calling *service*; ``actor`` attributes the
# *human* (their UPN).  The domain cores emit per-node ``<TYPE>.upserted``
# graph-write events and take no ``actor`` — and this wave may not change their
# signatures — so the adapter records the attribution itself, once per call.
#
# It goes into ``event_log`` via ``append_event``, NOT into ``outbox_events``.
# The two are not interchangeable as an audit substrate: ``event_log`` is
# INSERT-only, HMAC-signed and Merkle-chained, while ``outbox_events`` is a
# delivery queue — unsigned, un-chained, and ``schema.sql`` grants ``nce_app``
# UPDATE and DELETE on it.  An attribution record that the runtime role can
# rewrite or delete is not an attribution record.
#
# This costs one value in the ``EventType`` whitelist (``nce/event_types.py``)
# plus its provenance-only ForkedReplay handler (``nce/replay.py``), because
# ``append_event`` validates ``event_type`` against that Literal and
# ``_validate_handler_coverage`` requires every value to have a handler.
# ---------------------------------------------------------------------------
_AUTHORING_EVENT_TYPE: str = "system_design_authored"

#: Service identity recorded on the audit row.  This is the *service*, and it is
#: never conflated with ``actor``: ``agent_id`` says which component performed
#: the write, ``params["actor"]`` says on whose behalf.  Both, or neither.
_AGENT_ID: str = "system-design-author"


@mcp_handler
async def handle_system_design_ping(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """MCP tool: system_design_ping — liveness probe for the System Design vertical.

    Requires ``namespace_id`` in *arguments*.  Returns ``{"ok": true, "engine":
    "system_design"}`` on success; the ``@mcp_handler`` decorator converts a
    missing-namespace ``ValueError`` into an ``McpError(-32602)`` at call-site.
    """
    require_namespace_id(arguments)
    return json.dumps({"ok": True, "engine": "system_design"})


@mcp_handler
async def handle_system_design_publish_design_docs(
    engine: NCEEngine, arguments: dict[str, Any]
) -> str:
    """MCP tool: system_design_publish_design_docs — export a DESIGN to Lucid.

    **EXPORT ONLY** (spec correction, Wave 11 — Lucid import is cut).

    Requires ``namespace_id`` and ``design_id`` in *arguments*.
    Returns ``{"lucid_url": str}`` on success, ``{"lucid_url": null}`` when
    Lucid credentials are unset (clean no-op — Phase 1b is not a gate).

    The ``@mcp_handler`` decorator converts a missing-namespace
    ``ValueError`` into an ``McpError(-32602)`` at call-site.
    """
    require_namespace_id(arguments)
    if not arguments.get("design_id"):
        raise ValueError("design_id is required")
    result = await do_publish_design_docs(engine, arguments)
    return json.dumps(result)


@mcp_handler
async def handle_system_design_get_topology(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """MCP tool: system_design_get_topology — read a DESIGN's full topology.

    Read-only (``cacheable=True, admin_only=False, mutation=False``).

    Requires ``namespace_id`` and ``design_id`` in *arguments*.  Optionally
    accepts ``statuses``, a **live SQL-side lifecycle filter** since M6.W16b —
    it narrows ``devices``, ``racks`` and ``cables`` to nodes whose stored
    status is one of the given values, and a node with no lifecycle state row
    (or a null status) never matches.  ``[]`` or omitted means no filter.

    Returns the JSON-encoded result of ``do_get_topology``: ``design``,
    ``functional_locations``, ``devices`` (each with ``capabilities`` and
    ``ports``), ``racks`` (each with ``capabilities`` — W14, debt D5),
    ``cables``, ``edges``, ``geometry`` (canvas layout keyed by node label —
    W14) and ``version`` (the live optimistic-concurrency token; ``0`` means
    this design has never been authored).

    The ``@mcp_handler`` decorator converts a missing-namespace ``ValueError``
    into an ``McpError(-32602)`` at call-site.
    """
    require_namespace_id(arguments)
    if not arguments.get("design_id"):
        raise ValueError("design_id is required")
    result = await do_get_topology(engine, arguments)
    # No ``default=`` fallback on purpose: read.py already returns JSON-native
    # values, so a future non-encodable type must fail loudly here rather than
    # be stringified into a shape the REST route would not produce.
    return json.dumps(result)


@mcp_handler
async def handle_system_design_validate_design_graph(
    engine: NCEEngine, arguments: dict[str, Any]
) -> str:
    """MCP tool: system_design_validate_design_graph — run the five design checks.

    Read-only (``cacheable=False, admin_only=False, mutation=False``).
    ``cacheable=False`` on a read is deliberate and is Copper's contract: a
    design under active canvas editing must never be served a stale verdict.

    Requires ``namespace_id`` and ``design_id`` in *arguments*.

    Returns the JSON-encoded result of ``validate_design_graph`` **unchanged** —
    ``{"passed": bool, "reasons": list[str]}``.  This adapter adds nothing and
    subtracts nothing: the five checks' semantics live in
    ``validation_queries.py`` and are not this surface's to reinterpret.  In
    particular an unknown signal format does not fail the design, and the
    power/heat budget is informational (it contributes the totals to ``reasons``
    while returning ``passed=True``) — a wrapper that "improved" either would be
    changing the contract, not the presentation.

    The ``@mcp_handler`` decorator converts a missing-namespace ``ValueError``
    into an ``McpError(-32602)`` at call-site.
    """
    require_namespace_id(arguments)
    if not arguments.get("design_id"):
        raise ValueError("design_id is required")
    result = await do_validate_design(engine, arguments)
    # No ``default=`` fallback: the core returns a bool and a list of str, so a
    # future non-encodable value must fail loudly rather than be stringified
    # into a shape the REST route would not produce.
    return json.dumps(result)


@mcp_handler
async def handle_system_design_inspect_signal_flow(
    engine: NCEEngine, arguments: dict[str, Any]
) -> str:
    """MCP tool: system_design_inspect_signal_flow — inspect signal flow and chains.

    Read-only (``cacheable=False, admin_only=False, mutation=False``).
    ``cacheable=False`` matches validate_design_graph: active canvas editing
    must not serve stale inspector or path-tracing data.

    Requires ``namespace_id`` and ``design_id`` in *arguments*.
    Optional ``node_label`` narrows inspection to a specific DEVICE, PORT, or CABLE.
    Optional ``direction`` controls chain walk ("upstream", "downstream", "both").
    Optional ``max_depth`` limits hop depth (default 32).

    Returns the JSON-encoded result of ``do_inspect_signal_flow``.
    """
    require_namespace_id(arguments)
    if not arguments.get("design_id"):
        raise ValueError("design_id is required")
    result = await do_inspect_signal_flow(engine, arguments)
    return json.dumps(result)


@mcp_handler
async def handle_system_design_procurement_view(
    engine: NCEEngine, arguments: dict[str, Any]
) -> str:
    """MCP tool: system_design_procurement_view — frozen design grouped by ranked supplier for PR-1.

    Read-only (``cacheable=True, admin_only=False, mutation=False``).

    Requires ``namespace_id`` and ``design_id`` in *arguments*.
    Optional ``weights`` overrides procurement scoring weights.
    Optional ``candidates`` overrides supplier candidates.
    Optional ``require_frozen`` (bool) enforces that the design must be frozen.
    Optional ``design_version`` (int) overrides/asserts frozen version.
    Optional ``required_by_day`` (int) delivery deadline in days.

    Returns the JSON-encoded result of ``do_get_procurement_view``.
    """
    require_namespace_id(arguments)
    if not arguments.get("design_id"):
        raise ValueError("design_id is required")
    result = await do_get_procurement_view(engine, arguments)
    return json.dumps(result)


# ---------------------------------------------------------------------------
# W13b — shared argument policy for the two authoring tools.
#
# These four helpers are the ONLY thing the two authoring adapters share.  They
# hold no domain knowledge: they read the argument bag, they do not know what a
# DEVICE or a FUNCTIONAL_LOCATION is.
# ---------------------------------------------------------------------------


def expected_version_of(arguments: dict[str, Any]) -> int | None:
    """Return the caller's ``expected_version`` token, or ``None`` (Rev 2 §2).

    "Supplied" means present **with a non-null value**.  An explicit JSON
    ``null`` is treated as absent — it expresses no version expectation — and
    absence means last-writer-wins, exactly as before W14.  That distinction is
    unchanged from W13b; only what happens to a real token has changed.

    ``bool`` is refused even though it is an ``int`` subclass in Python: a
    ``true`` on the wire is a client bug, and coercing it to the token ``1``
    would compare against a real version and occasionally *succeed*.

    The upper bound is not cosmetic either.  ``bump_design_version`` binds this
    value to ``$4::bigint``; a Python ``int`` larger than that raises asyncpg's
    ``DataError``, which is **not** a ``ValueError`` and therefore escapes both
    surfaces' ``except ValueError`` branches into HTTP 500 / JSON-RPC
    ``-32603``.  A token out of range is a malformed argument, so it is refused
    here as one.

    🔴 This function is the ONLY enforcement.  ``nce/mcp_stdio_dispatch.py``
    performs no JSON-schema validation, so the ``"type": "integer"`` and
    ``"minimum": 0`` in ``mcp_stdio_tools.py``'s ``inputSchema`` are advisory
    documentation for the client and gate nothing server-side.

    Raises:
        ValueError: present but not an integer, negative, or beyond
            :data:`~nce.vertical_modules.system_design.geometry.BIGINT_MAX`.
            Mapped to ``McpError(-32602, reason=invalid_arguments)`` / HTTP 422
            — a malformed token is a malformed argument, and is deliberately
            NOT the conflict error.
    """
    value = arguments.get("expected_version")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("expected_version must be an integer when supplied")
    if value < 0:
        raise ValueError("expected_version must not be negative")
    if value > BIGINT_MAX:
        raise ValueError(f"expected_version must not exceed {BIGINT_MAX} (the column is BIGINT)")
    return value


def actor_of(arguments: dict[str, Any]) -> str | None:
    """Return the human ``actor`` (UPN) the caller supplied, or ``None``.

    Rev 2 §1: ``actor`` is **optional** and is never invented.  It is not
    defaulted to a service identity and never inferred from the authenticating
    key — the key proves which *service* is calling, which is a different fact.
    A blank or whitespace-only value is absence, not an empty-string actor.
    """
    actor = arguments.get("actor")
    if not isinstance(actor, str):
        return None
    return actor.strip() or None


def _require_str(arguments: dict[str, Any], field: str) -> str:
    """Return a required non-blank string argument.

    Raises:
        ValueError: mapped to ``McpError(-32602)`` by ``@mcp_handler``.
    """
    value = arguments.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required")
    return value


def _require_list(arguments: dict[str, Any], field: str) -> list[Any]:
    """Return a required list argument (may be empty — the core accepts that).

    Raises:
        ValueError: mapped to ``McpError(-32602)`` by ``@mcp_handler``.
    """
    value = arguments.get(field)
    if not isinstance(value, list):
        raise ValueError(f"{field} is required and must be a list")
    return value


def _optional_list(arguments: dict[str, Any], field: str) -> list[Any] | None:
    """Return an optional list argument, or ``None`` when absent.

    Raises:
        ValueError: when present but not a list.
    """
    value = arguments.get(field)
    if value is None:
        return None
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list when supplied")
    return value


def authoring_event_payload(
    *,
    namespace_id: str,
    design_id: str,
    tool: str,
    actor: str | None,
    authored: dict[str, Any],
    version: int,
    state_changes: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build the ``system_design_authored`` event params (Rev 2 §1).

    ``actor`` is present only when
    the caller supplied one: an omitted actor is recorded as **absent**, never
    as ``""`` and never as a synthesised service identity.  A consumer must be
    able to tell "no human was named" from "a human named the empty string".

    ``version`` (W14) is the token this write PRODUCED — the design's new
    version, not the one the caller supplied.  An audit row for an
    optimistic-concurrency write that omits the token it produced cannot be
    used to reconstruct the version timeline from the WORM log, which is the
    one thing an append-only, Merkle-chained audit substrate is for.  It is
    required rather than optional precisely so a future caller cannot omit it
    and leave a gap in that timeline.
    ``state_changes`` (W16) is the per-node lifecycle delta — one
    ``{"node_label", "node_type", "from", "to", "state_row_created",
    "resurrected"}`` entry per node whose state row this write touched.  Counts alone cannot answer *which*
    node became retirable and *from what*, and this is the one wave whose whole
    purpose is to gate a destructive operation, so "3 state rows were written"
    is not an audit record of it.  Combined with ``actor`` the row answers
    which, from what, to what, and by whom.

    It is present **only when this write changed some node's lifecycle**: an
    empty list would say "nothing changed" in a shape indistinguishable from a
    write by a caller that predates the field, and every other optional field
    on this payload follows the same absent-means-absent rule.  The FL tool
    never writes lifecycle state at all, so its events never carry the key.
    """
    payload: dict[str, Any] = {
        "design_id": design_id,
        "design_label": f"DESIGN:{design_id.upper()}",
        "namespace": namespace_id,
        "tool": tool,
        "authored": authored,
        "version": version,
    }
    if state_changes:
        payload["state_changes"] = state_changes
    if actor is not None:
        payload["actor"] = actor
    return payload


async def _emit_authoring_event(
    conn: Any,
    *,
    namespace_id: str,
    design_id: str,
    tool: str,
    actor: str | None,
    authored: dict[str, Any],
    version: int,
    state_changes: list[dict[str, Any]] | None = None,
) -> None:
    """Append one ``system_design_authored`` row to the WORM ``event_log``.

    Runs inside the caller's ``scoped_pg_session`` transaction, which is
    ``append_event``'s contract: it is INSERT-only and never commits or rolls
    back for itself, so the audit row lands if and only if the graph writes it
    describes also land.
    """
    await append_event(
        conn=conn,
        namespace_id=UUID(namespace_id),
        agent_id=_AGENT_ID,
        event_type=_AUTHORING_EVENT_TYPE,
        params=authoring_event_payload(
            namespace_id=namespace_id,
            design_id=design_id,
            tool=tool,
            actor=actor,
            authored=authored,
            version=version,
            state_changes=state_changes,
        ),
    )


# ---------------------------------------------------------------------------
# W13b — the two authoring adapters (the first external WRITE path into the graph).
#
# Each is written ONCE, as a coroutine over the same ``(engine, arguments)`` bag
# both surfaces already speak, and is called by BOTH the MCP tool below and the
# REST route in ``nce/admin_handlers/system_design.py``.  Two hand-written copies
# is how the MCP and REST surfaces of a shared core drift apart — that is the
# defect family the REST→MCP cache-invalidation fix had to clean up across 19
# routes, and the reason ``_shared.py`` now owns ``_json_safe`` outright.
# ---------------------------------------------------------------------------


async def author_device_topology_from_arguments(
    engine: NCEEngine, arguments: dict[str, Any]
) -> dict[str, Any]:
    """Validate *arguments*, then call ``do_author_device_topology`` **verbatim**.

    The single authoring path behind both ``system_design_author_topology`` (MCP)
    and ``POST /api/system-design/topology`` (REST).

    This adapter forwards every argument unchanged.  It does not interpret
    ``devices``/``connections``/``racks``, and in particular it passes
    ``capability.extra`` — including the reserved ``copper.port_kind`` /
    ``copper.rear_port`` / ``copper.rear_position`` keys — straight through
    unvalidated (Rev 2 §5: NCE stores, Copper interprets).

    Idempotency is the core's, not this adapter's: repeated identical calls
    collapse onto ``kg_nodes``' ``(label, namespace_id)`` and ``kg_edges``'
    ``(subject_label, predicate, object_label, namespace_id)`` unique
    constraints, so a canvas may re-author the same design indefinitely.

    **Additive only.**  Neither core issues a ``DELETE``, so idempotent is not
    the same as convergent: re-authoring adds and updates, never removes.  A
    device the user deleted on the canvas and then re-authored without survives
    in the graph with its ports, edges and capability row, and the read surface
    still returns it.  Expressing a removal is W17's job — this is not a
    full-state sync and must not be described as one.

    **Geometry (W14).**  Each device, port and rack may carry an optional
    ``geometry`` object, and each connection an optional ``cable_geometry``.
    They are written by ``geometry.do_author_geometry`` on the same connection,
    inside the same transaction, so a topology write and its layout can never
    half-land.

    **Lifecycle status (W16).**  Each device and rack may carry ``status``,
    ``revision`` and ``salience``; each connection carries the same three under
    ``cable_status`` / ``cable_revision`` / ``cable_salience`` for the CABLE
    node it names.  They travel inside the ``devices`` / ``racks`` /
    ``connections`` items this adapter already forwards **verbatim** — there is
    no separate top-level status argument and this adapter interprets none of
    them.  ``devices.do_author_device_topology`` writes them to
    ``system_design_node_state`` on the same connection and inside the same
    transaction as the graph rows.

    A state row is written **only when the node is genuinely new to the call,
    or the caller supplied one of the three keys.**  That is the whole point:
    this same coroutine serves an ordinary canvas save and a geometry-only
    drag, so a rule that wrote a row for every node touched would stamp
    ``'planned'`` onto legacy as-built equipment the first time anybody moved
    one 20 pixels.  A new node naming no status is stored as ``'planned'``; a
    pre-existing node supplying only ``revision`` gets a row whose ``status`` is
    NULL — data held, no lifecycle declared.  W17's retirement guard denies on
    an absent row AND on a NULL status, so both of those stay protected.

    PORT carries no lifecycle status: NetBox has none for a port, and the
    table's composite per-``node_type`` CHECK refuses a PORT row outright.  A
    port that carries one of the three keys is REFUSED with a ``ValueError``
    rather than silently ignored — a write that succeeds while dropping what
    the caller sent is the failure mode W13b refused ``expected_version`` for.
    The same refusal covers an unprefixed key on a connection, a ``cable_*``
    key on a device or a port, a ``cable_*`` key on a connection that names no
    ``cable_ref``, every casing and whitespace variant of those, and — one
    nesting level down — the same keys inside a ``capability`` or ``geometry``
    object, which until round 3 were accepted and dropped with a 200.

    A status outside the node type's vocabulary is refused by the DATABASE, and
    the core translates that refusal into the same ``ValueError`` — so it
    reaches the caller as ``-32602`` / 422 like every other bad argument rather
    than as an opaque internal error.

    Arguments:
        namespace_id (required), design_id (required), devices (required list),
        connections (optional list), racks (optional list), source_id (optional),
        actor (optional — Rev 2 §1), expected_version (optional — Rev 2 §2,
        LIVE since W14).

    Returns:
        The core result plus the W14 and W16 keys:
        ``{"authored": {"nodes", "edges", "capabilities", "state", "geometry"},
           "version": int}``.

        ``state`` is the number of DISTINCT node labels whose lifecycle row this
        call wrote, which is NOT one per DEVICE, RACK and CABLE authored.  A row
        is recorded only for a node genuinely new to the call, or one the caller
        sent a lifecycle key for, so an ordinary re-author reports ``0``.
        (Round 1's rule WAS the per-node one, and this sentence still described
        it two rounds later while the paragraph 30 lines above described the
        real one — on the published contract for the single adapter BOTH
        surfaces call, on a one-way door.)

        ``version`` is the design's NEW version — the token the caller passes on
        its next write.

    Raises:
        ValueError: a required argument is missing or the wrong shape.
        VersionConflictError: ``expected_version`` did not match.
    """
    namespace_id = require_namespace_id(arguments)
    expected_version = expected_version_of(arguments)
    design_id = _require_str(arguments, "design_id")
    devices = _require_list(arguments, "devices")
    connections = _optional_list(arguments, "connections")
    racks = _optional_list(arguments, "racks")
    source_id = arguments.get("source_id")
    actor = actor_of(arguments)

    async with scoped_pg_session(engine.pg_pool, namespace_id) as conn:
        # The compare-and-swap runs FIRST and on THIS connection, so it is
        # inside the write's own transaction: a stale token raises before any
        # graph row is written, and the increment cannot survive a write that
        # later fails.  It also takes the design's row lock for the duration,
        # which is what makes two concurrent writers serialise rather than
        # interleave.
        new_version = await bump_design_version(
            conn, UUID(namespace_id), design_id, expected_version
        )
        result = await do_author_device_topology(
            conn,
            namespace_id,
            design_id=design_id,
            devices=devices,
            connections=connections,
            racks=racks,
            source_id=source_id,
        )
        geometry_rows = await do_author_geometry(
            conn,
            namespace_id,
            design_id=design_id,
            devices=devices,
            connections=connections,
            racks=racks,
        )
        authored = dict(result.get("authored", {}))
        authored["geometry"] = geometry_rows
        # W16: the per-node lifecycle delta goes to the AUDIT EVENT, not to the
        # tool's return value.  The return contract is Copper's published shape
        # and this wave does not widen it; the question the delta answers —
        # which node became retirable, from what, by whom — is an audit
        # question, and event_log is the substrate that is INSERT-only,
        # HMAC-signed and Merkle-chained.
        state_changes = result.get("state_changes") or []
        result = {"authored": authored, "version": new_version}
        await _emit_authoring_event(
            conn,
            namespace_id=namespace_id,
            design_id=design_id,
            tool="system_design_author_topology",
            actor=actor,
            authored=authored,
            version=new_version,
            state_changes=state_changes,
        )

    return result


async def author_functional_location_from_arguments(
    engine: NCEEngine, arguments: dict[str, Any]
) -> dict[str, Any]:
    """Validate *arguments*, then call ``do_author_functional_location`` **verbatim**.

    The single authoring path behind
    ``system_design_author_functional_location`` (MCP) and
    ``POST /api/system-design/functional-location`` (REST).

    ``namespace_slug`` is required because it is a required keyword-only
    parameter of the core with no default: it is the deterministic prefix of
    every ``FL:`` label, so an adapter that guessed it would silently author a
    second, parallel tree.  Deriving one here would be a domain decision, and
    this wave makes none.

    **Geometry (W14).**  Each building, floor and room may carry an optional
    ``geometry`` object; room dimensions go in its ``meta`` under
    ``copper.room.w``/``.d``/``.h``, in meters.  ``positions`` are bare strings
    in this tool's contract, so a POSITION cannot carry geometry — a shape
    limit, not a decision, and reported rather than worked around.

    Arguments:
        namespace_id (required), namespace_slug (required), design_id (required),
        site_name (required), buildings (required list), design_lines (optional
        list), source_id (optional), actor (optional — Rev 2 §1),
        expected_version (optional — Rev 2 §2, LIVE since W14).

    Returns:
        ``{"authored": {"nodes", "edges", "geometry"}, "version": int}``.

    Raises:
        ValueError: a required argument is missing or the wrong shape.
        VersionConflictError: ``expected_version`` did not match.
    """
    namespace_id = require_namespace_id(arguments)
    expected_version = expected_version_of(arguments)
    namespace_slug = _require_str(arguments, "namespace_slug")
    design_id = _require_str(arguments, "design_id")
    site_name = _require_str(arguments, "site_name")
    buildings = _require_list(arguments, "buildings")
    design_lines = _optional_list(arguments, "design_lines")
    source_id = arguments.get("source_id")
    actor = actor_of(arguments)

    async with scoped_pg_session(engine.pg_pool, namespace_id) as conn:
        # First, on this connection — see the topology adapter for why.
        new_version = await bump_design_version(
            conn, UUID(namespace_id), design_id, expected_version
        )
        result = await do_author_functional_location(
            conn,
            namespace_id,
            namespace_slug=namespace_slug,
            design_id=design_id,
            site_name=site_name,
            buildings=buildings,
            design_lines=design_lines,
            source_id=source_id,
        )
        geometry_rows = await do_author_functional_location_geometry(
            conn,
            namespace_id,
            namespace_slug=namespace_slug,
            site_name=site_name,
            buildings=buildings,
        )
        authored = dict(result.get("authored", {}))
        authored["geometry"] = geometry_rows
        result = {"authored": authored, "version": new_version}
        await _emit_authoring_event(
            conn,
            namespace_id=namespace_id,
            design_id=design_id,
            tool="system_design_author_functional_location",
            actor=actor,
            authored=authored,
            version=new_version,
        )

    return result


# ---------------------------------------------------------------------------
# W17 — the retire adapter.  THE FIRST DELETE PATH IN THE CODEBASE.
#
# 🔴 THE TOOL'S NAME IS A DELIBERATE MISMATCH WITH ITS DEFAULT BEHAVIOUR.
# ``system_design_delete_planned`` / ``DELETE /api/system-design/planned`` are
# pinned by Copper's published contract, so neither may be renamed — but the
# default is a SOFT RETIRE and nothing is removed without ``permanent=true``.
# Every docstring on this path says so in its first line.  See ``retire.py``.
# ---------------------------------------------------------------------------


def permanent_of(arguments: dict[str, Any]) -> bool:
    """Return the caller's ``permanent`` flag, refusing anything but a real bool.

    🔴 **This is a destructive-path guard, not argument hygiene.**  Python's
    truthiness would read the JSON string ``"false"`` — and ``"no"``, and
    ``"0"`` — as ``True``, so a caller (or a BFF that stringifies query
    parameters, which is exactly what a ``DELETE`` with a query string invites)
    could ask for the safe default in words and get a permanent delete.  There
    is no coercion here for that reason: present-and-not-a-bool is refused as a
    malformed argument, and absent or ``null`` is the safe default ``False``.

    ``@mcp_handler`` maps the ``ValueError`` to ``-32602``; the REST route maps
    it to 422.

    Raises:
        ValueError: present with a non-boolean value.
    """
    value = arguments.get("permanent")
    if value is None:
        return False
    if not isinstance(value, bool):
        raise ValueError(
            "permanent must be a JSON boolean when supplied "
            "(a string is refused: 'false' would be truthy and would delete)"
        )
    return value


def retire_event_payload(
    *,
    namespace_id: str,
    design_id: str,
    actor: str | None,
    version: int,
    result: dict[str, Any],
) -> dict[str, Any]:
    """Build the audit-event params for a retire (Rev 2 §1, ``actor``).

    Recorded under the existing ``system_design_authored`` event type, with
    ``tool`` naming ``system_design_delete_planned`` — **not** under a new
    ``system_design_retired`` type.  That is a scope decision, disclosed rather
    than absorbed: minting an ``EventType`` value costs an edit to
    ``nce/event_types.py`` AND its paired provenance-only ForkedReplay handler
    in ``nce/replay.py`` (``append_event`` validates ``event_type`` against the
    Literal and ``_validate_handler_coverage`` requires every value to have a
    handler), and neither file is in this wave's ``Files:`` list.  ``tool``
    already discriminates the row and every consumer of this event type reads
    it, so the audit trail is complete; the dedicated type is reported to the
    orchestrator as follow-up.

    The shape is retirement's, not authoring's: it does **not** reuse
    ``authored``.  A count of "nodes written" on a call that wrote nothing and
    deleted five would be an audit record that reads as its own opposite.

    ``permanent`` is on the row unconditionally — including when it is
    ``False`` — and that is the one place this payload breaks the
    absent-means-absent rule the authoring payload follows.  On a destructive
    path "this was the reversible one" must be a positive assertion in the WORM
    log, not an inference a reader draws from a missing key that could equally
    mean the field predates them.

    ``actor`` follows the authoring rule: present only when supplied, never
    ``""`` and never a synthesised service identity.  On the permanent path the
    core has already refused an absent one, so a permanent row always carries it.
    """
    payload: dict[str, Any] = {
        "design_id": design_id,
        "design_label": f"DESIGN:{design_id.upper()}",
        "namespace": namespace_id,
        "tool": "system_design_delete_planned",
        "version": version,
        "permanent": bool(result.get("permanent")),
        "retired": result.get("retired") or [],
    }
    removed = result.get("removed")
    if removed:
        payload["removed"] = removed
    if actor is not None:
        payload["actor"] = actor
    return payload


async def retire_planned_from_arguments(
    engine: NCEEngine, arguments: dict[str, Any]
) -> dict[str, Any]:
    """SOFT-RETIRES by default — the pinned name says "delete" and this does not.

    Validates *arguments*, then calls ``do_retire_planned`` **verbatim**.  The
    single path behind ``system_design_delete_planned`` (MCP) and
    ``DELETE /api/system-design/planned`` (REST).  Nothing is removed unless the
    caller passes ``permanent=true``; the default writes the node's retired
    lifecycle status and floors its salience, and leaves every row in place.

    ``active`` deletion is **out of scope**: this path acts on nodes whose
    declared status is ``'planned'`` and only those.

    The version bump runs FIRST and on the same connection, exactly as the two
    authoring adapters do it: it takes the design's row lock for the duration
    (so a retire and a concurrent author serialise rather than interleave),
    honours ``expected_version``, and moves the token so a client polling the
    design sees that something changed.  A destructive call that left the
    concurrency token where it was would be invisible to exactly the clients
    that most need to notice it.

    Arguments:
        namespace_id (required), design_id (required),
        node_labels (required, non-empty list of canonical labels as returned by
        ``system_design_get_topology``; DEVICE / RACK / CABLE only, all of them
        belonging to ``design_id``),
        permanent (optional bool, default false — see :func:`permanent_of`),
        actor (optional in general — Rev 2 §1 — but **MANDATORY when
        permanent=true**, enforced by the core so both surfaces get it),
        expected_version (optional int — Rev 2 §2, LIVE since W14).

    Returns:
        ``{"permanent": bool, "retired": [...], "removed": {...} | None,
        "version": int}`` — ``version`` is the design's NEW token, the one to
        pass on the next write.

    Raises:
        ValueError: a missing or malformed argument.
        OwnershipError: this engine does not own the node type here.
        RetireDeniedError: a named node is not in a retirable state.  Nothing
            was changed.
        VersionConflictError: ``expected_version`` did not match.
    """
    namespace_id = require_namespace_id(arguments)
    expected_version = expected_version_of(arguments)
    design_id = _require_str(arguments, "design_id")
    node_labels = _require_list(arguments, "node_labels")
    permanent = permanent_of(arguments)
    actor = actor_of(arguments)

    async with scoped_pg_session(engine.pg_pool, namespace_id) as conn:
        new_version = await bump_design_version(
            conn, UUID(namespace_id), design_id, expected_version
        )
        result = await do_retire_planned(
            conn,
            namespace_id,
            design_id=design_id,
            node_labels=node_labels,
            permanent=permanent,
            actor=actor,
        )
        await append_event(
            conn=conn,
            namespace_id=UUID(namespace_id),
            agent_id=_AGENT_ID,
            event_type=_AUTHORING_EVENT_TYPE,
            params=retire_event_payload(
                namespace_id=namespace_id,
                design_id=design_id,
                actor=actor,
                version=new_version,
                result=result,
            ),
        )

    return dict(result, version=new_version)


def retire_denied_mcp_error(exc: RetireDeniedError) -> McpError:
    """Translate a retire refusal into JSON-RPC vocabulary.

    Code :data:`RETIRE_DENIED_MCP_CODE`, **not** ``-32602``: the arguments were
    well formed and the graph was not in the required state.  ``data.denials``
    carries every refused node with its machine-readable reason and its actual
    status, so a canvas that selected forty devices can show the user all forty
    answers without a second round trip, and ``data.reason`` is the stable
    discriminator, read from the one definition on :class:`RetireDeniedError` so
    the two surfaces cannot drift.
    """
    return McpError(
        RETIRE_DENIED_MCP_CODE,
        str(exc),
        data={"reason": exc.reason, "denials": exc.denials},
    )


def version_conflict_mcp_error(exc: VersionConflictError) -> McpError:
    """Translate a stale ``expected_version`` into JSON-RPC vocabulary.

    Code :data:`VERSION_CONFLICT_MCP_CODE`, **not** ``-32602``: "you are
    behind, re-read and retry" is a retryable fact about server state, while
    "Invalid params" is a permanent fault in the request.  ``data`` carries the
    expected and actual versions so a client can re-drive its own state machine
    without a second round trip, and ``data.reason`` remains the stable
    machine-readable discriminator, read from the one definition on
    ``VersionConflictError``.
    """
    return McpError(
        VERSION_CONFLICT_MCP_CODE,
        str(exc),
        data={
            "reason": exc.reason,
            "parameter": "expected_version",
            "expected_version": exc.expected,
            "actual_version": exc.actual,
        },
    )


# ---------------------------------------------------------------------------
# W13b — the two authoring MCP tools.
# ---------------------------------------------------------------------------


@mcp_handler
async def handle_system_design_author_topology(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """MCP tool: system_design_author_topology — author a DESIGN's device topology.

    Mutating (``cacheable=False, admin_only=False, mutation=True``).  Copper's
    published contract — the name and those three flags are not adjustable.
    Delegates to :func:`author_device_topology_from_arguments`; see it for the
    argument contract, including W16's per-item ``status`` / ``revision`` /
    ``salience`` (and their ``cable_``-prefixed twins on a connection).
    """
    try:
        result = await author_device_topology_from_arguments(engine, arguments)
    except VersionConflictError as exc:
        raise version_conflict_mcp_error(exc) from exc
    return json.dumps(result)


@mcp_handler
async def handle_system_design_author_functional_location(
    engine: NCEEngine, arguments: dict[str, Any]
) -> str:
    """MCP tool: system_design_author_functional_location — author the FL tree.

    Mutating (``cacheable=False, admin_only=False, mutation=True``).  Copper's
    published contract — the name and those three flags are not adjustable.
    Delegates to :func:`author_functional_location_from_arguments`; see it for
    the argument contract.
    """
    try:
        result = await author_functional_location_from_arguments(engine, arguments)
    except VersionConflictError as exc:
        raise version_conflict_mcp_error(exc) from exc
    return json.dumps(result)


# ---------------------------------------------------------------------------
# W17 — the retire MCP tool.
# ---------------------------------------------------------------------------


@mcp_handler
async def handle_system_design_delete_planned(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """MCP tool: system_design_delete_planned — SOFT-RETIRES by default; the name is a mismatch.

    🔴 **Read that first line again.**  The tool is called
    ``system_design_delete_planned`` and its REST twin is
    ``DELETE /api/system-design/planned`` because Copper's published contract
    pins both names and a rename breaks the front end — but the **default
    behaviour is a soft retire**: the node's lifecycle status becomes its node
    type's retired value (``'decommissioning'`` for a DEVICE or a CABLE,
    ``'deprecated'`` for a RACK — the vocabularies are disjoint) and its
    salience is floored.  **Nothing is removed.**  A genuine transactional
    delete of the node, its edges, its PORT children and all three of its
    side-table rows happens only when the caller passes ``permanent=true``,
    and that path additionally **requires ``actor``**.

    Only nodes whose declared status is ``'planned'`` can be touched.  A node
    with no lifecycle state row, or one whose ``status`` is NULL, is **denied** —
    and absence is the normal state of everything authored before W16, which is
    what keeps this tool away from real installed equipment.  Retiring
    ``active`` equipment is out of scope and is not expressible here.  One
    denied node denies the whole call.

    Mutating (``cacheable=False, admin_only=True, mutation=True``).
    ``admin_only=True`` is the one flag that differs from the two authoring
    tools, and it is not decoration: those add and update, this is the only tool
    in the module that can take something away.

    Delegates to :func:`retire_planned_from_arguments`; see it for the argument
    contract, and ``retire.py`` for every guard and the D12 side-table
    obligation.
    """
    try:
        result = await retire_planned_from_arguments(engine, arguments)
    except VersionConflictError as exc:
        raise version_conflict_mcp_error(exc) from exc
    except RetireDeniedError as exc:
        raise retire_denied_mcp_error(exc) from exc
    return json.dumps(result)


# ---------------------------------------------------------------------------
# M6.W26 -- the commercial half of the design loop (Batch 230a).
#
# Four cores existed with no route and no tool: AST-verified at zero callers for
# three of them, and do_propose_design excluded from this wave precisely because
# it has two (sales/commission.py:189, from_quote.py:231) and its return shape is
# load-bearing for commission -- it gets its own wave with a regression test.
#
# These adapters add nothing and subtract nothing. If a core needs changing to
# make one of these work, that is a different wave.
# ---------------------------------------------------------------------------


@mcp_handler
async def handle_system_design_from_quote(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """MCP tool: system_design_from_quote -- realise a Sales QUOTE into a DESIGN.

    MUTATING (``cacheable=False, admin_only=False, mutation=True``): the core
    lifts each quote line into a DESIGN plus one DESIGN_LINE, gap-fills missing
    accessories/infra/labour, and writes the cross-engine edge
    ``QUOTE -[realized_as]-> DESIGN``.

    Requires ``namespace_id`` and ``quote_id`` in *arguments*. Optional:
    ``design_id`` (defaults to ``DESIGN-<quote_id>``), ``namespace_slug`` (FL
    label prefix) and ``source_id``.

    The ``@mcp_handler`` decorator converts a missing-namespace ``ValueError``
    into an ``McpError(-32602)`` at call-site.
    """
    require_namespace_id(arguments)
    if not arguments.get("quote_id"):
        raise ValueError("quote_id is required")
    result = await do_design_from_quote(engine, arguments)
    return json.dumps(result, default=str)


@mcp_handler
async def handle_system_design_to_quote(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """MCP tool: system_design_to_quote -- derive quote lines back from a DESIGN.

    MUTATING (``cacheable=False, admin_only=False, mutation=True``): the core
    writes the cross-engine edge linking the DESIGN to the quote it produces.
    Sales still owns pricing and signing; this returns the lines, it does not
    price or freeze them.

    Requires ``namespace_id`` and ``design_id`` in *arguments*. Optional:
    ``source_id``.
    """
    require_namespace_id(arguments)
    if not arguments.get("design_id"):
        raise ValueError("design_id is required")
    result = await do_design_to_quote(engine, arguments)
    return json.dumps(result, default=str)


@mcp_handler
async def handle_system_design_generate_sow(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """MCP tool: system_design_generate_sow -- statement of work for a DESIGN.

    READ-ONLY (``cacheable=False, admin_only=False, mutation=False``). The core
    only reads: design meta, design lines and the FL nodes. ``cacheable=False``
    on a read follows ``system_design_validate_design_graph``'s precedent -- a
    design under active canvas editing must not be served a stale SOW, and there
    is no write here whose cache-generation bump would refresh it.

    Freeze-on-issue: ``version_number`` is derived deterministically from the
    design state, so re-issuing against an unchanged design returns the same
    version. Supplying ``version_number`` overrides it and marks the result
    frozen.

    Requires ``namespace_id`` and ``design_id`` in *arguments*. Optional:
    ``version_number``.
    """
    require_namespace_id(arguments)
    if not arguments.get("design_id"):
        raise ValueError("design_id is required")
    result = await do_generate_sow(engine, arguments)
    return json.dumps(result, default=str)


@mcp_handler
async def handle_system_design_enrich_design_lines(
    engine: NCEEngine, arguments: dict[str, Any]
) -> str:
    """MCP tool: system_design_enrich_design_lines -- fire enrichment for a design.

    SIDE-EFFECTING (``cacheable=False, admin_only=False, mutation=True``). The
    core reads the graph to find DESIGN_LINEs and their PRODUCTs, then fires
    scoped Product enrichment once per unique product UUID via
    ``_fire_product_enrichment`` -> ``enqueue_product_enrichment``, and attempts a
    Procurement TCO. It writes no graph rows itself, but it QUEUES work, so it is
    registered ``mutation=True`` rather than as a read: a caller must be able to
    tell that invoking it causes something to happen.

    Requires ``namespace_id`` and ``design_id`` in *arguments*. Optional:
    ``missing_fields`` (defaults to ``["etim_specs"]``).
    """
    require_namespace_id(arguments)
    if not arguments.get("design_id"):
        raise ValueError("design_id is required")
    result = await do_enrich_design_lines(engine, arguments)
    return json.dumps(result, default=str)


@mcp_handler
async def handle_system_design_propose_design(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """MCP tool: system_design_propose_design -- recall-driven BOM proposal.

    PROPOSE-ONLY (``cacheable=False, admin_only=False, mutation=False``). The core
    embeds the room brief, recalls the most similar past designs and returns
    proposed DESIGN_LINE dicts. It never auto-accepts, freezes or applies a line:
    every returned line carries ``validated: False``, and a human or a downstream
    tool must confirm each one before it is authored into the graph.

    Requires ``namespace_id`` and ``room_brief`` in *arguments*.

    Exposed SEPARATELY from its four M6.W26 siblings because, unlike them, this
    core is NOT orphaned: ``sales/commission.py:189`` and
    ``system_design/from_quote.py:231`` already call it, and commission embeds the
    result whole under ``system_design_proposal``. So this adapter must return the
    core's payload verbatim -- an envelope, a rename or a dropped key would break
    sales commission silently. Pinned by
    ``tests/unit/test_propose_design_surface_passthrough.py``.

    NOTE: ``top_k`` is optional (int, bounded [1, 50]). Defaults to
    ``cfg.NCE_SYSTEM_DESIGN_RECALL_TOP_K`` if not supplied (Wave SD-3).
    """
    require_namespace_id(arguments)
    if not arguments.get("room_brief"):
        raise ValueError("room_brief is required")
    result = await do_propose_design(engine, arguments)
    return json.dumps(result, default=str)


@mcp_handler
async def handle_system_design_get_standards(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """MCP tool: system_design_get_standards -- query curated AV cabling/mounting standards."""
    result = do_get_standards(engine, arguments or {})
    return json.dumps(result, default=str)


@mcp_handler
async def handle_system_design_get_signal_rules(
    engine: NCEEngine, arguments: dict[str, Any]
) -> str:
    """MCP tool: system_design_get_signal_rules -- query signal distribution rules or evaluate room inputs."""
    result = do_get_signal_rules(engine, arguments or {})
    return json.dumps(result, default=str)


@mcp_handler
async def handle_system_design_sync_device_capabilities(
    engine: NCEEngine, arguments: dict[str, Any]
) -> str:
    """MCP tool: system_design_sync_device_capabilities -- sync product ETIM features into device capabilities."""
    require_namespace_id(arguments)
    if not arguments.get("device_label"):
        raise ValueError("device_label is required")
    result = await do_sync_device_capabilities(engine, arguments)
    return json.dumps(result, default=str)


# ---------------------------------------------------------------------------
# Wave C-1: Functional Location Tree MCP Handlers
# ---------------------------------------------------------------------------


@mcp_handler
async def handle_system_design_list_functional_locations(
    engine: NCEEngine, arguments: dict[str, Any]
) -> str:
    """MCP tool: system_design_list_functional_locations -- search or list functional locations."""
    from nce.vertical_modules.system_design.fl_tree import search_fl_nodes

    namespace_id = require_namespace_id(arguments)
    q = arguments.get("query") or arguments.get("q")
    kind = arguments.get("kind")
    as_built = arguments.get("as_built")
    limit = int(arguments.get("limit") or 50)

    if getattr(engine, "pg_pool", None):
        async with scoped_pg_session(engine.pg_pool, namespace_id) as conn:
            results = await search_fl_nodes(
                conn, namespace_id, q=q, kind=kind, as_built=as_built, limit=limit
            )
    else:
        results = await search_fl_nodes(
            None, namespace_id, q=q, kind=kind, as_built=as_built, limit=limit
        )
    return json.dumps(results, default=str)


@mcp_handler
async def handle_system_design_get_functional_location(
    engine: NCEEngine, arguments: dict[str, Any]
) -> str:
    """MCP tool: system_design_get_functional_location -- retrieve a functional location by id or label."""
    from nce.vertical_modules.system_design.fl_tree import get_fl_node

    namespace_id = require_namespace_id(arguments)
    node_id = str(arguments.get("node_id") or "").strip()
    if not node_id:
        raise ValueError("node_id is required")

    if getattr(engine, "pg_pool", None):
        async with scoped_pg_session(engine.pg_pool, namespace_id) as conn:
            node = await get_fl_node(conn, namespace_id, node_id)
    else:
        node = await get_fl_node(None, namespace_id, node_id)
    return json.dumps(node, default=str)


@mcp_handler
async def handle_system_design_get_fl_children(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """MCP tool: system_design_get_fl_children -- retrieve child functional locations."""
    from nce.vertical_modules.system_design.fl_tree import get_fl_children

    namespace_id = require_namespace_id(arguments)
    node_id = str(arguments.get("node_id") or "").strip()
    if not node_id:
        raise ValueError("node_id is required")
    recursive = bool(arguments.get("recursive", False))

    if getattr(engine, "pg_pool", None):
        async with scoped_pg_session(engine.pg_pool, namespace_id) as conn:
            children = await get_fl_children(conn, namespace_id, node_id, recursive=recursive)
    else:
        children = await get_fl_children(None, namespace_id, node_id, recursive=recursive)
    return json.dumps(children, default=str)


@mcp_handler
async def handle_system_design_get_fl_ancestors(
    engine: NCEEngine, arguments: dict[str, Any]
) -> str:
    """MCP tool: system_design_get_fl_ancestors -- retrieve ancestor hierarchy chain."""
    from nce.vertical_modules.system_design.fl_tree import get_fl_ancestors

    namespace_id = require_namespace_id(arguments)
    node_id = str(arguments.get("node_id") or "").strip()
    if not node_id:
        raise ValueError("node_id is required")

    if getattr(engine, "pg_pool", None):
        async with scoped_pg_session(engine.pg_pool, namespace_id) as conn:
            ancestors = await get_fl_ancestors(conn, namespace_id, node_id)
    else:
        ancestors = await get_fl_ancestors(None, namespace_id, node_id)
    return json.dumps(ancestors, default=str)


@mcp_handler
async def handle_system_design_get_fl_path(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """MCP tool: system_design_get_fl_path -- retrieve full path [root, ..., node] and path string."""
    from nce.vertical_modules.system_design.fl_tree import get_fl_path

    namespace_id = require_namespace_id(arguments)
    node_id = str(arguments.get("node_id") or "").strip()
    if not node_id:
        raise ValueError("node_id is required")

    if getattr(engine, "pg_pool", None):
        async with scoped_pg_session(engine.pg_pool, namespace_id) as conn:
            path_info = await get_fl_path(conn, namespace_id, node_id)
    else:
        path_info = await get_fl_path(None, namespace_id, node_id)
    return json.dumps(path_info, default=str)


@mcp_handler
async def handle_system_design_move_functional_location(
    engine: NCEEngine, arguments: dict[str, Any]
) -> str:
    """MCP tool: system_design_move_functional_location -- move a node under a new parent with cycle detection."""
    from nce.vertical_modules.system_design.fl_tree import move_fl_node

    namespace_id = require_namespace_id(arguments)
    node_id = str(arguments.get("node_id") or "").strip()
    new_parent_id = str(arguments.get("new_parent_id") or "").strip()
    if not node_id or not new_parent_id:
        raise ValueError("node_id and new_parent_id are required")
    actor = actor_of(arguments)

    if getattr(engine, "pg_pool", None):
        async with scoped_pg_session(engine.pg_pool, namespace_id) as conn:
            result = await move_fl_node(conn, namespace_id, node_id, new_parent_id, actor=actor)
    else:
        result = await move_fl_node(None, namespace_id, node_id, new_parent_id, actor=actor)
    return json.dumps(result, default=str)


@mcp_handler
async def handle_system_design_merge_functional_locations(
    engine: NCEEngine, arguments: dict[str, Any]
) -> str:
    """MCP tool: system_design_merge_functional_locations -- merge absorbed node into survivor node."""
    from nce.vertical_modules.system_design.fl_tree import merge_fl_nodes

    namespace_id = require_namespace_id(arguments)
    survivor_id = str(arguments.get("survivor_id") or "").strip()
    absorbed_id = str(arguments.get("absorbed_id") or "").strip()
    if not survivor_id or not absorbed_id:
        raise ValueError("survivor_id and absorbed_id are required")
    reversible = bool(arguments.get("reversible", True))
    actor = actor_of(arguments)

    if getattr(engine, "pg_pool", None):
        async with scoped_pg_session(engine.pg_pool, namespace_id) as conn:
            result = await merge_fl_nodes(
                conn, namespace_id, survivor_id, absorbed_id, reversible=reversible, actor=actor
            )
    else:
        result = await merge_fl_nodes(
            None, namespace_id, survivor_id, absorbed_id, reversible=reversible, actor=actor
        )
    return json.dumps(result, default=str)


@mcp_handler
async def handle_system_design_promote_functional_location(
    engine: NCEEngine, arguments: dict[str, Any]
) -> str:
    """MCP tool: system_design_promote_functional_location -- promote FL from design-intent to as-built."""
    from nce.vertical_modules.system_design.fl_tree import promote_fl_node

    namespace_id = require_namespace_id(arguments)
    node_id = str(arguments.get("node_id") or "").strip()
    if not node_id:
        raise ValueError("node_id is required")
    actor = actor_of(arguments)

    if getattr(engine, "pg_pool", None):
        async with scoped_pg_session(engine.pg_pool, namespace_id) as conn:
            result = await promote_fl_node(conn, namespace_id, node_id, actor=actor)
    else:
        result = await promote_fl_node(None, namespace_id, node_id, actor=actor)
    return json.dumps(result, default=str)


# ---------------------------------------------------------------------------
# Room Categories & FL Metadata (Wave C-2)
# ---------------------------------------------------------------------------


@mcp_handler
async def handle_system_design_list_room_categories(
    engine: NCEEngine, arguments: dict[str, Any]
) -> str:
    """MCP tool: system_design_list_room_categories -- list standardized AV/engineering room categories."""
    from nce.vertical_modules.system_design.room_categories import do_get_room_categories

    categories = do_get_room_categories(engine, arguments)
    return json.dumps(categories, default=str)


@mcp_handler
async def handle_system_design_get_room_category(
    engine: NCEEngine, arguments: dict[str, Any]
) -> str:
    """MCP tool: system_design_get_room_category -- retrieve definition for a single room category."""
    from nce.vertical_modules.system_design.room_categories import do_get_room_category

    category_id = str(arguments.get("category_id") or arguments.get("id") or "").strip()
    if not category_id:
        raise ValueError("category_id is required")
    category = do_get_room_category(engine, category_id)
    return json.dumps(category, default=str)


@mcp_handler
async def handle_system_design_set_fl_room_category(
    engine: NCEEngine, arguments: dict[str, Any]
) -> str:
    """MCP tool: system_design_set_fl_room_category -- associate a room category with a functional location."""
    from nce.vertical_modules.system_design.room_categories import do_set_fl_room_category

    namespace_id = require_namespace_id(arguments)
    fl_id = str(
        arguments.get("fl_id") or arguments.get("node_id") or arguments.get("fl_id_or_label") or ""
    ).strip()
    if not fl_id:
        raise ValueError("fl_id is required")
    category_id = str(arguments.get("category_id") or "").strip()
    if not category_id:
        raise ValueError("category_id is required")

    params = {
        "fl_id": fl_id,
        "category_id": category_id,
        "actor": actor_of(arguments),
    }
    result = await do_set_fl_room_category(engine, namespace_id, params)
    return json.dumps(result, default=str)


@mcp_handler
async def handle_system_design_get_fl_room_category(
    engine: NCEEngine, arguments: dict[str, Any]
) -> str:
    """MCP tool: system_design_get_fl_room_category -- get room category for a functional location."""
    from nce.vertical_modules.system_design.room_categories import do_get_fl_room_category

    namespace_id = require_namespace_id(arguments)
    fl_id = str(
        arguments.get("fl_id") or arguments.get("node_id") or arguments.get("fl_id_or_label") or ""
    ).strip()
    if not fl_id:
        raise ValueError("fl_id is required")

    result = await do_get_fl_room_category(engine, namespace_id, {"fl_id": fl_id})
    return json.dumps(result, default=str)


@mcp_handler
async def handle_system_design_assign_fl_responsible(
    engine: NCEEngine, arguments: dict[str, Any]
) -> str:
    """MCP tool: system_design_assign_fl_responsible -- assign a responsible employee to a functional location."""
    from nce.vertical_modules.system_design.room_categories import do_assign_fl_responsible

    namespace_id = require_namespace_id(arguments)
    fl_id = str(
        arguments.get("fl_id") or arguments.get("node_id") or arguments.get("fl_id_or_label") or ""
    ).strip()
    if not fl_id:
        raise ValueError("fl_id is required")
    employee_id = str(arguments.get("employee_id") or "").strip()
    if not employee_id:
        raise ValueError("employee_id is required")
    role = str(arguments.get("role") or "primary").strip()

    params = {
        "fl_id": fl_id,
        "employee_id": employee_id,
        "role": role,
        "actor": actor_of(arguments),
    }
    result = await do_assign_fl_responsible(engine, namespace_id, params)
    return json.dumps(result, default=str)


@mcp_handler
async def handle_system_design_unassign_fl_responsible(
    engine: NCEEngine, arguments: dict[str, Any]
) -> str:
    """MCP tool: system_design_unassign_fl_responsible -- remove a responsible employee assignment from a functional location."""
    from nce.vertical_modules.system_design.room_categories import do_unassign_fl_responsible

    namespace_id = require_namespace_id(arguments)
    fl_id = str(
        arguments.get("fl_id") or arguments.get("node_id") or arguments.get("fl_id_or_label") or ""
    ).strip()
    if not fl_id:
        raise ValueError("fl_id is required")
    employee_id = str(arguments.get("employee_id") or "").strip()
    if not employee_id:
        raise ValueError("employee_id is required")

    params = {
        "fl_id": fl_id,
        "employee_id": employee_id,
        "actor": actor_of(arguments),
    }
    result = await do_unassign_fl_responsible(engine, namespace_id, params)
    return json.dumps(result, default=str)


@mcp_handler
async def handle_system_design_list_fl_responsible(
    engine: NCEEngine, arguments: dict[str, Any]
) -> str:
    """MCP tool: system_design_list_fl_responsible -- list employees assigned to a functional location."""
    from nce.vertical_modules.system_design.room_categories import do_list_fl_responsible

    namespace_id = require_namespace_id(arguments)
    fl_id = str(
        arguments.get("fl_id") or arguments.get("node_id") or arguments.get("fl_id_or_label") or ""
    ).strip()
    if not fl_id:
        raise ValueError("fl_id is required")

    results = await do_list_fl_responsible(engine, namespace_id, {"fl_id": fl_id})
    return json.dumps(results, default=str)


@mcp_handler
async def handle_system_design_list_my_responsible_fls(
    engine: NCEEngine, arguments: dict[str, Any]
) -> str:
    """MCP tool: system_design_list_my_responsible_fls -- list functional locations assigned to the calling employee."""
    from nce.vertical_modules.system_design.room_categories import do_list_my_responsible_fls

    namespace_id = require_namespace_id(arguments)
    employee_id = arguments.get("employee_id")

    results = await do_list_my_responsible_fls(engine, namespace_id, employee_id=employee_id)
    return json.dumps(results, default=str)


# ---------------------------------------------------------------------------
# Wave C-3: DESIGN Versions per Functional Location & Room Specifications
# ---------------------------------------------------------------------------


@mcp_handler
async def handle_system_design_list_designs(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """MCP tool: system_design_list_designs -- list designs with optional FL and active filters."""
    from nce.vertical_modules.system_design.design_versions import list_designs

    namespace_id = require_namespace_id(arguments)
    fl_id = arguments.get("functional_location_id") or arguments.get("fl_id")
    is_active_val = arguments.get("is_active")
    is_active = None
    if is_active_val is not None:
        if isinstance(is_active_val, str):
            is_active = is_active_val.lower() in ("true", "1", "yes")
        else:
            is_active = bool(is_active_val)
    query = arguments.get("query") or arguments.get("q")
    limit = int(arguments.get("limit", 50))
    offset = int(arguments.get("offset", 0))

    if getattr(engine, "pg_pool", None):
        async with scoped_pg_session(engine.pg_pool, namespace_id) as conn:
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
    return json.dumps(designs, default=str)


@mcp_handler
async def handle_system_design_get_design(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """MCP tool: system_design_get_design -- fetch details of a design version."""
    from nce.vertical_modules.system_design.design_versions import get_design

    namespace_id = require_namespace_id(arguments)
    design_id = str(arguments.get("design_id") or arguments.get("id") or "").strip()
    if not design_id:
        raise ValueError("design_id is required")

    if getattr(engine, "pg_pool", None):
        async with scoped_pg_session(engine.pg_pool, namespace_id) as conn:
            design = await get_design(conn, namespace_id, design_id)
    else:
        design = await get_design(None, namespace_id, design_id)
    return json.dumps(design, default=str)


@mcp_handler
async def handle_system_design_create_design(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """MCP tool: system_design_create_design -- create a new design version for a functional location."""
    from nce.vertical_modules.system_design.design_versions import create_design

    namespace_id = require_namespace_id(arguments)
    design_id = str(arguments.get("design_id") or arguments.get("id") or "").strip()
    name = str(arguments.get("name") or "").strip()
    fl_id = str(arguments.get("functional_location_id") or arguments.get("fl_id") or "").strip()
    version = int(arguments.get("version", 1))
    revision = arguments.get("revision")
    room_spec = arguments.get("room_spec")
    is_active = bool(arguments.get("is_active", False))
    metadata = arguments.get("metadata")
    source_id = arguments.get("source_id")

    if getattr(engine, "pg_pool", None):
        async with scoped_pg_session(engine.pg_pool, namespace_id) as conn:
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
    return json.dumps(created, default=str)


@mcp_handler
async def handle_system_design_update_design(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """MCP tool: system_design_update_design -- update design fields."""
    from nce.vertical_modules.system_design.design_versions import update_design

    namespace_id = require_namespace_id(arguments)
    design_id = str(arguments.get("design_id") or arguments.get("id") or "").strip()
    if not design_id:
        raise ValueError("design_id is required")

    name = arguments.get("name")
    revision = arguments.get("revision")
    room_spec = arguments.get("room_spec")
    metadata = arguments.get("metadata")
    is_active_val = arguments.get("is_active")
    is_active = None
    if is_active_val is not None:
        if isinstance(is_active_val, str):
            is_active = is_active_val.lower() in ("true", "1", "yes")
        else:
            is_active = bool(is_active_val)

    if getattr(engine, "pg_pool", None):
        async with scoped_pg_session(engine.pg_pool, namespace_id) as conn:
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
    return json.dumps(updated, default=str)


@mcp_handler
async def handle_system_design_set_active_design(
    engine: NCEEngine, arguments: dict[str, Any]
) -> str:
    """MCP tool: system_design_set_active_design -- atomically set design active for its FL."""
    from nce.vertical_modules.system_design.design_versions import set_active_design

    namespace_id = require_namespace_id(arguments)
    design_id = str(arguments.get("design_id") or arguments.get("id") or "").strip()
    if not design_id:
        raise ValueError("design_id is required")

    if getattr(engine, "pg_pool", None):
        async with scoped_pg_session(engine.pg_pool, namespace_id) as conn:
            active = await set_active_design(conn, namespace_id, design_id)
    else:
        active = await set_active_design(None, namespace_id, design_id)
    return json.dumps(active, default=str)


@mcp_handler
async def handle_system_design_get_active_design(
    engine: NCEEngine, arguments: dict[str, Any]
) -> str:
    """MCP tool: system_design_get_active_design -- fetch active design for a functional location."""
    from nce.vertical_modules.system_design.design_versions import get_active_design_for_fl

    namespace_id = require_namespace_id(arguments)
    fl_id = str(arguments.get("functional_location_id") or arguments.get("fl_id") or "").strip()
    if not fl_id:
        raise ValueError("functional_location_id is required")

    if getattr(engine, "pg_pool", None):
        async with scoped_pg_session(engine.pg_pool, namespace_id) as conn:
            active = await get_active_design_for_fl(conn, namespace_id, fl_id)
    else:
        active = await get_active_design_for_fl(None, namespace_id, fl_id)
    return json.dumps(active, default=str)


@mcp_handler
async def handle_system_design_get_room_spec(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """MCP tool: system_design_get_room_spec -- get ROOM_SPEC metadata from a design."""
    from nce.vertical_modules.system_design.design_versions import get_room_spec

    namespace_id = require_namespace_id(arguments)
    design_id = str(arguments.get("design_id") or arguments.get("id") or "").strip()
    if not design_id:
        raise ValueError("design_id is required")

    if getattr(engine, "pg_pool", None):
        async with scoped_pg_session(engine.pg_pool, namespace_id) as conn:
            spec = await get_room_spec(conn, namespace_id, design_id)
    else:
        spec = await get_room_spec(None, namespace_id, design_id)
    return json.dumps(spec, default=str)


@mcp_handler
async def handle_system_design_set_room_spec(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """MCP tool: system_design_set_room_spec -- update ROOM_SPEC metadata on a design."""
    from nce.vertical_modules.system_design.design_versions import set_room_spec

    namespace_id = require_namespace_id(arguments)
    design_id = str(arguments.get("design_id") or arguments.get("id") or "").strip()
    if not design_id:
        raise ValueError("design_id is required")
    room_spec = arguments.get("room_spec") or {}

    if getattr(engine, "pg_pool", None):
        async with scoped_pg_session(engine.pg_pool, namespace_id) as conn:
            updated_spec = await set_room_spec(conn, namespace_id, design_id, room_spec)
    else:
        updated_spec = await set_room_spec(None, namespace_id, design_id, room_spec)
    return json.dumps(updated_spec, default=str)


# ---------------------------------------------------------------------------
# Wave C-4: DESIGN_REQUEST intake queue handlers
# ---------------------------------------------------------------------------


@mcp_handler
async def handle_system_design_create_design_request(
    engine: NCEEngine, arguments: dict[str, Any]
) -> str:
    """MCP tool: system_design_create_design_request -- create a solution design request in the intake queue."""
    from nce.vertical_modules.system_design.design_requests import create_design_request

    namespace_id = require_namespace_id(arguments)
    title = str(arguments.get("title") or "").strip()
    quote_id = arguments.get("quote_id")
    functional_location_id = arguments.get("functional_location_id") or arguments.get("fl_id")
    description = arguments.get("description")
    priority = arguments.get("priority", "normal")
    owner_id = arguments.get("owner_id")
    room_spec = arguments.get("room_spec")
    metadata = arguments.get("metadata")
    request_id = arguments.get("request_id") or arguments.get("id")

    if getattr(engine, "pg_pool", None):
        async with scoped_pg_session(engine.pg_pool, namespace_id) as conn:
            created = await create_design_request(
                conn,
                namespace_id,
                title=title,
                quote_id=str(quote_id) if quote_id else None,
                functional_location_id=(
                    str(functional_location_id) if functional_location_id else None
                ),
                description=str(description) if description else None,
                priority=str(priority) if priority else "normal",
                owner_id=str(owner_id) if owner_id else None,
                room_spec=room_spec,
                metadata=metadata,
                request_id=str(request_id) if request_id else None,
            )
    else:
        created = await create_design_request(
            None,
            namespace_id,
            title=title,
            quote_id=str(quote_id) if quote_id else None,
            functional_location_id=(
                str(functional_location_id) if functional_location_id else None
            ),
            description=str(description) if description else None,
            priority=str(priority) if priority else "normal",
            owner_id=str(owner_id) if owner_id else None,
            room_spec=room_spec,
            metadata=metadata,
            request_id=str(request_id) if request_id else None,
        )
    return json.dumps(created, default=str)


@mcp_handler
async def handle_system_design_get_design_request(
    engine: NCEEngine, arguments: dict[str, Any]
) -> str:
    """MCP tool: system_design_get_design_request -- fetch a design request by ID."""
    from nce.vertical_modules.system_design.design_requests import get_design_request

    namespace_id = require_namespace_id(arguments)
    request_id = str(arguments.get("request_id") or arguments.get("id") or "").strip()
    if not request_id:
        raise ValueError("request_id is required")

    if getattr(engine, "pg_pool", None):
        async with scoped_pg_session(engine.pg_pool, namespace_id) as conn:
            req = await get_design_request(conn, namespace_id, request_id)
    else:
        req = await get_design_request(None, namespace_id, request_id)
    return json.dumps(req, default=str)


@mcp_handler
async def handle_system_design_list_design_requests(
    engine: NCEEngine, arguments: dict[str, Any]
) -> str:
    """MCP tool: system_design_list_design_requests -- list design requests in the intake queue."""
    from nce.vertical_modules.system_design.design_requests import list_design_requests

    namespace_id = require_namespace_id(arguments)
    status = arguments.get("status")
    owner_id = arguments.get("owner_id")
    quote_id = arguments.get("quote_id")
    functional_location_id = arguments.get("functional_location_id") or arguments.get("fl_id")
    priority = arguments.get("priority")
    query = arguments.get("query")
    limit = int(arguments.get("limit", 50))
    offset = int(arguments.get("offset", 0))

    if getattr(engine, "pg_pool", None):
        async with scoped_pg_session(engine.pg_pool, namespace_id) as conn:
            requests = await list_design_requests(
                conn,
                namespace_id,
                status=str(status) if status else None,
                owner_id=str(owner_id) if owner_id else None,
                quote_id=str(quote_id) if quote_id else None,
                functional_location_id=(
                    str(functional_location_id) if functional_location_id else None
                ),
                priority=str(priority) if priority else None,
                query=str(query) if query else None,
                limit=limit,
                offset=offset,
            )
    else:
        requests = await list_design_requests(
            None,
            namespace_id,
            status=str(status) if status else None,
            owner_id=str(owner_id) if owner_id else None,
            quote_id=str(quote_id) if quote_id else None,
            functional_location_id=(
                str(functional_location_id) if functional_location_id else None
            ),
            priority=str(priority) if priority else None,
            query=str(query) if query else None,
            limit=limit,
            offset=offset,
        )
    return json.dumps(requests, default=str)


@mcp_handler
async def handle_system_design_update_design_request(
    engine: NCEEngine, arguments: dict[str, Any]
) -> str:
    """MCP tool: system_design_update_design_request -- update fields on a design request."""
    from nce.vertical_modules.system_design.design_requests import update_design_request

    namespace_id = require_namespace_id(arguments)
    request_id = str(arguments.get("request_id") or arguments.get("id") or "").strip()
    if not request_id:
        raise ValueError("request_id is required")

    title = arguments.get("title")
    description = arguments.get("description")
    status = arguments.get("status")
    priority = arguments.get("priority")
    owner_id = arguments.get("owner_id")
    room_spec = arguments.get("room_spec")
    metadata = arguments.get("metadata")

    if getattr(engine, "pg_pool", None):
        async with scoped_pg_session(engine.pg_pool, namespace_id) as conn:
            updated = await update_design_request(
                conn,
                namespace_id,
                request_id,
                title=str(title) if title is not None else None,
                description=str(description) if description is not None else None,
                status=str(status) if status is not None else None,
                priority=str(priority) if priority is not None else None,
                owner_id=str(owner_id) if owner_id is not None else None,
                room_spec=room_spec,
                metadata=metadata,
            )
    else:
        updated = await update_design_request(
            None,
            namespace_id,
            request_id,
            title=str(title) if title is not None else None,
            description=str(description) if description is not None else None,
            status=str(status) if status is not None else None,
            priority=str(priority) if priority is not None else None,
            owner_id=str(owner_id) if owner_id is not None else None,
            room_spec=room_spec,
            metadata=metadata,
        )
    return json.dumps(updated, default=str)


@mcp_handler
async def handle_system_design_assign_design_request(
    engine: NCEEngine, arguments: dict[str, Any]
) -> str:
    """MCP tool: system_design_assign_design_request -- assign a design request to an owner."""
    from nce.vertical_modules.system_design.design_requests import assign_design_request

    namespace_id = require_namespace_id(arguments)
    request_id = str(arguments.get("request_id") or arguments.get("id") or "").strip()
    if not request_id:
        raise ValueError("request_id is required")
    owner_id = str(arguments.get("owner_id") or "").strip()
    if not owner_id:
        raise ValueError("owner_id is required")

    if getattr(engine, "pg_pool", None):
        async with scoped_pg_session(engine.pg_pool, namespace_id) as conn:
            assigned = await assign_design_request(conn, namespace_id, request_id, owner_id)
    else:
        assigned = await assign_design_request(None, namespace_id, request_id, owner_id)
    return json.dumps(assigned, default=str)


@mcp_handler
async def handle_system_design_complete_design_request(
    engine: NCEEngine, arguments: dict[str, Any]
) -> str:
    """MCP tool: system_design_complete_design_request -- complete a request and link resulting design."""
    from nce.vertical_modules.system_design.design_requests import complete_design_request

    namespace_id = require_namespace_id(arguments)
    request_id = str(arguments.get("request_id") or arguments.get("id") or "").strip()
    if not request_id:
        raise ValueError("request_id is required")
    design_id = str(arguments.get("design_id") or "").strip()
    if not design_id:
        raise ValueError("design_id is required")

    if getattr(engine, "pg_pool", None):
        async with scoped_pg_session(engine.pg_pool, namespace_id) as conn:
            completed = await complete_design_request(conn, namespace_id, request_id, design_id)
    else:
        completed = await complete_design_request(None, namespace_id, request_id, design_id)
    return json.dumps(completed, default=str)


@mcp_handler
async def handle_system_design_fulfill_request_from_quote(
    engine: NCEEngine, arguments: dict[str, Any]
) -> str:
    """MCP tool: system_design_fulfill_request_from_quote -- fulfill design request from its linked quote."""
    from nce.vertical_modules.system_design.design_requests import (
        fulfill_design_request_from_quote,
    )

    namespace_id = require_namespace_id(arguments)
    request_id = str(arguments.get("request_id") or arguments.get("id") or "").strip()
    if not request_id:
        raise ValueError("request_id is required")
    design_id = arguments.get("design_id")
    namespace_slug = arguments.get("namespace_slug")
    source_id = arguments.get("source_id")

    result = await fulfill_design_request_from_quote(
        engine,
        namespace_id,
        request_id,
        design_id=str(design_id) if design_id else None,
        namespace_slug=str(namespace_slug) if namespace_slug else None,
        source_id=str(source_id) if source_id else None,
    )
    return json.dumps(result, default=str)
