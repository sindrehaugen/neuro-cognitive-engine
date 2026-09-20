"""nce.resource_surface.spec — Declarative specification for C12 resource surfaces.

Phase A Wave A-1:
Defines ResourceSpec, the immutable declaration from which uniform REST routes,
MCP tools, filtering, search, pagination, optimistic concurrency, and tier
redaction are generated across all 17 vertical engines without hand-writing boilerplate.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class SecondaryTable:
    """A secondary table a multi-table ResourceSpec also reads and writes,
    joined to the primary table on a shared identity value.

    Wave: multi-table ResourceSpec support (2026-09-20), dispatched to close
    system_design's DEVICE/PORT/RACK/CABLE exemptions (``nce/resource_surface/
    exemptions.py``) -- each is a "multi-table spread", not 1:1 with a table,
    which is exactly the shape C12 had no declaration for before this.

    Attributes:
        table_name: The secondary table's name. Validated the same way
                    ``ResourceSpec.table_name`` is -- must be a real,
                    RLS-known table (``nce.event_log``), never silently
                    accepted.
        join_field: Column on the secondary table holding the SAME identity
                    value as the primary table's row for the same logical
                    resource (e.g. ``node_label``, matching
                    ``system_design_device_capabilities.node_label`` /
                    ``system_design_node_state.node_label`` for a DEVICE).
                    This is deliberately allowed to differ from
                    ``id_field`` in *name* while holding the same *value* --
                    the primary table's own identity column may be called
                    something else.
        fields:     Column names on the secondary table. A spec's
                    ``writable_fields`` entries that match one of these are
                    routed to THIS table on upsert instead of the primary
                    one; every other entry stays on the primary table. A
                    field name must not appear in more than one
                    ``SecondaryTable`` (validated) -- routing would be
                    ambiguous.

    WARNING -- do not target a table with application-level write validation.
    ``upsert_secondary_tables()`` (``nce/resource_surface/rest.py``) is a
    plain SELECT-then-branch: it enforces nothing beyond the target table's
    own DB-level CHECK/FK constraints. A table whose real invariants live in
    Python -- not the DDL -- must not be a ``SecondaryTable`` target without
    an explicit pre-write validation hook (not yet built; this dataclass has
    none). The known instance: ``system_design_geometry``. Its writer,
    ``nce/vertical_modules/system_design/geometry.py``'s ``validate_geometry()``,
    enforces half-U ``rack_position`` granularity, an exact-precision
    float-max bound, and ``rack_face``/``cable_type`` vocabularies that no
    CHECK constraint captures, and that function's own docstring states the
    intent outright: validation lives in exactly one place "so a caller that
    reaches this function directly ... cannot route around it. One place,
    not two." Routing ``rack_position``/``cable_length_m``/etc. through this
    class would be a second, unvalidated path into that same table -- add a
    validation hook to this dataclass first, wire it, and only then target
    ``system_design_geometry`` here.
    """

    table_name: str
    join_field: str
    fields: tuple[str, ...]

    def __post_init__(self) -> None:
        from nce.event_log import (
            EXPECTED_GLOBAL_TABLES,
            EXPECTED_SPECIAL_RLS_TABLES,
            EXPECTED_TENANT_RLS_TABLES,
        )

        if (
            self.table_name not in EXPECTED_TENANT_RLS_TABLES
            and self.table_name not in EXPECTED_GLOBAL_TABLES
            and self.table_name not in EXPECTED_SPECIAL_RLS_TABLES
        ):
            raise ValueError(
                f"SecondaryTable {self.table_name!r} is in neither "
                f"EXPECTED_TENANT_RLS_TABLES, EXPECTED_GLOBAL_TABLES, nor "
                f"EXPECTED_SPECIAL_RLS_TABLES in nce.event_log -- the same "
                f"check ResourceSpec.table_name gets, applied here too so a "
                f"secondary table can't silently be a typo."
            )
        if not self.fields:
            raise ValueError(
                f"SecondaryTable {self.table_name!r} declares no fields -- "
                f"a secondary table with nothing routed to it is never "
                f"written or read, which means it should not be declared "
                f"at all."
            )


@dataclass(frozen=True)
class ResourceSpec:
    """Declarative contract for a C12 resource surface.

    Attributes:
        engine:             Owning engine slug, e.g. 'inventory', 'sales'.
        entity:             Resource entity slug (kebab-case or snake-case),
                            e.g. 'stock-locations', 'inventory-items'.
        node_type:          Canonical node type matching node-ownership.json,
                            e.g. 'STOCK_LOCATION', 'INVENTORY_ITEM'.
        table_name:         Relational database table name, or None if backed
                            exclusively by the knowledge graph.
        id_field:           Primary identifier field name (default 'id').
        version_field:      Concurrency version field (e.g. 'updated_at' or 'version').
        soft_delete_field:  Field indicating archived status (e.g. 'is_archived', 'status').
        filterable_fields:  Tuple of column/attribute names allowed in GET query filters.
        searchable_fields:  Tuple of text columns searched when '?q=' is provided.
        writable_fields:    Tuple of fields accepted on POST (create) and PATCH (update).
        tier_allowlists:    Mapping of principal tier ('employee', 'contractor',
                            'external-customer') to allowed field names. Non-allowlisted
                            fields are stripped from responses for that tier.
        governed_verbs:     Verbs requiring @governed confirmation ('create', 'archive', etc.).
        description:        Human-readable description of the resource.
        enabled_guard:      Optional per-namespace opt-in check, e.g.
                            ``require_inventory_enabled`` from the owning engine's
                            ``_guard.py``. Called as ``await enabled_guard(pool,
                            namespace_id)`` at the top of every generated MCP handler
                            and REST route for a tenant-scoped spec, before any read
                            or write -- mirrors the hand-written boundary convention
                            (see e.g. ``nce/vertical_modules/inventory/_guard.py``'s
                            own docstring: apply at the handler/route boundary, never
                            inside a ``do_*`` core). Expected to raise a subclass of
                            ``nce.engine_registry.EngineDisabledError`` when the
                            namespace has not opted in; that exception is translated
                            the same way a hand-written handler's is. ``None`` (the
                            default) means the spec has no opt-in gate to enforce --
                            true for specs whose engine has no ``_guard.py`` at all.
        secondary_tables:   Tuple of ``SecondaryTable`` -- additional tables this
                            spec also reads and writes, each joined to the
                            primary row by identity value (see
                            ``SecondaryTable``). Empty by default: most specs
                            are still 1:1 with a single table, and this changes
                            nothing for them. Generated ``get``/``upsert``
                            handlers merge/split across every secondary table
                            declared here; ``list``/``archive`` touch the
                            primary table only (documented limitation, not an
                            oversight -- listing with N joins and archiving N
                            tables independently are real features a future
                            wave can add if a real spec needs them).
                            When ``table_name`` is ``None`` (a graph-primary
                            spec, e.g. DEVICE/RACK/CABLE), the primary row
                            lives in ``kg_nodes`` instead of a relational
                            table -- an identity row only (label, entity_type,
                            change_origin, timestamps; kg_nodes has no
                            attribute column of its own). Every secondary
                            table then joins on the node's ``label``, and the
                            generated write path always creates/updates the
                            ``kg_nodes`` row first, since every system_design
                            satellite table (device_capabilities, node_state,
                            geometry) carries a real FK to
                            ``kg_nodes(label, namespace_id)``.
    """

    engine: str
    entity: str
    node_type: str
    table_name: str | None = None
    id_field: str = "id"
    version_field: str | None = "updated_at"
    soft_delete_field: str | None = "is_archived"
    filterable_fields: tuple[str, ...] = ()
    searchable_fields: tuple[str, ...] = ()
    writable_fields: tuple[str, ...] = ()
    tier_allowlists: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    governed_verbs: tuple[str, ...] = ()
    description: str = ""
    storage_kind: str = "postgres"
    enabled_guard: Callable[[Any, str], Awaitable[None]] | None = None
    secondary_tables: tuple[SecondaryTable, ...] = ()
    tenant_scope: str = field(init=False)

    def __post_init__(self) -> None:
        if self.storage_kind == "mongo":
            scope = "tenant"
        elif self.storage_kind == "kg_nodes" or self.table_name is None:
            scope = "graph"
        elif self.table_name is not None:
            from nce.event_log import (
                EXPECTED_GLOBAL_TABLES,
                EXPECTED_SPECIAL_RLS_TABLES,
                EXPECTED_TENANT_RLS_TABLES,
            )

            if self.table_name in EXPECTED_GLOBAL_TABLES:
                scope = "global"
            elif (
                self.table_name in EXPECTED_TENANT_RLS_TABLES
                or self.table_name in EXPECTED_SPECIAL_RLS_TABLES
            ):
                scope = "tenant"
            else:
                raise ValueError(
                    f"Table {self.table_name!r} for resource {self.engine}:{self.entity} "
                    f"is in neither EXPECTED_TENANT_RLS_TABLES nor EXPECTED_GLOBAL_TABLES in nce.event_log"
                )
        else:
            scope = "graph"
        object.__setattr__(self, "tenant_scope", scope)

        if self.secondary_tables:
            # table_name is None here always means scope == "graph" (see the
            # elif chain above): kg_nodes IS the primary row (identity only --
            # label, entity_type, change_origin, timestamps; kg_nodes itself
            # has no attribute column). Every real field this spec declares
            # routes to a secondary table instead, joined on the node's label.
            # This is deliberately thin, not a limitation to work around: it
            # matches what DEVICE/RACK/CABLE's satellite tables' own
            # COMMENT ON TABLE already says ("kg_nodes has no payload column;
            # typed/queryable ... fields live here").
            seen_fields: dict[str, str] = {}
            for sec in self.secondary_tables:
                for f in sec.fields:
                    if f in seen_fields:
                        raise ValueError(
                            f"Resource {self.engine}:{self.entity}: field {f!r} is "
                            f"routed to both {seen_fields[f]!r} and {sec.table_name!r} "
                            f"-- a field must belong to exactly one table."
                        )
                    seen_fields[f] = sec.table_name

    @property
    def rest_slug(self) -> str:
        """Kebab-cased slug for URL paths (e.g. 'stock-locations')."""
        return self.entity.replace("_", "-").strip("-")

    @property
    def mcp_slug(self) -> str:
        """Snake-cased slug for MCP tool identifiers (e.g. 'stock_locations')."""
        return self.entity.replace("-", "_").strip("_")

    @property
    def rest_collection_path(self) -> str:
        """Canonical collection endpoint path."""
        return f"/api/{self.engine}/{self.rest_slug}"

    @property
    def rest_item_path(self) -> str:
        """Canonical item endpoint path."""
        return f"/api/{self.engine}/{self.rest_slug}/{{id}}"
