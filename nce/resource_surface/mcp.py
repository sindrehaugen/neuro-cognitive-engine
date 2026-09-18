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
from nce.mcp_errors import mcp_handler
from nce.resource_surface.rest import _get_mem_bucket, row_to_dict
from nce.resource_surface.spec import ResourceSpec

if TYPE_CHECKING:
    from nce.tool_registry import ToolSpec


def build_mcp_tool_definitions(spec: ResourceSpec) -> list[Tool]:
    """Generate the 4 mcp.types.Tool objects for a ResourceSpec."""
    prefix = f"{spec.engine}"
    slug = spec.mcp_slug
    entity_title = spec.node_type.replace("_", " ").title()

    list_tool_name = f"{prefix}_list_{slug}"
    get_tool_name = f"{prefix}_get_{slug}"
    upsert_tool_name = f"{prefix}_upsert_{slug}"
    archive_tool_name = f"{prefix}_archive_{slug}"

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
        "required": ["namespace_id"],
    }

    # Get Tool Schema
    get_schema = {
        "type": "object",
        "properties": {
            "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
            "id": {"type": "string", "description": f"Unique identifier of the {entity_title}."},
        },
        "required": ["namespace_id", "id"],
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
        "required": ["namespace_id"],
    }

    # Archive Tool Schema
    archive_schema = {
        "type": "object",
        "properties": {
            "namespace_id": {"type": "string", "description": "Caller namespace UUID."},
            "id": {"type": "string", "description": f"ID of {entity_title} to archive."},
            "reason": {"type": "string", "description": "Audit reason for archiving."},
        },
        "required": ["namespace_id", "id"],
    }

    return [
        Tool(
            name=list_tool_name,
            description=f"List and query {entity_title} resources for {spec.engine}.",
            inputSchema=list_schema,
        ),
        Tool(
            name=get_tool_name,
            description=f"Fetch a single {entity_title} resource by ID.",
            inputSchema=get_schema,
        ),
        Tool(
            name=upsert_tool_name,
            description=f"Create or update a {entity_title} resource.",
            inputSchema=upsert_schema,
        ),
        Tool(
            name=archive_tool_name,
            description=f"Soft-archive a {entity_title} resource.",
            inputSchema=archive_schema,
        ),
    ]


def build_mcp_tool_specs(spec: ResourceSpec) -> dict[str, ToolSpec]:
    """Generate execution handlers and ToolSpec entries for TOOL_REGISTRY."""
    from nce.tool_registry import ToolSpec

    prefix = f"{spec.engine}"
    slug = spec.mcp_slug

    list_tool_name = f"{prefix}_list_{slug}"
    get_tool_name = f"{prefix}_get_{slug}"
    upsert_tool_name = f"{prefix}_upsert_{slug}"
    archive_tool_name = f"{prefix}_archive_{slug}"

    async def handle_list(engine: Any, arguments: dict[str, Any]) -> str:
        ns_raw = arguments.get("namespace_id")
        if not ns_raw:
            return json.dumps({"error": "Missing required argument: namespace_id"})
        try:
            ns_uuid = UUID(str(ns_raw))
        except ValueError as exc:
            return json.dumps({"error": f"Invalid namespace_id: {exc}"})

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

        if hasattr(engine, "pg_pool") and engine.pg_pool and spec.table_name:
            async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
                where_clauses = ["namespace_id = $1"]
                params: list[Any] = [ns_uuid]
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
                    q_clauses = [f"{s_col}::text ILIKE ${idx}" for s_col in spec.searchable_fields]
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

                query = f"""
                    SELECT * FROM {spec.table_name}
                    WHERE {' AND '.join(where_clauses)}
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
            mem = _get_mem_bucket(spec, str(ns_uuid))
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
        ns_raw = arguments.get("namespace_id")
        item_id = arguments.get("id")
        if not ns_raw or not item_id:
            return json.dumps({"error": "Missing required argument: namespace_id or id"})
        try:
            ns_uuid = UUID(str(ns_raw))
        except ValueError as exc:
            return json.dumps({"error": f"Invalid namespace_id: {exc}"})

        item: dict[str, Any] | None = None
        if hasattr(engine, "pg_pool") and engine.pg_pool and spec.table_name:
            async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
                query = f"SELECT * FROM {spec.table_name} WHERE namespace_id = $1 AND {spec.id_field} = $2"
                row = await conn.fetchrow(query, ns_uuid, item_id)
                if row:
                    item = row_to_dict(row)
        else:
            mem = _get_mem_bucket(spec, str(ns_uuid))
            item = mem.get(str(item_id))

        if not item:
            return json.dumps({"error": f"{spec.node_type} {item_id} not found"})

        return json.dumps(item, default=str)

    async def handle_upsert(engine: Any, arguments: dict[str, Any]) -> str:
        ns_raw = arguments.get("namespace_id")
        if not ns_raw:
            return json.dumps({"error": "Missing required argument: namespace_id"})
        try:
            ns_uuid = UUID(str(ns_raw))
        except ValueError as exc:
            return json.dumps({"error": f"Invalid namespace_id: {exc}"})

        item_id = str(arguments.get("id") or uuid.uuid4())
        expected_version = arguments.get("expected_version")

        data: dict[str, Any] = {f: arguments[f] for f in spec.writable_fields if f in arguments}
        data[spec.id_field] = item_id
        data["namespace_id"] = str(ns_uuid)
        now_iso = datetime.now(timezone.utc).isoformat()
        if spec.version_field:
            data[spec.version_field] = now_iso

        if hasattr(engine, "pg_pool") and engine.pg_pool and spec.table_name:
            async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
                # Check for existing
                existing_row = await conn.fetchrow(
                    f"SELECT * FROM {spec.table_name} WHERE namespace_id = $1 AND {spec.id_field} = $2",
                    ns_uuid,
                    item_id,
                )
                if existing_row:
                    existing = row_to_dict(existing_row)
                    if expected_version and spec.version_field:
                        actual = str(existing.get(spec.version_field, ""))
                        if actual != str(expected_version):
                            return json.dumps(
                                {
                                    "error": f"Version conflict: expected {expected_version}, got {actual}",
                                    "reason": "version_conflict",
                                    "status_code": 409,
                                }
                            )

                    set_items = [f"{k} = ${i + 3}" for i, k in enumerate(data.keys())]
                    vals = list(data.values())
                    await conn.execute(
                        f"UPDATE {spec.table_name} SET {', '.join(set_items)} WHERE namespace_id = $1 AND {spec.id_field} = $2",
                        ns_uuid,
                        item_id,
                        *vals,
                    )
                else:
                    cols = list(data.keys())
                    vals = [data[c] for c in cols]
                    placeholders = [f"${i + 1}" for i in range(len(cols))]
                    await conn.execute(
                        f"INSERT INTO {spec.table_name} ({', '.join(cols)}) VALUES ({', '.join(placeholders)})",
                        *vals,
                    )
        else:
            mem = _get_mem_bucket(spec, str(ns_uuid))
            existing = mem.get(item_id)
            if existing and expected_version and spec.version_field:
                actual = str(existing.get(spec.version_field, ""))
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
            {"status": "ok", "id": item_id, "version": now_iso, "data": data}, default=str
        )

    async def handle_archive(engine: Any, arguments: dict[str, Any]) -> str:
        ns_raw = arguments.get("namespace_id")
        item_id = arguments.get("id")
        if not ns_raw or not item_id:
            return json.dumps({"error": "Missing required argument: namespace_id or id"})
        try:
            ns_uuid = UUID(str(ns_raw))
        except ValueError as exc:
            return json.dumps({"error": f"Invalid namespace_id: {exc}"})

        field_name = spec.soft_delete_field or "is_archived"
        if hasattr(engine, "pg_pool") and engine.pg_pool and spec.table_name:
            async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
                res = await conn.execute(
                    f"UPDATE {spec.table_name} SET {field_name} = true WHERE namespace_id = $1 AND {spec.id_field} = $2",
                    ns_uuid,
                    item_id,
                )
                if res.endswith("0"):
                    return json.dumps({"error": f"{spec.node_type} {item_id} not found"})
        else:
            mem = _get_mem_bucket(spec, str(ns_uuid))
            if str(item_id) not in mem:
                return json.dumps({"error": f"{spec.node_type} {item_id} not found"})
            mem[str(item_id)][field_name] = True

        return json.dumps({"status": "ok", "id": item_id, "archived": True})

    # Wrap names and decorate with @mcp_handler
    handle_list.__name__ = f"handle_{list_tool_name}"
    handle_get.__name__ = f"handle_{get_tool_name}"
    handle_upsert.__name__ = f"handle_{upsert_tool_name}"
    handle_archive.__name__ = f"handle_{archive_tool_name}"

    return {
        list_tool_name: ToolSpec(mcp_handler(handle_list), cacheable=True, engine=spec.engine),
        get_tool_name: ToolSpec(mcp_handler(handle_get), cacheable=True, engine=spec.engine),
        upsert_tool_name: ToolSpec(mcp_handler(handle_upsert), mutation=True, engine=spec.engine),
        archive_tool_name: ToolSpec(mcp_handler(handle_archive), mutation=True, engine=spec.engine),
    }
