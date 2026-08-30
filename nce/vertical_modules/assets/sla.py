"""
nce/vertical_modules/assets/sla.py
====================================
Assets' per-ROOM SLA coverage link — Module 9, Wave 7 (Batch 147, ``sla-attach``).

``SLA`` is a **4-way, per-aspect** shared concept (``00-ENGINES-ROADMAP.md`` §9.1,
the ``SLA (4-way — per aspect)`` row): **Agreements (3)** owns the driftsavtale
*terms*; **Assets (9)** owns the per-``FUNCTIONAL_LOCATION`` (per-ROOM) *coverage
link*; **Economy (8)** owns the *MRR/revenue*; **Support (10)** owns the *running
clock + breach state*. This module implements exactly Assets' one aspect —
:func:`do_attach_sla` — and nothing else of the four:

  * it **reads** the already-authored SLA terms from Agreements
    (``agreements/sla.py:get_sla_coverage``, a read-only, namespace-scoped query —
    the A2A seam), and
  * it **writes** the one edge Assets owns: ``FUNCTIONAL_LOCATION -[covered_by]->
    Agreement`` — the per-ROOM coverage link.

It never authors an ``AGREEMENT_TERM`` node, never calls
``agreements/sla.py:do_set_sla_coverage`` (the terms WRITER), and never touches a
clock/breach table (Support's aspect — no such table exists in this file, and
none is imported).

Why there is no ``SLA`` **node** — declared, not silent
-----------------------------------------------------------
TWO earlier sketches imply a standalone ``SLA`` kg_node, not one:

  1. ``docs/vertical_engines/09-assets-engine.md`` ("Graph contribution")
     sketches ``ASSET -[covered_by]-> SLA -[for]-> FUNCTIONAL_LOCATION``.
  2. ``docs/vertical_engines/00-ENGINES-ROADMAP.md:103`` (the "Key cross-engine
     edges" worked example) sketches a DIFFERENTLY-SHAPED chain: ``BOM_LINE
     -[installed_as]-> ASSET -[lives_in]-> ROOM -[covered_by]-> SLA``.

Both name an intermediate ``SLA`` node; both use ``covered_by`` as the edge
INTO it (independent confirmation the predicate name is right even though the
node they attach it to is not built). Both are superseded by the same later
text: (09)'s own "Review round-2 hardening" section (dated after the roadmap's
4-way split, and the section the doc says "govern[s] the build") states
*"``SLA`` is a 4-way co-owned node (roadmap §9.1) — Assets owns only the
per-ROOM coverage link,"* and the roadmap's own §9.1 ``SLA (4-way — per aspect)``
table row makes the same call for (00). Two independent facts confirm the node
was never meant to be built by this wave: (1) ``nce/config_data/node-ownership.json``
has no ``SLA`` row for ANY engine — authoring one would need a new ownership row,
which is config/DDL this wave's ``Files:`` list does not include; (2) no code
anywhere in this repository writes an ``entity_type = 'SLA'`` kg_node. This
module therefore collapses BOTH two/three-hop sketches into the one edge the
hardening note actually grants Assets: the room links directly to the
``Agreement`` that carries the terms, using the exact ``covered_by`` predicate
both original sketches already named.

Why the edge write needs no ownership row at all — the actual rule, not a
pattern-match
------------------------------------------------------------------------------
The absence of an ``SLA`` ownership row would matter only if this module
needed to WRITE an ``SLA`` (or any) kg_node — it does not, and the reason a
plain edge write is safe is a general rule, not a coincidence for this case:
``assert_owner`` (``nce/entity_resolution/ownership.py``) takes a ``node_type``
and gates ``kg_nodes`` writes only; ``kg_edges`` has no foreign key to
``kg_nodes`` at all, so an edge naming ANY endpoint — owned, unowned, or not
yet created — is outside Contract-A's ownership registry entirely.
``assets/graph.py:346-351``'s own ``_upsert_edge`` docstring states this
explicitly ("No ownership check: kg_edges has no FK to kg_nodes, so an edge
naming a cross-engine endpoint ... is always a safe write"), and
``economy/graph.py`` and ``procurement/graph.py`` write their own cross-engine
edges under the identical rule. This module's ``FL -[covered_by]-> Agreement``
write follows that same, already-ratified rule — it is not a new precedent and
not merely modeled on one.

Direction is the deliberate mirror of Agreements' own edge
---------------------------------------------------------------
``agreements/sla.py:do_set_sla_coverage`` asserts ``Agreement -[covers]->
FUNCTIONAL_LOCATION`` (the agreement's own bookkeeping: "which locations does
this driftsavtale name"). This module asserts the inverse, ``FUNCTIONAL_LOCATION
-[covered_by]-> Agreement``, under a DIFFERENT predicate — so the two engines'
rows never collide on ``kg_edges``' ``(subject_label, predicate, object_label,
namespace_id)`` unique constraint — and answers the different, room-first
question this engine exists to answer: "what SLA covers *this* room."

``agreement_id`` — the concrete param, not the doc's placeholder ``contract``
----------------------------------------------------------------------------------
The engine spec's ``Core functions`` line lists ``do_attach_sla(engine, params)
-> dict`` as taking ``{functional_location_id, contract}``. ``contract`` is not a
symbol anything in this repository defines. The one concrete, resolvable
identifier this module needs to call ``get_sla_coverage`` is the
``agreement_id`` Agreements already keys everything on (``Agreement:<id>``,
``AgreementTerm:<id>:sla_<key>``) — so that is the parameter name here,
consistent with the one module it must call.

Fails loud when Agreements has not authored terms yet
-----------------------------------------------------------
``do_attach_sla`` raises ``ValueError`` when ``get_sla_coverage`` reports no
``sla_terms`` for the given ``agreement_id``. This is a deliberate, in-scope
guard, not scope creep: a "coverage link" to an agreement with no recorded terms
would silently claim a room is SLA-covered when Agreements has authored nothing
to cover it with — the exact kind of premature/fabricated coverage the four-way
split exists to prevent.

Idempotency, confidence, namespace-scoping — copied verbatim from siblings
-------------------------------------------------------------------------------
Same ``ON CONFLICT ... DO UPDATE`` kg-upsert template as
``assets/graph.py:_upsert_edge`` and ``agreements/graph.py:upsert_agreement_edge``,
keyed on the same ``kg_edges`` unique constraint. ``confidence`` is the
structural ``1.0`` (a deterministic install-time link, not a scored match) and
lives on the edge only — ``kg_nodes`` has no such column and this module writes
no node. Every statement carries its own ``namespace_id`` predicate/parameter
(never RLS alone — the owner/superuser pool used by tests bypasses FORCE RLS).

Dependency direction (uncle-bob-craft)
------------------------------------------
Only ``asyncpg``, ``nce.db_utils.scoped_pg_session``, ``nce.mcp_args``, and the
one Agreements READ function are imported — no web/admin/HTTP framework, no
Support/Economy module, and no Agreements WRITE function. This module registers
no MCP tool and mounts no REST route (the ``Files:`` list for this wave is just
this module + its test); wiring ``do_attach_sla`` into ``tool_registry.py`` /
``admin_handlers/assets.py`` is a later wave's, matching the precedent already
set by ``assets/graph.py``'s own docstring for ``project_asset_to_graph``.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

import asyncpg  # type: ignore[import-untyped]

from nce.db_utils import scoped_pg_session
from nce.mcp_args import require_namespace_id
from nce.vertical_modules.agreements.sla import get_sla_coverage

log = logging.getLogger("nce.vertical_modules.assets.sla")

# The ONE edge predicate this module writes — Assets' aspect of the 4-way SLA
# (§9.1). Named to mirror, and never collide with, Agreements' own ``covers``
# edge (see module docstring, "Direction is the deliberate mirror").
_PRED_COVERED_BY: str = "covered_by"

# Structural (deterministic install-time link), not a scored match — matches
# assets/graph.py:_STRUCTURAL_CONFIDENCE and agreements/sla.py's term/edge writes.
_STRUCTURAL_CONFIDENCE: float = 1.0

# Engine-authored write, not an external-system sync — matches graph.py/seed.py's
# own _CHANGE_ORIGIN ('sync' is reserved for the D365 origin).
_CHANGE_ORIGIN: str = "agent"


# ---------------------------------------------------------------------------
# Label helpers — deterministic, copied from the sibling modules that already
# own these conventions (never invented here).
# ---------------------------------------------------------------------------


def _functional_location_label(namespace_slug: str, functional_location_id: str) -> str:
    """Canonical FUNCTIONAL_LOCATION label: ``FL:<NAMESPACE_SLUG>:<PATH>``.

    Produces a label EQUIVALENT to System Design's convention (source of
    truth: ``system_design/graph.py:_fl_label``) — not the same code path.
    ``_fl_label`` upper-cases each ``path_part`` SEPARATELY, then joins with
    ``:``; this function upper-cases the ALREADY-JOINED
    ``functional_location_id`` string once. The two are equivalent only
    because ``str.upper()`` does not touch the ``:`` separator, so a
    colon-joined ASCII id upper-cases identically either order — the same
    equivalence ``assets/graph.py:_functional_location_label`` and
    ``agreements/sla.py:_functional_location_label`` already rely on. A path
    component containing a casefolding-sensitive non-ASCII character could in
    principle diverge; none of the three modules guard against that today.
    FUNCTIONAL_LOCATION is owned by ``system_design`` — this module writes only
    the ``covered_by`` EDGE against this label and never authors the node.
    """
    return f"FL:{namespace_slug.upper()}:{functional_location_id.upper()}"


def _agreement_label(agreement_id: str | UUID) -> str:
    """Canonical Agreement label: ``Agreement:<id>`` — no case transform.

    Copied verbatim from ``agreements/sla.py:do_set_sla_coverage``
    (``agreement_label = f"Agreement:{agreement_uuid}"``) so the two engines'
    labels for the same agreement are byte-identical.
    """
    return f"Agreement:{agreement_id}"


# ---------------------------------------------------------------------------
# Private helpers — one responsibility each (uncle-bob-craft)
# ---------------------------------------------------------------------------


def _as_uuid(raw: Any, field: str) -> UUID:
    """Coerce a required UUID-ish value, rejecting blanks before any call."""
    if not raw:
        raise ValueError(f"'{field}' is required")
    return raw if isinstance(raw, UUID) else UUID(str(raw))


def _as_required_text(raw: Any, field: str) -> str:
    """Coerce to a required, non-empty, stripped string.

    Stripping matters for a LABEL: a whitespace-padded identifier would
    otherwise mint a second, visually identical edge endpoint that no lookup
    finds — same rationale as ``assets/graph.py:_as_required_text``.
    """
    text = str(raw or "").strip()
    if not text:
        raise ValueError(f"'{field}' is required")
    return text


async def _namespace_slug(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    namespace_id: UUID,
) -> str:
    """Return the namespace slug the FUNCTIONAL_LOCATION label is built from.

    Looked up by primary key — inherently single-namespace. Callers on the
    restricted ``nce_app`` role (which has no grant on ``namespaces`` — see
    ``assets/graph.py``'s module docstring, "The FL label needs the namespace
    SLUG") must pass ``namespace_slug`` to :func:`do_attach_sla` instead; this
    fallback is for owner-role callers only and is exercised by integration
    tests against a real database, not by this module's pure-unit tests.
    """
    slug = await conn.fetchval("SELECT slug FROM namespaces WHERE id = $1::uuid", str(namespace_id))
    if slug is None:
        raise ValueError(f"namespace {namespace_id} does not exist")
    return str(slug)


# ---------------------------------------------------------------------------
# Public: the core dual-surface function (do_<action>(engine, params) -> dict)
# ---------------------------------------------------------------------------


async def do_attach_sla(engine: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Attach the per-ROOM SLA coverage link, reading terms from Agreements.

    Writes exactly one thing — ``FUNCTIONAL_LOCATION -[covered_by]-> Agreement``
    — and authors nothing else. Does NOT create/update an ``AGREEMENT_TERM``
    node, does NOT call ``agreements/sla.py:do_set_sla_coverage`` (the terms
    writer), and does NOT touch any clock/breach table (Support's aspect).

    Parameters
    ----------
    engine:
        NCEEngine instance (only ``engine.pg_pool`` is used; may be a test stub).
    params:
        ``{
            "namespace_id":           str | UUID,   # required
            "agreement_id":           str | UUID,   # required
            "functional_location_id": str,          # required
            "namespace_slug":         str,           # optional — see below
        }``

        ``namespace_slug``, when supplied, skips the ``namespaces`` lookup
        entirely (required for an ``nce_app``-role caller — see
        ``assets/graph.py``'s module docstring for why that role cannot read
        ``namespaces``); when omitted it falls back to reading the row, which
        succeeds for an owner-role caller.

    Returns
    -------
    dict
        ``{
            "status": "ok",
            "agreement_id":           str,
            "functional_location_id": str,
            "covered_by_edge": {"subject": str, "predicate": "covered_by", "object": str},
            "sla_terms_read": [str, ...],   # the AgreementTerm:<id>:sla_<key> labels read
        }``

    Raises
    ------
    ValueError
        On a missing/invalid ``namespace_id`` / ``agreement_id`` /
        ``functional_location_id``, or when Agreements has no SLA terms on
        record for ``agreement_id`` (see module docstring, "Fails loud").
    """
    namespace_id = require_namespace_id(params)
    ns_uuid = UUID(str(namespace_id))

    agreement_uuid = _as_uuid(params.get("agreement_id"), "agreement_id")
    functional_location_id = _as_required_text(
        params.get("functional_location_id"), "functional_location_id"
    )
    agreement_label = _agreement_label(agreement_uuid)

    # 1. READ (the A2A seam) — Agreements owns the terms; this call is
    #    read-only (agreements/sla.py:get_sla_coverage issues only SELECTs).
    coverage = await get_sla_coverage(engine.pg_pool, ns_uuid, agreement_uuid)
    sla_terms: list[str] = list(coverage.get("sla_terms") or [])
    if not sla_terms:
        raise ValueError(
            f"agreement {agreement_uuid} has no SLA terms on record in Agreements; "
            "do_attach_sla only links coverage for terms Agreements has already authored"
        )

    # 2. WRITE — the ONE coverage-link edge Assets owns (§9.1 4-way SLA).
    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        namespace_slug = str(params.get("namespace_slug") or "").strip() or await _namespace_slug(
            conn, ns_uuid
        )
        fl_label = _functional_location_label(namespace_slug, functional_location_id)

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
            fl_label,
            _PRED_COVERED_BY,
            agreement_label,
            _STRUCTURAL_CONFIDENCE,
            str(ns_uuid),
            _CHANGE_ORIGIN,
        )

    log.info(
        "do_attach_sla: ns=%s fl=%s covered_by=%s terms_read=%d",
        ns_uuid,
        fl_label,
        agreement_label,
        len(sla_terms),
    )

    return {
        "status": "ok",
        "agreement_id": str(agreement_uuid),
        "functional_location_id": functional_location_id,
        "covered_by_edge": {
            "subject": fl_label,
            "predicate": _PRED_COVERED_BY,
            "object": agreement_label,
        },
        "sla_terms_read": sla_terms,
    }
