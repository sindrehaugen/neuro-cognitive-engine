"""nce.resource_surface.mcp — MCP tool generator for C12 resources.

Phase A Wave A-1:
Generates declarative MCP tools and executable handlers for any ResourceSpec:
  - {engine}_list_{entity}: collection query with filters, q search, and cursor pagination
  - {engine}_get_{entity}: single item fetch with tier redaction
  - {engine}_upsert_{entity}: create or update with concurrency protection
  - {engine}_archive_{entity}: soft archive

Outputs standard mcp.types.Tool definitions for mcp_stdio_tools and
nce.tool_registry.ToolSpec metadata for TOOL_REGISTRY.
"""

from __future__ import annotations

import base64
import json
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from uuid import UUID

from mcp.types import Tool

from nce.auth import NamespaceContext, set_namespace_context
from nce.db_utils import scoped_pg_session
from nce.entity_resolution.ownership import assert_owner
from nce.events.emit import emit_graph_write
from nce.mcp_errors import mcp_handler
from nce.resource_surface.rest import _get_mem_bucket, row_to_dict, upsert_secondary_tables
from nce.resource_surface.spec import ResourceSpec

if TYPE_CHECKING:
    from nce.tool_registry import ToolSpec


def _version_str(value: Any) -> str:
    """Normalise a stored version_field value to the same string shape handle_upsert
    hands the client (see its ``now.isoformat()`` return).

    NOT needed on the real-Postgres path: ``row_to_dict`` (this module imports it
    from ``nce.resource_surface.rest``) already maps every column through
    ``serialize_val``, which renders a ``datetime`` as ``.isoformat()`` -- the
    real-Postgres ``existing = row_to_dict(existing_row)`` above therefore already
    hands this function a string, and a bare ``str()`` on that string would have
    worked exactly as well. (An earlier version of this docstring claimed a real
    Postgres row round-trips as a raw ``datetime`` and that comparing it directly
    produced a spurious 409 on that path -- checked again after a peer review
    challenged it, and that claim was wrong: the mismatch this function actually
    guards against never existed on the real-Postgres path.)

    This function IS load-bearing on the in-memory fallback path: this same
    handler's fix stores a real ``datetime`` object in ``data[spec.version_field]``
    (needed for the real-Postgres INSERT/UPDATE bind), and ``mem[item_id] =
    dict(data)`` puts that same raw ``datetime`` straight into the in-memory
    bucket with no serialization step. Without this function, ``str(a_datetime)``
    on that path uses a space separator ("2026-09-19 18:00:59+00:00") where
    ``isoformat()`` uses "T" ("2026-09-19T18:00:59+00:00") -- comparing them
    directly would reject a correct expected_version as a spurious conflict, but
    only for a resource never backed by a real Postgres pool. Verified by mutation
    (tests/integration/test_resource_surface_upsert_live.py::
    test_upsert_in_memory_path_correct_expected_version_succeeds): removing this
    function makes exactly that test fail, and no other.
    """
    return value.isoformat() if isinstance(value, datetime) else str(value)


def build_mcp_tool_definitions(spec: ResourceSpec) -> list[Tool]:
    """Generate the mcp.types.Tool objects for a ResourceSpec (4 minus any
    verb named in ``spec.excluded_verbs``)."""
    prefix = f"{spec.engine}"
    slug = spec.mcp_slug
    entity_title = spec.node_type.replace("_", " ").title()

    list_tool_name = f"{prefix}_list_{slug}"
    get_tool_name = f"{prefix}_get_{slug}"
    upsert_tool_name = f"{prefix}_upsert_{slug}"
    archive_tool_name = f"{prefix}_archive_{slug}"

    is_tenant = spec.tenant_scope == "tenant"

    # List Tool Schema
    list_props: dict[str, Any] = {
        "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
        "limit": {"type": "integer", "default": 50, "minimum": 1, "maximum": 500},
        "cursor": {"type": "string", "description": "Pagination cursor from previous response."},
        "q": {"type": "string", "description": "Full-text search term."},
    }
    for f in spec.filterable_fields:
        list_props[f] = {"type": "string", "description": f"Filter by {f}."}

    list_schema = {
        "type": "object",
        "properties": list_props,
        "required": ["namespace_id"] if is_tenant else [],
    }

    # Get Tool Schema
    get_schema = {
        "type": "object",
        "properties": {
            "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
            "id": {"type": "string", "description": f"Unique identifier of the {entity_title}."},
        },
        "required": ["namespace_id", "id"] if is_tenant else ["id"],
    }

    # Upsert Tool Schema
    upsert_props: dict[str, Any] = {
        "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
        "id": {
            "type": "string",
            "description": f"Optional ID of existing {entity_title} to update.",
        },
        "expected_version": {"type": "string", "description": "Concurrency check version."},
    }
    for f in spec.writable_fields:
        upsert_props[f] = {"description": f"Value for {f}."}

    upsert_schema = {
        "type": "object",
        "properties": upsert_props,
        "required": ["namespace_id"] if is_tenant else [],
    }

    # Archive Tool Schema
    archive_schema = {
        "type": "object",
        "properties": {
            "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
            "id": {"type": "string", "description": f"ID of {entity_title} to archive."},
            "reason": {"type": "string", "description": "Audit reason for archiving."},
        },
        "required": ["namespace_id", "id"] if is_tenant else ["id"],
    }

    all_tools = {
        "list": Tool(
            name=list_tool_name,
            description=f"List and query {entity_title} resources for {spec.engine}.",
            inputSchema=list_schema,
        ),
        "get": Tool(
            name=get_tool_name,
            description=f"Fetch a single {entity_title} resource by ID.",
            inputSchema=get_schema,
        ),
        "upsert": Tool(
            name=upsert_tool_name,
            description=f"Create or update a {entity_title} resource.",
            inputSchema=upsert_schema,
        ),
        "archive": Tool(
            name=archive_tool_name,
            description=f"Soft-archive a {entity_title} resource.",
            inputSchema=archive_schema,
        ),
    }
    return [tool for verb, tool in all_tools.items() if verb not in spec.excluded_verbs]


def build_mcp_tool_specs(spec: ResourceSpec) -> dict[str, ToolSpec]:
    """Generate execution handlers and ToolSpec entries for TOOL_REGISTRY
    (4 minus any verb named in ``spec.excluded_verbs``)."""
    from nce.tool_registry import ToolSpec

    prefix = f"{spec.engine}"
    slug = spec.mcp_slug

    list_tool_name = f"{prefix}_list_{slug}"
    get_tool_name = f"{prefix}_get_{slug}"
    upsert_tool_name = f"{prefix}_upsert_{slug}"
    archive_tool_name = f"{prefix}_archive_{slug}"

    is_tenant = spec.tenant_scope == "tenant"
    is_global = spec.tenant_scope == "global"
    is_graph = spec.tenant_scope == "graph"
    # An enabled_guard's subject is the CALLER's namespace, not the table's
    # storage scope -- a global spec like PRODUCT_SKU still needs a real
    # namespace_id to check opt-in against. Omitting namespace_id must be
    # refused, not silently treated as "no namespace to gate," matching the
    # hand-written boundary (e.g. nce/admin_handlers/product.py) which
    # already requires namespace_id unconditionally for a gated engine.
    #
    # is_graph is included for the same reason: kg_nodes rows are namespace-
    # scoped (UNIQUE(label, namespace_id), schema.sql), so a graph-primary
    # spec is exactly as namespace-bound as a tenant one. Before Wave 3
    # (2026-09-20) this never mattered -- storage_kind="kg_nodes" was an
    # unconditional 501, so no graph-primary spec ever reached a real query.
    # Once it does, omitting namespace_id must not silently bind
    # `WHERE namespace_id = NULL` and return zero rows -- it must 422, same
    # as every other namespace-scoped spec.
    requires_namespace = is_tenant or is_graph or spec.enabled_guard is not None

    async def handle_list(engine: Any, arguments: dict[str, Any]) -> str:
        ns_raw = arguments.get("namespace_id")
        ns_uuid: UUID | None = None
        if requires_namespace:
            if not ns_raw:
                return json.dumps({"error": "Missing required argument: namespace_id"})
            try:
                ns_uuid = UUID(str(ns_raw))
            except ValueError as exc:
                return json.dumps({"error": f"Invalid namespace_id: {exc}"})
        else:
            if ns_raw:
                try:
                    ns_uuid = UUID(str(ns_raw))
                except ValueError as exc:
                    return json.dumps({"error": f"Invalid namespace_id: {exc}"})

        if ns_uuid and spec.enabled_guard and hasattr(engine, "pg_pool") and engine.pg_pool:
            # Raises a subclass of EngineDisabledError, caught and translated
            # by the @mcp_handler decorator this function is wrapped in below
            # (build_mcp_tool_specs' return dict) -- not caught here, same
            # contract as a hand-written handler's own require_*_enabled call.
            #
            # `ns_uuid` is guaranteed non-None here whenever enabled_guard is
            # set: `requires_namespace` above already refused a missing
            # namespace_id for exactly that case, including for a
            # tenant_scope="global" spec like PRODUCT_SKU (its hand-written
            # boundary requires namespace_id unconditionally too -- see
            # `requires_namespace`'s own comment for the incident this fixes).
            await spec.enabled_guard(engine.pg_pool, str(ns_uuid))

        if ns_uuid:
            try:
                set_namespace_context(NamespaceContext(namespace_id=ns_uuid))
            except Exception:
                pass

        limit = max(1, min(int(arguments.get("limit", 50)), 500))
        cursor = arguments.get("cursor")
        q = arguments.get("q")

        active_filters = {
            f: arguments[f]
            for f in spec.filterable_fields
            if f in arguments and arguments[f] is not None
        }

        items: list[dict[str, Any]] = []
        next_cursor: str | None = None

        if hasattr(engine, "pg_pool") and engine.pg_pool:
            if spec.storage_kind == "mongo":
                return json.dumps(
                    {
                        "error": f"{spec.storage_kind} backend storage is not supported yet for {spec.entity}",
                        "status_code": 501,
                    }
                )
            if is_graph:
                # kg_nodes-primary list: primary (identity) rows only, same
                # documented limitation as a postgres-primary multi-table
                # spec's own list -- see SecondaryTable's docstring.
                session_ns = ns_uuid or UUID("00000000-0000-0000-0000-000000000000")
                async with scoped_pg_session(engine.pg_pool, session_ns) as conn:
                    where_clauses = ["entity_type = $1", "namespace_id = $2"]
                    params: list[Any] = [spec.node_type, ns_uuid]
                    idx = 3

                    for f_col, f_val in active_filters.items():
                        where_clauses.append(f"{f_col} = ${idx}")
                        params.append(f_val)
                        idx += 1

                    if q and spec.searchable_fields:
                        q_clauses = [
                            f"{s_col}::text ILIKE ${idx}" for s_col in spec.searchable_fields
                        ]
                        where_clauses.append(f"({' OR '.join(q_clauses)})")
                        params.append(f"%{q}%")
                        idx += 1

                    if cursor:
                        try:
                            dec = base64.b64decode(cursor).decode("utf-8")
                            where_clauses.append(f"label < ${idx}")
                            params.append(dec)
                            idx += 1
                        except Exception:
                            pass

                    params.append(limit + 1)
                    limit_idx = idx
                    where_sql = f"WHERE {' AND '.join(where_clauses)}"
                    query = f"""
                        SELECT id, label, entity_type, namespace_id, change_origin,
                               created_at, updated_at
                        FROM kg_nodes
                        {where_sql}
                        ORDER BY label DESC
                        LIMIT ${limit_idx}
                    """
                    rows = await conn.fetch(query, *params)
                    has_more = len(rows) > limit
                    page_rows = rows[:limit]
                    for r in page_rows:
                        items.append(row_to_dict(r))
                    if has_more and page_rows:
                        last_label = str(page_rows[-1]["label"])
                        next_cursor = base64.b64encode(last_label.encode("utf-8")).decode("utf-8")
            elif spec.table_name:
                session_ns = ns_uuid or UUID("00000000-0000-0000-0000-000000000000")
                async with scoped_pg_session(engine.pg_pool, session_ns) as conn:
                    if is_global:
                        where_clauses: list[str] = []
                        params: list[Any] = []
                        idx = 1
                    else:
                        where_clauses = ["namespace_id = $1"]
                        params = [ns_uuid]
                        idx = 2

                    if spec.soft_delete_field:
                        where_clauses.append(
                            f"({spec.soft_delete_field} = false OR {spec.soft_delete_field} IS NULL)"
                        )

                    for f_col, f_val in active_filters.items():
                        where_clauses.append(f"{f_col} = ${idx}")
                        params.append(f_val)
                        idx += 1

                    if q and spec.searchable_fields:
                        q_clauses = [
                            f"{s_col}::text ILIKE ${idx}" for s_col in spec.searchable_fields
                        ]
                        where_clauses.append(f"({' OR '.join(q_clauses)})")
                        params.append(f"%{q}%")
                        idx += 1

                    if cursor:
                        try:
                            cursor_id = base64.b64decode(cursor).decode("utf-8")
                            where_clauses.append(f"{spec.id_field} < ${idx}")
                            params.append(cursor_id)
                            idx += 1
                        except Exception:
                            pass

                    params.append(limit + 1)
                    limit_idx = idx

                    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
                    query = f"""
                        SELECT * FROM {spec.table_name}
                        {where_sql}
                        ORDER BY {spec.id_field} DESC
                        LIMIT ${limit_idx}
                    """
                    rows = await conn.fetch(query, *params)
                    has_more = len(rows) > limit
                    page_rows = rows[:limit]
                    for r in page_rows:
                        items.append(row_to_dict(r))

                    if has_more and page_rows:
                        last_id = str(page_rows[-1][spec.id_field])
                        next_cursor = base64.b64encode(last_id.encode("utf-8")).decode("utf-8")
        else:
            mem = _get_mem_bucket(spec, str(ns_uuid) if ns_uuid else None)
            raw_items = list(mem.values())
            filtered = []
            for it in raw_items:
                if spec.soft_delete_field and (
                    it.get(spec.soft_delete_field) is True
                    or it.get(spec.soft_delete_field) == "archived"
                ):
                    continue
                match = True
                for f_col, f_val in active_filters.items():
                    if str(it.get(f_col)) != str(f_val):
                        match = False
                        break
                if match and q and spec.searchable_fields:
                    text_corpus = " ".join(str(it.get(c, "")) for c in spec.searchable_fields)
                    if q.lower() not in text_corpus.lower():
                        match = False
                if match:
                    filtered.append(it)
            items = filtered[:limit]

        return json.dumps(
            {"items": items, "next_cursor": next_cursor, "total": len(items)}, default=str
        )

    async def handle_get(engine: Any, arguments: dict[str, Any]) -> str:
        item_id = arguments.get("id")
        if not item_id:
            return json.dumps({"error": "Missing required argument: id"})

        ns_raw = arguments.get("namespace_id")
        ns_uuid: UUID | None = None
        if requires_namespace:
            if not ns_raw:
                return json.dumps({"error": "Missing required argument: namespace_id"})
            try:
                ns_uuid = UUID(str(ns_raw))
            except ValueError as exc:
                return json.dumps({"error": f"Invalid namespace_id: {exc}"})
        else:
            if ns_raw:
                try:
                    ns_uuid = UUID(str(ns_raw))
                except ValueError as exc:
                    return json.dumps({"error": f"Invalid namespace_id: {exc}"})

        if ns_uuid and spec.enabled_guard and hasattr(engine, "pg_pool") and engine.pg_pool:
            # Raises a subclass of EngineDisabledError, caught and translated
            # by the @mcp_handler decorator this function is wrapped in below
            # (build_mcp_tool_specs' return dict) -- not caught here, same
            # contract as a hand-written handler's own require_*_enabled call.
            #
            # `ns_uuid` is guaranteed non-None here whenever enabled_guard is
            # set: `requires_namespace` above already refused a missing
            # namespace_id for exactly that case, including for a
            # tenant_scope="global" spec like PRODUCT_SKU (its hand-written
            # boundary requires namespace_id unconditionally too -- see
            # `requires_namespace`'s own comment for the incident this fixes).
            await spec.enabled_guard(engine.pg_pool, str(ns_uuid))

        item: dict[str, Any] | None = None
        if hasattr(engine, "pg_pool") and engine.pg_pool:
            if spec.storage_kind == "mongo":
                return json.dumps(
                    {
                        "error": f"{spec.storage_kind} backend storage is not supported yet for {spec.entity}",
                        "status_code": 501,
                    }
                )
            if is_graph:
                session_ns = ns_uuid or UUID("00000000-0000-0000-0000-000000000000")
                async with scoped_pg_session(engine.pg_pool, session_ns) as conn:
                    query = """
                        SELECT id, label, entity_type, namespace_id, change_origin,
                               created_at, updated_at
                        FROM kg_nodes
                        WHERE entity_type = $1 AND namespace_id = $2
                          AND (label = $3 OR id::text = $3)
                        LIMIT 1
                    """
                    row = await conn.fetchrow(query, spec.node_type, ns_uuid, item_id)
                    if row:
                        item = row_to_dict(row)
                        node_label = item["label"]
                        for sec in spec.secondary_tables:
                            sec_row = await conn.fetchrow(
                                f"SELECT * FROM {sec.table_name} WHERE namespace_id = $1 AND {sec.join_field} = $2",
                                ns_uuid,
                                node_label,
                            )
                            if sec_row:
                                item.update(row_to_dict(sec_row))
            elif spec.table_name:
                session_ns = ns_uuid or UUID("00000000-0000-0000-0000-000000000000")
                async with scoped_pg_session(engine.pg_pool, session_ns) as conn:
                    if is_global:
                        query = f"SELECT * FROM {spec.table_name} WHERE {spec.id_field} = $1"
                        row = await conn.fetchrow(query, item_id)
                    else:
                        query = f"SELECT * FROM {spec.table_name} WHERE namespace_id = $1 AND {spec.id_field} = $2"
                        row = await conn.fetchrow(query, ns_uuid, item_id)
                    if row:
                        item = row_to_dict(row)
                        # Multi-table spec: merge each secondary table's row into
                        # the response, joined on its own join_field holding the
                        # same identity value as this item_id. A secondary table
                        # with no matching row yet (never upserted into) is
                        # simply absent from the merge, not an error -- the
                        # primary row is still a real, gettable resource on its
                        # own.
                        for sec in spec.secondary_tables:
                            if is_global:
                                sec_row = await conn.fetchrow(
                                    f"SELECT * FROM {sec.table_name} WHERE {sec.join_field} = $1",
                                    item_id,
                                )
                            else:
                                sec_row = await conn.fetchrow(
                                    f"SELECT * FROM {sec.table_name} WHERE namespace_id = $1 AND {sec.join_field} = $2",
                                    ns_uuid,
                                    item_id,
                                )
                            if sec_row:
                                item.update(row_to_dict(sec_row))
        else:
            mem = _get_mem_bucket(spec, str(ns_uuid) if ns_uuid else None)
            item = mem.get(str(item_id))

        if not item:
            return json.dumps({"error": f"{spec.node_type} {item_id} not found"})

        return json.dumps(item, default=str)

    async def handle_upsert(engine: Any, arguments: dict[str, Any]) -> str:
        ns_raw = arguments.get("namespace_id")
        ns_uuid: UUID | None = None
        if requires_namespace:
            if not ns_raw:
                return json.dumps({"error": "Missing required argument: namespace_id"})
            try:
                ns_uuid = UUID(str(ns_raw))
            except ValueError as exc:
                return json.dumps({"error": f"Invalid namespace_id: {exc}"})
        else:
            if ns_raw:
                try:
                    ns_uuid = UUID(str(ns_raw))
                except ValueError as exc:
                    return json.dumps({"error": f"Invalid namespace_id: {exc}"})

        if ns_uuid and spec.enabled_guard and hasattr(engine, "pg_pool") and engine.pg_pool:
            # Raises a subclass of EngineDisabledError, caught and translated
            # by the @mcp_handler decorator this function is wrapped in below
            # (build_mcp_tool_specs' return dict) -- not caught here, same
            # contract as a hand-written handler's own require_*_enabled call.
            #
            # `ns_uuid` is guaranteed non-None here whenever enabled_guard is
            # set: `requires_namespace` above already refused a missing
            # namespace_id for exactly that case, including for a
            # tenant_scope="global" spec like PRODUCT_SKU (its hand-written
            # boundary requires namespace_id unconditionally too -- see
            # `requires_namespace`'s own comment for the incident this fixes).
            await spec.enabled_guard(engine.pg_pool, str(ns_uuid))

        item_id = str(arguments.get("id") or uuid.uuid4())
        expected_version = arguments.get("expected_version")

        data: dict[str, Any] = {f: arguments[f] for f in spec.writable_fields if f in arguments}
        data[spec.id_field] = item_id
        if not is_global or "namespace_id" in spec.writable_fields:
            if ns_uuid:
                data["namespace_id"] = str(ns_uuid)
        now = datetime.now(timezone.utc)
        if spec.version_field:
            data[spec.version_field] = now

        if hasattr(engine, "pg_pool") and engine.pg_pool:
            if spec.storage_kind == "mongo":
                return json.dumps(
                    {
                        "error": f"{spec.storage_kind} backend storage is not supported yet for {spec.entity}",
                        "status_code": 501,
                    }
                )
            if is_graph:
                node_label = item_id
                session_ns = ns_uuid or UUID("00000000-0000-0000-0000-000000000000")
                async with scoped_pg_session(engine.pg_pool, session_ns) as conn:
                    # Same deny-by-default guard every hand-written kg_nodes
                    # writer makes (system_design/devices.py, project/
                    # convert.py, ...) before its own INSERT/UPDATE.
                    # OwnershipError propagates uncaught -- the @mcp_handler
                    # wrapper this function is registered under (see
                    # build_mcp_tool_specs' own return dict) already
                    # translates it to MCP_SCOPE_FORBIDDEN, the same
                    # contract enabled_guard's EngineDisabledError uses
                    # above, so it is not caught here either.
                    await assert_owner(conn, session_ns, spec.node_type, spec.engine)

                    existing_row = await conn.fetchrow(
                        """
                        SELECT id, label, entity_type, namespace_id, change_origin,
                               created_at, updated_at
                        FROM kg_nodes
                        WHERE entity_type = $1 AND namespace_id = $2 AND label = $3
                        """,
                        spec.node_type,
                        ns_uuid,
                        node_label,
                    )
                    if existing_row and expected_version and spec.version_field:
                        existing = row_to_dict(existing_row)
                        actual = _version_str(existing.get(spec.version_field, ""))
                        if actual != str(expected_version):
                            return json.dumps(
                                {
                                    "error": f"Version conflict: expected {expected_version}, got {actual}",
                                    "reason": "version_conflict",
                                    "status_code": 409,
                                }
                            )

                    # kg_nodes identity row first -- every system_design
                    # satellite table carries a real FK to
                    # kg_nodes(label, namespace_id), so this must land before
                    # any secondary-table write, not after.
                    cols = ["label", "entity_type", "namespace_id"]
                    vals_id: list[Any] = [node_label, spec.node_type, ns_uuid]
                    if "change_origin" in data:
                        cols.append("change_origin")
                        vals_id.append(data["change_origin"])
                    placeholders = [f"${i + 1}" for i in range(len(cols))]
                    set_items = [f"{c} = EXCLUDED.{c}" for c in cols if c != "label"]
                    await conn.execute(
                        f"""
                        INSERT INTO kg_nodes ({", ".join(cols)})
                        VALUES ({", ".join(placeholders)})
                        ON CONFLICT (label, namespace_id) DO UPDATE
                            SET {", ".join(set_items)}, updated_at = NOW()
                        """,
                        *vals_id,
                    )

                    secondary_field_names = {f for sec in spec.secondary_tables for f in sec.fields}
                    sec_data = {k: v for k, v in data.items() if k in secondary_field_names}
                    if "node_type" in secondary_field_names:
                        sec_data["node_type"] = spec.node_type
                    if sec_data:
                        await upsert_secondary_tables(
                            conn, spec, node_label, ns_uuid, is_global, sec_data
                        )

                    await emit_graph_write(
                        conn,
                        namespace_id=session_ns,
                        node_type=spec.node_type,
                        op="upserted",
                        node_id=node_label,
                    )
            elif spec.table_name:
                # Multi-table spec: route each secondary table's own fields
                # out of the primary column list first -- the primary
                # INSERT/UPDATE below must never reference a column that
                # only exists on a secondary table. `data` itself is left
                # untouched (a `.pop` here would also affect the in-memory
                # fallback's single-blob storage, which is not what a
                # secondary-table split means there).
                secondary_field_names = {f for sec in spec.secondary_tables for f in sec.fields}
                primary_data = (
                    {k: v for k, v in data.items() if k not in secondary_field_names}
                    if secondary_field_names
                    else data
                )
                session_ns = ns_uuid or UUID("00000000-0000-0000-0000-000000000000")
                async with scoped_pg_session(engine.pg_pool, session_ns) as conn:
                    # Check for existing
                    if is_global:
                        existing_row = await conn.fetchrow(
                            f"SELECT * FROM {spec.table_name} WHERE {spec.id_field} = $1",
                            item_id,
                        )
                    else:
                        existing_row = await conn.fetchrow(
                            f"SELECT * FROM {spec.table_name} WHERE namespace_id = $1 AND {spec.id_field} = $2",
                            ns_uuid,
                            item_id,
                        )
                    if existing_row:
                        existing = row_to_dict(existing_row)
                        if expected_version and spec.version_field:
                            actual = _version_str(existing.get(spec.version_field, ""))
                            if actual != str(expected_version):
                                return json.dumps(
                                    {
                                        "error": f"Version conflict: expected {expected_version}, got {actual}",
                                        "reason": "version_conflict",
                                        "status_code": 409,
                                    }
                                )

                        vals = list(primary_data.values())
                        if is_global:
                            set_items = [
                                f"{k} = ${i + 2}" for i, k in enumerate(primary_data.keys())
                            ]
                            await conn.execute(
                                f"UPDATE {spec.table_name} SET {', '.join(set_items)} WHERE {spec.id_field} = $1",
                                item_id,
                                *vals,
                            )
                        else:
                            set_items = [
                                f"{k} = ${i + 3}" for i, k in enumerate(primary_data.keys())
                            ]
                            await conn.execute(
                                f"UPDATE {spec.table_name} SET {', '.join(set_items)} WHERE namespace_id = $1 AND {spec.id_field} = $2",
                                ns_uuid,
                                item_id,
                                *vals,
                            )
                    else:
                        cols = list(primary_data.keys())
                        vals = [primary_data[c] for c in cols]
                        placeholders = [f"${i + 1}" for i in range(len(cols))]
                        await conn.execute(
                            f"INSERT INTO {spec.table_name} ({', '.join(cols)}) VALUES ({', '.join(placeholders)})",
                            *vals,
                        )

                    # Multi-table spec: shared with rest.py's handle_create
                    # and handle_patch -- see upsert_secondary_tables' own
                    # docstring for why this is not a blind ON CONFLICT.
                    await upsert_secondary_tables(conn, spec, item_id, ns_uuid, is_global, data)
        else:
            mem = _get_mem_bucket(spec, str(ns_uuid) if ns_uuid else None)
            existing = mem.get(item_id)
            if existing and expected_version and spec.version_field:
                actual = _version_str(existing.get(spec.version_field, ""))
                if actual != str(expected_version):
                    return json.dumps(
                        {
                            "error": f"Version conflict: expected {expected_version}, got {actual}",
                            "reason": "version_conflict",
                            "status_code": 409,
                        }
                    )
            mem[item_id] = dict(data)

        return json.dumps(
            {"status": "ok", "id": item_id, "version": now.isoformat(), "data": data},
            default=str,
        )

    async def handle_archive(engine: Any, arguments: dict[str, Any]) -> str:
        item_id = arguments.get("id")
        if not item_id:
            return json.dumps({"error": "Missing required argument: id"})

        ns_raw = arguments.get("namespace_id")
        ns_uuid: UUID | None = None
        if requires_namespace:
            if not ns_raw:
                return json.dumps({"error": "Missing required argument: namespace_id"})
            try:
                ns_uuid = UUID(str(ns_raw))
            except ValueError as exc:
                return json.dumps({"error": f"Invalid namespace_id: {exc}"})
        else:
            if ns_raw:
                try:
                    ns_uuid = UUID(str(ns_raw))
                except ValueError as exc:
                    return json.dumps({"error": f"Invalid namespace_id: {exc}"})

        if ns_uuid and spec.enabled_guard and hasattr(engine, "pg_pool") and engine.pg_pool:
            # Raises a subclass of EngineDisabledError, caught and translated
            # by the @mcp_handler decorator this function is wrapped in below
            # (build_mcp_tool_specs' return dict) -- not caught here, same
            # contract as a hand-written handler's own require_*_enabled call.
            #
            # `ns_uuid` is guaranteed non-None here whenever enabled_guard is
            # set: `requires_namespace` above already refused a missing
            # namespace_id for exactly that case, including for a
            # tenant_scope="global" spec like PRODUCT_SKU (its hand-written
            # boundary requires namespace_id unconditionally too -- see
            # `requires_namespace`'s own comment for the incident this fixes).
            await spec.enabled_guard(engine.pg_pool, str(ns_uuid))

        field_name = spec.soft_delete_field or "is_archived"
        if hasattr(engine, "pg_pool") and engine.pg_pool:
            if spec.storage_kind == "mongo":
                return json.dumps(
                    {
                        "error": f"{spec.storage_kind} backend storage is not supported yet for {spec.entity}",
                        "status_code": 501,
                    }
                )
            if is_graph:
                # kg_nodes and every system_design satellite table have no
                # generic soft-delete/is_archived column -- inventing one is
                # a system_design content decision, not a mechanical gap.
                return json.dumps(
                    {
                        "error": f"Archive is not supported for {spec.entity}: kg_nodes-primary "
                        f"specs have no generic soft-delete column",
                        "status_code": 501,
                    }
                )
            if spec.table_name:
                session_ns = ns_uuid or UUID("00000000-0000-0000-0000-000000000000")
                async with scoped_pg_session(engine.pg_pool, session_ns) as conn:
                    if is_global:
                        res = await conn.execute(
                            f"UPDATE {spec.table_name} SET {field_name} = true WHERE {spec.id_field} = $1",
                            item_id,
                        )
                    else:
                        res = await conn.execute(
                            f"UPDATE {spec.table_name} SET {field_name} = true WHERE namespace_id = $1 AND {spec.id_field} = $2",
                            ns_uuid,
                            item_id,
                        )
                    if res.endswith("0"):
                        return json.dumps({"error": f"{spec.node_type} {item_id} not found"})
        else:
            mem = _get_mem_bucket(spec, str(ns_uuid) if ns_uuid else None)
            if str(item_id) not in mem:
                return json.dumps({"error": f"{spec.node_type} {item_id} not found"})
            mem[str(item_id)][field_name] = True

        return json.dumps({"status": "ok", "id": item_id, "archived": True})

    # Wrap names and decorate with @mcp_handler
    handle_list.__name__ = f"handle_{list_tool_name}"
    handle_get.__name__ = f"handle_{get_tool_name}"
    handle_upsert.__name__ = f"handle_{upsert_tool_name}"
    handle_archive.__name__ = f"handle_{archive_tool_name}"

    all_specs = {
        "list": (
            list_tool_name,
            ToolSpec(mcp_handler(handle_list), cacheable=True, engine=spec.engine),
        ),
        "get": (
            get_tool_name,
            ToolSpec(mcp_handler(handle_get), cacheable=True, engine=spec.engine),
        ),
        "upsert": (
            upsert_tool_name,
            ToolSpec(mcp_handler(handle_upsert), mutation=True, engine=spec.engine),
        ),
        "archive": (
            archive_tool_name,
            ToolSpec(mcp_handler(handle_archive), mutation=True, engine=spec.engine),
        ),
    }
    return {
        name: tool_spec
        for verb, (name, tool_spec) in all_specs.items()
        if verb not in spec.excluded_verbs
    }
