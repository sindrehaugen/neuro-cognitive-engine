"""
nce/vertical_modules/assets/graph.py
=====================================
Cognitive-graph projection for the Assets vertical module (Module 9, Wave 2b
— ``asset-graph-projection``, Batch 142b).

Projects an asset ALREADY RECORDED by Batch 142's
``nce/vertical_modules/assets/seed.py`` into the graph:

  * an ``ASSET`` ``kg_nodes`` row,
  * ``BOM_LINE -[installed_as]-> ASSET`` — the seed edge written at install
    handover, and
  * ``ASSET -[lives_in]-> FUNCTIONAL_LOCATION`` — the room-centric anchor.

Per ``docs/vertical_engines/09-assets-engine.md`` ("Graph contribution", the
§4 edge contract) and ``00-ENGINES-ROADMAP.md`` §9.1/§9.6. Migration 054's
header states the intent this module implements: ``bom_line_id`` and
``functional_location_id`` are "plain TEXT REFERENCES-IN-NAME-ONLY, carrying
the originating identifier so Batch 142b can build both edges by reading this
row".

Projection ONLY: this module creates no tables, changes no ``assets`` row, and
is the complement of ``seed.py``, which deliberately writes no graph at all.

This module OWNS Module 9's first Contract-A row
--------------------------------------------------
``assert_owner`` is deny-by-default. Before this wave
``nce/config_data/node-ownership.json`` held no ``ASSET`` row and no row of
any kind owned by an ``assets`` engine, so every ``ASSET`` node write was
refused. This wave adds exactly one row —
``{"node_type": "ASSET", "owner_engine": "assets", "transition": null}`` —
following the ``GOODS_RECEIPT`` / ``INVENTORY_RMA`` whole-node precedent:
Assets is the sole writer-of-record for the whole ``ASSET`` node, unlike
``MARGIN``, which is per-transition because several engines write one node's
dimensions in sequence.

Two node types this module deliberately does NOT claim
---------------------------------------------------------
``FUNCTIONAL_LOCATION`` is owned by ``system_design`` (already registered).
``BOM_LINE`` belongs to Batches 132a/133b and NOTHING in the program creates
those nodes yet. Both are written here as edge ENDPOINTS ONLY. That is sound
rather than a shortcut: ``kg_edges`` is label-keyed with **no foreign key to
kg_nodes**, so an edge naming a node that does not exist yet is a legitimate
assertion that resolves the moment the owning engine creates it — the exact
rule ``agreements/sla.py`` already relies on for its ``covers`` edge, and the
reason no ``BOM_LINE`` node had to be authored here.

Label conventions — copied from the owning engines, not invented
------------------------------------------------------------------
``ASSET:<ASSET_ID>``
    ``asset_id`` is migration 054's ``assets.id`` (a UUID primary key) — the
    stable identity ``do_seed_asset_from_bom`` already returns. Deliberately
    NOT keyed on ``serial``: ``serial`` is nullable by design (a seed
    legitimately precedes the installer's scan — see ``seed.py``), so a
    serial-keyed label could not be built at all for an unscanned asset.

``FL:<NAMESPACE_SLUG>:<PATH>``
    System Design's convention (source of truth:
    ``system_design/graph.py:_fl_label``), reproduced exactly the way
    ``agreements/sla.py:_functional_location_label`` already reproduces it
    from a single opaque ``functional_location_id``. This module never
    authors the node.

``BOM_LINE:<BOM_LINE_ID>``
    ⚠ **The one convention this wave could not copy — declared, not silent.**
    ``project/convert.py:_bom_line_label`` builds a THREE-part label,
    ``BOM_LINE:<QUOTE_ID>:<LINE_REF>``, from two arguments. Migration 054's
    ``assets.bom_line_id`` is a SINGLE opaque TEXT column, and no code in the
    repo maps one such token to that pair — ``bom_line_id`` appears nowhere
    outside ``seed.py``. This module therefore treats ``bom_line_id`` as the
    already-joined identifier component and prefixes it, mirroring how
    ``sla.py`` treats a colon-delimited ``functional_location_id`` as the
    site-path component(s) of an ``FL:`` label.

    THE CONSEQUENCE, ACCEPTED AND NAMED. The labels line up only when the
    caller stores ``bom_line_id`` as ``"<QUOTE_ID>:<LINE_REF>"`` (e.g.
    ``"Q001:AMP01"`` -> ``BOM_LINE:Q001:AMP01``, which matches
    ``convert.py`` exactly). A caller storing a flat token (``"BL-1"``)
    yields ``BOM_LINE:BL-1``, which will match no ``BOM_LINE`` node that
    Batch 132a later creates, and the ``installed_as`` edge would then dangle
    permanently. Nothing in this wave detects that, because there is nothing
    to compare against yet: no ``BOM_LINE`` node exists anywhere in the
    program. When Batch 132a lands, this helper must be re-checked against
    whatever identifier that wave persists — it is the single point of
    change, which is why it is a named helper rather than an inline f-string.

⚠ The FL label needs the namespace SLUG, which ``nce_app`` cannot read
------------------------------------------------------------------------
System Design's ``FL:`` labels embed the namespace slug, so the ``lives_in``
edge cannot be built without it — and ``namespaces`` carries **no grant at all
for the restricted ``nce_app`` role** (verified on this base: the only grantee
is the owner). A slug lookup therefore raises
``asyncpg.InsufficientPrivilegeError`` on exactly the connection production
uses. Discovered by driving this module through a real ``nce_app`` pool; it is
not hypothetical, and ``tests/test_assets_graph.py`` pins it.

Consequence for this module: :func:`project_asset_to_graph` accepts an
optional ``namespace_slug``. When supplied, no ``namespaces`` read happens at
all and the projection works under ``nce_app``; when omitted it falls back to
reading the row, which succeeds for an owner-role caller. The failure is
deliberately left LOUD — the privilege error propagates rather than being
caught and the ``lives_in`` edge silently dropped, which would fabricate an
asset with no room while reporting success.

This is a PRE-EXISTING gap, not one this wave introduced.
``agreements/sla.py:do_set_sla_coverage`` (``sla.py:148``) performs the
identical ``SELECT slug FROM namespaces`` inside its own
``scoped_pg_session`` to build the same ``FL:`` label, so it carries the same
defect.

**Why nothing had caught it** — corrected after review; an earlier revision of
this paragraph got the reason wrong and named two files that do not exercise
``sla.py`` at all. The real reason is narrower and more interesting: the test
that *does* cover it, ``tests/test_agreements_sla.py``, is **not** in
``test_ci_integration_coverage.py``'s ``KNOWN_UNWIRED`` list and **is** wired
into CI's "Integration — M3 Agreements" step, calling ``do_set_sla_coverage``
at seven sites. It never fails because it drives the function through the
owner ``pg_pool`` fixture, which is a superuser and can read ``namespaces``.
A wired, passing, genuinely-run integration test can still miss a
privilege defect entirely if it never uses the role production would use.

Fixing that module, or granting ``nce_app`` SELECT on ``namespaces``, is DDL
and another module's file — out of scope here (rule 6), reported rather than
silently patched.

No ``assets_source_id`` provenance column exists — also declared
------------------------------------------------------------------
Every sibling projection module tags its rows with an engine-specific
provenance column (``economy_source_id``, ``system_design_source_id``,
``vendors_source_id``, ``agreements_source_id``, ``procurement_source_id``,
``d365_source_id``), each added by that engine's own migration (037, 038,
042, 046, ...). There is **no** ``assets_source_id`` column on ``kg_nodes``
or ``kg_edges``, and this wave's brief forbids migrations, so this module
writes none. Adding one is a later wave's DDL, not an omission to discover:
writing to a non-existent column would fail loudly, and inventing the column
here would be exactly the out-of-scope DDL rule 6 prohibits.

Idempotency is by upsert, never by check-then-write
-----------------------------------------------------
Both writes use the kg-upsert template (``ON CONFLICT ... DO UPDATE``) keyed
on the existing UNIQUE constraints — ``(label, namespace_id)`` for
``kg_nodes``, ``(subject_label, predicate, object_label, namespace_id)`` for
``kg_edges``. Re-projecting the same asset therefore touches the same rows
rather than creating new ones, and two concurrent projections cannot both
insert. A ``SELECT ... THEN INSERT`` pre-check would let them.

``confidence`` lives on ``kg_edges`` ONLY (rule 7) — ``kg_nodes`` has no such
column. Both edges are structural rather than predictive, so both carry 1.0,
matching ``system_design/graph.py:_STRUCTURAL_CONFIDENCE``.

Scoped explicitly by ``namespace_id``, never by RLS alone
-------------------------------------------------------------
Every statement below carries its own ``namespace_id`` predicate as well as
running inside the caller's ``scoped_pg_session``. The owner/superuser pool
used by integration tests BYPASSES ``FORCE ROW LEVEL SECURITY``, so an
RLS-only query passes its own test and leaks in production — this has bitten
B67, B120 and B130.

Dependency direction (uncle-bob-craft)
------------------------------------------
Only ``asyncpg`` and the shared ``nce.entity_resolution.ownership`` /
``nce.events.emit`` primitives are imported — no web/HTTP/admin framework
imports and nothing from another vertical module. Like ``economy/graph.py``,
this module takes an open ``conn`` rather than an engine, so the caller owns
the transaction and the node, both edges and the outbox event commit or roll
back together. It registers no MCP tool and mounts no REST route; wiring it
to ``do_seed_asset_from_bom`` is a later wave's.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

import asyncpg  # type: ignore[import-untyped]

from nce.entity_resolution.ownership import assert_owner
from nce.events.emit import emit_graph_write

log = logging.getLogger("nce.vertical_modules.assets.graph")

# ---------------------------------------------------------------------------
# Engine identifier and node type — must match node-ownership.json
# ---------------------------------------------------------------------------
_ASSETS_ENGINE: str = "assets"

# The ONE node type this engine owns and writes (transition:null — whole node).
_NODE_TYPE_ASSET: str = "ASSET"

# Edge predicates written here (09-assets-engine.md, the §4 edge contract).
_PRED_INSTALLED_AS: str = "installed_as"
_PRED_LIVES_IN: str = "lives_in"

# Both edges are structural (a deterministic consequence of the install), not
# a scored match — mirrors system_design/graph.py's _STRUCTURAL_CONFIDENCE.
_STRUCTURAL_CONFIDENCE: float = 1.0

# Engine-authored write, not an external-system sync — matches seed.py's own
# _DEFAULT_CHANGE_ORIGIN ('sync' is reserved for the D365 origin).
_CHANGE_ORIGIN: str = "agent"


# ---------------------------------------------------------------------------
# Label helpers — deterministic, so idempotency holds across re-runs
# ---------------------------------------------------------------------------


def _asset_label(asset_id: str | UUID) -> str:
    """Canonical kg_nodes label for an ASSET node: ``ASSET:<ASSET_ID>``.

    Keyed on migration 054's ``assets.id`` — the identity
    ``do_seed_asset_from_bom`` returns — never on the nullable ``serial``
    (see the module docstring).
    """
    return f"ASSET:{str(asset_id).upper()}"


def _bom_line_label(bom_line_id: str) -> str:
    """Label of the BOM_LINE this asset was installed from.

    ⚠ Read the module docstring's "Label conventions" section before changing
    this: ``project/convert.py`` builds a three-part
    ``BOM_LINE:<QUOTE_ID>:<LINE_REF>`` from two arguments, while
    ``assets.bom_line_id`` is one opaque token, and no mapping between the two
    exists anywhere in the repo. The token is treated as the already-joined
    identifier component, so ``"Q001:AMP01"`` reproduces ``convert.py``'s
    label exactly and a flat token does not.

    Assets never authors the BOM_LINE node — Batch 132a owns it, and nothing
    creates one yet. ``kg_edges`` has no FK to ``kg_nodes``, so naming it here
    is a forward assertion, not a dangling write.
    """
    return f"BOM_LINE:{bom_line_id.upper()}"


def _functional_location_label(namespace_slug: str, functional_location_id: str) -> str:
    """Canonical FUNCTIONAL_LOCATION label: ``FL:<NAMESPACE_SLUG>:<PATH>``.

    Mirrors System Design's convention exactly (source of truth:
    ``system_design/graph.py:_fl_label``), reproduced the same way
    ``agreements/sla.py:_functional_location_label`` already reproduces it
    from a single opaque id. FUNCTIONAL_LOCATION is owned by ``system_design``
    (``transition: null``, already in node-ownership.json) — this module
    writes the ``lives_in`` EDGE to that label and never authors the node.
    """
    return f"FL:{namespace_slug.upper()}:{functional_location_id.upper()}"


# ---------------------------------------------------------------------------
# Private helpers — one responsibility each
# ---------------------------------------------------------------------------


def _as_uuid(raw: Any, field: str) -> UUID:
    """Coerce a required UUID-ish value, rejecting blanks before any DB call."""
    if not raw:
        raise ValueError(f"'{field}' is required")
    return raw if isinstance(raw, UUID) else UUID(str(raw))


def _as_required_text(raw: Any, field: str) -> str:
    """Coerce to a required, non-empty, stripped string.

    Stripping matters for a LABEL: a whitespace-padded identifier would
    otherwise mint a second, visually identical node that no lookup finds.
    """
    text = str(raw or "").strip()
    if not text:
        raise ValueError(f"'{field}' is required")
    return text


async def _namespace_slug(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    namespace_id: UUID,
) -> str:
    """Return the namespace slug that the FUNCTIONAL_LOCATION label is built from.

    System Design's ``FL:`` labels embed the slug (see
    ``system_design/graph.py:_fl_label``), so the ``lives_in`` edge can only
    name the right room by reading it. Looked up by primary key, so this is
    inherently single-namespace.

    ⚠ Requires SELECT on ``namespaces``, which the restricted ``nce_app`` role
    does NOT have (module docstring). Callers on that role must pass
    ``namespace_slug`` to :func:`project_asset_to_graph` instead. The
    ``InsufficientPrivilegeError`` is deliberately NOT caught here: dropping
    the ``lives_in`` edge on a permission error would report a successful
    projection for an asset whose room never landed.
    """
    slug = await conn.fetchval("SELECT slug FROM namespaces WHERE id = $1::uuid", str(namespace_id))
    if slug is None:
        raise ValueError(f"namespace {namespace_id} does not exist")
    return str(slug)


async def _upsert_asset_node(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    namespace_id: UUID,
    label: str,
) -> None:
    """Upsert the ASSET kg_node and emit its transactional outbox event.

    Guarded by ``assert_owner`` (deny-by-default — Contract A). ``transition``
    is left at its ``None`` default: Assets owns the whole node, so the
    node-type-wide row is the one that must grant this.

    No ``*_source_id`` column is written — none exists for this engine (see
    the module docstring). ``confidence`` is NOT written either: ``kg_nodes``
    has no such column and never should (rule 7).
    """
    await assert_owner(conn, namespace_id, _NODE_TYPE_ASSET, _ASSETS_ENGINE)

    await conn.execute(
        """
        INSERT INTO kg_nodes (label, entity_type, namespace_id, change_origin)
        VALUES ($1, $2, $3::uuid, $4)
        ON CONFLICT (label, namespace_id) DO UPDATE
            SET entity_type   = EXCLUDED.entity_type,
                change_origin = EXCLUDED.change_origin,
                updated_at    = NOW()
        """,
        label,
        _NODE_TYPE_ASSET,
        str(namespace_id),
        _CHANGE_ORIGIN,
    )

    await emit_graph_write(
        conn,
        namespace_id=namespace_id,
        node_type=_NODE_TYPE_ASSET,
        op="upserted",
        node_id=label,
    )


async def _upsert_edge(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    namespace_id: UUID,
    subject_label: str,
    predicate: str,
    object_label: str,
) -> None:
    """Upsert a single kg_edges row. ``confidence`` lives on the edge only (rule 7).

    No ownership check: ``kg_edges`` has no FK to ``kg_nodes``, so an edge
    naming a cross-engine endpoint (BOM_LINE, FUNCTIONAL_LOCATION) is always a
    safe write — the same rule ``economy/graph.py:_upsert_edge`` and
    ``procurement/graph.py:upsert_offers_edge`` already state.
    """
    await conn.execute(
        """
        INSERT INTO kg_edges
            (subject_label, predicate, object_label, confidence, namespace_id, change_origin)
        VALUES ($1, $2, $3, $4, $5::uuid, $6)
        ON CONFLICT (subject_label, predicate, object_label, namespace_id) DO UPDATE
            SET confidence    = EXCLUDED.confidence,
                change_origin = EXCLUDED.change_origin,
                updated_at    = NOW()
        """,
        subject_label,
        predicate,
        object_label,
        _STRUCTURAL_CONFIDENCE,
        str(namespace_id),
        _CHANGE_ORIGIN,
    )


# ---------------------------------------------------------------------------
# Public: the projection
# ---------------------------------------------------------------------------


async def project_asset_to_graph(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    namespace_id: str | UUID,
    *,
    asset_id: str | UUID,
    bom_line_id: str,
    functional_location_id: str | None = None,
    namespace_slug: str | None = None,
) -> dict[str, Any]:
    """Project one seeded asset into the graph. Idempotent.

    Writes the ``ASSET`` node plus ``BOM_LINE -[installed_as]-> ASSET`` and,
    when the asset has a room, ``ASSET -[lives_in]-> FUNCTIONAL_LOCATION``.
    Changes no ``assets`` row and creates no table.

    *conn* must already carry the RLS namespace GUC (i.e. come from
    ``scoped_pg_session``) and be inside a transaction, so the node, the edges
    and the outbox event commit or roll back as one.

    ``functional_location_id`` is optional because migration 054 makes the
    column nullable: an asset seeded before its room is known has no
    ``lives_in`` edge to write yet, and a later re-projection adds it. This is
    NOT silently skipped — the returned ``lives_in`` key is ``None``, so a
    caller can tell "no room recorded" from "edge written".

    ``namespace_slug`` is read from ``namespaces`` when omitted. Pass it when
    *conn* is an ``nce_app`` connection: that role has no grant on
    ``namespaces``, so the lookup would raise
    ``asyncpg.InsufficientPrivilegeError`` (see the module docstring). It is
    ignored entirely when there is no ``functional_location_id``, since no
    ``FL:`` label is built then.

    Returns
    -------
    dict
        ``{"asset_label": str,
           "installed_as": {"subject": str, "predicate": str, "object": str},
           "lives_in": {"subject": str, "predicate": str, "object": str} | None}``

    Raises
    ------
    ValueError
        ``namespace_id``/``asset_id``/``bom_line_id`` missing or blank, or the
        namespace does not exist.
    nce.entity_resolution.ownership.OwnershipError
        No ``ASSET`` ownership row grants ``assets`` the write in this
        namespace (deny-by-default).
    """
    ns_uuid = _as_uuid(namespace_id, "namespace_id")
    asset_uuid = _as_uuid(asset_id, "asset_id")
    bom_line = _as_required_text(bom_line_id, "bom_line_id")

    asset_label = _asset_label(asset_uuid)
    bom_line_label = _bom_line_label(bom_line)

    await _upsert_asset_node(conn, ns_uuid, asset_label)
    await _upsert_edge(conn, ns_uuid, bom_line_label, _PRED_INSTALLED_AS, asset_label)

    lives_in: dict[str, str] | None = None
    location = (functional_location_id or "").strip()
    if location:
        slug = (namespace_slug or "").strip() or await _namespace_slug(conn, ns_uuid)
        fl_label = _functional_location_label(slug, location)
        await _upsert_edge(conn, ns_uuid, asset_label, _PRED_LIVES_IN, fl_label)
        lives_in = {
            "subject": asset_label,
            "predicate": _PRED_LIVES_IN,
            "object": fl_label,
        }

    return {
        "asset_label": asset_label,
        "installed_as": {
            "subject": bom_line_label,
            "predicate": _PRED_INSTALLED_AS,
            "object": asset_label,
        },
        "lives_in": lives_in,
    }


# ---------------------------------------------------------------------------
# Public: reading the projection back (READ-ONLY)
# ---------------------------------------------------------------------------


async def read_asset_projection(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    namespace_id: str | UUID,
    asset_id: str | UUID,
) -> dict[str, Any] | None:
    """READ-ONLY: the projected ASSET node and its two edges, or ``None``.

    Returns ``None`` when this namespace has no such ASSET node — never a
    fabricated placeholder, matching ``economy/graph.py:find_posted_to_po``'s
    "absence is absence" rule.

    Namespace-scoped EXPLICITLY on every statement (rule 8). That predicate is
    the only defence here: the owner/superuser pool bypasses FORCE RLS, and
    the ASSET label is identical across namespaces for the same ``asset_id``,
    so without it a read would return whichever tenant's rows the planner
    reached first.

    Returns
    -------
    dict | None
        ``{"asset_label": str, "installed_as": [str, ...],
           "lives_in": [str, ...]}`` — the edge lists hold the BOM_LINE
        subject labels and FUNCTIONAL_LOCATION object labels respectively.
    """
    ns_uuid = _as_uuid(namespace_id, "namespace_id")
    label = _asset_label(_as_uuid(asset_id, "asset_id"))

    node = await conn.fetchval(
        """
        SELECT label FROM kg_nodes
        WHERE label = $1 AND entity_type = $2 AND namespace_id = $3::uuid
        """,
        label,
        _NODE_TYPE_ASSET,
        str(ns_uuid),
    )
    if node is None:
        return None

    installed_as = await conn.fetch(
        """
        SELECT subject_label FROM kg_edges
        WHERE object_label = $1 AND predicate = $2 AND namespace_id = $3::uuid
        ORDER BY subject_label
        """,
        label,
        _PRED_INSTALLED_AS,
        str(ns_uuid),
    )
    lives_in = await conn.fetch(
        """
        SELECT object_label FROM kg_edges
        WHERE subject_label = $1 AND predicate = $2 AND namespace_id = $3::uuid
        ORDER BY object_label
        """,
        label,
        _PRED_LIVES_IN,
        str(ns_uuid),
    )

    return {
        "asset_label": str(node),
        "installed_as": [str(row["subject_label"]) for row in installed_as],
        "lives_in": [str(row["object_label"]) for row in lives_in],
    }
