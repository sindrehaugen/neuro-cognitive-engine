"""C1 entity-resolution match primitive — rank and score only.

``resolve()`` is the single fuzzy-match-and-rank function every NCE engine
calls.  It **ranks and scores** candidate nodes against existing kg_nodes
entries; it never auto-merges (Wave 6) and never decides survivorship
(Wave 7).

Design rules (uncle-bob-craft / dependency rule):
  - Domain core: no web, HTTP, admin, or framework imports.
  - One function, one job: normalize → match → rank → return.
  - Scoring is delegated entirely to pg_trgm ``similarity()`` in SQL.
  - The caller holds the ``scoped_pg_session`` context; ``resolve()``
    accepts the already-scoped ``conn`` and the explicit ``namespace_id``
    guard (belt-and-braces, per project convention).
  - Returns ``[]`` cleanly when there are no nodes of that type; never
    raises on an empty candidate or empty kg_nodes.
  - Confidence is in [0, 1] — a composite pg_trgm similarity; it is
    returned data, NOT a kg_nodes column.

Caller contract:
  ``conn`` must already have the RLS namespace GUC set (obtained via
  ``scoped_pg_session``).  The explicit ``namespace_id`` WHERE clause is
  a redundant safety guard that keeps the invariant visible at the SQL
  level — identical to the pattern in ``nce/entity_resolution/ownership.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

import asyncpg  # type: ignore[import-untyped]

from nce.entity_resolution.normalizers import normalize

# Maximum number of ranked candidates returned per call.
_TOP_N: int = 25


@dataclass(frozen=True, slots=True)
class Match:
    """A single ranked match result.

    Attributes
    ----------
    node_id:
        UUID of the matched kg_nodes row.
    score:
        Composite pg_trgm similarity in [0, 1].  Higher is more similar.
    matched_on:
        List of key names that contributed to the composite score.
    """

    node_id: UUID
    score: float
    matched_on: list[str] = field(default_factory=list)


async def resolve(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    *,
    namespace_id: str | UUID,
    candidate: dict[str, str],
    keys: list[str],
    node_type: str,
) -> list[Match]:
    """Rank and score existing nodes against a candidate using pg_trgm.

    Normalizes each key in ``candidate`` with the Wave-4 normalizer whose
    ``name`` matches the key (e.g. key ``"manufacturer"`` uses the
    ``manufacturer`` normalizer; unrecognised keys fall back to casefold).
    Scores each kg_nodes row in the namespace with a weighted-average
    pg_trgm ``similarity()`` across all requested keys and returns at most
    ``_TOP_N`` results ordered by score descending.

    Parameters
    ----------
    conn:
        An asyncpg connection that already has the RLS namespace GUC set
        (i.e. obtained via ``scoped_pg_session``).  The explicit
        ``namespace_id`` predicate in the SQL is a belt-and-braces guard.
    namespace_id:
        Active namespace UUID.
    candidate:
        Dictionary of key → raw value for the entity being resolved.
        Keys not present in ``keys`` are ignored.
    keys:
        Ordered list of key names to compare.  Each key is looked up in
        ``candidate``; missing keys are skipped (not an error).
    node_type:
        The ``entity_type`` column value to filter kg_nodes (e.g. ``'device'``).

    Returns
    -------
    list[Match]:
        Ranked matches, highest score first.  Empty list when there are no
        nodes of ``node_type`` in the namespace or when ``candidate`` has no
        usable keys.

    Notes
    -----
    - Never auto-merges and never writes to any table — read-only.
    - Never logs candidate values at INFO level (PII guard, Rule 8).
    """
    ns_uuid = UUID(str(namespace_id)) if not isinstance(namespace_id, UUID) else namespace_id

    # Build {key: normalized_value} for keys present in candidate.
    normalized_keys: dict[str, str] = {}
    for key in keys:
        raw = candidate.get(key)
        if raw is not None:
            normalized_keys[key] = normalize(raw, key)

    # No usable keys → nothing to compare; return empty list (not an error).
    if not normalized_keys:
        return []

    # Build the composite similarity SQL.
    # Each key contributes one similarity() term; the composite score is
    # the simple average across all contributing keys.  This gives a
    # balanced [0, 1] score regardless of how many keys are supplied.
    #
    # We use the kg_nodes.label column as the match target because that is
    # the single searchable text column on kg_nodes.  For a multi-key
    # candidate we concatenate the normalized values into a composite
    # search string with space separation before passing to pg_trgm, so
    # that a two-key candidate ("cisco" + "catalyst") produces the same
    # similarity as searching "cisco catalyst" against "cisco catalyst".
    #
    # Per-key similarity: similarity(normalized_key_value, label).  The
    # composite is the unweighted average so that a single high-similarity
    # key does not get diluted by unrelated keys; callers who need weighted
    # scoring should call resolve() multiple times with different key sets.
    similarity_terms = " + ".join(
        f"similarity(${i + 3}, n.label)" for i in range(len(normalized_keys))
    )
    composite_score_expr = f"({similarity_terms}) / {len(normalized_keys)}.0"

    query = f"""
        SELECT
            n.id                      AS node_id,
            {composite_score_expr}    AS score
        FROM   kg_nodes n
        WHERE  n.namespace_id = $1
          AND  n.entity_type  = $2
        ORDER BY score DESC
        LIMIT  {_TOP_N}
    """  # noqa: S608 — namespace_id filtered; no user-controlled identifiers in SQL

    params: list[object] = [ns_uuid, node_type, *normalized_keys.values()]

    rows = await conn.fetch(query, *params)

    matched_key_names = list(normalized_keys.keys())
    return [
        Match(
            node_id=UUID(str(row["node_id"])),
            score=float(row["score"]),
            matched_on=matched_key_names,
        )
        for row in rows
    ]
