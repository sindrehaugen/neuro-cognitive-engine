"""
nce/vertical_modules/economy/close_narrative.py
=================================================
Period-close narrative — C9a retrieval-grounded generation. Batch 128 /
M8.W13 (the final wave of Module 8), per
``docs/vertical_engines/08-economy-engine.md``'s "Advisor" AI feature
("close-narrative — period-close summary from the ledger — deterministic
template today, cognitive-recall enriched") and this wave's dependency on
Module 0.W21 (``nce.structural.grounded``) + Wave 6 (``graph.py``'s
PERIOD/INVOICE/``recognized_in`` primitives).

The discipline this module exists to enforce
---------------------------------------------
C9a's whole point is that **every claim in generated prose is node-linked**:
prose is CONSTRUCTED from cited graph facts, never free-generated and then
checked. Batch 21's audit caught an earlier grounded-generation helper
echoing a caller-supplied fact string back as if it had come from the graph
— this module is built so that mistake cannot recur here:

  1. :func:`_collect_period_claim_ids` is the ONLY source of claims, and it
     emits ``{"node_id": ...}`` exclusively — never a fact string. Every id
     it returns comes from a fresh ``SELECT id FROM kg_nodes`` that already
     required the node to exist in THIS namespace right now.
  2. A ``recognized_in`` edge whose subject label has no live ``kg_nodes``
     row today — a real possibility, because ``kg_edges`` carries no
     foreign key to ``kg_nodes`` (see ``graph.py``'s module docstring,
     point 3: "the boundary edge is consumed by reading, never re-derived")
     — contributes **no claim at all**. This module never fabricates a
     node id to fill the gap.
  3. :func:`do_generate_close_narrative` hands those ids straight to
     ``nce.structural.grounded.ground()`` and returns exactly what it
     produces (``prose`` built only from DB-retrieved ``kg_nodes.label``
     text, plus ``citations``/``dropped``) — this module adds no second,
     un-audited templating step on top.

Two structural layers therefore refuse an ungrounded claim independently:
this module's own collector (which cannot name a node that does not exist),
and ``ground()`` itself (which re-verifies every id against ``kg_nodes``
under the namespace GUC before it may enter prose). Neither can be skipped
by a caller — there is no parameter on :func:`do_generate_close_narrative`
that accepts a fact string.

Dependency rule (uncle-bob-craft): this module imports only ``asyncpg`` and
``nce.structural.ground`` — no web/HTTP/admin imports.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

import asyncpg  # type: ignore[import-untyped]

from nce.structural import ground

log = logging.getLogger("nce.vertical_modules.economy.close_narrative")

_PRED_RECOGNIZED_IN = "recognized_in"


def _period_label(period_id: str) -> str:
    """Canonical ``kg_nodes`` label for a PERIOD node.

    Mirrors ``graph.py``'s ``_period_label`` exactly — reimplemented locally
    rather than imported (dependencies point inward; the same
    reimplementation choice ``cascade.py``/``graph.py`` already make for
    ``ngaap.py``'s helpers, see those modules' docstrings).
    """
    return f"PERIOD:{period_id.strip().upper()}"


async def _collect_period_claim_ids(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    namespace_id: UUID,
    period_id: str,
) -> list[dict[str, str | UUID]]:
    """Namespace-scoped: ids of the PERIOD node itself plus every node that
    ``-[recognized_in]->`` it, restricted to labels that resolve to a live
    ``kg_nodes`` row right now.

    Explicitly filtered by ``namespace_id`` (rule 2 / invariant 2) — never
    relies on RLS alone. A dangling ``recognized_in`` edge (a subject label
    with no matching ``kg_nodes`` row) simply does not appear in the result
    — see the module docstring point 2.
    """
    label = _period_label(period_id)
    rows = await conn.fetch(
        """
        SELECT id FROM kg_nodes
        WHERE namespace_id = $1::uuid
          AND (
              label = $2
              OR label IN (
                  SELECT subject_label FROM kg_edges
                  WHERE object_label = $2
                    AND predicate = $3
                    AND namespace_id = $1::uuid
              )
          )
        """,
        namespace_id,
        label,
        _PRED_RECOGNIZED_IN,
    )
    return [{"node_id": str(row["id"])} for row in rows]


async def do_generate_close_narrative(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    *,
    namespace_id: str | UUID,
    period_id: str,
) -> dict[str, Any]:
    """Generate a C9a-grounded period-close narrative.

    Parameters
    ----------
    conn:
        An ``asyncpg`` connection that already has the RLS namespace GUC set
        (i.e. obtained via ``scoped_pg_session``) — same contract as
        ``nce.structural.grounded.ground``.
    namespace_id:
        The namespace whose PERIOD/INVOICE graph facts are read.
    period_id:
        The accounting period identifier (matches the ``PERIOD:{ID}``
        ``kg_nodes`` label ``graph.py``'s ``upsert_period_node`` writes).

    Returns
    -------
    dict with ``period_id`` (echoed), ``prose`` (built ONLY from
    DB-retrieved ``kg_nodes.label`` text for facts that exist today),
    ``citations`` (``{"node_id", "fact"}`` for every claim that resolved),
    and ``dropped`` (claims ``ground()`` itself could not re-verify — empty
    here in the common case, because :func:`_collect_period_claim_ids`
    never hands it an id it did not just read from ``kg_nodes``).

    Raises
    ------
    ValueError
        *period_id* is not a non-empty string.
    """
    if not isinstance(period_id, str) or not period_id.strip():
        raise ValueError("do_generate_close_narrative: period_id must be a non-empty string")

    ns_uuid = UUID(str(namespace_id)) if not isinstance(namespace_id, UUID) else namespace_id
    clean_period_id = period_id.strip()

    claims = await _collect_period_claim_ids(conn, ns_uuid, clean_period_id)
    template = f"Period {clean_period_id.upper()} close: {{facts}}"

    result = await ground(conn, namespace_id=ns_uuid, claims=claims, template=template)

    return {
        "period_id": clean_period_id,
        "prose": result["prose"],
        "citations": result["citations"],
        "dropped": result["dropped"],
    }
