"""nce.resource_surface.rest — Starlette REST route generator for C12 resources.

Phase A Wave A-1:
Constructs uniform Starlette Route instances for any declared ResourceSpec:
  - GET /api/{engine}/{entity} (list with filters, q search, sort, cursor pagination, fields projection)
  - GET /api/{engine}/{entity}/{id} (detail with tier redaction)
  - POST /api/{engine}/{entity} (create with writable field validation)
  - PATCH /api/{engine}/{entity}/{id} (update with If-Match / expected_version 409 concurrency)
  - POST /api/{engine}/{entity}/{id}/archive (soft archive)
  - POST /api/{engine}/{entity}/{id}/restore (soft restore)
  - GET /api/{engine}/{entity}/{id}/events (timeline via event_log)
  - GET|POST /api/{engine}/{entity}/{id}/comments
  - GET|POST|DELETE /api/{engine}/{entity}/{id}/tags
  - POST /api/{engine}/{entity}/bulk

CRITICAL MANDATE:
All error responses MUST call admin_error_response. Never inline an error dict.
"""

from __future__ import annotations

import base64
import logging
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from nce import admin_state
from nce.admin_handlers._shared import bump_mcp_cache_generation
from nce.admin_http_support import admin_error_response
from nce.auth import NamespaceContext, set_namespace_context
from nce.db_utils import scoped_pg_session
from nce.resource_surface.comments import (
    add_entity_tag,
    append_entity_comment,
    fetch_entity_comments,
    fetch_entity_tags,
    remove_entity_tag,
)
from nce.resource_surface.events import fetch_entity_events
from nce.resource_surface.spec import ResourceSpec

log = logging.getLogger(__name__)

_SENSITIVE_FIELD_SUBSTRINGS = ("margin", "cost", "bid")

# In-memory mock store used when admin_state.engine or pg_pool is absent (e.g. unit test mocks)
_MEM_STORE: dict[str, dict[str, dict[str, Any]]] = {}
_MEM_COMMENTS: dict[str, list[dict[str, Any]]] = {}
_MEM_TAGS: dict[str, set[str]] = {}


def _get_mem_bucket(spec: ResourceSpec, ns: str | None = None) -> dict[str, Any]:
    if spec.tenant_scope == "global":
        key = f"{spec.engine}:{spec.entity}:__global__"
    elif spec.tenant_scope == "graph":
        key = f"{spec.engine}:{spec.entity}:{ns or '__graph__'}"
    else:
        key = f"{spec.engine}:{spec.entity}:{ns}"
    if key not in _MEM_STORE:
        _MEM_STORE[key] = {}
    return _MEM_STORE[key]


def _clear_mem_store() -> None:
    _MEM_STORE.clear()
    _MEM_COMMENTS.clear()
    _MEM_TAGS.clear()
    try:
        from nce.vertical_modules.documents.service import _clear_mem_store as _clear_docs

        _clear_docs()
    except Exception:
        pass
    try:
        from nce.vertical_modules.legal_entities.service import _clear_mem_store as _clear_le

        _clear_le()
    except Exception:
        pass


def serialize_val(val: Any) -> Any:
    """Serialize database values to JSON-safe representations."""
    if isinstance(val, (datetime, date)):
        return val.isoformat()
    if isinstance(val, UUID):
        return str(val)
    if isinstance(val, Decimal):
        return float(val)
    if isinstance(val, (bytes, bytearray)):
        return base64.b64encode(val).decode("ascii")
    if isinstance(val, dict):
        return {k: serialize_val(v) for k, v in val.items()}
    if isinstance(val, list):
        return [serialize_val(v) for v in val]
    return val


def _version_str(value: Any) -> str:
    """Normalise a stored version_field value to the isoformat() shape the
    client is actually handed back. Duplicated from
    ``nce.resource_surface.mcp`` (which imports FROM this module, so the
    reverse import would be circular) -- see that module's copy for the full
    reasoning: a real Postgres row round-trips version_field as a
    ``datetime``, and ``str(a_datetime)`` uses a space separator where
    ``isoformat()`` uses "T", so comparing the two forms directly makes every
    expected_version check on a real Postgres-backed resource fail with a
    spurious conflict.
    """
    return value.isoformat() if isinstance(value, datetime) else str(value)


def row_to_dict(row: Any) -> dict[str, Any]:
    """Convert an asyncpg Record or mapping to a JSON-serializable dict."""
    if hasattr(row, "keys"):
        return {k: serialize_val(row[k]) for k in row.keys()}
    if isinstance(row, dict):
        return {k: serialize_val(v) for k, v in row.items()}
    return dict(row)


def redact_item(item: dict[str, Any], spec: ResourceSpec, principal_tier: str) -> dict[str, Any]:
    """Apply C3/C8 principal tier redaction to an item dict."""
    # Employee tier has full visibility
    if principal_tier == "employee":
        return dict(item)

    tier_allowed = spec.tier_allowlists.get(principal_tier)
    out: dict[str, Any] = {}
    for k, v in item.items():
        # Strip sensitive keywords from non-employee tiers
        lower_k = k.lower()
        if any(sub in lower_k for sub in _SENSITIVE_FIELD_SUBSTRINGS):
            continue
        if tier_allowed is not None and k not in tier_allowed:
            continue
        out[k] = v
    return out


def resolve_principal_tier(request: Request) -> str:
    """Derive principal tier from request headers or auth state."""
    tier = (
        (
            request.headers.get("X-NCE-Principal-Tier")
            or request.headers.get("X-NCE-Principal-Kind")
            or ""
        )
        .strip()
        .lower()
    )
    if tier in ("employee", "contractor", "external-customer"):
        return tier
    state_kind = getattr(request.state, "principal_kind", None)
    if state_kind in ("employee", "contractor", "external-customer"):
        return state_kind
    return "employee"


def extract_namespace_id(
    request: Request, body: dict[str, Any] | None = None, required: bool = True
) -> tuple[UUID | None, JSONResponse | None]:
    """Extract and validate namespace_id from query params, body, or headers."""
    raw = request.query_params.get("namespace_id")
    if not raw and body and isinstance(body, dict):
        raw = body.get("namespace_id")
    if not raw:
        raw = request.headers.get("X-NCE-Namespace-ID")

    if not raw or not str(raw).strip():
        if not required:
            return None, None
        exc = ValueError("Missing required query param: namespace_id")
        return None, admin_error_response(
            "Missing required query param: namespace_id", exc, status_code=422
        )

    try:
        ns_uuid = UUID(str(raw).strip())
        return ns_uuid, None
    except ValueError as exc:
        return None, admin_error_response(f"Invalid namespace_id: {exc}", exc, status_code=422)


def make_resource_routes(spec: ResourceSpec) -> list[Route]:
    """Generate all Starlette Route definitions for a ResourceSpec."""

    prefix = spec.rest_collection_path
    is_tenant = spec.tenant_scope == "tenant"
    is_global = spec.tenant_scope == "global"
    is_graph = spec.tenant_scope == "graph"

    async def handle_list(request: Request) -> Response:
        ns_uuid, err_resp = extract_namespace_id(request, required=is_tenant)
        if err_resp:
            return err_resp
        if ns_uuid is not None:
            try:
                set_namespace_context(NamespaceContext(namespace_id=ns_uuid))
            except Exception:
                pass

        tier = resolve_principal_tier(request)
        limit = 50
        try:
            raw_limit = request.query_params.get("limit")
            if raw_limit:
                limit = max(1, min(int(raw_limit), 500))
        except ValueError as exc:
            return admin_error_response("Invalid limit parameter", exc, status_code=400)

        cursor = request.query_params.get("cursor")
        q = request.query_params.get("q")
        fields_filter = request.query_params.get("fields")
        allowed_fields = [f.strip() for f in fields_filter.split(",")] if fields_filter else None

        # Filter params from declared filterable fields
        active_filters = {
            f: request.query_params.get(f)
            for f in spec.filterable_fields
            if request.query_params.get(f) is not None
        }

        # Query live database if pool available, else memory store
        items: list[dict[str, Any]] = []
        next_cursor: str | None = None

        if admin_state.engine and getattr(admin_state.engine, "pg_pool", None):
            if spec.storage_kind in ("mongo", "kg_nodes") or is_graph:
                exc = NotImplementedError(
                    f"{spec.storage_kind} backend storage is not supported yet for {spec.entity}"
                )
                return admin_error_response(
                    f"{spec.storage_kind} backend storage is not supported yet for {spec.entity}",
                    exc,
                    status_code=501,
                )
            if spec.table_name:
                try:
                    session_ns = ns_uuid or UUID("00000000-0000-0000-0000-000000000000")
                    async with scoped_pg_session(admin_state.engine.pg_pool, session_ns) as conn:
                        if is_global:
                            where_clauses: list[str] = []
                            params: list[Any] = []
                            idx = 1
                        else:
                            where_clauses = ["namespace_id = $1"]
                            params = [ns_uuid]
                            idx = 2

                        if spec.soft_delete_field:
                            include_archived = (
                                request.query_params.get("include_archived", "false").lower()
                                == "true"
                            )
                            if not include_archived:
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
                                dec = base64.b64decode(cursor).decode("utf-8")
                                cursor_id = dec
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
                except Exception as exc:
                    return admin_error_response(
                        f"Failed to query {spec.entity}: {exc}", exc, status_code=500
                    )
        else:
            # In-memory fallback
            mem = _get_mem_bucket(spec, str(ns_uuid) if ns_uuid else None)
            raw_items = list(mem.values())
            # Apply filters
            soft_del_field = spec.soft_delete_field or "is_archived"
            filtered = []
            for it in raw_items:
                if not request.query_params.get("include_archived", "false").lower() == "true":
                    if it.get(soft_del_field) is True or it.get(soft_del_field) == "archived":
                        continue
                match = True
                for f_col, f_val in active_filters.items():
                    val_str = str(it.get(f_col, ""))
                    query_str = str(f_val)
                    if val_str.lower() != query_str.lower():
                        match = False
                        break
                if match and q and spec.searchable_fields:
                    text_corpus = " ".join(str(it.get(c, "")) for c in spec.searchable_fields)
                    if q.lower() not in text_corpus.lower():
                        match = False
                if match:
                    filtered.append(it)
            items = filtered[:limit]

        # Apply tier redactions and fields projection
        processed_items = []
        for it in items:
            redacted = redact_item(it, spec, tier)
            if allowed_fields:
                redacted = {k: v for k, v in redacted.items() if k in allowed_fields}
            processed_items.append(redacted)

        return JSONResponse(
            {"items": processed_items, "next_cursor": next_cursor, "total": len(processed_items)}
        )

    async def handle_get(request: Request) -> Response:
        ns_uuid, err_resp = extract_namespace_id(request, required=is_tenant)
        if err_resp:
            return err_resp

        item_id = request.path_params.get("id")
        if not item_id:
            return admin_error_response(
                "Missing id path parameter", ValueError("Missing id"), status_code=400
            )

        tier = resolve_principal_tier(request)

        item: dict[str, Any] | None = None
        if admin_state.engine and getattr(admin_state.engine, "pg_pool", None):
            if spec.storage_kind in ("mongo", "kg_nodes") or is_graph:
                exc = NotImplementedError(
                    f"{spec.storage_kind} backend storage is not supported yet for {spec.entity}"
                )
                return admin_error_response(
                    f"{spec.storage_kind} backend storage is not supported yet for {spec.entity}",
                    exc,
                    status_code=501,
                )
            if spec.table_name:
                try:
                    session_ns = ns_uuid or UUID("00000000-0000-0000-0000-000000000000")
                    async with scoped_pg_session(admin_state.engine.pg_pool, session_ns) as conn:
                        if is_global:
                            query = f"SELECT * FROM {spec.table_name} WHERE {spec.id_field} = $1"
                            row = await conn.fetchrow(query, item_id)
                        else:
                            query = f"SELECT * FROM {spec.table_name} WHERE namespace_id = $1 AND {spec.id_field} = $2"
                            row = await conn.fetchrow(query, ns_uuid, item_id)
                        if row:
                            item = row_to_dict(row)
                except Exception as exc:
                    return admin_error_response(
                        f"Database fetch error: {exc}", exc, status_code=500
                    )
        else:
            mem = _get_mem_bucket(spec, str(ns_uuid) if ns_uuid else None)
            item = mem.get(str(item_id))

        if not item:
            return admin_error_response(
                f"{spec.node_type} {item_id} not found",
                KeyError(f"{spec.node_type} not found"),
                status_code=404,
            )

        redacted = redact_item(item, spec, tier)
        return JSONResponse(redacted)

    async def handle_create(request: Request) -> Response:
        try:
            body = await request.json()
        except Exception as exc:
            return admin_error_response("Malformed JSON body", exc, status_code=400)

        tier = resolve_principal_tier(request)
        if is_global and tier == "external-customer":
            exc = PermissionError("External customers cannot modify global catalog resources")
            return admin_error_response(
                "External customers cannot modify global resources", exc, status_code=403
            )

        ns_uuid, err_resp = extract_namespace_id(request, body, required=is_tenant)
        if err_resp:
            return err_resp

        # Filter to allowed writable fields
        data: dict[str, Any] = {}
        for f in spec.writable_fields:
            if f in body:
                data[f] = body[f]

        import uuid

        item_id = str(body.get(spec.id_field) or uuid.uuid4())
        data[spec.id_field] = item_id
        if not is_global or "namespace_id" in spec.writable_fields:
            if ns_uuid:
                data["namespace_id"] = str(ns_uuid)
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        if spec.version_field:
            # Kept as the isoformat() string in `data` itself -- `data` also
            # becomes the in-memory-fallback row below, and every OTHER
            # handler reading that shared bucket (handle_get, handle_list,
            # handle_archive, ...) returns it via a bare JSONResponse with no
            # datetime fallback (unlike mcp.py, which wraps every response in
            # json.dumps(..., default=str)). Storing a real datetime here
            # would fix this handler and break all of those. The real
            # datetime is bound only at the actual SQL parameter list below,
            # which is the only place that needs one.
            data[spec.version_field] = now_iso
        if spec.soft_delete_field and spec.soft_delete_field not in data:
            data[spec.soft_delete_field] = False

        if admin_state.engine and getattr(admin_state.engine, "pg_pool", None):
            if spec.storage_kind in ("mongo", "kg_nodes") or is_graph:
                exc = NotImplementedError(
                    f"{spec.storage_kind} backend storage is not supported yet for {spec.entity}"
                )
                return admin_error_response(
                    f"{spec.storage_kind} backend storage is not supported yet for {spec.entity}",
                    exc,
                    status_code=501,
                )
            if spec.table_name:
                try:
                    session_ns = ns_uuid or UUID("00000000-0000-0000-0000-000000000000")
                    async with scoped_pg_session(admin_state.engine.pg_pool, session_ns) as conn:
                        cols = list(data.keys())
                        vals = [now if c == spec.version_field else data[c] for c in cols]
                        val_placeholders = [f"${i + 1}" for i in range(len(cols))]
                        query = f"""
                            INSERT INTO {spec.table_name} ({', '.join(cols)})
                            VALUES ({', '.join(val_placeholders)})
                            RETURNING *
                        """
                        row = await conn.fetchrow(query, *vals)
                        created = row_to_dict(row) if row else data
                except Exception as exc:
                    return admin_error_response(
                        f"Failed to create {spec.entity}: {exc}", exc, status_code=500
                    )
        else:
            mem = _get_mem_bucket(spec, str(ns_uuid) if ns_uuid else None)
            if "created_at" not in data:
                data["created_at"] = now_iso
            mem[item_id] = dict(data)
            created = dict(data)
            if spec.engine == "documents" and spec.entity == "documents":
                try:
                    from nce.vertical_modules.documents.models import DocumentRecord
                    from nce.vertical_modules.documents.service import _MEM_DOCUMENTS

                    doc_rec = DocumentRecord(
                        id=UUID(item_id),
                        namespace_id=ns_uuid or UUID("00000000-0000-0000-0000-000000000000"),
                        title=str(data.get("title", "")),
                        document_ref=str(data.get("document_ref", "")),
                        source_kind=str(data.get("source_kind", "sharepoint")),
                        document_kind=str(data.get("document_kind", "other")),
                        file_name=data.get("file_name"),
                        mime_type=data.get("mime_type"),
                        file_size_bytes=data.get("file_size_bytes"),
                        sha256=data.get("sha256"),
                        tags=tuple(data.get("tags") or ()),
                        metadata=dict(data.get("metadata") or {}),
                        archived=bool(data.get("archived", False)),
                        created_at=datetime.fromisoformat(now_iso),
                        updated_at=datetime.fromisoformat(now_iso),
                    )
                    _MEM_DOCUMENTS.setdefault(str(ns_uuid), {})[item_id] = doc_rec
                except Exception:
                    pass
            elif spec.engine == "legal_entities" and spec.entity == "legal_entities":
                try:
                    from nce.vertical_modules.legal_entities.models import LegalEntityRecord
                    from nce.vertical_modules.legal_entities.service import _MEM_LEGAL_ENTITIES

                    le_rec = LegalEntityRecord(
                        id=UUID(item_id),
                        namespace_id=ns_uuid or UUID("00000000-0000-0000-0000-000000000000"),
                        org_nr=str(data.get("org_nr", "")),
                        name=str(data.get("name", "")),
                        group_parent_org_nr=data.get("group_parent_org_nr"),
                        roles=tuple(data.get("roles") or ()),
                        country=str(data.get("country", "NO")),
                        metadata=dict(data.get("metadata") or {}),
                        archived=bool(data.get("archived", False)),
                        created_at=datetime.fromisoformat(now_iso),
                        updated_at=datetime.fromisoformat(now_iso),
                    )
                    _MEM_LEGAL_ENTITIES.setdefault(str(ns_uuid), {})[item_id] = le_rec
                except Exception:
                    pass

        if admin_state.engine:
            await bump_mcp_cache_generation(
                admin_state.engine,
                engine_name=spec.engine,
                route=f"api_{spec.engine}_{spec.mcp_slug}_create",
            )

        return JSONResponse(
            serialize_val(
                {"status": "ok", "id": item_id, "version": now_iso, "item": created, **created}
            ),
            status_code=201,
        )

    async def handle_patch(request: Request) -> Response:
        try:
            body = await request.json()
        except Exception as exc:
            return admin_error_response("Malformed JSON body", exc, status_code=400)

        tier = resolve_principal_tier(request)
        if is_global and tier == "external-customer":
            exc = PermissionError("External customers cannot modify global catalog resources")
            return admin_error_response(
                "External customers cannot modify global resources", exc, status_code=403
            )

        ns_uuid, err_resp = extract_namespace_id(request, body, required=is_tenant)
        if err_resp:
            return err_resp

        item_id = request.path_params.get("id")
        if not item_id:
            return admin_error_response(
                "Missing id path parameter", ValueError("Missing id"), status_code=400
            )

        # Optimistic concurrency check via If-Match header or expected_version parameter
        if_match = request.headers.get("If-Match")
        expected_version = body.get("expected_version") or if_match
        if expected_version:
            expected_version = str(expected_version).strip().strip('"')

        # Fetch existing record
        existing: dict[str, Any] | None = None
        if admin_state.engine and getattr(admin_state.engine, "pg_pool", None):
            if spec.storage_kind in ("mongo", "kg_nodes") or is_graph:
                exc = NotImplementedError(
                    f"{spec.storage_kind} backend storage is not supported yet for {spec.entity}"
                )
                return admin_error_response(
                    f"{spec.storage_kind} backend storage is not supported yet for {spec.entity}",
                    exc,
                    status_code=501,
                )
            if spec.table_name:
                try:
                    session_ns = ns_uuid or UUID("00000000-0000-0000-0000-000000000000")
                    async with scoped_pg_session(admin_state.engine.pg_pool, session_ns) as conn:
                        if is_global:
                            query = f"SELECT * FROM {spec.table_name} WHERE {spec.id_field} = $1"
                            row = await conn.fetchrow(query, item_id)
                        else:
                            query = f"SELECT * FROM {spec.table_name} WHERE namespace_id = $1 AND {spec.id_field} = $2"
                            row = await conn.fetchrow(query, ns_uuid, item_id)
                        if row:
                            existing = row_to_dict(row)
                except Exception as exc:
                    return admin_error_response(f"Database error: {exc}", exc, status_code=500)
        else:
            mem = _get_mem_bucket(spec, str(ns_uuid) if ns_uuid else None)
            existing = mem.get(str(item_id))

        if not existing:
            return admin_error_response(
                f"{spec.node_type} {item_id} not found",
                KeyError("Resource not found"),
                status_code=404,
            )

        if expected_version and spec.version_field:
            actual_version = _version_str(existing.get(spec.version_field, ""))
            if actual_version != expected_version:
                exc = ValueError(
                    f"Version conflict: expected {expected_version}, got {actual_version}"
                )
                return admin_error_response(
                    "Version conflict",
                    exc,
                    status_code=409,
                    extra={
                        "reason": "version_conflict",
                        "parameter": "expected_version",
                        "expected_version": expected_version,
                        "actual_version": actual_version,
                        "current_version": actual_version,
                    },
                )

        # Update writable fields
        updates: dict[str, Any] = {}
        for f in spec.writable_fields:
            if f in body:
                updates[f] = body[f]

        now = datetime.now(timezone.utc)
        if spec.version_field:
            # String in `updates` for the same reason as handle_create above:
            # `updates` also becomes (via {**existing, **updates}) the
            # in-memory-fallback row, read back by other handlers with no
            # datetime-safe JSON fallback. Real datetime bound only in `vals`
            # below, for the actual SQL parameter list.
            updates[spec.version_field] = now.isoformat()

        if admin_state.engine and getattr(admin_state.engine, "pg_pool", None) and spec.table_name:
            try:
                session_ns = ns_uuid or UUID("00000000-0000-0000-0000-000000000000")
                async with scoped_pg_session(admin_state.engine.pg_pool, session_ns) as conn:
                    vals = [now if k == spec.version_field else v for k, v in updates.items()]
                    if is_global:
                        set_items = [f"{k} = ${i + 2}" for i, k in enumerate(updates.keys())]
                        query = f"""
                            UPDATE {spec.table_name}
                            SET {', '.join(set_items)}
                            WHERE {spec.id_field} = $1
                            RETURNING *
                        """
                        row = await conn.fetchrow(query, item_id, *vals)
                    else:
                        set_items = [f"{k} = ${i + 3}" for i, k in enumerate(updates.keys())]
                        query = f"""
                            UPDATE {spec.table_name}
                            SET {', '.join(set_items)}
                            WHERE namespace_id = $1 AND {spec.id_field} = $2
                            RETURNING *
                        """
                        row = await conn.fetchrow(query, ns_uuid, item_id, *vals)
                    updated = row_to_dict(row) if row else {**existing, **updates}
            except Exception as exc:
                return admin_error_response(
                    f"Failed to update {spec.entity}: {exc}", exc, status_code=500
                )
        else:
            mem = _get_mem_bucket(spec, str(ns_uuid) if ns_uuid else None)
            updated = {**existing, **updates}
            mem[str(item_id)] = updated

        if admin_state.engine:
            await bump_mcp_cache_generation(
                admin_state.engine,
                engine_name=spec.engine,
                route=f"api_{spec.engine}_{spec.mcp_slug}_patch",
            )

        return JSONResponse(
            serialize_val({"status": "ok", "id": item_id, "item": updated, **updated})
        )

    async def handle_archive(request: Request) -> Response:
        body: dict[str, Any] = {}
        try:
            body = await request.json()
        except Exception:
            pass

        tier = resolve_principal_tier(request)
        if is_global and tier == "external-customer":
            exc = PermissionError("External customers cannot modify global catalog resources")
            return admin_error_response(
                "External customers cannot modify global resources", exc, status_code=403
            )

        ns_uuid, err_resp = extract_namespace_id(request, body, required=is_tenant)
        if err_resp:
            return err_resp

        item_id = request.path_params.get("id")
        if not item_id:
            return admin_error_response(
                "Missing id parameter", ValueError("Missing id"), status_code=400
            )

        field_name = spec.soft_delete_field or "is_archived"
        if admin_state.engine and getattr(admin_state.engine, "pg_pool", None):
            if spec.storage_kind in ("mongo", "kg_nodes") or is_graph:
                exc = NotImplementedError(
                    f"{spec.storage_kind} backend storage is not supported yet for {spec.entity}"
                )
                return admin_error_response(
                    f"{spec.storage_kind} backend storage is not supported yet for {spec.entity}",
                    exc,
                    status_code=501,
                )
            if spec.table_name:
                try:
                    session_ns = ns_uuid or UUID("00000000-0000-0000-0000-000000000000")
                    async with scoped_pg_session(admin_state.engine.pg_pool, session_ns) as conn:
                        if is_global:
                            query = f"""
                                UPDATE {spec.table_name}
                                SET {field_name} = true
                                WHERE {spec.id_field} = $1
                                RETURNING *
                            """
                            row = await conn.fetchrow(query, item_id)
                        else:
                            query = f"""
                                UPDATE {spec.table_name}
                                SET {field_name} = true
                                WHERE namespace_id = $1 AND {spec.id_field} = $2
                                RETURNING *
                            """
                            row = await conn.fetchrow(query, ns_uuid, item_id)
                        if not row:
                            return admin_error_response(
                                "Resource not found", KeyError("Not found"), status_code=404
                            )
                except Exception as exc:
                    return admin_error_response(f"Failed to archive: {exc}", exc, status_code=500)
        else:
            mem = _get_mem_bucket(spec, str(ns_uuid) if ns_uuid else None)
            if str(item_id) not in mem:
                return admin_error_response(
                    "Resource not found", KeyError("Not found"), status_code=404
                )
            mem[str(item_id)][field_name] = True

        if admin_state.engine:
            await bump_mcp_cache_generation(
                admin_state.engine,
                engine_name=spec.engine,
                route=f"api_{spec.engine}_{spec.mcp_slug}_archive",
            )

        return JSONResponse({"status": "ok", "id": item_id, "archived": True})

    async def handle_restore(request: Request) -> Response:
        body: dict[str, Any] = {}
        try:
            body = await request.json()
        except Exception:
            pass

        tier = resolve_principal_tier(request)
        if is_global and tier == "external-customer":
            exc = PermissionError("External customers cannot modify global catalog resources")
            return admin_error_response(
                "External customers cannot modify global resources", exc, status_code=403
            )

        ns_uuid, err_resp = extract_namespace_id(request, body, required=is_tenant)
        if err_resp:
            return err_resp

        item_id = request.path_params.get("id")
        if not item_id:
            return admin_error_response(
                "Missing id parameter", ValueError("Missing id"), status_code=400
            )

        field_name = spec.soft_delete_field or "is_archived"
        if admin_state.engine and getattr(admin_state.engine, "pg_pool", None):
            if spec.storage_kind in ("mongo", "kg_nodes") or is_graph:
                exc = NotImplementedError(
                    f"{spec.storage_kind} backend storage is not supported yet for {spec.entity}"
                )
                return admin_error_response(
                    f"{spec.storage_kind} backend storage is not supported yet for {spec.entity}",
                    exc,
                    status_code=501,
                )
            if spec.table_name:
                try:
                    session_ns = ns_uuid or UUID("00000000-0000-0000-0000-000000000000")
                    async with scoped_pg_session(admin_state.engine.pg_pool, session_ns) as conn:
                        if is_global:
                            query = f"""
                                UPDATE {spec.table_name}
                                SET {field_name} = false
                                WHERE {spec.id_field} = $1
                                RETURNING *
                            """
                            row = await conn.fetchrow(query, item_id)
                        else:
                            query = f"""
                                UPDATE {spec.table_name}
                                SET {field_name} = false
                                WHERE namespace_id = $1 AND {spec.id_field} = $2
                                RETURNING *
                            """
                            row = await conn.fetchrow(query, ns_uuid, item_id)
                        if not row:
                            return admin_error_response(
                                "Resource not found", KeyError("Not found"), status_code=404
                            )
                except Exception as exc:
                    return admin_error_response(f"Failed to restore: {exc}", exc, status_code=500)
        else:
            mem = _get_mem_bucket(spec, str(ns_uuid) if ns_uuid else None)
            if str(item_id) not in mem:
                return admin_error_response(
                    "Resource not found", KeyError("Not found"), status_code=404
                )
            mem[str(item_id)][field_name] = False

        if admin_state.engine:
            await bump_mcp_cache_generation(
                admin_state.engine,
                engine_name=spec.engine,
                route=f"api_{spec.engine}_{spec.mcp_slug}_restore",
            )

        return JSONResponse({"status": "ok", "id": item_id, "archived": False})

    async def handle_events(request: Request) -> Response:
        ns_uuid, err_resp = extract_namespace_id(request, required=is_tenant)
        if err_resp:
            return err_resp

        item_id = request.path_params.get("id")
        if not item_id:
            return admin_error_response(
                "Missing id parameter", ValueError("Missing id"), status_code=400
            )

        if admin_state.engine and getattr(admin_state.engine, "pg_pool", None):
            if spec.storage_kind in ("mongo", "kg_nodes") or is_graph:
                exc = NotImplementedError(
                    f"{spec.storage_kind} backend storage is not supported yet for {spec.entity}"
                )
                return admin_error_response(
                    f"{spec.storage_kind} backend storage is not supported yet for {spec.entity}",
                    exc,
                    status_code=501,
                )
            try:
                session_ns = ns_uuid or UUID("00000000-0000-0000-0000-000000000000")
                async with scoped_pg_session(admin_state.engine.pg_pool, session_ns) as conn:
                    events = await fetch_entity_events(conn, session_ns, spec, str(item_id))
                    return JSONResponse({"events": events, "count": len(events)})
            except Exception as exc:
                return admin_error_response(f"Failed to query events: {exc}", exc, status_code=500)
        return JSONResponse({"events": [], "count": 0})

    async def handle_list_comments(request: Request) -> Response:
        ns_uuid, err_resp = extract_namespace_id(request, required=is_tenant)
        if err_resp:
            return err_resp

        item_id = request.path_params.get("id")
        if not item_id:
            return admin_error_response(
                "Missing id parameter", ValueError("Missing id"), status_code=400
            )

        if admin_state.engine and getattr(admin_state.engine, "pg_pool", None):
            if spec.storage_kind in ("mongo", "kg_nodes") or is_graph:
                exc = NotImplementedError(
                    f"{spec.storage_kind} backend storage is not supported yet for {spec.entity}"
                )
                return admin_error_response(
                    f"{spec.storage_kind} backend storage is not supported yet for {spec.entity}",
                    exc,
                    status_code=501,
                )
            try:
                session_ns = ns_uuid or UUID("00000000-0000-0000-0000-000000000000")
                async with scoped_pg_session(admin_state.engine.pg_pool, session_ns) as conn:
                    comments = await fetch_entity_comments(conn, session_ns, spec, str(item_id))
                    return JSONResponse({"comments": comments, "count": len(comments)})
            except Exception as exc:
                return admin_error_response(
                    f"Failed to query comments: {exc}", exc, status_code=500
                )
        mem_key = f"{ns_uuid or '__global__'}:{item_id}"
        mem_comments = _MEM_COMMENTS.get(mem_key, [])
        return JSONResponse({"comments": mem_comments, "count": len(mem_comments)})

    async def handle_add_comment(request: Request) -> Response:
        try:
            body = await request.json()
        except Exception as exc:
            return admin_error_response("Malformed JSON body", exc, status_code=400)

        ns_uuid, err_resp = extract_namespace_id(request, body, required=is_tenant)
        if err_resp:
            return err_resp

        item_id = request.path_params.get("id")
        comment_text = body.get("comment") or body.get("body")
        if not item_id or not comment_text:
            return admin_error_response(
                "Missing required id or comment text",
                ValueError("Validation error"),
                status_code=400,
            )

        author = body.get("author", "operator")
        if admin_state.engine and getattr(admin_state.engine, "pg_pool", None):
            if spec.storage_kind in ("mongo", "kg_nodes") or is_graph:
                exc = NotImplementedError(
                    f"{spec.storage_kind} backend storage is not supported yet for {spec.entity}"
                )
                return admin_error_response(
                    f"{spec.storage_kind} backend storage is not supported yet for {spec.entity}",
                    exc,
                    status_code=501,
                )
            try:
                session_ns = ns_uuid or UUID("00000000-0000-0000-0000-000000000000")
                async with scoped_pg_session(admin_state.engine.pg_pool, session_ns) as conn:
                    res = await append_entity_comment(
                        conn, session_ns, spec, str(item_id), str(comment_text), str(author)
                    )
                    return JSONResponse({"status": "ok", "comment": res}, status_code=201)
            except Exception as exc:
                return admin_error_response(f"Failed to add comment: {exc}", exc, status_code=500)
        mem_key = f"{ns_uuid or '__global__'}:{item_id}"
        import uuid

        entry = {
            "id": str(uuid.uuid4()),
            "entity_id": item_id,
            "comment": comment_text,
            "body": comment_text,
            "author": author,
        }
        _MEM_COMMENTS.setdefault(mem_key, []).append(entry)
        return JSONResponse({"status": "ok", "comment": entry}, status_code=201)

    async def handle_list_tags(request: Request) -> Response:
        ns_uuid, err_resp = extract_namespace_id(request, required=is_tenant)
        if err_resp:
            return err_resp

        item_id = request.path_params.get("id")
        if not item_id:
            return admin_error_response(
                "Missing id parameter", ValueError("Missing id"), status_code=400
            )

        if admin_state.engine and getattr(admin_state.engine, "pg_pool", None):
            if spec.storage_kind in ("mongo", "kg_nodes") or is_graph:
                exc = NotImplementedError(
                    f"{spec.storage_kind} backend storage is not supported yet for {spec.entity}"
                )
                return admin_error_response(
                    f"{spec.storage_kind} backend storage is not supported yet for {spec.entity}",
                    exc,
                    status_code=501,
                )
            try:
                session_ns = ns_uuid or UUID("00000000-0000-0000-0000-000000000000")
                async with scoped_pg_session(admin_state.engine.pg_pool, session_ns) as conn:
                    tags = await fetch_entity_tags(conn, session_ns, spec, str(item_id))
                    return JSONResponse({"tags": tags, "count": len(tags)})
            except Exception as exc:
                return admin_error_response(f"Failed to query tags: {exc}", exc, status_code=500)
        mem_key = f"{ns_uuid or '__global__'}:{item_id}"
        tag_list = sorted(_MEM_TAGS.get(mem_key, set()))
        return JSONResponse({"tags": tag_list, "count": len(tag_list)})

    async def handle_add_tag(request: Request) -> Response:
        try:
            body = await request.json()
        except Exception as exc:
            return admin_error_response("Malformed JSON body", exc, status_code=400)

        ns_uuid, err_resp = extract_namespace_id(request, body, required=is_tenant)
        if err_resp:
            return err_resp

        item_id = request.path_params.get("id")
        tag = body.get("tag")
        if not item_id or not tag:
            return admin_error_response(
                "Missing required id or tag", ValueError("Validation error"), status_code=400
            )

        if admin_state.engine and getattr(admin_state.engine, "pg_pool", None):
            if spec.storage_kind in ("mongo", "kg_nodes") or is_graph:
                exc = NotImplementedError(
                    f"{spec.storage_kind} backend storage is not supported yet for {spec.entity}"
                )
                return admin_error_response(
                    f"{spec.storage_kind} backend storage is not supported yet for {spec.entity}",
                    exc,
                    status_code=501,
                )
            try:
                session_ns = ns_uuid or UUID("00000000-0000-0000-0000-000000000000")
                async with scoped_pg_session(admin_state.engine.pg_pool, session_ns) as conn:
                    tags = await add_entity_tag(conn, session_ns, spec, str(item_id), str(tag))
                    return JSONResponse({"status": "ok", "tags": tags})
            except Exception as exc:
                return admin_error_response(f"Failed to add tag: {exc}", exc, status_code=500)
        mem_key = f"{ns_uuid or '__global__'}:{item_id}"
        _MEM_TAGS.setdefault(mem_key, set()).add(str(tag))
        tag_list = sorted(_MEM_TAGS[mem_key])
        return JSONResponse({"status": "ok", "tags": tag_list})

    async def handle_remove_tag(request: Request) -> Response:
        ns_uuid, err_resp = extract_namespace_id(request, required=is_tenant)
        if err_resp:
            return err_resp

        item_id = request.path_params.get("id")
        tag = request.path_params.get("tag")
        if not item_id or not tag:
            return admin_error_response(
                "Missing required id or tag parameter",
                ValueError("Validation error"),
                status_code=400,
            )

        if admin_state.engine and getattr(admin_state.engine, "pg_pool", None):
            if spec.storage_kind in ("mongo", "kg_nodes") or is_graph:
                exc = NotImplementedError(
                    f"{spec.storage_kind} backend storage is not supported yet for {spec.entity}"
                )
                return admin_error_response(
                    f"{spec.storage_kind} backend storage is not supported yet for {spec.entity}",
                    exc,
                    status_code=501,
                )
            try:
                session_ns = ns_uuid or UUID("00000000-0000-0000-0000-000000000000")
                async with scoped_pg_session(admin_state.engine.pg_pool, session_ns) as conn:
                    tags = await remove_entity_tag(conn, session_ns, spec, str(item_id), str(tag))
                    return JSONResponse({"status": "ok", "tags": tags})
            except Exception as exc:
                return admin_error_response(f"Failed to remove tag: {exc}", exc, status_code=500)
        mem_key = f"{ns_uuid or '__global__'}:{item_id}"
        if mem_key in _MEM_TAGS and str(tag) in _MEM_TAGS[mem_key]:
            _MEM_TAGS[mem_key].remove(str(tag))
        tag_list = sorted(_MEM_TAGS.get(mem_key, set()))
        return JSONResponse({"status": "ok", "tags": tag_list})

    async def handle_list_documents(request: Request) -> Response:
        ns_uuid, err_resp = extract_namespace_id(request, required=is_tenant)
        if err_resp:
            return err_resp

        item_id = request.path_params.get("id")
        if not item_id:
            return admin_error_response(
                "Missing id parameter", ValueError("Missing id"), status_code=400
            )

        tier = resolve_principal_tier(request)
        include_archived = request.query_params.get("include_archived", "").strip().lower() in (
            "true",
            "1",
        )

        from nce.vertical_modules.documents.resources import DOCUMENT_SPEC
        from nce.vertical_modules.documents.service import list_entity_documents

        session_ns = ns_uuid or UUID("00000000-0000-0000-0000-000000000000")
        if admin_state.engine and getattr(admin_state.engine, "pg_pool", None):
            if spec.storage_kind in ("mongo", "kg_nodes") or is_graph:
                exc = NotImplementedError(
                    f"{spec.storage_kind} backend storage is not supported yet for {spec.entity}"
                )
                return admin_error_response(
                    f"{spec.storage_kind} backend storage is not supported yet for {spec.entity}",
                    exc,
                    status_code=501,
                )
            try:
                async with scoped_pg_session(admin_state.engine.pg_pool, session_ns) as conn:
                    docs = await list_entity_documents(
                        conn,
                        session_ns,
                        spec.node_type,
                        str(item_id),
                        include_archived=include_archived,
                    )
            except Exception as exc:
                return admin_error_response(
                    f"Failed to list entity documents: {exc}", exc, status_code=500
                )
        else:
            docs = await list_entity_documents(
                None,
                session_ns,
                spec.node_type,
                str(item_id),
                include_archived=include_archived,
            )

        redacted = [redact_item(d, DOCUMENT_SPEC, tier) for d in docs]
        return JSONResponse({"documents": redacted, "count": len(redacted)})

    async def handle_attach_document(request: Request) -> Response:
        try:
            body = await request.json()
        except Exception as exc:
            return admin_error_response("Malformed JSON body", exc, status_code=400)

        tier = resolve_principal_tier(request)
        if is_global and tier == "external-customer":
            exc = PermissionError("External customers cannot modify global catalog resources")
            return admin_error_response(
                "External customers cannot modify global resources", exc, status_code=403
            )

        ns_uuid, err_resp = extract_namespace_id(
            request, body if isinstance(body, dict) else None, required=is_tenant
        )
        if err_resp:
            return err_resp

        item_id = request.path_params.get("id")
        if not item_id:
            return admin_error_response(
                "Missing id parameter", ValueError("Missing id"), status_code=400
            )

        if not isinstance(body, dict):
            return admin_error_response(
                "Request body must be a JSON object",
                ValueError("Invalid body"),
                status_code=400,
            )

        relation = str(body.get("relation") or "about")
        doc_id_raw = body.get("document_id") or body.get("doc_id")
        doc_data = body.get("document") if isinstance(body.get("document"), dict) else None

        from nce.vertical_modules.documents.service import link_document, register_document

        session_ns = ns_uuid or UUID("00000000-0000-0000-0000-000000000000")

        if admin_state.engine and getattr(admin_state.engine, "pg_pool", None):
            if spec.storage_kind in ("mongo", "kg_nodes") or is_graph:
                exc = NotImplementedError(
                    f"{spec.storage_kind} backend storage is not supported yet for {spec.entity}"
                )
                return admin_error_response(
                    f"{spec.storage_kind} backend storage is not supported yet for {spec.entity}",
                    exc,
                    status_code=501,
                )
            try:
                async with scoped_pg_session(admin_state.engine.pg_pool, session_ns) as conn:
                    if doc_id_raw:
                        try:
                            doc_uuid = UUID(str(doc_id_raw))
                        except Exception as exc:
                            return admin_error_response(
                                f"Invalid document UUID: {doc_id_raw}", exc, status_code=400
                            )
                    elif doc_data or ("title" in body and "document_ref" in body):
                        dinfo = doc_data or body
                        title = dinfo.get("title")
                        doc_ref = dinfo.get("document_ref")
                        if not title or not doc_ref:
                            return admin_error_response(
                                "Missing required title or document_ref for document registration",
                                ValueError("Missing fields"),
                                status_code=400,
                            )
                        new_doc = await register_document(
                            conn,
                            session_ns,
                            str(title),
                            str(doc_ref),
                            source_kind=str(dinfo.get("source_kind", "sharepoint")),
                            document_kind=str(dinfo.get("document_kind", "other")),
                            file_name=dinfo.get("file_name"),
                            mime_type=dinfo.get("mime_type"),
                            file_size_bytes=dinfo.get("file_size_bytes"),
                            sha256=dinfo.get("sha256"),
                            tags=tuple(dinfo.get("tags") or ()),
                            metadata=dinfo.get("metadata") or {},
                        )
                        doc_uuid = new_doc.id
                    else:
                        return admin_error_response(
                            "Must provide document_id or document registration fields (title, document_ref)",
                            ValueError("Validation error"),
                            status_code=400,
                        )

                    link_rec = await link_document(
                        conn,
                        session_ns,
                        doc_uuid,
                        spec.node_type,
                        str(item_id),
                        relation=relation,
                    )
            except Exception as exc:
                return admin_error_response(
                    f"Failed to attach document: {exc}", exc, status_code=500
                )
        else:
            if doc_id_raw:
                try:
                    doc_uuid = UUID(str(doc_id_raw))
                except Exception as exc:
                    return admin_error_response(
                        f"Invalid document UUID: {doc_id_raw}", exc, status_code=400
                    )
            elif doc_data or ("title" in body and "document_ref" in body):
                dinfo = doc_data or body
                title = dinfo.get("title")
                doc_ref = dinfo.get("document_ref")
                if not title or not doc_ref:
                    return admin_error_response(
                        "Missing required title or document_ref for document registration",
                        ValueError("Missing fields"),
                        status_code=400,
                    )
                new_doc = await register_document(
                    None,
                    session_ns,
                    str(title),
                    str(doc_ref),
                    source_kind=str(dinfo.get("source_kind", "sharepoint")),
                    document_kind=str(dinfo.get("document_kind", "other")),
                    file_name=dinfo.get("file_name"),
                    mime_type=dinfo.get("mime_type"),
                    file_size_bytes=dinfo.get("file_size_bytes"),
                    sha256=dinfo.get("sha256"),
                    tags=tuple(dinfo.get("tags") or ()),
                    metadata=dinfo.get("metadata") or {},
                )
                doc_uuid = new_doc.id
            else:
                return admin_error_response(
                    "Must provide document_id or document registration fields (title, document_ref)",
                    ValueError("Validation error"),
                    status_code=400,
                )

            link_rec = await link_document(
                None, session_ns, doc_uuid, spec.node_type, str(item_id), relation=relation
            )

        if admin_state.engine:
            await bump_mcp_cache_generation(
                admin_state.engine,
                engine_name=spec.engine,
                route=f"api_{spec.engine}_{spec.mcp_slug}_documents",
            )

        return JSONResponse(
            {
                "status": "ok",
                "link_id": str(link_rec.id),
                "document_id": str(doc_uuid),
                "relation": link_rec.relation,
                "entity_type": link_rec.entity_type,
                "entity_id": link_rec.entity_id,
            },
            status_code=201,
        )

    async def handle_detach_document(request: Request) -> Response:
        tier = resolve_principal_tier(request)
        if is_global and tier == "external-customer":
            exc = PermissionError("External customers cannot modify global catalog resources")
            return admin_error_response(
                "External customers cannot modify global resources", exc, status_code=403
            )

        ns_uuid, err_resp = extract_namespace_id(request, required=is_tenant)
        if err_resp:
            return err_resp

        item_id = request.path_params.get("id")
        doc_id_raw = request.path_params.get("doc_id")
        if not item_id or not doc_id_raw:
            return admin_error_response(
                "Missing id or doc_id parameter",
                ValueError("Missing parameters"),
                status_code=400,
            )

        try:
            doc_uuid = UUID(str(doc_id_raw))
        except Exception as exc:
            return admin_error_response(
                f"Invalid document UUID: {doc_id_raw}", exc, status_code=400
            )

        from nce.vertical_modules.documents.service import unlink_document

        session_ns = ns_uuid or UUID("00000000-0000-0000-0000-000000000000")

        if admin_state.engine and getattr(admin_state.engine, "pg_pool", None):
            if spec.storage_kind in ("mongo", "kg_nodes") or is_graph:
                exc = NotImplementedError(
                    f"{spec.storage_kind} backend storage is not supported yet for {spec.entity}"
                )
                return admin_error_response(
                    f"{spec.storage_kind} backend storage is not supported yet for {spec.entity}",
                    exc,
                    status_code=501,
                )
            try:
                async with scoped_pg_session(admin_state.engine.pg_pool, session_ns) as conn:
                    unlinked = await unlink_document(
                        conn, session_ns, doc_uuid, spec.node_type, str(item_id)
                    )
            except Exception as exc:
                return admin_error_response(
                    f"Failed to detach document: {exc}", exc, status_code=500
                )
        else:
            unlinked = await unlink_document(
                None, session_ns, doc_uuid, spec.node_type, str(item_id)
            )

        if not unlinked:
            exc = KeyError("Document link not found")
            return admin_error_response("Document link not found", exc, status_code=404)

        if admin_state.engine:
            await bump_mcp_cache_generation(
                admin_state.engine,
                engine_name=spec.engine,
                route=f"api_{spec.engine}_{spec.mcp_slug}_documents",
            )

        return JSONResponse({"status": "ok", "unlinked": True})

    async def handle_bulk(request: Request) -> Response:
        try:
            body = await request.json()
        except Exception as exc:
            return admin_error_response("Malformed JSON body", exc, status_code=400)

        tier = resolve_principal_tier(request)
        if is_global and tier == "external-customer":
            exc = PermissionError("External customers cannot modify global catalog resources")
            return admin_error_response(
                "External customers cannot modify global resources", exc, status_code=403
            )

        ns_uuid, err_resp = extract_namespace_id(
            request, body if isinstance(body, dict) else None, required=is_tenant
        )
        if err_resp:
            return err_resp

        items_to_insert = body.get("items") if isinstance(body, dict) else body
        if not isinstance(items_to_insert, list):
            return admin_error_response(
                "Bulk payload must contain 'items' list",
                ValueError("Validation error"),
                status_code=400,
            )

        if admin_state.engine and getattr(admin_state.engine, "pg_pool", None):
            if spec.storage_kind in ("mongo", "kg_nodes") or is_graph:
                exc = NotImplementedError(
                    f"{spec.storage_kind} backend storage is not supported yet for {spec.entity}"
                )
                return admin_error_response(
                    f"{spec.storage_kind} backend storage is not supported yet for {spec.entity}",
                    exc,
                    status_code=501,
                )

        created_ids: list[str] = []
        import uuid

        session_ns = ns_uuid or UUID("00000000-0000-0000-0000-000000000000")
        for it in items_to_insert:
            if not isinstance(it, dict):
                continue
            item_id = str(it.get(spec.id_field) or uuid.uuid4())
            data = {k: it[k] for k in spec.writable_fields if k in it}
            data[spec.id_field] = item_id
            if not is_global or "namespace_id" in spec.writable_fields:
                if ns_uuid:
                    data["namespace_id"] = str(ns_uuid)
            if (
                admin_state.engine
                and getattr(admin_state.engine, "pg_pool", None)
                and spec.table_name
            ):
                async with scoped_pg_session(admin_state.engine.pg_pool, session_ns) as conn:
                    cols = list(data.keys())
                    vals = [data[c] for c in cols]
                    placeholders = [f"${i + 1}" for i in range(len(cols))]
                    await conn.execute(
                        f"INSERT INTO {spec.table_name} ({', '.join(cols)}) VALUES ({', '.join(placeholders)})",
                        *vals,
                    )
            else:
                mem = _get_mem_bucket(spec, str(ns_uuid) if ns_uuid else None)
                mem[item_id] = dict(data)
            created_ids.append(item_id)

        if admin_state.engine:
            await bump_mcp_cache_generation(
                admin_state.engine,
                engine_name=spec.engine,
                route=f"api_{spec.engine}_{spec.mcp_slug}_bulk",
            )

        return JSONResponse(
            {"status": "ok", "count": len(created_ids), "ids": created_ids}, status_code=201
        )

    return [
        Route(prefix, endpoint=handle_list, methods=["GET"]),
        Route(prefix, endpoint=handle_create, methods=["POST"]),
        Route(f"{prefix}/bulk", endpoint=handle_bulk, methods=["POST"]),
        Route(f"{prefix}/{{id}}", endpoint=handle_get, methods=["GET"]),
        Route(f"{prefix}/{{id}}", endpoint=handle_patch, methods=["PATCH"]),
        Route(f"{prefix}/{{id}}/archive", endpoint=handle_archive, methods=["POST"]),
        Route(f"{prefix}/{{id}}/restore", endpoint=handle_restore, methods=["POST"]),
        Route(f"{prefix}/{{id}}/events", endpoint=handle_events, methods=["GET"]),
        Route(f"{prefix}/{{id}}/comments", endpoint=handle_list_comments, methods=["GET"]),
        Route(f"{prefix}/{{id}}/comments", endpoint=handle_add_comment, methods=["POST"]),
        Route(f"{prefix}/{{id}}/tags", endpoint=handle_list_tags, methods=["GET"]),
        Route(f"{prefix}/{{id}}/tags", endpoint=handle_add_tag, methods=["POST"]),
        Route(f"{prefix}/{{id}}/tags/{{tag}}", endpoint=handle_remove_tag, methods=["DELETE"]),
        Route(f"{prefix}/{{id}}/documents", endpoint=handle_list_documents, methods=["GET"]),
        Route(f"{prefix}/{{id}}/documents", endpoint=handle_attach_document, methods=["POST"]),
        Route(
            f"{prefix}/{{id}}/documents/{{doc_id}}",
            endpoint=handle_detach_document,
            methods=["DELETE"],
        ),
    ]
