"""
nce/vertical_modules/inventory/schema_seed.py
================================================
Idempotent warehouse+van seed for the Inventory engine (Module 11, Wave 1 —
``locations-stock-tables``). Backs migration 050's ``stock_locations`` table.

Per ``docs/vertical_engines/11-inventory-engine.md`` Build phase B1: "seed
warehouse + N vans" — one ``STOCK_LOCATION`` row of ``kind='warehouse'`` plus
``van_count`` flat top-level rows of ``kind='van'``, per namespace.

STOCK_LOCATION is not a FUNCTIONAL_LOCATION
--------------------------------------------
A warehouse/van is a company-internal LOGISTICS location — a different
ontology from the customer-site ``FUNCTIONAL_LOCATION`` tree System
Design/D365 own (docs' "Review round-2 hardening" #1). This module never
reads or writes anything named ``functional_location`` and never touches the
graph (``kg_nodes``/``kg_edges``) at all — this wave is table-only.

Idempotency
-----------
:func:`seed_warehouse_and_vans` is safe to call more than once for the same
namespace: each top-level location is get-or-created via
``_get_or_create_top_level_location``, which relies on migration 050's
partial unique index ``uq_stock_locations_top_level_name`` (one row per
``(namespace_id, kind, name)`` among parentless rows) as the
``ON CONFLICT`` arbiter. A second call with the same ``van_count`` creates
zero new rows and returns the same ids with ``"created": False``.

Dependency direction (uncle-bob-craft)
---------------------------------------
This module imports only ``asyncpg`` and ``nce.db_utils.scoped_pg_session``
— no web/HTTP/admin framework imports, matching the rest of this vertical's
planned domain-core layering. ``NCEEngine`` is imported under
``TYPE_CHECKING`` only, mirroring ``economy/contracts.py``'s convention.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any
from uuid import UUID

import asyncpg  # type: ignore[import-untyped]

from nce.db_utils import scoped_pg_session

if TYPE_CHECKING:
    from nce.orchestrator import NCEEngine

log = logging.getLogger("nce.vertical_modules.inventory.schema_seed")

# the reference implementation's module-10 handoff: "1 logistics-person, 1 warehouse, 6 vans"
# (docs/handoff/04-virksomhets-modulkart.md:59) — the default shape a fresh
# namespace seeds, overridable per call.
DEFAULT_VAN_COUNT = 6
DEFAULT_WAREHOUSE_NAME = "Main Warehouse"
DEFAULT_VAN_NAME_PREFIX = "Van"

# `ON CONFLICT (namespace_id, kind, name) WHERE parent_id IS NULL` targets
# migration 050's `uq_stock_locations_top_level_name` partial unique index —
# the arbiter that makes this INSERT idempotent for top-level locations only
# (zone/bin seeding, if ever added, would need a different arbiter).
_INSERT_TOP_LEVEL_SQL = """
    INSERT INTO stock_locations (namespace_id, kind, name, parent_id, level)
    VALUES ($1, $2, $3, NULL, 0)
    ON CONFLICT (namespace_id, kind, name) WHERE parent_id IS NULL
    DO NOTHING
    RETURNING id
"""

_SELECT_TOP_LEVEL_SQL = """
    SELECT id FROM stock_locations
    WHERE namespace_id = $1 AND kind = $2 AND name = $3 AND parent_id IS NULL
"""


async def _get_or_create_top_level_location(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    namespace_id: UUID,
    *,
    kind: str,
    name: str,
) -> tuple[UUID, bool]:
    """Get-or-create one flat top-level ``stock_locations`` row.

    Returns ``(id, created)`` — ``created`` is ``True`` only on the call that
    actually inserted the row; a repeat call for the same
    ``(namespace_id, kind, name)`` returns the existing id with
    ``created=False`` and writes nothing.

    Caller contract: ``conn`` must be inside a transaction with the RLS
    namespace GUC already set (``scoped_pg_session`` — the caller below —
    or an equivalent ``set_namespace_context`` call).
    """
    inserted = await conn.fetchrow(_INSERT_TOP_LEVEL_SQL, namespace_id, kind, name)
    if inserted is not None:
        return inserted["id"], True

    existing = await conn.fetchrow(_SELECT_TOP_LEVEL_SQL, namespace_id, kind, name)
    if existing is None:
        # ON CONFLICT DO NOTHING fired (a row exists) but the immediate
        # re-SELECT found none — only reachable if something deleted the row
        # between the two statements inside this same transaction, which
        # cannot happen on a plain INSERT ... DO NOTHING follow-up SELECT.
        # Fail loudly rather than silently returning a location that was
        # never seeded.
        raise RuntimeError(
            f"seed_warehouse_and_vans: conflict on (kind={kind!r}, name={name!r}) "
            "but no matching row found on re-select"
        )
    return existing["id"], False


async def seed_warehouse_and_vans(
    engine: NCEEngine,
    namespace_id: str | UUID,
    *,
    van_count: int = DEFAULT_VAN_COUNT,
    warehouse_name: str = DEFAULT_WAREHOUSE_NAME,
    van_name_prefix: str = DEFAULT_VAN_NAME_PREFIX,
) -> dict[str, Any]:
    """Idempotently seed one warehouse + ``van_count`` vans for *namespace_id*.

    Both the warehouse and every van are flat top-level ``stock_locations``
    rows (``parent_id IS NULL``, ``level = 0``) — a van is NOT nested under
    the warehouse; see ``docs/vertical_engines/11-inventory-engine.md``'s
    "hierarchical (warehouse→zone→bin; van=flat top-level)" line and
    migration 050's ``stock_locations_hierarchy_shape`` CHECK constraint,
    which refuses any row that violates that shape.

    Parameters
    ----------
    engine:
        Any object exposing ``.pg_pool`` (an ``asyncpg.Pool``) — typically
        an ``NCEEngine`` instance.
    namespace_id:
        The tenant namespace to seed.
    van_count:
        Number of van locations to create (default 6, the reference implementation's module-10
        handoff shape). Must be ``>= 0``.
    warehouse_name:
        Name for the single warehouse row.
    van_name_prefix:
        Prefix for each van's name; vans are named
        ``f"{van_name_prefix}-{i}"`` for ``i`` in ``1..van_count``.

    Returns
    -------
    dict
        ``{"ok": True, "warehouse": {"id", "name", "created"},
        "vans": [{"id", "name", "created"}, ...], "van_count": int}``.

    Raises
    ------
    ValueError
        ``van_count`` is negative, or not an int (``bool`` included — a
        ``True``/``False`` van_count is refused rather than silently
        treated as 1/0).
    """
    if isinstance(van_count, bool) or not isinstance(van_count, int):
        raise ValueError(
            f"seed_warehouse_and_vans: 'van_count' must be a non-negative int, got {van_count!r}"
        )
    if van_count < 0:
        raise ValueError(f"seed_warehouse_and_vans: 'van_count' must be >= 0, got {van_count}")

    ns_uuid = UUID(str(namespace_id)) if not isinstance(namespace_id, UUID) else namespace_id

    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        warehouse_id, warehouse_created = await _get_or_create_top_level_location(
            conn, ns_uuid, kind="warehouse", name=warehouse_name
        )

        vans: list[dict[str, Any]] = []
        for i in range(1, van_count + 1):
            van_name = f"{van_name_prefix}-{i}"
            van_id, van_created = await _get_or_create_top_level_location(
                conn, ns_uuid, kind="van", name=van_name
            )
            vans.append({"id": str(van_id), "name": van_name, "created": van_created})

    log.info(
        "seed_warehouse_and_vans: namespace=%s warehouse_created=%s vans=%d",
        ns_uuid,
        warehouse_created,
        len(vans),
    )
    return {
        "ok": True,
        "warehouse": {
            "id": str(warehouse_id),
            "name": warehouse_name,
            "created": warehouse_created,
        },
        "vans": vans,
        "van_count": len(vans),
    }
