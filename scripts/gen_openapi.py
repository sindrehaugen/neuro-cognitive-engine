#!/usr/bin/env python
"""Generate OpenAPI 3.1.0 specification for NCE from registries and route tables.

NCE exposes capabilities through:
  - HTTP/REST Starlette route tables (admin app, me app, customer portal, a2a server)
  - Declarative C12 ResourceSpecs (uniform resource surfaces with CRUD, filtering, search)
  - Declarative MCP tools (agent JSON-RPC tool definitions)
  - Public Pydantic data models

This script compiles these sources of truth into a deterministic OpenAPI 3.1.0
specification document at:
  - docs/_generated/openapi.json
  - openapi.json (repo root)

For C12 ResourceSpecs, the per-item response schema documents the curated
surface only -- id_field, version_field, soft_delete_field, and the union of
writable_fields/filterable_fields -- not every column the live response
actually contains. A real response is always a superset: storage-internal
columns present on every tenant-scoped table (namespace_id, created_at) are
never in any spec's field lists, so they never appear in any schema, on any
registered spec (see OPENAPI_RESPONSE_SCHEMA_SWEEP.md, 2026-09-20, for the
full accounting and why this is accepted rather than fixed absent a real
consumer of this file). A consumer following this schema gets at least what
is documented, never less -- the gap runs in the safe direction, but "this
schema documents everything the endpoint returns" is not a claim this script
makes or should be read as making.

Usage:
    python scripts/gen_openapi.py                  # write both files
    python scripts/gen_openapi.py --check          # exit 1 if files are stale or missing
"""

from __future__ import annotations

import argparse
import inspect
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

# Ensure test/dev master key exists for imports
os.environ.setdefault("NCE_MASTER_KEY", "dev-schema-key-32chars-long-xxxx")

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

# check_schema_drift.py's own drift comparison never reads nullability (its
# module docstring names that explicitly) -- expected_column_nullability()
# is a second, independent function in that file, extracted from the same
# static schema.sql text this script has no other source for. Not a design
# layering violation: both are standalone scripts in this same directory,
# and tests already import reusable parsing primitives from that file the
# same way (tests/unit/test_schema_drift_detector.py,
# tests/unit/test_resource_surface_archive_column_exists.py).
from check_schema_drift import expected_column_nullability  # noqa: E402

_DOCS_OPENAPI = _ROOT / "docs" / "_generated" / "openapi.json"
_ROOT_OPENAPI = _ROOT / "openapi.json"

_PARAM_REGEX = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


def _clean_schema(obj: Any) -> Any:
    """Recursively clean schema object for valid OpenAPI 3.1 JSON serialization."""
    if isinstance(obj, dict):
        cleaned: dict[str, Any] = {}
        for k, v in obj.items():
            if k == "title" and isinstance(v, str):
                continue  # strip noisy internal titles
            cleaned[k] = _clean_schema(v)
        return cleaned
    if isinstance(obj, list):
        return [_clean_schema(item) for item in obj]
    return obj


def _writable_field_is_nullable(spec: Any, field: str, column_nullability: dict) -> bool | None:
    """Resolve ``field`` (a ``writable_field``/``filterable_field`` on a
    ``ResourceSpec``) to the real table that actually stores it, and look
    up whether ``schema.sql`` declares that column ``NOT NULL``.

    A graph-primary spec (``table_name is None``) routes each field through
    whichever ``SecondaryTable`` declares it in its own ``fields`` --
    checked first, since a graph-primary spec's ``table_name`` is `None`
    and would otherwise resolve to nothing. A table-backed spec's fields
    live on ``spec.table_name`` directly.

    Returns `None` (not `True`) when the field can't be resolved to a real
    column at all -- a field the spec declares that schema.sql doesn't
    know about is a different, pre-existing problem (drift between the
    spec and the DDL), not something this function should paper over by
    guessing nullable. Callers must treat `None` as "leave the declared
    schema as it was", never as "assume nullable".
    """
    table: str | None = None
    for sec in spec.secondary_tables:
        if field in sec.fields:
            table = sec.table_name
            break
    if table is None and spec.table_name:
        table = spec.table_name
    if table is None:
        return None
    return column_nullability.get(table, {}).get(field)


def generate_openapi_spec() -> dict[str, Any]:
    """Compile the complete OpenAPI 3.1.0 specification document."""
    from starlette.routing import Route

    from nce.admin_app import build_admin_routes
    from nce.mcp_stdio_tools import TOOLS
    from nce.resource_surface import get_all_resource_specs
    from nce.tool_registry import TOOL_REGISTRY

    # Import secondary apps if available
    secondary_routes: list[tuple[str, Any]] = []
    try:
        import nce.me_app

        secondary_routes.append(("me", nce.me_app.app.routes))
    except Exception:
        pass

    try:
        from nce.vertical_modules.customer_portal.app import build_customer_portal_app

        secondary_routes.append(("customer_portal", build_customer_portal_app().routes))
    except Exception:
        pass

    try:
        import nce.a2a_server

        secondary_routes.append(("a2a", nce.a2a_server.app.routes))
    except Exception:
        pass

    # Collect all resource specs
    specs_list = get_all_resource_specs()
    specs_by_pair: dict[tuple[str, str], Any] = {(s.engine, s.entity): s for s in specs_list}
    # One static parse of schema.sql for the whole run -- see
    # _writable_field_is_nullable's own docstring for how a spec's field is
    # resolved to a table/column pair against this map.
    _column_nullability = expected_column_nullability()

    # Initialize OpenAPI 3.1.0 document
    doc: dict[str, Any] = {
        "openapi": "3.1.0",
        "info": {
            "title": "Neuro-Cognitive Engine API",
            "version": "1.6.0",
            "description": (
                "OpenAPI 3.1 specification for the Neuro-Cognitive Engine (NCE) REST endpoints, "
                "C12 resource surfaces, and MCP agent tool schemas."
            ),
        },
        "servers": [
            {
                "url": "/",
                "description": "Active instance root",
            }
        ],
        "paths": {},
        "components": {
            "schemas": {},
            "securitySchemes": {
                "HMACAuth": {
                    "type": "apiKey",
                    "name": "Authorization",
                    "in": "header",
                    "description": (
                        "Three-header HMAC-SHA256 signature contract: "
                        "X-NCE-Timestamp (unix epoch), X-NCE-Nonce (uuid), "
                        "Authorization (HMAC-SHA256 <hex_signature>)"
                    ),
                },
                "BearerAuth": {
                    "type": "http",
                    "scheme": "bearer",
                    "bearerFormat": "JWT",
                    "description": "JWT Bearer token for subject-scoped requests",
                },
                "mTLSAuth": {
                    "type": "mutualTLS",
                    "description": "Mutual TLS client certificate authentication",
                },
            },
            "x-mcp-tools": {},
        },
    }

    schemas = doc["components"]["schemas"]
    paths = doc["paths"]

    # 1. Standard Error Schema
    schemas["ErrorResponse"] = {
        "type": "object",
        "properties": {
            "error": {
                "type": "string",
                "description": "Human-readable error explanation",
            },
            "code": {
                "type": "integer",
                "description": "Machine-readable status code",
            },
            "details": {
                "type": "object",
                "additionalProperties": True,
                "description": "Structured refusal or validation details",
            },
        },
        "required": ["error"],
    }

    # 2. Outbound Webhook Schemas (Wave A-7)
    schemas["OutboundWebhook"] = {
        "type": "object",
        "properties": {
            "id": {"type": "string", "format": "uuid"},
            "namespace_id": {"type": "string", "format": "uuid"},
            "url": {"type": "string", "format": "uri"},
            "selectors": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of subscribed event selectors (supports * and prefix wildcard)",
            },
            "is_active": {"type": "boolean"},
            "description": {"type": "string"},
            "created_at": {"type": "string", "format": "date-time"},
            "updated_at": {"type": "string", "format": "date-time"},
        },
        "required": ["id", "namespace_id", "url", "selectors"],
    }
    schemas["OutboundWebhookCreate"] = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "format": "uri"},
            "secret": {"type": "string", "description": "HMAC shared secret for signing"},
            "selectors": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of subscribed event selectors",
            },
            "description": {"type": "string", "default": ""},
            "is_active": {"type": "boolean", "default": True},
        },
        "required": ["url", "secret", "selectors"],
    }

    # 3. Schemas for C12 ResourceSpecs
    for spec in specs_list:
        model_name = f"{spec.engine.capitalize()}_{spec.entity.replace('-', '_')}"
        # tenant_scope == "graph" (spec.py's derived storage_kind == "kg_nodes"
        # or table_name is None branch -- the same test rest.py itself uses
        # as `is_graph`): the response's real identifying column is always
        # kg_nodes.label, regardless of what spec.id_field declares --
        # confirmed by tracing rest.py's handle_get/handle_create response
        # assembly (upsert_secondary_tables explicitly excludes join_field
        # from what gets merged back, so a graph-primary response never
        # contains an id_field-named key unless id_field already happens to
        # be "label"). spec.id_field is untouched here: it is a separate
        # concern, the CREATE request body's input key (rest.py:718).
        # See OPENAPI_RESPONSE_SCHEMA_SWEEP.md, Finding 3.
        identity_prop = "label" if spec.tenant_scope == "graph" else spec.id_field
        # kg_nodes.label is TEXT (schema.sql:207), not a UUID -- the surrogate
        # UUID lives in kg_nodes.id, a column no graph-primary spec exposes
        # as its identity property. "format": "uuid" is only accurate for
        # the non-graph, real-UUID-primary-key case.
        identity_schema: dict[str, Any] = (
            {"type": "string"}
            if spec.tenant_scope == "graph"
            else {"type": "string", "format": "uuid"}
        )
        item_schema: dict[str, Any] = {
            "type": "object",
            "properties": {
                identity_prop: identity_schema,
            },
            "required": [identity_prop],
            "description": spec.description or f"{spec.engine} {spec.entity} resource",
        }
        if spec.version_field:
            item_schema["properties"][spec.version_field] = {
                "type": "string",
                "format": "date-time",
            }
        if spec.soft_delete_field:
            item_schema["properties"][spec.soft_delete_field] = {"type": "boolean"}

        # Add writable and filterable properties
        all_props = set(spec.writable_fields) | set(spec.filterable_fields)
        for prop in sorted(all_props):
            if prop not in item_schema["properties"]:
                # A column schema.sql declares nullable (no NOT NULL, no
                # ADD COLUMN ... NOT NULL) can genuinely come back `null`
                # -- read.py's own three-facts contract for
                # system_design_node_state is the concrete case this was
                # measured against (OPENAPI_RESPONSE_SCHEMA_SWEEP.md,
                # 2026-09-21 addendum), but the condition is structural:
                # any writable_field routed to a nullable column shows it,
                # not just these six specs. OpenAPI 3.1 (this doc's own
                # declared version) dropped the 3.0 `nullable: true`
                # keyword -- `type: [string, null]` is the 3.1 form.
                # `is_nullable is None` (field not resolvable to a real
                # column) leaves the bare scalar type exactly as before --
                # never guessed.
                is_nullable = _writable_field_is_nullable(spec, prop, _column_nullability)
                prop_type: Any = ["string", "null"] if is_nullable else "string"
                item_schema["properties"][prop] = {
                    "type": prop_type,
                    "description": f"Field {prop}",
                }

        schemas[model_name] = item_schema

        # Create Schema
        create_props = {f: {"type": "string"} for f in spec.writable_fields}
        schemas[f"{model_name}_Create"] = {
            "type": "object",
            "properties": create_props,
            "description": f"Payload for creating a {spec.engine} {spec.entity}",
        }

        # Update (Patch) Schema
        patch_props = dict(create_props)
        if spec.version_field:
            patch_props["expected_version"] = {
                "type": "string",
                "description": "Optimistic concurrency lock version",
            }
        schemas[f"{model_name}_Update"] = {
            "type": "object",
            "properties": patch_props,
            "description": f"Payload for updating a {spec.engine} {spec.entity}",
        }

        # List Response Schema
        schemas[f"{model_name}_List"] = {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {"$ref": f"#/components/schemas/{model_name}"},
                },
                "next_cursor": {
                    # `nullable: true` is the OpenAPI 3.0 keyword; this
                    # document declares "openapi": "3.1.0" (3.1 dropped
                    # `nullable` entirely -- a 3.1-aware validator silently
                    # ignores it, leaving the schema exactly as wrong as a
                    # bare `type: "string"`). `type: [string, null]` is the
                    # 3.1 form. Pre-existing, unrelated to a specific spec
                    # -- fixed alongside the writable-field version of the
                    # same defect class rather than left shipping.
                    "type": ["string", "null"],
                    "description": "Cursor token for fetching the next page",
                },
                "total": {
                    "type": "integer",
                    "description": "Total matching count if requested",
                },
            },
            "required": ["items"],
        }

    # 4. Schemas for Resource Common Sub-resources
    schemas["EntityComment"] = {
        "type": "object",
        "properties": {
            "id": {"type": "string", "format": "uuid"},
            "author": {"type": "string"},
            "text": {"type": "string"},
            "created_at": {"type": "string", "format": "date-time"},
        },
        "required": ["id", "text", "created_at"],
    }
    schemas["EntityCommentInput"] = {
        "type": "object",
        "properties": {
            "text": {"type": "string"},
            "author": {"type": "string"},
        },
        "required": ["text"],
    }
    schemas["EntityTagInput"] = {
        "type": "object",
        "properties": {
            "tag": {"type": "string"},
        },
        "required": ["tag"],
    }
    schemas["EntityTimelineEvent"] = {
        "type": "object",
        "properties": {
            "id": {"type": "string", "format": "uuid"},
            "event_type": {"type": "string"},
            "payload": {"type": "object", "additionalProperties": True},
            "created_at": {"type": "string", "format": "date-time"},
        },
        "required": ["id", "event_type", "created_at"],
    }

    # 5. MCP Tool Schemas from mcp_stdio_tools.TOOLS
    for tool in TOOLS:
        schema_name = f"Tool_{tool.name}_Input"
        if tool.inputSchema:
            schemas[schema_name] = _clean_schema(tool.inputSchema)

        # Record tool metadata in x-mcp-tools
        spec = TOOL_REGISTRY.get(tool.name)
        doc["components"]["x-mcp-tools"][tool.name] = {
            "name": tool.name,
            "description": tool.description,
            "inputSchema": {"$ref": f"#/components/schemas/{schema_name}"},
            "admin_only": getattr(spec, "admin_only", False) if spec else False,
            "mutation": getattr(spec, "mutation", False) if spec else False,
            "cacheable": getattr(spec, "cacheable", False) if spec else False,
            "migration": getattr(spec, "migration", False) if spec else False,
        }

    # 6. Build Paths from Starlette Route Tables
    all_source_routes: list[tuple[str, Any]] = [("admin", r) for r in build_admin_routes()]
    for source_name, rlist in secondary_routes:
        for r in rlist:
            all_source_routes.append((source_name, r))

    # Standard responses
    common_responses = {
        "400": {
            "description": "Validation failure or malformed input",
            "content": {
                "application/json": {"schema": {"$ref": "#/components/schemas/ErrorResponse"}}
            },
        },
        "401": {
            "description": "Missing, expired, or invalid authentication credentials",
            "content": {
                "application/json": {"schema": {"$ref": "#/components/schemas/ErrorResponse"}}
            },
        },
        "403": {
            "description": "Principal tier refusal or cross-tenant boundary violation",
            "content": {
                "application/json": {"schema": {"$ref": "#/components/schemas/ErrorResponse"}}
            },
        },
        "404": {
            "description": "Resource or endpoint not found",
            "content": {
                "application/json": {"schema": {"$ref": "#/components/schemas/ErrorResponse"}}
            },
        },
    }

    for source_name, route in all_source_routes:
        if not isinstance(route, Route):
            continue

        raw_path = route.path
        methods = [m.upper() for m in (route.methods or {"GET"}) if m.upper() != "HEAD"]
        if not methods:
            methods = ["GET"]

        # Parse path parameters
        path_param_names = _PARAM_REGEX.findall(raw_path)
        path_parameters = [
            {
                "name": p,
                "in": "path",
                "required": True,
                "schema": {
                    "type": "string",
                    "format": "uuid" if ("id" in p or p.endswith("_id")) else "string",
                },
                "description": f"URL path parameter {p}",
            }
            for p in path_param_names
        ]

        # Determine tag and matched ResourceSpec
        matched_spec: Any | None = None
        tag = source_name
        path_parts = raw_path.strip("/").split("/")
        if len(path_parts) >= 3 and path_parts[0] == "api":
            potential_engine = path_parts[1]
            potential_entity = path_parts[2]
            matched_spec = specs_by_pair.get((potential_engine, potential_entity))
            if matched_spec:
                tag = matched_spec.engine
            else:
                tag = potential_engine
        elif len(path_parts) >= 2 and path_parts[0] == "api":
            tag = path_parts[1]

        if raw_path not in paths:
            paths[raw_path] = {}

        path_item = paths[raw_path]

        # Handler docstring and endpoint name
        endpoint_name = getattr(route.endpoint, "__name__", str(route.endpoint))
        endpoint_doc = inspect.getdoc(route.endpoint) or ""
        doc_lines = [line.strip() for line in endpoint_doc.split("\n") if line.strip()]
        summary = doc_lines[0] if doc_lines else f"{endpoint_name} ({raw_path})"
        description = "\n".join(doc_lines[1:]) if len(doc_lines) > 1 else summary

        for method in methods:
            m_lower = method.lower()
            op_id = f"{m_lower}_{raw_path.strip('/').replace('/', '_').replace('{', '').replace('}', '').replace('-', '_')}"

            # Query parameters
            query_parameters: list[dict[str, Any]] = []
            if (
                matched_spec
                and m_lower == "get"
                and not raw_path.endswith("}")
                and "/events" not in raw_path
                and "/comments" not in raw_path
                and "/tags" not in raw_path
            ):
                # C12 list endpoint query parameters
                query_parameters.extend(
                    [
                        {
                            "name": "limit",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "integer", "default": 50, "maximum": 500},
                            "description": "Maximum number of records to return",
                        },
                        {
                            "name": "cursor",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                            "description": "Opaque pagination cursor",
                        },
                        {
                            "name": "sort",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                            "description": "Sort column with optional +/- prefix",
                        },
                        {
                            "name": "fields",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                            "description": "Comma-separated list of projected fields",
                        },
                    ]
                )
                if matched_spec.searchable_fields:
                    query_parameters.append(
                        {
                            "name": "q",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                            "description": f"Full-text search query across: {', '.join(matched_spec.searchable_fields)}",
                        }
                    )
                for f in matched_spec.filterable_fields:
                    query_parameters.append(
                        {
                            "name": f,
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                            "description": f"Equality filter on {f}",
                        }
                    )

            # Assemble operation object
            operation: dict[str, Any] = {
                "operationId": op_id,
                "tags": [tag],
                "summary": summary,
                "description": description,
                "parameters": path_parameters + query_parameters,
                "responses": dict(common_responses),
            }

            # Configure request body
            if method in ("POST", "PUT", "PATCH"):
                req_body_schema: dict[str, Any] | None = None
                if raw_path == "/api/admin/webhooks" and method == "POST":
                    req_body_schema = {"$ref": "#/components/schemas/OutboundWebhookCreate"}
                elif matched_spec:
                    model_name = f"{matched_spec.engine.capitalize()}_{matched_spec.entity.replace('-', '_')}"
                    if raw_path.endswith("/comments") and method == "POST":
                        req_body_schema = {"$ref": "#/components/schemas/EntityCommentInput"}
                    elif raw_path.endswith("/tags") and method == "POST":
                        req_body_schema = {"$ref": "#/components/schemas/EntityTagInput"}
                    elif raw_path.endswith("/bulk") and method == "POST":
                        req_body_schema = {
                            "type": "object",
                            "properties": {
                                "items": {
                                    "type": "array",
                                    "items": {"$ref": f"#/components/schemas/{model_name}_Create"},
                                }
                            },
                            "required": ["items"],
                        }
                    elif method == "POST":
                        req_body_schema = {"$ref": f"#/components/schemas/{model_name}_Create"}
                    elif method == "PATCH":
                        req_body_schema = {"$ref": f"#/components/schemas/{model_name}_Update"}
                else:
                    req_body_schema = {
                        "type": "object",
                        "additionalProperties": True,
                        "description": "Request payload",
                    }

                if req_body_schema:
                    operation["requestBody"] = {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": req_body_schema,
                            }
                        },
                    }

            # Configure success response
            status_code = (
                "201"
                if (
                    method == "POST"
                    and not raw_path.endswith("/trigger")
                    and not raw_path.endswith("/search")
                )
                else "200"
            )
            resp_schema: dict[str, Any] = {"type": "object", "additionalProperties": True}

            if raw_path == "/api/admin/webhooks" and method == "GET":
                resp_schema = {
                    "type": "array",
                    "items": {"$ref": "#/components/schemas/OutboundWebhook"},
                }
            elif raw_path == "/api/admin/webhooks" and method == "POST":
                resp_schema = {"$ref": "#/components/schemas/OutboundWebhook"}
            elif matched_spec:
                model_name = (
                    f"{matched_spec.engine.capitalize()}_{matched_spec.entity.replace('-', '_')}"
                )
                if raw_path.endswith("/events") and method == "GET":
                    resp_schema = {
                        "type": "array",
                        "items": {"$ref": "#/components/schemas/EntityTimelineEvent"},
                    }
                elif raw_path.endswith("/comments") and method == "GET":
                    resp_schema = {
                        "type": "array",
                        "items": {"$ref": "#/components/schemas/EntityComment"},
                    }
                elif raw_path.endswith("/tags") and method == "GET":
                    resp_schema = {
                        "type": "array",
                        "items": {"type": "string"},
                    }
                elif m_lower == "get" and not raw_path.endswith("}"):
                    resp_schema = {"$ref": f"#/components/schemas/{model_name}_List"}
                else:
                    resp_schema = {"$ref": f"#/components/schemas/{model_name}"}

            operation["responses"][status_code] = {
                "description": "Successful operation",
                "content": {
                    "application/json": {
                        "schema": resp_schema,
                    }
                },
            }

            path_item[m_lower] = operation

    return doc


def generate_json_string(indent: int = 2) -> str:
    """Return the generated OpenAPI specification serialized as a sorted JSON string."""
    spec = generate_openapi_spec()
    return json.dumps(spec, indent=indent, sort_keys=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        type=Path,
        default=_DOCS_OPENAPI,
        help=f"Output file path (default: {_DOCS_OPENAPI})",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=_ROOT_OPENAPI,
        help=f"Repo root openapi path (default: {_ROOT_OPENAPI})",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate that existing openapi.json documents match current generation",
    )
    args = parser.parse_args()

    content = generate_json_string()

    if args.check:
        errors: list[str] = []
        for target, name in [
            (args.out, "docs/_generated/openapi.json"),
            (args.root, "openapi.json"),
        ]:
            if not target.exists():
                errors.append(f"{name} does not exist.")
            else:
                existing = target.read_text(encoding="utf-8")
                if existing != content:
                    errors.append(f"{name} is stale or has drifted from live route/spec tables.")

        if errors:
            print("CHECK FAILED: OpenAPI document drift detected:", file=sys.stderr)
            for err in errors:
                print(f"  - {err}", file=sys.stderr)
            print("Regenerate with: python scripts/gen_openapi.py", file=sys.stderr)
            return 1

        print("CHECK PASSED: OpenAPI specification is up to date.")
        return 0

    # Write target files
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(content, encoding="utf-8")
    print(f"Wrote {args.out}")

    args.root.write_text(content, encoding="utf-8")
    print(f"Wrote {args.root}")

    # Summary
    spec = json.loads(content)
    total_paths = len(spec.get("paths", {}))
    total_ops = sum(len(p) for p in spec.get("paths", {}).values())
    total_schemas = len(spec.get("components", {}).get("schemas", {}))
    total_tools = len(spec.get("components", {}).get("x-mcp-tools", {}))
    print(
        f"OpenAPI 3.1.0 generation complete: "
        f"{total_paths} paths, {total_ops} operations, {total_schemas} schemas, {total_tools} MCP tools."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
