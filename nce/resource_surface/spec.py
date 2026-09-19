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
