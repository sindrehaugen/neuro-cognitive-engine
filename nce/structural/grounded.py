"""
C9a — retrieval-grounded-generation helper.

Design contract (§9.3 / 99-shared-core-foundation.md):
  Prose is CONSTRUCTED from cited graph facts, never free-generated then
  claim-checked.  Every emitted claim carries a source-node link.
  An unbacked claim — one whose node_id has no matching kg_nodes row — is
  DROPPED and reported; it never enters the output prose.

  CRITICAL: the fact text that enters prose is sourced exclusively from
  ``kg_nodes.label`` retrieved from the database.  The caller-supplied
  ``node_id`` is used only as a lookup key; a caller-supplied ``fact``
  string is NEVER forwarded to prose or citations.

Dependency rule (uncle-bob-craft):
  This module is domain core.  It must NOT import web/HTTP/admin modules.
  The only external dependency is asyncpg (the DB driver already used
  throughout nce/).  Templating is plain string substitution — no new dep.

Usage:
    async with scoped_pg_session(pool, namespace_id) as conn:
        result = await ground(
            conn,
            namespace_id=namespace_id,
            claims=[{"node_id": some_uuid}],
            template="Summary: {facts}",
        )
    # result["prose"]       — the assembled output (only backed claims)
    # result["citations"]   — list[{"node_id": str, "fact": str}]
    # result["dropped"]     — list[{"node_id": str}]
"""

from __future__ import annotations

from uuid import UUID

import asyncpg  # type: ignore[import-untyped]


async def ground(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    *,
    namespace_id: str | UUID,
    claims: list[dict[str, str | UUID]],
    template: str,
) -> dict[str, object]:
    """Resolve claims against kg_nodes and template the backed ones into prose.

    Parameters
    ----------
    conn:
        An asyncpg connection that already has the RLS namespace GUC set
        (i.e. obtained via ``scoped_pg_session``).
    namespace_id:
        The namespace whose graph nodes are trusted.  Used to ensure the
        node-existence check is scoped — even though the session GUC already
        filters the view, an explicit WHERE clause makes the invariant
        unambiguous and safe against future pooling changes.
    claims:
        Ordered list of ``{"node_id": str | UUID}`` dicts.
        Each claim references a specific graph node by its id; the fact text
        is sourced from ``kg_nodes.label``, never from the caller.
    template:
        A ``str.format``-style template with a single ``{facts}`` placeholder.
        The backed facts (DB-retrieved labels) are joined with ``"  "``
        (double space) and substituted for ``{facts}``.

    Returns
    -------
    dict with three keys:
        ``prose``     — the rendered output string (empty string when all
                        claims are dropped).
        ``citations`` — list of ``{"node_id": str, "fact": str}`` for every
                        claim that resolved to a real graph node; ``fact`` is
                        the ``label`` value read from ``kg_nodes``.
        ``dropped``   — list of ``{"node_id": str}`` for every claim that
                        had no matching ``kg_nodes`` row (excluded from
                        prose; reported here so callers can audit the gap).
    """
    ns_uuid = UUID(str(namespace_id)) if not isinstance(namespace_id, UUID) else namespace_id

    citations: list[dict[str, str]] = []
    dropped: list[dict[str, str]] = []

    for claim in claims:
        raw_id = claim["node_id"]

        try:
            node_uuid = UUID(str(raw_id))
        except (ValueError, AttributeError):
            dropped.append({"node_id": str(raw_id)})
            continue

        # Source the fact text directly from kg_nodes.label.
        # The WHERE namespace_id clause is explicit so the invariant is
        # visible at the call site — not merely an implicit RLS side effect.
        # The caller-supplied claim carries no "fact" string; prose is
        # constructed exclusively from the DB-retrieved label.
        row = await conn.fetchrow(
            "SELECT id, label FROM kg_nodes WHERE id = $1 AND namespace_id = $2",
            node_uuid,
            ns_uuid,
        )

        if row is None:
            dropped.append({"node_id": str(node_uuid)})
        else:
            # fact is sourced from the database — never from caller input.
            citations.append({"node_id": str(node_uuid), "fact": row["label"]})

    # Construct prose ONLY from backed (cited) DB-sourced facts.
    if citations:
        assembled_facts = "  ".join(c["fact"] for c in citations)
        prose = template.format(facts=assembled_facts)
    else:
        prose = ""

    return {
        "prose": prose,
        "citations": citations,
        "dropped": dropped,
    }
