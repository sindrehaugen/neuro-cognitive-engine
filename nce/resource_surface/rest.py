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
from nce.admin_http_support import admin_error_response, ownership_denied_response
from nce.auth import NamespaceContext, set_namespace_context
from nce.db_utils import scoped_pg_session
from nce.engine_registry import EngineDisabledError
from nce.entity_resolution.ownership import OwnershipError, assert_owner
from nce.events.emit import emit_graph_write
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


async def upsert_secondary_tables(
    conn: Any,
    spec: ResourceSpec,
    item_id: str,
    ns_uuid: Any,
    is_global: bool,
    data: dict[str, Any],
) -> dict[str, Any]:
    """Write each of ``spec.secondary_tables``' routed fields present in
    ``data``, keyed on ``join_field = item_id``. Shared by mcp.py's
    ``handle_upsert`` and this module's own ``handle_create``/``handle_patch``
    -- the same "two generated backends from one spec" reasoning that made
    #284's fix need to land in both applies here too, so this is ONE
    implementation both import, not two copies that can drift.

    Checks for an existing row first and UPDATEs only the supplied columns,
    or INSERTs a fresh row with everything supplied -- never a blind
    ``INSERT ... ON CONFLICT DO UPDATE``. Found live while writing this
    wave's own test: a PATCH that only touched ``status`` failed a NOT NULL
    constraint on ``system_design_node_state.node_type``, because
    ``INSERT ... ON CONFLICT``'s INSERT branch is validated against the
    table's constraints regardless of whether the conflict resolves to
    UPDATE -- a partial update has no reason to supply a column it isn't
    changing, and previously-first-cut ON CONFLICT design demanded it
    supply every NOT NULL column on every call, existing row or not.

    Returns the dict of secondary fields actually written (join_field and
    namespace_id excluded), for callers that merge it into their own
    response.
    """
    written: dict[str, Any] = {}
    for sec in spec.secondary_tables:
        sec_data = {f: data[f] for f in sec.fields if f in data}
        if not sec_data:
            continue
        if is_global:
            existing = await conn.fetchrow(
                f"SELECT 1 FROM {sec.table_name} WHERE {sec.join_field} = $1", item_id
            )
        else:
            existing = await conn.fetchrow(
                f"SELECT 1 FROM {sec.table_name} WHERE namespace_id = $1 AND {sec.join_field} = $2",
                ns_uuid,
                item_id,
            )
        if existing:
            cols = list(sec_data.keys())
            vals = list(sec_data.values())
            if is_global:
                set_items = [f"{c} = ${i + 2}" for i, c in enumerate(cols)]
                await conn.execute(
                    f"UPDATE {sec.table_name} SET {', '.join(set_items)} WHERE {sec.join_field} = $1",
                    item_id,
                    *vals,
                )
            else:
                set_items = [f"{c} = ${i + 3}" for i, c in enumerate(cols)]
                await conn.execute(
                    f"UPDATE {sec.table_name} SET {', '.join(set_items)} WHERE namespace_id = $1 AND {sec.join_field} = $2",
                    ns_uuid,
                    item_id,
                    *vals,
                )
        else:
            insert_data = dict(sec_data)
            insert_data[sec.join_field] = item_id
            if not is_global:
                insert_data["namespace_id"] = str(ns_uuid) if ns_uuid else None
            cols = list(insert_data.keys())
            vals = list(insert_data.values())
            placeholders = [f"${i + 1}" for i in range(len(cols))]
            await conn.execute(
                f"INSERT INTO {sec.table_name} ({', '.join(cols)}) VALUES ({', '.join(placeholders)})",
                *vals,
            )
        written.update(sec_data)
    return written


def redact_item(item: dict[str, Any], spec: ResourceSpec, principal_tier: str) -> dict[str, Any]:
    """Apply C3/C8 principal tier redaction to an item dict.

    A tier absent from ``spec.tier_allowlists`` is treated as an explicit
    empty allowlist (deny), never as unfiltered. An undeclared tier means
    nobody has decided what it may see -- the safe reading of that silence
    is "nothing", not "everything".
    """
    # Employee tier has full visibility
    if principal_tier == "employee":
        return dict(item)

    tier_allowed = spec.tier_allowlists.get(principal_tier, ())
    out: dict[str, Any] = {}
    for k, v in item.items():
        # Strip sensitive keywords from non-employee tiers
        lower_k = k.lower()
        if any(sub in lower_k for sub in _SENSITIVE_FIELD_SUBSTRINGS):
            continue
        if k not in tier_allowed:
            continue
        out[k] = v
    return out


def resolve_principal_tier(request: Request) -> str:
    """Derive principal tier from verified auth state only.

    Caller-supplied headers are never trusted for this. The verified value
    lives on request.state.namespace_ctx.principal_kind: this generated
    surface only ever runs behind admin_app.py's HMACAuthMiddleware, whose
    own _resolve_namespace_context sets request.state.namespace_ctx on
    every authenticated request (nce/auth.py) -- there is no bare
    request.state.principal_kind attribute anywhere in this codebase, so
    checking for one would always miss the real, populated context.
    A principal with no verified tier gets the least-privileged fallback
    rather than the most-privileged one.
    """
    ns_ctx = getattr(request.state, "namespace_ctx", None)
    state_kind = getattr(ns_ctx, "principal_kind", None)
    if state_kind in ("employee", "contractor", "external-customer"):
        return state_kind
    return "unverified"


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


async def _enforce_enabled_guard(spec: ResourceSpec, ns_uuid: UUID | None) -> JSONResponse | None:
    """Run ``spec.enabled_guard`` at the REST boundary, same contract as a
    hand-written route's own ``require_*_enabled`` call. Real-Postgres-backed
    path only, mirroring every other generated-surface fallback split in this
    module -- the in-memory store has no ``namespaces`` row to check opt-in
    against, and it is never the deployment this gate protects.

    Deliberately NOT keyed on ``spec.tenant_scope`` -- that describes the
    underlying TABLE's RLS scope, not whether a per-namespace opt-in applies
    to the calling tenant. PRODUCT_SKU is ``tenant_scope == "global"`` (a
    shared parts catalog with no namespace_id column) but its hand-written
    boundary (nce/admin_handlers/product.py) still requires a namespace_id
    and still gates on it: the catalog is shared, whether a given tenant may
    read/write it through the API is not. Keying this on tenant_scope would
    have silently left PRODUCT_SKU one of the 18 ungated specs this gate
    exists to close.
    """
    if (
        ns_uuid is None
        or spec.enabled_guard is None
        or not admin_state.engine
        or not getattr(admin_state.engine, "pg_pool", None)
    ):
        return None
    try:
        await spec.enabled_guard(admin_state.engine.pg_pool, str(ns_uuid))
    except EngineDisabledError as exc:
        return admin_error_response(
            f"{spec.engine} vertical is not enabled for this namespace",
            exc,
            status_code=409,
        )
    return None


def make_resource_routes(spec: ResourceSpec) -> list[Route]:
    """Generate all Starlette Route definitions for a ResourceSpec."""

    prefix = spec.rest_collection_path
    is_tenant = spec.tenant_scope == "tenant"
    is_global = spec.tenant_scope == "global"
    is_graph = spec.tenant_scope == "graph"
    # An enabled_guard's subject is the CALLER's namespace, not the table's
    # storage scope -- a global spec like PRODUCT_SKU still needs a real
    # namespace_id to check opt-in against. Omitting namespace_id entirely
    # must be refused (422, same as the hand-written boundary, e.g.
    # nce/admin_handlers/product.py:70), not silently skip the gate. Found
    # live 2026-09-19: the first cut of this gate keyed `extract_namespace_id`
    # on `is_tenant` alone, so a caller that omitted namespace_id for a
    # global spec got `ns_uuid = None` and `_enforce_enabled_guard` never ran
    # -- the generated surface stayed strictly more permissive than the
    # hand-written one for exactly that one input shape.
    # is_graph is included for the same reason as is_tenant: kg_nodes rows
    # are namespace-scoped (UNIQUE(label, namespace_id), schema.sql), so a
    # graph-primary spec is exactly as namespace-bound as a tenant one.
    # Before Wave 3 (2026-09-20) this never mattered -- storage_kind=
    # "kg_nodes" was an unconditional 501, so no graph-primary spec ever
    # reached a real query. Once it does, omitting namespace_id must not
    # silently bind `WHERE namespace_id = NULL` and return zero rows -- it
    # must 422, same as every other namespace-scoped spec.
    requires_namespace = is_tenant or is_graph or spec.enabled_guard is not None

    async def handle_list(request: Request) -> Response:
        ns_uuid, err_resp = extract_namespace_id(request, required=requires_namespace)
        if err_resp:
            return err_resp
        guard_resp = await _enforce_enabled_guard(spec, ns_uuid)
        if guard_resp:
            return guard_resp
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
            if spec.storage_kind == "mongo":
                exc = NotImplementedError(
                    f"{spec.storage_kind} backend storage is not supported yet for {spec.entity}"
                )
                return admin_error_response(
                    f"{spec.storage_kind} backend storage is not supported yet for {spec.entity}",
                    exc,
                    status_code=501,
                )
            if is_graph:
                # kg_nodes-primary list: primary (identity) rows only, same
                # documented limitation as a postgres-primary multi-table
                # spec's own list -- see SecondaryTable's docstring. Filters
                # and search apply to kg_nodes' own real columns (label,
                # change_origin, timestamps), not the secondary tables.
                try:
                    session_ns = ns_uuid or UUID("00000000-0000-0000-0000-000000000000")
                    async with scoped_pg_session(admin_state.engine.pg_pool, session_ns) as conn:
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
                            next_cursor = base64.b64encode(last_label.encode("utf-8")).decode(
                                "utf-8"
                            )
                except Exception as exc:
                    return admin_error_response(
                        f"Failed to query {spec.entity}: {exc}", exc, status_code=500
                    )
            elif spec.table_name:
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
        ns_uuid, err_resp = extract_namespace_id(request, required=requires_namespace)
        if err_resp:
            return err_resp
        guard_resp = await _enforce_enabled_guard(spec, ns_uuid)
        if guard_resp:
            return guard_resp

        item_id = request.path_params.get("id")
        if not item_id:
            return admin_error_response(
                "Missing id path parameter", ValueError("Missing id"), status_code=400
            )

        tier = resolve_principal_tier(request)

        item: dict[str, Any] | None = None
        if admin_state.engine and getattr(admin_state.engine, "pg_pool", None):
            if spec.storage_kind == "mongo":
                exc = NotImplementedError(
                    f"{spec.storage_kind} backend storage is not supported yet for {spec.entity}"
                )
                return admin_error_response(
                    f"{spec.storage_kind} backend storage is not supported yet for {spec.entity}",
                    exc,
                    status_code=501,
                )
            if is_graph:
                try:
                    session_ns = ns_uuid or UUID("00000000-0000-0000-0000-000000000000")
                    async with scoped_pg_session(admin_state.engine.pg_pool, session_ns) as conn:
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
                            # Multi-table spec: merge each secondary table's
                            # row -- a missing secondary row is not an error,
                            # the kg_nodes identity row is still a real
                            # resource on its own.
                            for sec in spec.secondary_tables:
                                sec_row = await conn.fetchrow(
                                    f"SELECT * FROM {sec.table_name} WHERE namespace_id = $1 AND {sec.join_field} = $2",
                                    ns_uuid,
                                    node_label,
                                )
                                if sec_row:
                                    item.update(row_to_dict(sec_row))
                except Exception as exc:
                    return admin_error_response(
                        f"Database fetch error: {exc}", exc, status_code=500
                    )
            elif spec.table_name:
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
                            # Multi-table spec: merge each secondary table's
                            # row (same reasoning as the MCP handler's own
                            # copy of this -- a missing secondary row is not
                            # an error, the primary row is still a real
                            # resource on its own).
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

        # Apply tier redactions and fields projection -- same generic
        # ?fields=a,b,c mechanism handle_list already offers (charter Wave
        # B-5's "light" quote view: GET .../quotes/{id}?fields=<curated-list>
        # rather than a new named-view concept on ResourceSpec itself).
        fields_filter = request.query_params.get("fields")
        allowed_fields = [f.strip() for f in fields_filter.split(",")] if fields_filter else None

        redacted = redact_item(item, spec, tier)
        if allowed_fields:
            redacted = {k: v for k, v in redacted.items() if k in allowed_fields}
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

        ns_uuid, err_resp = extract_namespace_id(request, body, required=requires_namespace)
        if err_resp:
            return err_resp
        guard_resp = await _enforce_enabled_guard(spec, ns_uuid)
        if guard_resp:
            return guard_resp

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
            if spec.storage_kind == "mongo":
                exc = NotImplementedError(
                    f"{spec.storage_kind} backend storage is not supported yet for {spec.entity}"
                )
                return admin_error_response(
                    f"{spec.storage_kind} backend storage is not supported yet for {spec.entity}",
                    exc,
                    status_code=501,
                )
            if is_graph:
                try:
                    node_label = item_id
                    session_ns = ns_uuid or UUID("00000000-0000-0000-0000-000000000000")
                    async with scoped_pg_session(admin_state.engine.pg_pool, session_ns) as conn:
                        # Every hand-written kg_nodes writer in this codebase
                        # (system_design/devices.py, project/convert.py,
                        # project/case_study.py, ...) calls assert_owner
                        # before the INSERT and emit_graph_write after --
                        # deny-by-default ownership plus an outbox event any
                        # subscriber can react to. The generated surface must
                        # honor the same two invariants for the same table,
                        # not silently bypass them because the write happens
                        # to be declarative instead of hand-written.
                        await assert_owner(conn, session_ns, spec.node_type, spec.engine)

                        # kg_nodes identity row first -- every system_design
                        # satellite table (device_capabilities, node_state,
                        # geometry) carries a real FK to
                        # kg_nodes(label, namespace_id), so this must land
                        # before any secondary-table write, not after.
                        cols = ["label", "entity_type", "namespace_id"]
                        vals: list[Any] = [node_label, spec.node_type, ns_uuid]
                        if "change_origin" in data:
                            cols.append("change_origin")
                            vals.append(data["change_origin"])
                        placeholders = [f"${i + 1}" for i in range(len(cols))]
                        set_items = [f"{c} = EXCLUDED.{c}" for c in cols if c != "label"]
                        query = f"""
                            INSERT INTO kg_nodes ({", ".join(cols)})
                            VALUES ({", ".join(placeholders)})
                            ON CONFLICT (label, namespace_id) DO UPDATE
                                SET {", ".join(set_items)}, updated_at = NOW()
                            RETURNING id, label, entity_type, namespace_id, change_origin,
                                      created_at, updated_at
                        """
                        row = await conn.fetchrow(query, *vals)
                        created = row_to_dict(row)

                        secondary_field_names = {
                            f for sec in spec.secondary_tables for f in sec.fields
                        }
                        sec_data = {k: v for k, v in data.items() if k in secondary_field_names}
                        if "node_type" in secondary_field_names:
                            sec_data["node_type"] = spec.node_type
                        if sec_data:
                            created.update(
                                await upsert_secondary_tables(
                                    conn, spec, node_label, ns_uuid, is_global, sec_data
                                )
                            )

                        await emit_graph_write(
                            conn,
                            namespace_id=session_ns,
                            node_type=spec.node_type,
                            op="upserted",
                            node_id=node_label,
                        )
                except OwnershipError as exc:
                    return ownership_denied_response(exc)
                except Exception as exc:
                    return admin_error_response(
                        f"Failed to create {spec.entity}: {exc}", exc, status_code=500
                    )
            elif spec.table_name:
                try:
                    secondary_field_names = {f for sec in spec.secondary_tables for f in sec.fields}
                    primary_data = (
                        {k: v for k, v in data.items() if k not in secondary_field_names}
                        if secondary_field_names
                        else data
                    )
                    session_ns = ns_uuid or UUID("00000000-0000-0000-0000-000000000000")
                    async with scoped_pg_session(admin_state.engine.pg_pool, session_ns) as conn:
                        cols = list(primary_data.keys())
                        vals = [now if c == spec.version_field else primary_data[c] for c in cols]
                        val_placeholders = [f"${i + 1}" for i in range(len(cols))]
                        query = f"""
                            INSERT INTO {spec.table_name} ({', '.join(cols)})
                            VALUES ({', '.join(val_placeholders)})
                            RETURNING *
                        """
                        row = await conn.fetchrow(query, *vals)
                        created = row_to_dict(row) if row else primary_data

                        # Multi-table spec: shared with mcp.py's
                        # handle_upsert and this module's own handle_patch --
                        # see upsert_secondary_tables' own docstring for why
                        # this is not a blind ON CONFLICT.
                        created.update(
                            await upsert_secondary_tables(
                                conn, spec, item_id, ns_uuid, is_global, data
                            )
                        )
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

        ns_uuid, err_resp = extract_namespace_id(request, body, required=requires_namespace)
        if err_resp:
            return err_resp
        guard_resp = await _enforce_enabled_guard(spec, ns_uuid)
        if guard_resp:
            return guard_resp

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
            if spec.storage_kind == "mongo":
                exc = NotImplementedError(
                    f"{spec.storage_kind} backend storage is not supported yet for {spec.entity}"
                )
                return admin_error_response(
                    f"{spec.storage_kind} backend storage is not supported yet for {spec.entity}",
                    exc,
                    status_code=501,
                )
            if is_graph:
                try:
                    session_ns = ns_uuid or UUID("00000000-0000-0000-0000-000000000000")
                    async with scoped_pg_session(admin_state.engine.pg_pool, session_ns) as conn:
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
                            existing = row_to_dict(row)
                            node_label = existing["label"]
                            for sec in spec.secondary_tables:
                                sec_row = await conn.fetchrow(
                                    f"SELECT * FROM {sec.table_name} WHERE namespace_id = $1 AND {sec.join_field} = $2",
                                    ns_uuid,
                                    node_label,
                                )
                                if sec_row:
                                    existing.update(row_to_dict(sec_row))
                except Exception as exc:
                    return admin_error_response(f"Database error: {exc}", exc, status_code=500)
            elif spec.table_name:
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

        if (
            admin_state.engine
            and getattr(admin_state.engine, "pg_pool", None)
            and (spec.table_name or is_graph)
        ):
            try:
                secondary_field_names = {f for sec in spec.secondary_tables for f in sec.fields}
                session_ns = ns_uuid or UUID("00000000-0000-0000-0000-000000000000")
                async with scoped_pg_session(admin_state.engine.pg_pool, session_ns) as conn:
                    if is_graph:
                        node_label = existing["label"]
                        # Same deny-by-default guard handle_create makes --
                        # a PATCH mutates the same owned kg_nodes row (or its
                        # secondary tables), so it needs the same check, not
                        # just the initial write.
                        await assert_owner(conn, session_ns, spec.node_type, spec.engine)

                        # Only `change_origin` is a real writable column on
                        # kg_nodes itself; everything else routes to a
                        # secondary table.
                        primary_updates = {k: v for k, v in updates.items() if k == "change_origin"}
                        if primary_updates:
                            row = await conn.fetchrow(
                                """
                                UPDATE kg_nodes
                                SET change_origin = $3, updated_at = NOW()
                                WHERE namespace_id = $1 AND label = $2
                                RETURNING id, label, entity_type, namespace_id,
                                          change_origin, created_at, updated_at
                                """,
                                ns_uuid,
                                node_label,
                                primary_updates["change_origin"],
                            )
                            updated = row_to_dict(row) if row else {**existing, **primary_updates}
                        else:
                            updated = dict(existing)

                        sec_updates = {
                            k: v for k, v in updates.items() if k in secondary_field_names
                        }
                        if sec_updates:
                            updated.update(
                                await upsert_secondary_tables(
                                    conn, spec, node_label, ns_uuid, is_global, sec_updates
                                )
                            )

                        await emit_graph_write(
                            conn,
                            namespace_id=session_ns,
                            node_type=spec.node_type,
                            op="upserted",
                            node_id=node_label,
                        )
                    else:
                        primary_updates = (
                            {k: v for k, v in updates.items() if k not in secondary_field_names}
                            if secondary_field_names
                            else updates
                        )
                        vals = [
                            now if k == spec.version_field else v
                            for k, v in primary_updates.items()
                        ]
                        if is_global:
                            set_items = [
                                f"{k} = ${i + 2}" for i, k in enumerate(primary_updates.keys())
                            ]
                            query = f"""
                                UPDATE {spec.table_name}
                                SET {', '.join(set_items)}
                                WHERE {spec.id_field} = $1
                                RETURNING *
                            """
                            row = await conn.fetchrow(query, item_id, *vals)
                        else:
                            set_items = [
                                f"{k} = ${i + 3}" for i, k in enumerate(primary_updates.keys())
                            ]
                            query = f"""
                                UPDATE {spec.table_name}
                                SET {', '.join(set_items)}
                                WHERE namespace_id = $1 AND {spec.id_field} = $2
                                RETURNING *
                            """
                            row = await conn.fetchrow(query, ns_uuid, item_id, *vals)
                        updated = row_to_dict(row) if row else {**existing, **primary_updates}

                        # Multi-table spec: shared with handle_create and
                        # mcp.py's handle_upsert -- see upsert_secondary_tables'
                        # own docstring.
                        updated.update(
                            await upsert_secondary_tables(
                                conn, spec, item_id, ns_uuid, is_global, updates
                            )
                        )
            except OwnershipError as exc:
                return ownership_denied_response(exc)
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

        ns_uuid, err_resp = extract_namespace_id(request, body, required=requires_namespace)
        if err_resp:
            return err_resp
        guard_resp = await _enforce_enabled_guard(spec, ns_uuid)
        if guard_resp:
            return guard_resp

        item_id = request.path_params.get("id")
        if not item_id:
            return admin_error_response(
                "Missing id parameter", ValueError("Missing id"), status_code=400
            )

        field_name = spec.soft_delete_field or "is_archived"
        if admin_state.engine and getattr(admin_state.engine, "pg_pool", None):
            if spec.storage_kind == "mongo":
                exc = NotImplementedError(
                    f"{spec.storage_kind} backend storage is not supported yet for {spec.entity}"
                )
                return admin_error_response(
                    f"{spec.storage_kind} backend storage is not supported yet for {spec.entity}",
                    exc,
                    status_code=501,
                )
            if is_graph:
                # kg_nodes and every system_design satellite table
                # (device_capabilities, node_state, geometry) have no
                # generic soft-delete/is_archived column -- inventing one
                # (e.g. overloading node_state.status) is a system_design
                # content decision, not a mechanical resource_surface gap.
                exc = NotImplementedError(
                    f"{spec.entity} has no soft-delete field on its kg_nodes-primary storage"
                )
                return admin_error_response(
                    f"Archive is not supported for {spec.entity}: kg_nodes-primary specs have "
                    f"no generic soft-delete column",
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

        ns_uuid, err_resp = extract_namespace_id(request, body, required=requires_namespace)
        if err_resp:
            return err_resp
        guard_resp = await _enforce_enabled_guard(spec, ns_uuid)
        if guard_resp:
            return guard_resp

        item_id = request.path_params.get("id")
        if not item_id:
            return admin_error_response(
                "Missing id parameter", ValueError("Missing id"), status_code=400
            )

        field_name = spec.soft_delete_field or "is_archived"
        if admin_state.engine and getattr(admin_state.engine, "pg_pool", None):
            if spec.storage_kind == "mongo":
                exc = NotImplementedError(
                    f"{spec.storage_kind} backend storage is not supported yet for {spec.entity}"
                )
                return admin_error_response(
                    f"{spec.storage_kind} backend storage is not supported yet for {spec.entity}",
                    exc,
                    status_code=501,
                )
            if is_graph:
                exc = NotImplementedError(
                    f"{spec.entity} has no soft-delete field on its kg_nodes-primary storage"
                )
                return admin_error_response(
                    f"Restore is not supported for {spec.entity}: kg_nodes-primary specs have "
                    f"no generic soft-delete column",
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
        ns_uuid, err_resp = extract_namespace_id(request, required=requires_namespace)
        if err_resp:
            return err_resp
        guard_resp = await _enforce_enabled_guard(spec, ns_uuid)
        if guard_resp:
            return guard_resp

        item_id = request.path_params.get("id")
        if not item_id:
            return admin_error_response(
                "Missing id parameter", ValueError("Missing id"), status_code=400
            )

        if admin_state.engine and getattr(admin_state.engine, "pg_pool", None):
            try:
                session_ns = ns_uuid or UUID("00000000-0000-0000-0000-000000000000")
                async with scoped_pg_session(admin_state.engine.pg_pool, session_ns) as conn:
                    events = await fetch_entity_events(conn, session_ns, spec, str(item_id))
                    return JSONResponse({"events": events, "count": len(events)})
            except Exception as exc:
                return admin_error_response(f"Failed to query events: {exc}", exc, status_code=500)
        return JSONResponse({"events": [], "count": 0})

    async def handle_list_comments(request: Request) -> Response:
        ns_uuid, err_resp = extract_namespace_id(request, required=requires_namespace)
        if err_resp:
            return err_resp
        guard_resp = await _enforce_enabled_guard(spec, ns_uuid)
        if guard_resp:
            return guard_resp

        item_id = request.path_params.get("id")
        if not item_id:
            return admin_error_response(
                "Missing id parameter", ValueError("Missing id"), status_code=400
            )

        if admin_state.engine and getattr(admin_state.engine, "pg_pool", None):
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

        ns_uuid, err_resp = extract_namespace_id(request, body, required=requires_namespace)
        if err_resp:
            return err_resp
        guard_resp = await _enforce_enabled_guard(spec, ns_uuid)
        if guard_resp:
            return guard_resp

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
        ns_uuid, err_resp = extract_namespace_id(request, required=requires_namespace)
        if err_resp:
            return err_resp
        guard_resp = await _enforce_enabled_guard(spec, ns_uuid)
        if guard_resp:
            return guard_resp

        item_id = request.path_params.get("id")
        if not item_id:
            return admin_error_response(
                "Missing id parameter", ValueError("Missing id"), status_code=400
            )

        if admin_state.engine and getattr(admin_state.engine, "pg_pool", None):
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

        ns_uuid, err_resp = extract_namespace_id(request, body, required=requires_namespace)
        if err_resp:
            return err_resp
        guard_resp = await _enforce_enabled_guard(spec, ns_uuid)
        if guard_resp:
            return guard_resp

        item_id = request.path_params.get("id")
        tag = body.get("tag")
        if not item_id or not tag:
            return admin_error_response(
                "Missing required id or tag", ValueError("Validation error"), status_code=400
            )

        if admin_state.engine and getattr(admin_state.engine, "pg_pool", None):
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
        ns_uuid, err_resp = extract_namespace_id(request, required=requires_namespace)
        if err_resp:
            return err_resp
        guard_resp = await _enforce_enabled_guard(spec, ns_uuid)
        if guard_resp:
            return guard_resp

        item_id = request.path_params.get("id")
        tag = request.path_params.get("tag")
        if not item_id or not tag:
            return admin_error_response(
                "Missing required id or tag parameter",
                ValueError("Validation error"),
                status_code=400,
            )

        if admin_state.engine and getattr(admin_state.engine, "pg_pool", None):
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
        ns_uuid, err_resp = extract_namespace_id(request, required=requires_namespace)
        if err_resp:
            return err_resp
        guard_resp = await _enforce_enabled_guard(spec, ns_uuid)
        if guard_resp:
            return guard_resp

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
            request, body if isinstance(body, dict) else None, required=requires_namespace
        )
        if err_resp:
            return err_resp
        guard_resp = await _enforce_enabled_guard(spec, ns_uuid)
        if guard_resp:
            return guard_resp

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

        ns_uuid, err_resp = extract_namespace_id(request, required=requires_namespace)
        if err_resp:
            return err_resp
        guard_resp = await _enforce_enabled_guard(spec, ns_uuid)
        if guard_resp:
            return guard_resp

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
        """Bulk create. Contract (Q-48, ruled 2026-09-20): all-or-nothing.

        A bulk-create call either creates every item or none of them --
        there is no partial-success outcome and no per-item result
        reporting. This is the single statement of that contract; nothing
        else in this generator should restate it, only reference it.

        Table-backed specs: every item is validated structurally (must be a
        JSON object) before any write is attempted, refusing the whole
        batch with 400 if one isn't. Constraint-level invalidity a table
        enforces itself (a unique/check/not-null violation) is still caught
        by the shared transaction around the write loop -- the loop aborts
        and rolls back the whole batch, so the persisted end state is
        identical to a pre-validated refusal (zero rows), even though the
        mechanism is reactive rather than a dry run. Measured before
        shipping: no table reachable through this route carries an INSERT
        trigger with a side effect outside that transaction (grepped
        schema.sql for AFTER INSERT -- one exists, on economy_postings, not
        C12-registered), so this reactive rollback has no observable gap
        against a true pre-write dry run today.

        kg_nodes-primary specs: refused outright (501). The all-or-nothing
        policy above answers the REPORTING semantics question; it does not
        by itself define identity-plus-satellite partial-failure behaviour
        for a spec with no table_name, and there is still no real caller
        needing it built (Q-48 ruling: do not build bulk for these specs
        speculatively).
        """
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
            request, body if isinstance(body, dict) else None, required=requires_namespace
        )
        if err_resp:
            return err_resp
        guard_resp = await _enforce_enabled_guard(spec, ns_uuid)
        if guard_resp:
            return guard_resp

        items_to_insert = body.get("items") if isinstance(body, dict) else body
        if not isinstance(items_to_insert, list):
            return admin_error_response(
                "Bulk payload must contain 'items' list",
                ValueError("Validation error"),
                status_code=400,
            )

        if admin_state.engine and getattr(admin_state.engine, "pg_pool", None):
            if spec.storage_kind == "mongo":
                exc = NotImplementedError(
                    f"{spec.storage_kind} backend storage is not supported yet for {spec.entity}"
                )
                return admin_error_response(
                    f"{spec.storage_kind} backend storage is not supported yet for {spec.entity}",
                    exc,
                    status_code=501,
                )
            if is_graph:
                # Deliberately refused, not a dead guard: the per-item loop
                # below gates its real write on `spec.table_name` truthy and
                # falls back to the in-memory mock bucket otherwise. Without
                # this refusal a kg_nodes-primary spec would silently write
                # bulk items to the mock store in production instead of
                # kg_nodes -- a wrong-backend bug, not a redundant check.
                #
                # The REPORTING semantics question (all-or-nothing) is
                # answered -- see this function's own docstring -- but that
                # doesn't by itself define identity-plus-satellite
                # partial-failure behaviour for a spec with no table_name
                # (item 3 of 10 fails: which of the identity row and the
                # already-written satellite rows for items 1-2 come back
                # out?), and there is still no real caller needing this
                # built (Q-48 ruling: unimplemented pending one, not
                # unimplemented for lack of a decision).
                exc = NotImplementedError(
                    f"Bulk create is not supported yet for kg_nodes-primary spec {spec.entity}"
                )
                return admin_error_response(
                    f"Bulk create is not implemented for {spec.entity}: reporting semantics "
                    f"are all-or-nothing (Q-48), but kg_nodes-primary identity-plus-satellite "
                    f"partial-failure behaviour has no implementation yet and no caller "
                    f"requiring one",
                    exc,
                    status_code=501,
                )

        if any(not isinstance(it, dict) for it in items_to_insert):
            # Structural pre-write validation: every item must be a JSON
            # object before ANY item is written, table-backed or in-memory.
            # Previously a non-dict item was silently skipped mid-loop --
            # the batch still partially wrote, contradicting all-or-nothing
            # without even surfacing as an error (the caller saw 201 with a
            # count lower than what they sent). This is the one check this
            # generic route can make generically, with no per-spec
            # knowledge; a table's own constraints (unique/check/not-null)
            # still can only be discovered by attempting the write, and
            # remain covered by the transaction rollback below, not by a
            # pre-write check here (see this function's own docstring).
            return admin_error_response(
                "Bulk payload rejected: every item in 'items' must be a JSON object "
                "(all-or-nothing -- no items were created)",
                ValueError("Validation error"),
                status_code=400,
            )

        created_ids: list[str] = []
        import uuid

        session_ns = ns_uuid or UUID("00000000-0000-0000-0000-000000000000")

        def _prepare_item(it: dict[str, Any]) -> tuple[str, dict[str, Any]]:
            item_id = str(it.get(spec.id_field) or uuid.uuid4())
            data = {k: it[k] for k in spec.writable_fields if k in it}
            data[spec.id_field] = item_id
            if not is_global or "namespace_id" in spec.writable_fields:
                if ns_uuid:
                    data["namespace_id"] = str(ns_uuid)
            return item_id, data

        if admin_state.engine and getattr(admin_state.engine, "pg_pool", None) and spec.table_name:
            # All-or-nothing (Q-48 follow-on): one shared transaction for the
            # whole batch, not one scoped_pg_session per item. scoped_pg_session
            # already wraps its yielded block in conn.transaction() -- opening
            # it once per item meant item N's own INSERT committed on its own
            # before item N+1 was ever attempted, so a mid-batch failure left
            # whatever had already committed in place and silently dropped the
            # rest. That was never a chosen semantics, it was the absence of
            # one: nothing decided "roll back all" or "report partial", the
            # per-item transaction boundary just made a crash look like a
            # partial success. This restores the guarantee every caller of a
            # bulk-create endpoint already assumes -- it succeeded or it
            # didn't -- without deciding the SEPARATE, still-open question of
            # whether a future version should report per-item results (Q-48).
            try:
                async with scoped_pg_session(admin_state.engine.pg_pool, session_ns) as conn:
                    for it in items_to_insert:
                        # Every item is already confirmed a dict by the
                        # structural pre-check above -- nothing to skip here.
                        item_id, data = _prepare_item(it)
                        cols = list(data.keys())
                        vals = [data[c] for c in cols]
                        placeholders = [f"${i + 1}" for i in range(len(cols))]
                        await conn.execute(
                            f"INSERT INTO {spec.table_name} ({', '.join(cols)}) VALUES ({', '.join(placeholders)})",
                            *vals,
                        )
                        created_ids.append(item_id)
            except Exception as exc:
                return admin_error_response(
                    f"Bulk create failed for {spec.entity}: no items were created "
                    f"(all-or-nothing -- the batch that was in progress was rolled back)",
                    exc,
                    status_code=500,
                )
        else:
            # In-memory mock bucket (no real pg_pool, e.g. unit tests): no
            # transaction exists to roll back, and nothing here can partially
            # persist across a process crash the way a real INSERT sequence
            # could -- unaffected by this fix.
            for it in items_to_insert:
                # Every item is already confirmed a dict by the structural
                # pre-check above -- nothing to skip here.
                item_id, data = _prepare_item(it)
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

    # Verb-tagged so a spec can opt a verb out (spec.excluded_verbs) -- see
    # ResourceSpec.excluded_verbs's docstring for the REST-side grouping:
    # "upsert" covers create+patch+bulk, "archive" covers archive+restore,
    # mirroring MCP's single upsert/archive tools. Sub-resource routes
    # (events/comments/tags/documents) carry no verb tag and are never
    # excluded -- they do not depend on which core verbs exist.
    core_routes: list[tuple[str, Route]] = [
        ("list", Route(prefix, endpoint=handle_list, methods=["GET"])),
        ("upsert", Route(prefix, endpoint=handle_create, methods=["POST"])),
        ("upsert", Route(f"{prefix}/bulk", endpoint=handle_bulk, methods=["POST"])),
        ("get", Route(f"{prefix}/{{id}}", endpoint=handle_get, methods=["GET"])),
        ("upsert", Route(f"{prefix}/{{id}}", endpoint=handle_patch, methods=["PATCH"])),
        ("archive", Route(f"{prefix}/{{id}}/archive", endpoint=handle_archive, methods=["POST"])),
        ("archive", Route(f"{prefix}/{{id}}/restore", endpoint=handle_restore, methods=["POST"])),
    ]
    sub_resource_routes: list[Route] = [
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
    return [
        route for verb, route in core_routes if verb not in spec.excluded_verbs
    ] + sub_resource_routes
