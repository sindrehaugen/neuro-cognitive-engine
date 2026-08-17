"""C1 write-path ownership guard.

Consults ``node_ownership_registry`` to enforce the single-writer invariant:
a cross-engine write to a node type NOT owned by the writing engine is refused.

Design rules (uncle-bob-craft / dependency rule):
  - Domain core: no web, HTTP, admin, or framework imports.
  - One function, one job: consult the registry, raise or pass.
  - ``OwnershipError`` is defined locally and kept thin.
  - Deny by default: if no registry row exists the write is refused.
  - Per-transition awareness: a transition-specific row takes precedence over
    the node-type-wide row.

Caller contract:
  ``conn`` must already have the RLS namespace GUC set (i.e. obtained via
  ``scoped_pg_session``).  The explicit ``namespace_id`` WHERE clause is a
  redundant safety guard that keeps the invariant visible at the SQL level.
"""

from __future__ import annotations

from uuid import UUID

import asyncpg  # type: ignore[import-untyped]


class OwnershipError(Exception):
    """Raised when an engine attempts to write a node type it does not own.

    Attributes
    ----------
    node_type:
        The contested node type.
    writer_engine:
        The engine that attempted the write.
    owner_engine:
        The engine registered as sole writer in the registry, or ``None``
        when no registry row exists (deny-by-default applies).
    transition:
        The transition that was checked, or ``None`` for a node-type-wide check.
    """

    def __init__(
        self,
        *,
        node_type: str,
        writer_engine: str,
        owner_engine: str | None,
        transition: str | None,
    ) -> None:
        self.node_type = node_type
        self.writer_engine = writer_engine
        self.owner_engine = owner_engine
        self.transition = transition
        if owner_engine is None:
            detail = "no ownership row registered (deny-by-default)"
        else:
            detail = f"registered owner is '{owner_engine}'"
        scope = f" (transition='{transition}')" if transition is not None else ""
        super().__init__(
            f"Engine '{writer_engine}' is not permitted to write node_type "
            f"'{node_type}'{scope}: {detail}."
        )


async def assert_owner(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    namespace_id: str | UUID,
    node_type: str,
    writer_engine: str,
    transition: str | None = None,
) -> None:
    """Assert that ``writer_engine`` owns ``node_type`` in the registry.

    Raises ``OwnershipError`` when:
    - the registry contains a row for this (namespace, node_type, transition)
      or (namespace, node_type) whose ``owner_engine`` differs from
      ``writer_engine``, OR
    - no registry row exists at all (deny-by-default).

    Per-transition rule: when ``transition`` is given the lookup first tries
    the transition-specific row; if none exists it falls back to the
    node-type-wide row (``transition IS NULL``).  Deny-by-default applies
    when neither row is found.

    Parameters
    ----------
    conn:
        An asyncpg connection that already has the RLS namespace GUC set
        (i.e. obtained via ``scoped_pg_session``).
    namespace_id:
        Active namespace UUID.  Passed as an explicit WHERE predicate in
        addition to RLS — keeps the invariant visible at the SQL level.
    node_type:
        The node type being written (e.g. ``'device'``).
    writer_engine:
        Identifier of the engine requesting the write.
    transition:
        Optional transition label (e.g. ``'create'``, ``'update'``).
        When supplied the guard is per-transition aware.

    Raises
    ------
    OwnershipError:
        The write is not permitted.
    """
    ns_uuid = UUID(str(namespace_id)) if not isinstance(namespace_id, UUID) else namespace_id

    # Prefer the transition-specific row when transition is given; fall back to
    # the node-type-wide row.  A single query returns at most two candidates
    # ordered so the transition-specific row sorts first (NULL last).
    owner_engine: str | None = await _lookup_owner(conn, ns_uuid, node_type, transition)

    if owner_engine is None:
        # No row registered — deny by default.
        raise OwnershipError(
            node_type=node_type,
            writer_engine=writer_engine,
            owner_engine=None,
            transition=transition,
        )

    if owner_engine != writer_engine:
        raise OwnershipError(
            node_type=node_type,
            writer_engine=writer_engine,
            owner_engine=owner_engine,
            transition=transition,
        )


async def _lookup_owner(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    namespace_id: UUID,
    node_type: str,
    transition: str | None,
) -> str | None:
    """Return the most-specific registered ``owner_engine`` or ``None``.

    Most-specific means: transition-specific row beats node-type-wide row.
    Both share the same ``namespace_id`` + ``node_type`` scope.
    The caller's RLS GUC already filters by namespace; the explicit
    ``namespace_id`` predicate is a belt-and-braces guard.
    """
    if transition is not None:
        # Fetch transition-specific row first, then fall back to the
        # node-type-wide row.  ORDER BY NULLS LAST puts the transition row
        # first; LIMIT 1 returns only the most specific match.
        row = await conn.fetchrow(
            """
            SELECT owner_engine
            FROM   node_ownership_registry
            WHERE  namespace_id = $1
              AND  node_type    = $2
              AND  (transition = $3 OR transition IS NULL)
            ORDER BY (transition IS NULL)
            LIMIT 1
            """,
            namespace_id,
            node_type,
            transition,
        )
    else:
        # No transition supplied — look for the node-type-wide row only.
        row = await conn.fetchrow(
            """
            SELECT owner_engine
            FROM   node_ownership_registry
            WHERE  namespace_id = $1
              AND  node_type    = $2
              AND  transition IS NULL
            LIMIT 1
            """,
            namespace_id,
            node_type,
        )

    return row["owner_engine"] if row is not None else None
