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
from nce.vertical_modules.system_design.devices import do_author_device_topology
from nce.vertical_modules.system_design.geometry import (
    BIGINT_MAX,
    VersionConflictError,
    bump_design_version,
    do_author_functional_location_geometry,
    do_author_geometry,
)
from nce.vertical_modules.system_design.graph import do_author_functional_location
from nce.vertical_modules.system_design.lucid import do_publish_design_docs
from nce.vertical_modules.system_design.read import do_get_topology
from nce.vertical_modules.system_design.retire import (
    RetireDeniedError,
    do_retire_planned,
)
from nce.vertical_modules.system_design.validation_queries import validate_design_graph

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
    result = await validate_design_graph(engine, arguments)
    # No ``default=`` fallback: the core returns a bool and a list of str, so a
    # future non-encodable value must fail loudly rather than be stringified
    # into a shape the REST route would not produce.
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
