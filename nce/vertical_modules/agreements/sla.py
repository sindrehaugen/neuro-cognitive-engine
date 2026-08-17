"""
nce/vertical_modules/agreements/sla.py
========================================
SLA coverage for the Agreements vertical module — M3.W10 "sla-coverage-edge".

Agreements owns the SLA **terms** (the driftsavtale clauses) and asserts the
single ``covers FUNCTIONAL_LOCATION`` coverage edge.  It deliberately owns
NOTHING else of the four-way SLA boundary (§9.1):

  - the SLA **clock** (response/resolution timers) is owned by Support;
  - the per-room / per-asset coverage link is owned by Assets;
  - the ``FUNCTIONAL_LOCATION`` node itself is owned by System Design.

So this wave writes exactly two things and no more:

  1. one ``AGREEMENT_TERM`` node per SLA term (``sla_<key>``), via
     ``graph.upsert_agreement_term_node`` — each term node is an identity
     anchor; the term VALUE is not persisted on ``kg_nodes`` (the established
     engine pattern — see ``graph.py`` / ``kickback.py``), it lives in the
     source record;
  2. one ``Agreement:<id> -covers-> FL:<...>`` edge, via
     ``graph.upsert_agreement_edge``.

Coverage is a label-based assertion
-----------------------------------
``kg_edges`` is label-keyed with NO foreign key to ``kg_nodes``.  The coverage
edge is therefore written against the deterministic FUNCTIONAL_LOCATION label
(System Design's convention — see ``system_design/graph.py:_fl_label``:
``FL:<NAMESPACE_SLUG>:<PATH>`` upper-cased) **even when the FL node does not yet
exist**.  That is correct and intended: Agreements asserts coverage; Assets /
System Design own the FL node and will create it independently.  When they do,
the labels line up and the edge resolves.

Design invariants (uncle-bob-craft)
-------------------------------------
- Dependencies point inward: the graph writes go through ``graph.py``'s
  ownership-guarded upserts; nothing is imported from admin_handlers.
- Explicit ``namespace_id = $N::uuid`` predicate on every SQL read (no
  RLS-only reliance; owner-pool test roles can bypass FORCE RLS).
- Required params fail loud with ``ValueError`` before any DB access.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

import asyncpg  # type: ignore[import-untyped]

from nce.db_utils import scoped_pg_session
from nce.mcp_args import require_namespace_id
from nce.vertical_modules.agreements.graph import (
    upsert_agreement_edge,
    upsert_agreement_term_node,
)

log = logging.getLogger("nce.vertical_modules.agreements.sla")

# Coverage-edge predicate — the ONE edge Agreements writes for an SLA.
_PRED_COVERS = "covers"

# SLA term-type prefix.  Node labels are ``AgreementTerm:<id>:sla_<key>``.
_SLA_TERM_PREFIX = "sla_"


def _functional_location_label(namespace_slug: str, functional_location_id: str) -> str:
    """Build the FUNCTIONAL_LOCATION label for the coverage edge.

    Mirrors System Design's convention exactly (source of truth:
    ``nce/vertical_modules/system_design/graph.py:_fl_label``) —
    ``FL:<NAMESPACE_SLUG>:<PATH>`` with every component upper-cased so the same
    location always maps to the same label regardless of input casing.  The
    ``functional_location_id`` is treated as the site-path component(s); a
    colon-delimited id upper-cases identically to System Design's per-part
    upper-casing, so multi-part ids line up too.

    FUNCTIONAL_LOCATION nodes are owned by System Design — Agreements only
    references the label (``kg_edges`` is label-based, no FK), so the coverage
    edge lands even before the FL node exists.
    """
    return f"FL:{namespace_slug.upper()}:{functional_location_id.upper()}"


async def do_set_sla_coverage(engine: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Write the SLA terms + the single ``covers`` coverage edge for an agreement.

    Parameters
    ----------
    engine:
        NCEEngine instance (only ``engine.pg_pool`` is used; may be a test stub).
    params:
        ``{
            "namespace_id":           str | UUID,   # required
            "agreement_id":           str | UUID,   # required
            "functional_location_id": str,          # required
            "sla_terms":              dict,         # required, e.g.
                                                    #   {"responseHours": 4,
                                                    #    "coverageWindow": "24x7",
                                                    #    "resolutionHours": 24}
        }``

    Returns
    -------
    dict
        ``{
            "status": "ok",
            "agreement_id":           str,
            "functional_location_id": str,
            "covers_edge": {"subject": str, "predicate": "covers", "object": str},
            "sla_terms_written": [str, ...],   # the sla_<key> term types written
        }``

    Raises
    ------
    ValueError
        On a missing/invalid ``namespace_id`` / ``agreement_id`` /
        ``functional_location_id`` / ``sla_terms``.
    """
    namespace_id = require_namespace_id(params)
    ns_uuid = uuid.UUID(str(namespace_id))

    agreement_id_raw = params.get("agreement_id")
    if not agreement_id_raw:
        raise ValueError("agreement_id is required")
    functional_location_id = params.get("functional_location_id")
    if not functional_location_id:
        raise ValueError("functional_location_id is required")
    sla_terms = params.get("sla_terms")
    if not sla_terms:
        raise ValueError("sla_terms is required")
    if not isinstance(sla_terms, dict):
        raise ValueError("sla_terms must be a dict of term-name -> value")

    agreement_uuid = uuid.UUID(str(agreement_id_raw))
    agreement_label = f"Agreement:{agreement_uuid}"
    source_id = str(agreement_uuid)
    fl_id_str = str(functional_location_id)

    sla_terms_written: list[str] = []

    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        # Resolve the namespace slug so the coverage edge points at the exact
        # FUNCTIONAL_LOCATION label System Design authors (see extract.py for
        # the same scoped read of the namespaces row).
        slug_row = await conn.fetchrow(
            "SELECT slug FROM namespaces WHERE id = $1::uuid",
            str(ns_uuid),
        )
        if slug_row is None or not slug_row["slug"]:
            raise ValueError(f"namespace {ns_uuid} not found or has no slug")
        fl_label = _functional_location_label(str(slug_row["slug"]), fl_id_str)

        # 1. SLA term nodes (identity anchors — value not stored on kg_nodes).
        for key, value in sla_terms.items():
            term_type = f"{_SLA_TERM_PREFIX}{key}"
            value_str = json.dumps(value) if isinstance(value, (list, dict)) else str(value)
            await upsert_agreement_term_node(
                conn,
                ns_uuid,
                agreement_id=agreement_uuid,
                term_type=term_type,
                value=value_str,
                confidence=1.0,
                agreements_source_id=source_id,
            )
            sla_terms_written.append(term_type)

        # 2. The ONE coverage edge (Agreements asserts coverage; System Design
        #    owns the FL node — no FL node is created here).
        await upsert_agreement_edge(
            conn,
            ns_uuid,
            subject_label=agreement_label,
            predicate=_PRED_COVERS,
            object_label=fl_label,
            confidence=1.0,
            agreements_source_id=source_id,
        )

    log.info(
        "do_set_sla_coverage: agreement=%s ns=%s covers=%s terms=%d",
        agreement_uuid,
        ns_uuid,
        fl_label,
        len(sla_terms_written),
    )

    return {
        "status": "ok",
        "agreement_id": source_id,
        "functional_location_id": fl_id_str,
        "covers_edge": {
            "subject": agreement_label,
            "predicate": _PRED_COVERS,
            "object": fl_label,
        },
        "sla_terms_written": sla_terms_written,
    }


async def get_sla_coverage(
    pool: asyncpg.Pool,
    namespace_id: str | uuid.UUID,
    agreement_id: str | uuid.UUID,
    limit: int = 100,
) -> dict[str, Any]:
    """Read-only, namespace-scoped view of an agreement's SLA coverage.

    Returns the ``covers`` edge(s) for the agreement plus the ``sla_*`` term
    node labels — proving coverage is queryable after
    :func:`do_set_sla_coverage`.

    Returns
    -------
    dict
        ``{
            "agreement_id": str,
            "covers":    [{"object": str, "confidence": float}, ...],
            "sla_terms": [str, ...],   # AgreementTerm:<id>:sla_<key> labels
        }``
    """
    ns_uuid = uuid.UUID(str(namespace_id))
    agreement_uuid = uuid.UUID(str(agreement_id))
    agreement_label = f"Agreement:{agreement_uuid}"
    # LIKE pattern for the sla_ term nodes.  The underscore after "sla" is a
    # literal, so it is escaped (``\_``) to keep it from acting as a single-char
    # LIKE wildcard; the UUID prefix has no special LIKE characters.
    term_pattern = f"AgreementTerm:{agreement_uuid}:{_SLA_TERM_PREFIX[:-1]}\\_%"

    async with scoped_pg_session(pool, ns_uuid) as conn:
        edge_rows = await conn.fetch(
            """
            SELECT object_label, confidence
            FROM   kg_edges
            WHERE  subject_label = $1
              AND  predicate = $2
              AND  namespace_id = $3::uuid
            ORDER BY object_label
            LIMIT  $4
            """,
            agreement_label,
            _PRED_COVERS,
            str(ns_uuid),
            limit,
        )
        term_rows = await conn.fetch(
            """
            SELECT label
            FROM   kg_nodes
            WHERE  entity_type = 'AGREEMENT_TERM'
              AND  namespace_id = $1::uuid
              AND  label LIKE $2 ESCAPE '\\'
            ORDER BY label
            LIMIT  $3
            """,
            str(ns_uuid),
            term_pattern,
            limit,
        )

    return {
        "agreement_id": str(agreement_uuid),
        "covers": [
            {"object": r["object_label"], "confidence": float(r["confidence"])} for r in edge_rows
        ],
        "sla_terms": [r["label"] for r in term_rows],
    }
