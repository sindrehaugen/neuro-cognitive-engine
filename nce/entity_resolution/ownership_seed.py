"""Contract A ownership seed (§9.1).

Reads the config-as-IP ownership map and idempotently inserts per-namespace
rows into ``node_ownership_registry`` so the deny-by-default guard in
``assert_owner`` passes for registered node types.

Design rules (uncle-bob-craft / dependency rule):
  - Domain core: no web, HTTP, admin, or framework imports.
  - One public function, one job: seed the registry for a namespace.
  - JSON map is loaded once at module import and cached (avoids repeated I/O).
  - Idempotency is enforced at the SQL level (NOT EXISTS guard); calling this
    function twice for the same namespace is always safe.

Caller contract:
  ``conn`` must already have the RLS namespace GUC set (i.e. the caller has
  called ``set_namespace_context`` before invoking this function).
  ``namespace_id`` is still passed explicitly as a belt-and-braces safety
  guard that keeps the invariant visible at the SQL level.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any
from uuid import UUID

import asyncpg  # type: ignore[import-untyped]

log = logging.getLogger("nce.entity_resolution.ownership_seed")

# ---------------------------------------------------------------------------
# Module-level cache — loaded once on first import, never reloaded at runtime.
# ---------------------------------------------------------------------------

_OWNERSHIP_MAP_PATH: Path = (
    Path(__file__).resolve().parent.parent / "config_data" / "node-ownership.json"
)


def _load_ownership_map() -> list[dict[str, Any]]:
    """Parse the ownership JSON and return the ``ownership`` list."""
    raw = _OWNERSHIP_MAP_PATH.read_text(encoding="utf-8")
    data: dict[str, Any] = json.loads(raw)
    entries: list[dict[str, Any]] = data.get("ownership", [])
    return entries


_OWNERSHIP_ENTRIES: list[dict[str, Any]] = _load_ownership_map()

# ---------------------------------------------------------------------------
# SQL — idempotent INSERT via NOT EXISTS (handles NULL transition correctly).
# ---------------------------------------------------------------------------

_INSERT_SQL = """
INSERT INTO node_ownership_registry (namespace_id, node_type, transition, owner_engine)
SELECT $1, $2, $3, $4
WHERE NOT EXISTS (
    SELECT 1 FROM node_ownership_registry
    WHERE namespace_id = $1
      AND node_type    = $2
      AND transition IS NOT DISTINCT FROM $3
)
"""

# Bulk counterpart: one set-based statement covering every namespace.
_BULK_INSERT_SQL = """
INSERT INTO node_ownership_registry (namespace_id, node_type, transition, owner_engine)
SELECT n.id, v.node_type, v.transition, v.owner_engine
FROM namespaces n
CROSS JOIN UNNEST($1::text[], $2::text[], $3::text[])
     AS v(node_type, transition, owner_engine)
WHERE NOT EXISTS (
    SELECT 1 FROM node_ownership_registry r
    WHERE r.namespace_id = n.id
      AND r.node_type    = v.node_type
      AND r.transition IS NOT DISTINCT FROM v.transition
)
"""


async def seed_node_ownership_registry(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    namespace_id: str | UUID,
) -> int:
    """Idempotently seed ``node_ownership_registry`` for *namespace_id*.

    Iterates every entry in ``nce/config_data/node-ownership.json`` and
    inserts a row for this namespace when no matching row already exists.
    The NOT EXISTS guard uses ``IS NOT DISTINCT FROM`` to handle NULL
    transition values correctly.

    Parameters
    ----------
    conn:
        An asyncpg connection that already has the RLS namespace GUC set
        (caller must call ``set_namespace_context`` beforehand).
    namespace_id:
        The namespace UUID to seed.  Passed explicitly in every SQL
        predicate as a belt-and-braces redundant safety guard.

    Returns
    -------
    int
        The number of rows actually inserted (0 when already seeded).
    """
    ns_uuid = UUID(str(namespace_id)) if not isinstance(namespace_id, UUID) else namespace_id
    inserted = 0
    for entry in _OWNERSHIP_ENTRIES:
        node_type: str = entry["node_type"]
        owner_engine: str = entry["owner_engine"]
        transition: str | None = entry.get("transition")
        status = await conn.execute(
            _INSERT_SQL,
            ns_uuid,
            node_type,
            transition,
            owner_engine,
        )
        # asyncpg returns "INSERT 0 N" — parse the row count.
        try:
            count = int(status.split()[-1])
        except (AttributeError, ValueError, IndexError):
            count = 0
        inserted += count
    log.debug(
        "seed_node_ownership_registry: ns=%s inserted=%d",
        ns_uuid,
        inserted,
    )
    return inserted


async def seed_node_ownership_all_namespaces(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
) -> int:
    """Idempotently seed every existing namespace in a single statement.

    Startup-backfill counterpart to :func:`seed_node_ownership_registry`.
    The per-namespace variant costs one round trip per ownership entry, so
    a boot-time loop over N namespaces issues ``N * len(_OWNERSHIP_ENTRIES)``
    statements and comes to dominate startup once N grows.  This collapses
    that to one set-based INSERT with the same NOT EXISTS idempotency guard.

    Caller contract:
      ``conn`` must be a connection whose role bypasses RLS (the owner pool),
      because a single statement writes rows for every namespace at once.

    Returns
    -------
    int
        The number of rows actually inserted (0 when already seeded).
    """
    node_types = [e["node_type"] for e in _OWNERSHIP_ENTRIES]
    transitions = [e.get("transition") for e in _OWNERSHIP_ENTRIES]
    owner_engines = [e["owner_engine"] for e in _OWNERSHIP_ENTRIES]
    status = await conn.execute(_BULK_INSERT_SQL, node_types, transitions, owner_engines)
    try:
        inserted = int(status.split()[-1])
    except (AttributeError, ValueError, IndexError):
        inserted = 0
    log.debug("seed_node_ownership_all_namespaces: inserted=%d", inserted)
    return inserted
